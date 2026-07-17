"""ScheduleHookBase 基类 + RetryDecision 测试（3.3）。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.tasks.hooks.base import ScheduleHookBase, RetryDecision


class TestScheduleHookBase(unittest.TestCase):
    """ScheduleHookBase 提供 no-op 默认实现，避免 AttributeError。"""

    def test_before_execute_returns_ctx_unchanged(self):
        """默认 before_execute 返回 ctx 不变。"""
        hook = ScheduleHookBase()
        ctx = {"test": "value"}
        result = asyncio.run(hook.before_execute(ctx))
        self.assertIs(result, ctx)

    def test_on_failure_returns_give_up(self):
        """默认 on_failure 返回 give_up（不重试）。"""
        hook = ScheduleHookBase()
        decision = asyncio.run(hook.on_failure({}, ValueError("test")))
        self.assertFalse(decision.retry)
        self.assertEqual(decision.delay_seconds, 0)

    def test_after_execute_is_noop(self):
        """默认 after_execute 为 no-op，不抛异常。"""
        hook = ScheduleHookBase()
        # 不应抛异常
        asyncio.run(hook.after_execute({}, MagicMock()))

    def test_name_default_empty(self):
        """name 默认空串。"""
        hook = ScheduleHookBase()
        self.assertEqual(hook.name, "")


class TestRetryDecision(unittest.TestCase):
    """RetryDecision 工厂方法。"""

    def test_give_up(self):
        d = RetryDecision.give_up(reason="测试放弃")
        self.assertFalse(d.retry)
        self.assertEqual(d.reason, "测试放弃")

    def test_retry_after(self):
        d = RetryDecision.retry_after(seconds=300, reason="重试")
        self.assertTrue(d.retry)
        self.assertEqual(d.delay_seconds, 300)
        self.assertEqual(d.reason, "重试")


if __name__ == "__main__":
    unittest.main()
