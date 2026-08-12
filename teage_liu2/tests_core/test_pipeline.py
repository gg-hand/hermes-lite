"""契约测试:零枝干可跑 / 历史落盘重启恢复 / 循环形态无工具终止。"""

from __future__ import annotations

import asyncio
import pytest

from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import HookChain
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


def _run_chat(pipeline, session_id, user_input, system=None):
    """同步跑一次流式对话,返回事件列表。"""
    return asyncio.run(
        _collect_list(pipeline.chat_stream(session_id, user_input, system=system))
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


async def _collect_list(agen):
    return [ev async for ev in agen]
