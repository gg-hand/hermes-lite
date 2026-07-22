"""NotifyHook 测试（6.1，Q7/Q8/Q11）。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

from teage_liu.tasks.hooks.notify_hook import NotifyHook


class TestNotifyHookFailureCount(unittest.TestCase):
    """Q8: _failure_counts 写入 CronScheduler 实例（非 HookRegistry）。"""

    def _make_scheduler_ref(self):
        scheduler = MagicMock()
        scheduler._failure_counts = {}
        return scheduler

    def test_failure_count_increments_on_failure(self):
        scheduler = self._make_scheduler_ref()
        hook = NotifyHook(config={}, scheduler_ref=scheduler)
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        result = MagicMock(success=False)

        asyncio.run(hook.after_execute(ctx, result))
        self.assertEqual(scheduler._failure_counts["s1"], 1)

    def test_failure_count_resets_on_success(self):
        scheduler = self._make_scheduler_ref()
        scheduler._failure_counts["s1"] = 2
        hook = NotifyHook(config={}, scheduler_ref=scheduler)
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        result = MagicMock(success=True)

        asyncio.run(hook.after_execute(ctx, result))
        self.assertNotIn("s1", scheduler._failure_counts)

    def test_failure_count_zero_removes_key(self):
        """count=0 时从 _failure_counts 移除 key（不留 0 值）。"""
        scheduler = self._make_scheduler_ref()
        scheduler._failure_counts["s1"] = 1
        hook = NotifyHook(config={}, scheduler_ref=scheduler)
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        result = MagicMock(success=True)

        asyncio.run(hook.after_execute(ctx, result))
        self.assertNotIn("s1", scheduler._failure_counts)


class TestNotifyHookAutoDisable(unittest.TestCase):
    """连续失败达阈值 → 自动 disable。"""

    def test_threshold_reached_disables_schedule(self):
        scheduler = MagicMock()
        scheduler._failure_counts = {}
        hook = NotifyHook(
            config={"consecutive_failures_threshold": 3},
            scheduler_ref=scheduler,
        )
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        ctx.schedule.enabled = True
        result = MagicMock(success=False)

        # 模拟前 2 次失败
        scheduler._failure_counts["s1"] = 2
        asyncio.run(hook.after_execute(ctx, result))
        self.assertFalse(ctx.schedule.enabled)

    def test_default_threshold_is_3(self):
        hook = NotifyHook(config={}, scheduler_ref=MagicMock())
        self.assertEqual(hook.threshold, 3)


class TestNotifyHookChannels(unittest.TestCase):
    """邮件 + Webhook 双通道。"""

    def test_email_disabled_not_sent(self):
        scheduler = MagicMock()
        scheduler._failure_counts = {}
        hook = NotifyHook(
            config={"channels": {"email": {"enabled": False}}},
            scheduler_ref=scheduler,
        )
        hook._send_email = AsyncMock()
        hook._send_webhook = AsyncMock()
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        result = MagicMock(success=False)

        asyncio.run(hook.after_execute(ctx, result))
        hook._send_email.assert_not_awaited()

    def test_email_enabled_sent(self):
        scheduler = MagicMock()
        scheduler._failure_counts = {}
        hook = NotifyHook(
            config={"channels": {"email": {"enabled": True}}},
            scheduler_ref=scheduler,
        )
        hook._send_email = AsyncMock()
        hook._send_webhook = AsyncMock()
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        ctx.retry_count = 0
        ctx.retry_max = 3
        result = MagicMock(success=False)

        asyncio.run(hook.after_execute(ctx, result))
        hook._send_email.assert_awaited_once()

    def test_both_channels_sent_in_parallel(self):
        scheduler = MagicMock()
        scheduler._failure_counts = {}
        hook = NotifyHook(
            config={"channels": {
                "email": {"enabled": True},
                "webhook": {"enabled": True},
            }},
            scheduler_ref=scheduler,
        )
        hook._send_email = AsyncMock()
        hook._send_webhook = AsyncMock()
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        ctx.retry_count = 0
        ctx.retry_max = 3
        result = MagicMock(success=False)

        asyncio.run(hook.after_execute(ctx, result))
        hook._send_email.assert_awaited_once()
        hook._send_webhook.assert_awaited_once()

    def test_channel_failure_does_not_propagate(self):
        """单个通道失败不影响其他通道与主流程。"""
        scheduler = MagicMock()
        scheduler._failure_counts = {}
        hook = NotifyHook(
            config={"channels": {"email": {"enabled": True}}},
            scheduler_ref=scheduler,
        )
        hook._send_email = AsyncMock(side_effect=Exception("SMTP down"))
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        ctx.retry_count = 0
        ctx.retry_max = 3
        result = MagicMock(success=False)

        # 不应抛异常
        asyncio.run(hook.after_execute(ctx, result))


class TestNotifyHookNotifiedFields(unittest.TestCase):
    """RunSummary.notified / notification_channels 由 NotifyHook 写入。"""

    def test_notified_set_true_on_send(self):
        scheduler = MagicMock()
        scheduler._failure_counts = {}
        hook = NotifyHook(
            config={"channels": {"email": {"enabled": True}}},
            scheduler_ref=scheduler,
        )
        hook._send_email = AsyncMock()
        ctx = MagicMock()
        ctx.schedule.id = "s1"
        ctx.retry_count = 0
        ctx.retry_max = 3
        result = MagicMock(success=False)
        result.notified = False
        result.notification_channels = []

        asyncio.run(hook.after_execute(ctx, result))
        self.assertTrue(result.notified)
        self.assertIn("email", result.notification_channels)


if __name__ == "__main__":
    unittest.main()
