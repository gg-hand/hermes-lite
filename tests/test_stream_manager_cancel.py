"""StreamManager cancel_callback 槽位测试（spec Task 2）。

验证 spec 中三个核心场景：
- 注册 callback → trigger_cancel → callback 被调用，返回 True
- 未注册 → trigger_cancel → 返回 False
- 流结束（unregister_event）后 trigger → 返回 False

额外覆盖：
- set_cancel_callback(None) 清理后 trigger 返回 False
- register 覆盖旧流时同步清理旧 callback
- trigger_cancel 后槽位被清空（二次 trigger 返回 False）
- unregister_event 所有权不匹配时不误清新流 callback
- unregister（无所有权检查版）清理 callback

运行方式:
    python -m pytest tests/test_stream_manager_cancel.py -v
    python tests/test_stream_manager_cancel.py
"""

from __future__ import annotations

import os
import sys
import threading
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from src.stream_manager import StreamManager


def _make_callback(call_flag: threading.Event, name: str = "cb"):
    """构造一个 async close 回调，调用时设置 call_flag。"""

    async def fake_close():
        call_flag.set()

    fake_close.__name__ = name
    return fake_close


class TestStreamManagerCancelCallback(unittest.IsolatedAsyncioTestCase):
    """验证 StreamManager 的 cancel_callback 槽位机制。"""

    def setUp(self):
        self.sm = StreamManager()
        self.session_id = "test-session-1"

    # ── spec 三个核心场景 ──

    async def test_register_then_trigger_callback_called(self):
        """注册 callback → trigger_cancel → callback 被调用，返回 True。"""
        called = threading.Event()
        cb = _make_callback(called, "close_v1")

        await self.sm.set_cancel_callback(self.session_id, cb)
        result = await self.sm.trigger_cancel(self.session_id)

        self.assertTrue(result, "trigger_cancel 应返回 True（callback 已注册）")
        self.assertTrue(called.is_set(), "callback 应被调用")

    async def test_trigger_without_registration_returns_false(self):
        """未注册 callback → trigger_cancel → 返回 False。"""
        result = await self.sm.trigger_cancel(self.session_id)
        self.assertFalse(result, "未注册 callback 时应返回 False")

    async def test_trigger_after_unregister_event_returns_false(self):
        """流结束（unregister_event）后 trigger → 返回 False。"""
        called = threading.Event()
        cb = _make_callback(called, "close_v1")

        cancel_event = self.sm.register(self.session_id)
        await self.sm.set_cancel_callback(self.session_id, cb)

        # 流结束，unregister_event 清理 callback 槽位
        self.sm.unregister_event(self.session_id, cancel_event)

        result = await self.sm.trigger_cancel(self.session_id)
        self.assertFalse(result, "unregister_event 后 trigger_cancel 应返回 False")
        self.assertFalse(called.is_set(), "callback 不应被调用（已清理）")

    # ── 额外覆盖 ──

    async def test_set_none_clears_callback(self):
        """set_cancel_callback(None) 清理后 trigger 返回 False。"""
        called = threading.Event()
        cb = _make_callback(called, "close_v1")

        await self.sm.set_cancel_callback(self.session_id, cb)
        # 显式清理
        await self.sm.set_cancel_callback(self.session_id, None)

        result = await self.sm.trigger_cancel(self.session_id)
        self.assertFalse(result, "set None 后 trigger_cancel 应返回 False")
        self.assertFalse(called.is_set(), "callback 不应被调用（已清理）")

    async def test_trigger_clears_slot_second_trigger_false(self):
        """trigger_cancel 后槽位被清空，二次 trigger 返回 False。"""
        called = threading.Event()
        cb = _make_callback(called, "close_v1")

        await self.sm.set_cancel_callback(self.session_id, cb)

        first = await self.sm.trigger_cancel(self.session_id)
        self.assertTrue(first, "首次 trigger 应返回 True")
        self.assertTrue(called.is_set(), "callback 应被调用")

        second = await self.sm.trigger_cancel(self.session_id)
        self.assertFalse(second, "二次 trigger 应返回 False（槽位已清空）")

    async def test_register_replaces_old_callback(self):
        """register 覆盖旧流时同步清理旧 callback（防止旧流 callback 误刷新流）。"""
        old_called = threading.Event()
        new_called = threading.Event()
        old_cb = _make_callback(old_called, "old_close")
        new_cb = _make_callback(new_called, "new_close")

        # 旧流注册
        await self.sm.set_cancel_callback(self.session_id, old_cb)
        # 新流覆盖（register 内部清理旧 callback）
        self.sm.register(self.session_id)
        # 注册新 callback
        await self.sm.set_cancel_callback(self.session_id, new_cb)

        result = await self.sm.trigger_cancel(self.session_id)
        self.assertTrue(result, "trigger 应返回 True（新 callback 已注册）")
        self.assertFalse(
            old_called.is_set(),
            "旧 callback 不应被调用（register 已清理）",
        )
        self.assertTrue(new_called.is_set(), "新 callback 应被调用")

    async def test_unregister_event_ownership_mismatch_keeps_callback(self):
        """unregister_event 所有权不匹配时不误清新流 callback。"""
        called = threading.Event()
        cb = _make_callback(called, "new_close")

        old_event = self.sm.register(self.session_id)
        # 新流覆盖旧流
        new_event = self.sm.register(self.session_id)
        await self.sm.set_cancel_callback(self.session_id, cb)

        # 旧流 finally 用旧 event 调 unregister_event（所有权不匹配，应跳过）
        self.sm.unregister_event(self.session_id, old_event)

        result = await self.sm.trigger_cancel(self.session_id)
        self.assertTrue(
            result,
            "所有权不匹配时不应清理新流 callback，trigger 应返回 True",
        )
        self.assertTrue(called.is_set(), "新流 callback 应被调用")

    async def test_unregister_clears_callback(self):
        """unregister（无所有权检查版）清理 callback。"""
        called = threading.Event()
        cb = _make_callback(called, "close_v1")

        await self.sm.set_cancel_callback(self.session_id, cb)
        self.sm.unregister(self.session_id)

        result = await self.sm.trigger_cancel(self.session_id)
        self.assertFalse(result, "unregister 后 trigger_cancel 应返回 False")
        self.assertFalse(called.is_set(), "callback 不应被调用（已清理）")

    async def test_existing_cancel_event_mechanism_unchanged(self):
        """cancel_event（threading.Event）机制保持不变，与 cancel_callback 并行。"""
        cancel_event = self.sm.register(self.session_id)
        self.assertIsInstance(cancel_event, threading.Event)
        self.assertFalse(cancel_event.is_set())

        # 注册 callback 后 cancel_event 仍可用
        called = threading.Event()
        cb = _make_callback(called, "close_v1")
        await self.sm.set_cancel_callback(self.session_id, cb)

        # cancel() 仍可正常设置 event
        ok = self.sm.cancel(self.session_id)
        self.assertTrue(ok, "cancel() 应返回 True")
        self.assertTrue(cancel_event.is_set(), "cancel_event 应被设置")

        # trigger_cancel 也可独立调用（双路径设计）
        result = await self.sm.trigger_cancel(self.session_id)
        self.assertTrue(result, "trigger_cancel 应返回 True")
        self.assertTrue(called.is_set(), "callback 应被调用")


if __name__ == "__main__":
    unittest.main(verbosity=2)
