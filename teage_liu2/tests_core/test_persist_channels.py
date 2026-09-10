"""P-7 双档落盘机制的**实现级**测试（2026-09-10 评审 P-10）。

为什么放在这里而不是协议黄金用例：flush / background 档位是 PENDING P-7 的
**experimental 机制**，`storage.spec.md` 的 S-1 条款并未规定档位。把它写进黄金用例
会让"按 S-1 正确实现但不支持 `log_message_buffered`"的宿主无故变红。故协议的归协议
（用例 18 只断言落盘序 / 消息类型 / content_blocks），实验机制的归实现测试。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from teage_liu2.core.hooks import HookChain
from teage_liu2.core.pipeline import ChatPipeline, MODE_LOOP
from .fake_llm import FakeLLMClient

_SCRIPT = [{"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}]


class _RecordingStore:
    """记录落盘调用与档位（鸭子类型，不落真库）。"""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str, Optional[str]]] = []

    def ensure_session(self, session_id: str) -> None:
        pass

    def get_session_messages(self, session_id: str, limit: Optional[int] = None,
                             before_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return []

    def log_message(self, session_id: str, role: str, content: str, **kw: Any) -> None:
        self.records.append(("direct", role, kw.get("message_type")))

    def log_message_buffered(self, session_id: str, role: str, content: str, **kw: Any) -> None:
        self.records.append(("buffered", role, kw.get("message_type")))

    def close(self) -> None:
        pass


class _LegacyStore:
    """没有 `log_message_buffered` 的实现（默认 SQLite 形态）→ 一律直发。"""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str, Optional[str]]] = []

    def ensure_session(self, session_id: str) -> None:
        pass

    def get_session_messages(self, session_id: str, limit: Optional[int] = None,
                             before_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return []

    def log_message(self, session_id: str, role: str, content: str, **kw: Any) -> None:
        self.records.append(("direct", role, kw.get("message_type")))

    def close(self) -> None:
        pass


async def _collect(agen):
    return [ev async for ev in agen]


def _run(store) -> None:
    pipeline = ChatPipeline(
        llm_client=FakeLLMClient(list(_SCRIPT)), history_store=store,
        hooks=HookChain(), mode=MODE_LOOP,
    )
    asyncio.run(_collect(pipeline.chat_stream("s-ch", "hi")))


def test_user_flush_and_assistant_background_channels():
    """user 前置走 flush 档(direct) → assistant 走 background 档(buffered)。"""
    store = _RecordingStore()
    _run(store)
    assert store.records == [
        ("direct", "user", None),
        ("buffered", "assistant", "assistant"),
    ], f"档位错误: {store.records}"


def test_store_without_buffered_capability_stays_direct():
    """无 `log_message_buffered` 的实现 → 全部直发，行为与 P-7 之前一致。"""
    store = _LegacyStore()
    _run(store)
    assert store.records == [
        ("direct", "user", None),
        ("direct", "assistant", "assistant"),
    ]
