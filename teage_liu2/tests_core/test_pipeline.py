"""契约测试:零枝干可跑 / 历史落盘重启恢复 / 循环形态无工具终止。"""

from __future__ import annotations

import asyncio
import pytest

from teage_liu2.core.actions import AppendMessage, SetStop
from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.pipeline import ChatPipeline, MODE_BARE, MODE_LOOP
from teage_liu2.core.types import (
    EV_DONE,
    EV_TEXT_DELTA,
    TERMINATION_NORMAL,
)
from .fake_llm import FakeLLMClient


def make_pipeline(tmp_path, llm, mode=MODE_LOOP, hooks=None):
    store = SQLiteHistoryStore(str(tmp_path / "test_sessions.db"))
    pipeline = ChatPipeline(
        llm_client=llm,
        history_store=store,
        hooks=hooks or HookChain(),
        mode=mode,
        base_system_prompt="测试系统提示词",
    )
    return pipeline, store


async def _collect_list(agen):
    return [ev async for ev in agen]


def collect(events):
    """收集事件流,返回 (文本, done_event 列表)。"""
    texts = []
    dones = []
    for ev in events:
        if ev.get("type") == EV_TEXT_DELTA:
            texts.append(ev.get("text", ""))
        elif ev.get("type") == EV_DONE:
            dones.append(ev)
    return "".join(texts), dones


def _run_chat(pipeline, session_id, user_input, system=None, cancel_event=None):
    """同步跑一次流式对话,返回事件列表。"""
    return asyncio.run(
        _collect_list(pipeline.chat_stream(session_id, user_input, system=system, cancel_event=cancel_event))
    )


def test_bare_mode_zero_branches_multiturn(tmp_path):
    """验收:裸形态 + 零枝干,多轮问答跑通,历史落盘。"""
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "第一轮回答"}], "stop_reason": "end_turn"},
        {"content": [{"type": "text", "text": "第二轮回答"}], "stop_reason": "end_turn"},
    ])
    pipeline, store = make_pipeline(tmp_path, llm, mode=MODE_BARE)

    events1 = _run_chat(pipeline, "s1", "你好")
    text1, dones1 = collect(events1)
    assert text1 == "第一轮回答"
    assert dones1 and dones1[0]["is_complete"] is True
    assert dones1[0]["termination_reason"] == TERMINATION_NORMAL

    text2, dones2 = collect(_run_chat(pipeline, "s1", "继续"))
    assert text2 == "第二轮回答"

    # 历史落盘:两轮 user + 两轮 assistant
    messages = store.get_session_messages("s1")
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]

    # LLM 收到完整历史(第二轮调用时含第一轮消息)
    assert len(llm.last_messages) == 3  # 第一轮 user/assistant + 当前 user
    assert llm.last_messages[0]["role"] == "user"
    assert llm.last_messages[1]["role"] == "assistant"
    assert llm.last_messages[2]["role"] == "user"


def test_restart_recovery(tmp_path):
    """验收:重启可恢复 —— 新 pipeline 实例读同一 SQLite 文件拿到历史。"""
    llm1 = FakeLLMClient([
        {"content": [{"type": "text", "text": "第一轮回答"}], "stop_reason": "end_turn"},
    ])
    pipeline1, store1 = make_pipeline(tmp_path, llm1, mode=MODE_BARE)
    _run_chat(pipeline1, "s_restart", "你好")
    store1.close()

    # 模拟重启:新 LLM / 新 store / 新 pipeline,同一 db 文件
    llm2 = FakeLLMClient([
        {"content": [{"type": "text", "text": "第二轮回答"}], "stop_reason": "end_turn"},
    ])
    store2 = SQLiteHistoryStore(str(tmp_path / "test_sessions.db"))
    pipeline2 = ChatPipeline(
        llm_client=llm2, history_store=store2, mode=MODE_BARE
    )
    text2, _ = collect(_run_chat(pipeline2, "s_restart", "继续"))
    assert text2 == "第二轮回答"
    # 新实例的 LLM 看到了重启前的历史(1 user + 1 assistant + 当前 user)
    assert len(llm2.last_messages) == 3
    assert llm2.last_messages[0]["content"] == "你好"
    store2.close()


def test_loop_mode_plain_turn(tmp_path):
    """验收:循环形态 + 零枝干,LLM 只回文本 → 单轮 done(normal)。"""
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "你好呀"}], "stop_reason": "end_turn"},
    ])
    pipeline, store = make_pipeline(tmp_path, llm, mode=MODE_LOOP)

    events = _run_chat(pipeline, "s_loop", "嗨")
    types = [ev["type"] for ev in events]
    assert types[0] == "step_start"
    assert "text_delta" in types
    assert types[-1] == "done"
    done = events[-1]
    assert done["is_complete"] is True
    assert done["termination_reason"] == TERMINATION_NORMAL
    assert done["response"] == "你好呀"
    assert done["messages"][-1]["role"] == "assistant"


def test_loop_no_tool_executor_graceful_stop(tmp_path):
    """验收:循环形态,LLM 想调工具但零枝干 → 友好终止不崩。"""
    llm = FakeLLMClient([
        {
            "content": [
                {"type": "text", "text": "我需要查一下"},
                {"type": "tool_use", "id": "tool_1", "name": "web_search", "input": {"q": "天气"}},
            ],
            "stop_reason": "tool_use",
        },
    ])
    pipeline, store = make_pipeline(tmp_path, llm, mode=MODE_LOOP)

    events = _run_chat(pipeline, "s_tool", "查天气")
    done = events[-1]
    assert done["type"] == "done"
    assert done["is_complete"] is False
    assert done["termination_reason"] == "no_tool_executor"
    # 已产出的文本保留
    assert done["response"] == "我需要查一下"
    # 末尾未应答的 tool_calls 被清理(防下轮 400)
    assert done["messages"][-1]["role"] != "assistant" or "tool_use" not in [
        b.get("type") for b in (done["messages"][-1].get("content") or [])
        if isinstance(b, dict)
    ]


# ---------------------------------------------------------------------------
# 第 4 步编排定稿验收(E3 ctx / E2 on_error / C1 校验 / E6 形态 / E8 并发)
# ---------------------------------------------------------------------------
def test_unknown_mode_fails_at_construction(tmp_path):
    """验收(Q8 根治):未知 mode 构造即失败(启动失败,非运行时崩溃)。"""
    store = SQLiteHistoryStore(str(tmp_path / "mode.db"))
    with pytest.raises(ValueError, match="未知形态"):
        ChatPipeline(llm_client=FakeLLMClient(), history_store=store, mode="loopx")


def test_ctx_round_and_started_at(tmp_path):
    """验收(E3):snapshot.round 随 loop 每轮递增(启动 0);started_at 为对话开始时间。"""
    seen = {}

    class RoundObserver(Branch):
        name = "round_observer"

        async def on_tool_call(self, snapshot, tool_name, tool_input):
            if tool_name != "echo":
                return NotImplemented
            seen["round_first"] = snapshot.round  # 第一轮工具执行时
            seen["started_at"] = snapshot.started_at
            return "回声"

    llm = FakeLLMClient([
        {
            "content": [{"type": "tool_use", "id": "t1", "name": "echo", "input": {}}],
            "stop_reason": "tool_use",
        },
        {"content": [{"type": "text", "text": "收尾"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    hooks.register(RoundObserver())
    store = SQLiteHistoryStore(str(tmp_path / "round.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    _run_chat(pipeline, "s_ctx", "调工具")
    assert seen["round_first"] == 1  # 启动 0,每轮递增
    assert isinstance(seen["started_at"], str)  # ISO 时间戳


class _ErrorObserver(Branch):
    """记录 on_error 调用的枝干(供 E2 测试)。"""

    name = "err_observer"

    def __init__(self, errors):
        self.errors = errors

    async def on_error(self, snapshot, error):
        self.errors.append(error)


def test_on_error_called_on_failure_not_on_success(tmp_path):
    """验收(E2):LLM 失败 → on_error 被调用;正常完成不触发。"""
    errors = []

    # 场景 1:LLM 失败(error 事件,无 done)
    class FailingLLM(FakeLLMClient):
        async def chat_main_stream(self, *args, **kwargs):
            raise RuntimeError("API 挂了")
            yield

    hooks = HookChain()
    hooks.register(_ErrorObserver(errors))
    store = SQLiteHistoryStore(str(tmp_path / "err.db"))
    pipeline = ChatPipeline(llm_client=FailingLLM(), history_store=store, hooks=hooks)
    events = _run_chat(pipeline, "s_fail", "问题")
    assert events[-1]["type"] == "error"
    assert len(errors) == 1
    assert isinstance(errors[0], Exception)

    # 场景 2:正常完成 → 不触发
    errors2 = []
    hooks2 = HookChain()
    hooks2.register(_ErrorObserver(errors2))
    store2 = SQLiteHistoryStore(str(tmp_path / "ok.db"))
    pipeline2 = ChatPipeline(
        llm_client=FakeLLMClient([
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"},
        ]),
        history_store=store2,
        hooks=hooks2,
    )
    _run_chat(pipeline2, "s_ok", "问题")
    assert errors2 == []


def test_on_error_on_intercept(tmp_path):
    """验收(E2):枝干拦截(ctx.stop)→ on_error 被调用(不完整对话)。"""
    errors = []

    class StopBranch(Branch):
        name = "stopper"

        async def before(self, snapshot):
            return [SetStop(reason="test")]

    hooks = HookChain()
    hooks.register(StopBranch())
    hooks.register(_ErrorObserver(errors))
    store = SQLiteHistoryStore(str(tmp_path / "intercept.db"))
    pipeline = ChatPipeline(llm_client=FakeLLMClient(), history_store=store, hooks=hooks)
    events = _run_chat(pipeline, "s_intercept", "问题")
    assert events[-1]["type"] == "done"
    assert events[-1]["termination_reason"] == "intercepted"
    assert len(errors) == 1  # 拦截视为未完成 → on_error


def test_illegal_action_rejected(tmp_path):
    """验收(C1/§15-A1):before 返回非法 action(AppendMessage role=assistant)→
    schema 级拒绝(HOOK_INVALID_ACTION),对话正常继续,LLM 仍被调用。"""
    class BadBefore(Branch):
        name = "bad_before"

        async def before(self, snapshot):
            # §4.4:AppendMessage 仅允许 role=user(交替约束;assistant 需求走 RFC)
            return [AppendMessage(message={"role": "assistant", "content": "非法追加"})]

    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "正常回答"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    hooks.register(BadBefore())
    store = SQLiteHistoryStore(str(tmp_path / "c1.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    events = _run_chat(pipeline, "s_c1", "问题")
    assert events[-1]["type"] == "done"       # 非法 action 被拒,对话继续
    assert llm.calls == 1                      # LLM 正常调用
    # 快照未含非法 assistant 消息(LLM 收到的消息末尾是 user)
    assert llm.last_messages[-1]["role"] == "user"


def test_cancel_before_first_step_no_crash(tmp_path):
    """验收(P1-1):首轮 step 前取消 → done(user_cancel),不崩溃(UnboundLocalError)。"""
    import threading

    cancel_event = threading.Event()
    cancel_event.set()  # 首轮前已取消
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "不应被调用"}], "stop_reason": "end_turn"},
    ])
    store = SQLiteHistoryStore(str(tmp_path / "cancel.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store)

    events = _run_chat(pipeline, "s_cancel", "问题", cancel_event=cancel_event)
    done = events[-1]
    assert done["type"] == "done"
    assert done["termination_reason"] == "user_cancel"
    assert llm.calls == 0  # 取消 = 不调 LLM
    # done 字段齐整(无崩溃时的默认值)
    assert done["usage"] is None
    assert done["content_blocks"] == []
    assert done["stop_reason"] == "end_turn"


def test_after_response_text_is_final_round(tmp_path):
    """验收(P2-1):多轮工具场景 after 收到 AfterResponse.text = 最后轮文本(非全轮拼接)。"""
    seen = {}

    class AfterProbe(Branch):
        name = "after_probe"

        async def after(self, snapshot, response):
            seen["text"] = response.text

        async def on_tool_call(self, snapshot, tool_name, tool_input):
            if tool_name != "echo":
                return NotImplemented
            return "回声"

    llm = FakeLLMClient([
        {
            "content": [
                {"type": "text", "text": "我需要查一下"},
                {"type": "tool_use", "id": "t1", "name": "echo", "input": {}},
            ],
            "stop_reason": "tool_use",
        },
        {"content": [{"type": "text", "text": "查到了:晴天"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    hooks.register(AfterProbe())
    store = SQLiteHistoryStore(str(tmp_path / "after2.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    events = _run_chat(pipeline, "s_after2", "查天气")
    assert events[-1]["type"] == "done"
    # done.response 已正确(最后轮),after 摘要须与其一致
    assert events[-1]["response"] == "查到了:晴天"
    assert seen["text"] == "查到了:晴天"  # 不是 "我需要查一下查到了:晴天"


def test_intercept_done_has_full_fields(tmp_path):
    """验收(P3-4):拦截 done 事件字段齐整(含 usage/content_blocks/stop_reason)。"""
    class StopBranch(Branch):
        name = "stopper"

        async def before(self, snapshot):
            return [SetStop(reason="test")]

    hooks = HookChain()
    hooks.register(StopBranch())
    store = SQLiteHistoryStore(str(tmp_path / "intercept8.db"))
    pipeline = ChatPipeline(llm_client=FakeLLMClient(), history_store=store, hooks=hooks)

    events = _run_chat(pipeline, "s_intercept8", "问题")
    done = events[-1]
    assert done["type"] == "done"
    assert done["termination_reason"] == "intercepted"
    for key in ("usage", "content_blocks", "stop_reason"):
        assert key in done  # 8 键齐整,观测枝干可依赖统一结构


def test_history_window_messages(tmp_path):
    """验收(D2):超窗口取最近 N 条,不报错;窗口可配置。"""
    store = SQLiteHistoryStore(str(tmp_path / "win.db"))
    store.ensure_session("s_win")
    for i in range(60):  # 60 user + 60 assistant = 120 条
        store.log_message("s_win", "user", f"问题{i}")
        store.log_message("s_win", "assistant", f"回答{i}")

    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "收尾"}], "stop_reason": "end_turn"},
    ])
    pipeline = ChatPipeline(
        llm_client=llm, history_store=store, mode=MODE_BARE,
        history_window_messages=100,
    )
    _run_chat(pipeline, "s_win", "最新问题")
    # 最近 100 条历史 + 当前输入(120 条中窗口外的前 20 条被丢弃)
    assert len(llm.last_messages) == 101
    assert llm.last_messages[0]["content"] == "问题10"  # 第 20 条(0 基)起


def test_concurrent_sessions_no_crosstalk(tmp_path):
    """验收(E8):两会话交错对话无状态串扰(并发契约)。"""
    def respond(messages):
        first = messages[0]["content"]
        if "问题A" in str(first):
            return {"content": [{"type": "text", "text": "回答A"}], "stop_reason": "end_turn"}
        return {"content": [{"type": "text", "text": "回答B"}], "stop_reason": "end_turn"}

    llm = FakeLLMClient([respond, respond])
    store = SQLiteHistoryStore(str(tmp_path / "concurrent.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, mode=MODE_BARE)

    async def run(sid, q):
        return [ev async for ev in pipeline.chat_stream(sid, q)]

    async def main():
        e1, e2 = await asyncio.gather(run("sA", "问题A"), run("sB", "问题B"))
        return e1, e2

    events_a, events_b = asyncio.run(main())
    assert events_a[-1]["type"] == "done"
    assert events_a[-1]["response"] == "回答A"
    assert events_b[-1]["response"] == "回答B"

    # 两会话落盘各自独立(无串扰)
    msgs_a = store.get_session_messages("sA")
    msgs_b = store.get_session_messages("sB")
    assert [m["content"] for m in msgs_a] == ["问题A", "回答A"]
    assert [m["content"] for m in msgs_b] == ["问题B", "回答B"]
