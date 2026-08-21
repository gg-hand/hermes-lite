"""落盘机制专项测试:事件驱动落盘的正确性。

覆盖三条关键性质:
- ① user 消息在事件流开始前落盘(断连/失败都不丢用户输入)
- ② assistant 在 done 事件到达时落盘(完成即可见,流中可查)
- ③ finally 兜底:生成器被 aclose()(SSE 断连)时部分输出也落盘
"""

from __future__ import annotations

import asyncio

from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.pipeline import ChatPipeline, MODE_BARE, MODE_LOOP
from .fake_llm import FakeLLMClient

# 共享事件循环:生成器逐步推进(先 __anext__ 后 aclose)必须发生在
# 同一事件循环内(asyncio.timeout 定时器等绑定在创建它的循环上,
# 跨循环推进会状态混乱)
_LOOP = asyncio.new_event_loop()


def _anext(agen):
    return _LOOP.run_until_complete(agen.__anext__())


async def _drain_coro(agen):
    return [ev async for ev in agen]


def _drain(agen):
    return _LOOP.run_until_complete(_drain_coro(agen))


def make_pipeline(tmp_path, llm, mode=MODE_LOOP, hooks=None):
    store = SQLiteHistoryStore(str(tmp_path / "persist.db"))
    pipeline = ChatPipeline(
        llm_client=llm, history_store=store, mode=mode, hooks=hooks
    )
    return pipeline, store


def test_user_persisted_before_events(tmp_path):
    """验收①:事件流开始前 user 已落盘(流中可查,断连不丢)。"""
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "回答"}], "stop_reason": "end_turn"},
    ])
    pipeline, store = make_pipeline(tmp_path, llm)

    agen = pipeline.chat_stream("s_persist", "你好")
    # 消费第一个事件(step_start)——此时 user 必须已落盘
    first = _anext(agen)
    assert first["type"] == "step_start"
    msgs = store.get_session_messages("s_persist")
    assert [m["role"] for m in msgs] == ["user"]
    assert msgs[0]["content"] == "你好"
    # 收尾(正常消费完)
    rest = _drain(agen)
    assert rest[-1]["type"] == "done"


def test_assistant_persisted_at_done(tmp_path):
    """验收②:done 事件到达时 assistant 立即落盘(完成即可见)。"""
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "完整回答"}], "stop_reason": "end_turn"},
    ])
    pipeline, store = make_pipeline(tmp_path, llm)

    events = _drain(pipeline.chat_stream("s_done", "问题"))
    msgs = store.get_session_messages("s_done")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "完整回答"
    # done 事件的 response 与落盘一致(单一事实源:主干收集的文本)
    assert events[-1]["response"] == "完整回答"


def test_disconnect_persists_partial(tmp_path):
    """验收③:生成器被 aclose()(SSE 断连)→ user 已落盘 + 部分输出兜底落盘。"""
    # LLM 产出长文本分多次 delta,消费到一半断连
    llm = FakeLLMClient([
        {
            "content": [{"type": "text", "text": "这是一段很长的回答第一部分,第二部分还没说完"}],
            "stop_reason": "end_turn",
        },
    ])
    pipeline, store = make_pipeline(tmp_path, llm)

    agen = pipeline.chat_stream("s_cut", "问题")
    # 消费到第一个 text_delta 后模拟客户端断开
    ev = _anext(agen)          # step_start
    while ev["type"] != "text_delta":
        ev = _anext(agen)
    # 此时已产出部分文本,断连
    _LOOP.run_until_complete(agen.aclose())

    msgs = store.get_session_messages("s_cut")
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user"                    # user 不丢
    assert "assistant" in roles                  # partial 兜底落盘
    partial = [m for m in msgs if m["role"] == "assistant"][0]["content"]
    assert "第一部分" in partial                   # 已产出部分保留


def test_llm_failure_persists_user_only(tmp_path):
    """验收:LLM 失败(error 事件,无产出)→ user 落盘,无 assistant。"""
    class FailingLLM(FakeLLMClient):
        # 真实协议:失败 = raise(step 层捕获转 error 事件)
        # (yield 使其成为 async generator,raise 在迭代时抛出)
        async def chat_main_stream(self, *args, **kwargs):
            raise RuntimeError("API 挂了")
            yield

    llm = FailingLLM()
    pipeline, store = make_pipeline(tmp_path, llm)

    events = _drain(pipeline.chat_stream("s_fail", "问题"))
    assert events[-1]["type"] == "error"
    msgs = store.get_session_messages("s_fail")
    assert [m["role"] for m in msgs] == ["user"]  # 无空 assistant 消息


def test_partial_before_error_persisted(tmp_path):
    """验收:流中途失败(已有部分输出)→ partial 兜底落盘,不丢已产出文本。"""
    class PartialThenFailLLM(FakeLLMClient):
        async def chat_main_stream(self, *args, **kwargs):
            yield {"type": "text", "text": "先说了一半"}
            raise RuntimeError("中途失败")
            yield

    llm = PartialThenFailLLM()
    pipeline, store = make_pipeline(tmp_path, llm)

    events = _drain(pipeline.chat_stream("s_mid", "问题"))
    assert events[-1]["type"] == "error"
    msgs = store.get_session_messages("s_mid")
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user"
    assert "assistant" in roles
    partial = [m for m in msgs if m["role"] == "assistant"][0]["content"]
    assert partial == "先说了一半"


def test_multiround_tool_response_is_final_text(tmp_path):
    """验收:多轮工具场景 done.response 与落盘 = 最后轮文本(非全轮拼接)。

    回归锚定(Q1):第一轮文本+tool_use → 第二轮收尾文本,若主干把全部
    text_delta 拼接覆盖 loop 的 response,会得到 "我需要查一下查到了:晴天"
    这种从未真实存在的合并消息,且持久污染历史。
    """
    class EchoTool(Branch):
        name = "echo"

        async def on_tool_call(self, snapshot, tool_name, tool_input):
            return "回声结果"

    llm = FakeLLMClient([
        {
            "content": [
                {"type": "text", "text": "我需要查一下"},
                {"type": "tool_use", "id": "t1", "name": "echo", "input": {"text": "hi"}},
            ],
            "stop_reason": "tool_use",
        },
        {"content": [{"type": "text", "text": "查到了:晴天"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    hooks.register(EchoTool())
    pipeline, store = make_pipeline(tmp_path, llm, hooks=hooks)

    events = _drain(pipeline.chat_stream("s_multiround", "查天气"))
    done = events[-1]
    assert done["type"] == "done"
    assert done["response"] == "查到了:晴天"  # 不是 "我需要查一下查到了:晴天"
    msgs = store.get_session_messages("s_multiround")
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "查到了:晴天"  # 落盘与 done.response 一致


def test_tool_only_round_persisted_structured(tmp_path):
    """验收:纯工具轮次 → assistant 以 content_blocks(tool_use 结构)落盘,无空消息。

    N2 精神延续:消息级落盘后,assistant 消息要么有文本,要么有 tool_use 结构
    (重建时 content_blocks 优先,LLM 不会收到空 assistant)。
    """
    import json

    class EchoTool(Branch):
        name = "echo"

        async def on_tool_call(self, snapshot, tool_name, tool_input):
            return "回声结果"

    # 三轮全部纯 tool_use(无文本),直至 max_loops 耗尽
    llm = FakeLLMClient([
        {
            "content": [{"type": "tool_use", "id": f"t{i}", "name": "echo", "input": {}}],
            "stop_reason": "tool_use",
        }
        for i in range(3)
    ])
    hooks = HookChain()
    hooks.register(EchoTool())
    pipeline, store = make_pipeline(tmp_path, llm, hooks=hooks)

    _drain(pipeline.chat_stream("s_toolonly", "转圈"))
    msgs = store.get_session_messages("s_toolonly")
    assert msgs[0]["role"] == "user"
    for m in msgs:
        if m["role"] == "assistant":
            # 每条 assistant 都有 tool_use 结构(非空),无空消息
            blocks = json.loads(m["content_blocks"])
            assert blocks and blocks[0]["type"] == "tool_use"
    # 工具结果也落盘(配对)
    assert any(m["role"] == "user" and m.get("content_blocks") for m in msgs[1:])


def test_tool_round_persisted_pairing_restart_recovery(tmp_path):
    """验收(D1 §1.4):工具轮次消息落盘,重启后重建配对结构。

    事件驱动落盘:
    - step_end → assistant(content_blocks 含 tool_use)
    - tool_result → 缓冲 → 下轮 step_start 聚合落盘 user(tool_results 配对 tool_use_id)
    重启(新 store 实例)读历史 → normalize 重建 → LLM 收到完整配对结构。
    """
    import json

    from teage_liu2.core.types import normalize_history

    class EchoTool(Branch):
        name = "echo"

        async def on_tool_call(self, snapshot, tool_name, tool_input):
            return "回声: hi"

    llm = FakeLLMClient([
        {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "echo", "input": {"text": "hi"}},
            ],
            "stop_reason": "tool_use",
        },
        {"content": [{"type": "text", "text": "工具返回了: hi"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    hooks.register(EchoTool())
    pipeline, store = make_pipeline(tmp_path, llm, hooks=hooks)

    _drain(pipeline.chat_stream("s_pair", "回声 hi"))
    store.close()

    # 落盘检查:user / assistant(tool_use) / user(tool_results) / assistant(收尾)
    store2 = SQLiteHistoryStore(str(tmp_path / "persist.db"))
    msgs = store2.get_session_messages("s_pair")
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"]
    # assistant(tool_use) 消息的 content_blocks 含 tool_use 块
    tool_use_msg = msgs[1]
    blocks = json.loads(tool_use_msg["content_blocks"])
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["id"] == "t1"
    # user(tool_results) 消息配对 tool_use_id
    tool_result_msg = msgs[2]
    result_blocks = json.loads(tool_result_msg["content_blocks"])
    assert result_blocks[0]["type"] == "tool_result"
    assert result_blocks[0]["tool_use_id"] == "t1"
    assert "回声: hi" in result_blocks[0]["content"]

    # 重启重建:normalize_history 优先 content_blocks,LLM 可收到配对结构
    rebuilt = normalize_history(msgs)
    assert rebuilt[1]["content"][0]["type"] == "tool_use"
    assert rebuilt[2]["content"][0]["type"] == "tool_result"
    assert rebuilt[2]["content"][0]["tool_use_id"] == "t1"


def test_bare_mode_persist_consistency(tmp_path):
    """验收:bare 形态同样满足事件驱动落盘(user 前置 + done 即写)。"""
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "裸形态回答"}], "stop_reason": "end_turn"},
    ])
    pipeline, store = make_pipeline(tmp_path, llm, mode=MODE_BARE)

    agen = pipeline.chat_stream("s_bare", "问题")
    first = _anext(agen)
    assert first["type"] == "step_start"
    msgs = store.get_session_messages("s_bare")
    assert [m["role"] for m in msgs] == ["user"]  # 流中 user 已可见
    rest = _drain(agen)
    assert rest[-1]["type"] == "done"
    msgs = store.get_session_messages("s_bare")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "裸形态回答"


