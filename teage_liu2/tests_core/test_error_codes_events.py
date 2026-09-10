"""错误码事件面 / 日志面锚定（2026-09-10 评审 FIX-4 + P-13）。

背景：`errors.spec §5` 声明"事件面（error 事件 code 字段）"与"日志面
（`CODE: ` 前缀）"双通道，但此前**零自动化锚定** —— 删掉全部 `code` 键，
行为套件与 pytest 均全绿。本文件补上两个面的直接断言。
"""
from __future__ import annotations

import asyncio
import logging

from teage_liu2.core.actions import AppendMessage
from teage_liu2.core.config import core_config_from
from teage_liu2.core.errors import (
    CONFIG_INVALID_VALUE,
    CONFIG_UNKNOWN_KEY,
    ERROR_CODES,
    HOOK_INVALID_ACTION,
    LLM_API_ERROR,
)
from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.pipeline import ChatPipeline, MODE_LOOP
from .fake_llm import FakeLLMClient


class _ErrLLM(FakeLLMClient):
    """LLM 流式调用直接抛错（事件面错误码锚定用）。"""

    async def chat_main_stream(self, *a, **kw):  # noqa: ANN002, ANN003 - 桩
        self.calls += 1
        raise RuntimeError("boom")
        yield  # pragma: no cover


class _BadActions(Branch):
    """返回非法 action（role=assistant 越权追加）→ HOOK_INVALID_ACTION。"""

    name = "bad_actions"

    async def before(self, snapshot):  # noqa: ANN001 - 桩
        return [AppendMessage(message={"role": "assistant", "content": "越权追加"})]


def _make(tmp_path, llm, hooks=None):
    store = SQLiteHistoryStore(str(tmp_path / "codes.db"))
    pipeline = ChatPipeline(
        llm_client=llm, history_store=store, hooks=hooks or HookChain(),
        mode=MODE_LOOP, base_system_prompt="sys",
    )
    return pipeline, store


async def _collect(agen):
    return [ev async for ev in agen]


# ---------------------------------------------------------------------------
# 事件面
# ---------------------------------------------------------------------------
def test_error_event_carries_llm_code(tmp_path):
    """LLM 失败 → error 事件必须携带 errors 域错误码（此前完全无锚定）。"""
    pipeline, store = _make(tmp_path, _ErrLLM([]))
    try:
        events = asyncio.run(_collect(pipeline.chat_stream("s-code", "触发 LLM 失败")))
    finally:
        store.close()
    errs = [e for e in events if e.get("type") == "error"]
    assert errs, "应产生 error 事件"
    assert errs[0].get("code") == LLM_API_ERROR
    assert errs[0]["code"] in ERROR_CODES
    assert not [e for e in events if e.get("type") == "done"], "error 路径不产生 done"


# ---------------------------------------------------------------------------
# 日志面
# ---------------------------------------------------------------------------
def test_invalid_action_logs_code(tmp_path, caplog):
    """非法 action → 日志面 `HOOK_INVALID_ACTION: ` 前缀，且对话不中断。"""
    hooks = HookChain()
    hooks.register(_BadActions())
    llm = FakeLLMClient([{"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}])
    pipeline, store = _make(tmp_path, llm, hooks=hooks)
    try:
        with caplog.at_level(logging.ERROR, logger="teage_liu2.core.snapshot"):
            events = asyncio.run(_collect(pipeline.chat_stream("s-code2", "hi")))
    finally:
        store.close()
    assert HOOK_INVALID_ACTION in caplog.text, "非法 action 必须以错误码前缀记日志"
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["termination_reason"] == "normal", "隔离后对话应照常完成"


# ---------------------------------------------------------------------------
# 配置面（T2.3：错误码此前是"死码"）
# ---------------------------------------------------------------------------
def test_config_error_codes_carry_code():
    """配置错误消息必须携带 CONFIG_* 错误码（可读错误 + 可断言）。"""
    try:
        core_config_from({"core": {"no_such_key": 1}})
        raise AssertionError("未知键应抛错")
    except ValueError as e:
        assert CONFIG_UNKNOWN_KEY in str(e)

    try:
        core_config_from({"core": {"max_loops": 0}})
        raise AssertionError("越界值应抛错")
    except ValueError as e:
        assert CONFIG_INVALID_VALUE in str(e)
