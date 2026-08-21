"""循环形态测试:工具执行回喂 / max_loops 耗尽。"""

from __future__ import annotations

import asyncio
import pytest

from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.loop import ReactLoop
from teage_liu2.core.pipeline import ChatPipeline
from teage_liu2.core.types import (
    EV_DONE,
    EV_TOOL_RESULT,
    EV_TOOL_USE,
    Snapshot,
)
from .fake_llm import FakeLLMClient


async def _collect_list(agen):
    return [ev async for ev in agen]


class EchoToolBranch(Branch):
    """返回 '回声: {tool_input}' 的极简工具枝干。"""

    name = "echo_tools"

    def __init__(self):
        self.calls = []

    async def on_tool_call(self, snapshot, tool_name, tool_input):
        if tool_name != "echo":
            return NotImplemented
        self.calls.append((tool_name, dict(tool_input)))
        return f"回声: {tool_input.get('text', '')}"


def test_loop_tool_executed_and_fed_back(tmp_path):
    """验收:工具枝干执行 → tool_result 回喂 → LLM 第二轮基于结果收尾。"""
    llm = FakeLLMClient([
        {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "echo", "input": {"text": "hello"}},
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "text", "text": "工具返回了: hello"}],
            "stop_reason": "end_turn",
        },
    ])
    hooks = HookChain()
    tool_branch = EchoToolBranch()
    hooks.register(tool_branch)
    store = SQLiteHistoryStore(str(tmp_path / "loop.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    events = asyncio.run(_collect_list(pipeline.chat_stream("s", "回声 hello")))

    types = [ev["type"] for ev in events]
    assert types.count("step_start") == 2       # 两轮 LLM 调用
    assert EV_TOOL_USE in types
    assert EV_TOOL_RESULT in types
    assert tool_branch.calls == [("echo", {"text": "hello"})]

    done = events[-1]
    assert done["type"] == EV_DONE
    assert done["is_complete"] is True
    assert done["response"] == "工具返回了: hello"

    # 回喂正确:第二轮 LLM 收到的消息含 tool_result 且 tool_use_id=t1
    second_call_messages = llm.last_messages
    assert second_call_messages[-1]["role"] == "user"
    tool_results = second_call_messages[-1]["content"]
    assert tool_results[0]["type"] == "tool_result"
    assert tool_results[0]["tool_use_id"] == "t1"
    assert "hello" in tool_results[0]["content"]


def test_loop_max_loops_exhausted(tmp_path):
    """验收:max_loops 耗尽 → done(is_complete=False, max_loops)。"""
    llm = FakeLLMClient([
        {
            "content": [{"type": "tool_use", "id": f"t{i}", "name": "echo", "input": {"text": "x"}}],
            "stop_reason": "tool_use",
        }
        for i in range(3)  # 脚本 3 个 tool_use,无收尾文本
    ])
    hooks = HookChain()
    hooks.register(EchoToolBranch())
    store = SQLiteHistoryStore(str(tmp_path / "maxloops.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks, max_loops=2)

    events = asyncio.run(_collect_list(pipeline.chat_stream("s", "循环")))

    done = events[-1]
    assert done["type"] == EV_DONE
    assert done["is_complete"] is False
    assert done["termination_reason"] == "max_loops"
    # 无收尾文本,response 为空(不崩)
    assert isinstance(done["response"], str)


class ExplodingToolBranch(Branch):
    """on_tool_call 必抛异常的枝干(模拟工具实现 bug)。"""

    name = "exploding_tools"

    async def on_tool_call(self, snapshot, tool_name, tool_input):
        raise RuntimeError("工具内部爆炸")


def test_tool_exception_fed_back_as_is_error(tmp_path):
    """验收:工具枝干异常 → 不穿透事件流;tool_result is_error 回喂,对话正常收尾。

    回归锚定(N1):异常须由 dispatch 层捕获转为 tool_result is_error 回喂 LLM
    (计划 §4.4 责任矩阵),而不是穿透 chat_stream 让 SSE 客户端收到 500。
    """
    llm = FakeLLMClient([
        {
            "content": [{"type": "tool_use", "id": "t1", "name": "boom", "input": {}}],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "text", "text": "工具出错了,但我还在"}],
            "stop_reason": "end_turn",
        },
    ])
    hooks = HookChain()
    hooks.register(ExplodingToolBranch())
    store = SQLiteHistoryStore(str(tmp_path / "boom.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    # 事件流不抛异常,正常以 done 收尾
    events = asyncio.run(_collect_list(pipeline.chat_stream("s_boom", "调用工具")))
    assert events[-1]["type"] == EV_DONE
    # tool_result 带 is_error 标记,错误信息可见
    tool_results = [ev for ev in events if ev.get("type") == EV_TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is True
    assert "工具执行出错" in tool_results[0]["result"]
    # 第二轮 LLM 收到 is_error 回喂(tool_result 内容含错误)
    fed_back = llm.last_messages[-1]["content"]
    assert fed_back[0]["type"] == "tool_result"
    assert fed_back[0]["is_error"] is True
    assert "工具内部爆炸" in fed_back[0]["content"]


def test_loop_direct_with_snapshot():
    """验收:ReactLoop 可直接用 Snapshot 驱动(不经 pipeline)。"""
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "直接驱动"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    loop = ReactLoop(llm, hooks, max_loops=5)
    snapshot = Snapshot(session_id="s_direct", user_input="hi")

    events = asyncio.run(
        _collect_list(loop.run_stream(snapshot, [{"role": "user", "content": "hi"}], system=None))
    )

    assert events[-1]["type"] == EV_DONE
    assert events[-1]["response"] == "直接驱动"
