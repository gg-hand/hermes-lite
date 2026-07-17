"""CatchUpHook 测试（5.1，Q9 串行 await）。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

from hermes.tasks.hooks.catchup_hook import CatchUpHook


class TestCatchUpHookBeforeExecute(unittest.TestCase):
    """before_execute 运行时为空操作（仅启动扫描调用 scan_and_compensate）。"""

    def test_before_execute_returns_ctx_unchanged(self):
        hook = CatchUpHook(config={})
        ctx = MagicMock()
        result = asyncio.run(hook.before_execute(ctx))
        self.assertIs(result, ctx)


class TestCatchUpHookScanAndCompensate(unittest.TestCase):
    """scan_and_compensate 启动时扫描过期调度项。"""

    def _make_scheduler(self, schedules, run_direct_mock=None):
        scheduler = MagicMock()
        scheduler._schedules = schedules
        scheduler._cron_exprs = {}
        scheduler._run_schedule_direct = run_direct_mock or AsyncMock()
        scheduler._persist = MagicMock()
        return scheduler

    def test_skip_policy_does_not_execute(self):
        """catch_up_policy=skip 时不调用 _run_schedule_direct，仅更新 next_run。"""
        hook = CatchUpHook(config={"default_policy": "skip"})
        past_dt = datetime.now() - timedelta(hours=2)
        schedule = MagicMock()
        schedule.id = "s1"
        schedule.enabled = True
        schedule.next_run = past_dt.isoformat()
        schedule.catch_up_policy = "skip"

        cron_expr = MagicMock()
        cron_expr.next_run.return_value = datetime.now() + timedelta(days=1)
        scheduler = self._make_scheduler([schedule])
        scheduler._cron_exprs = {"s1": cron_expr}

        compensated = asyncio.run(hook.scan_and_compensate(scheduler))
        self.assertEqual(compensated, [])
        scheduler._run_schedule_direct.assert_not_awaited()
        scheduler._persist.assert_called_once()

    def test_execute_once_policy_triggers_run(self):
        """catch_up_policy=execute_once 时调用 _run_schedule_direct。"""
        hook = CatchUpHook(config={"default_policy": "skip"})
        past_dt = datetime.now() - timedelta(hours=2)
        schedule = MagicMock()
        schedule.id = "s1"
        schedule.enabled = True
        schedule.next_run = past_dt.isoformat()
        schedule.catch_up_policy = "execute_once"

        cron_expr = MagicMock()
        cron_expr.next_run.return_value = datetime.now() + timedelta(days=1)
        scheduler = self._make_scheduler([schedule], run_direct_mock=AsyncMock())
        scheduler._cron_exprs = {"s1": cron_expr}

        compensated = asyncio.run(hook.scan_and_compensate(scheduler))
        self.assertEqual(compensated, ["s1"])
        scheduler._run_schedule_direct.assert_awaited_once_with(schedule)

    def test_default_policy_fallback_when_schedule_missing_attr(self):
        """schedule 无 catch_up_policy 字段时用 hook.default_policy。"""
        hook = CatchUpHook(config={"default_policy": "execute_once"})
        past_dt = datetime.now() - timedelta(hours=2)
        schedule = MagicMock(spec=["id", "enabled", "next_run"])
        schedule.id = "s1"
        schedule.enabled = True
        schedule.next_run = past_dt.isoformat()

        cron_expr = MagicMock()
        cron_expr.next_run.return_value = datetime.now() + timedelta(days=1)
        scheduler = self._make_scheduler([schedule])
        scheduler._cron_exprs = {"s1": cron_expr}

        compensated = asyncio.run(hook.scan_and_compensate(scheduler))
        self.assertEqual(compensated, ["s1"])

    def test_disabled_schedule_skipped(self):
        """enabled=False 的调度不补偿。"""
        hook = CatchUpHook(config={"default_policy": "execute_once"})
        schedule = MagicMock()
        schedule.id = "s1"
        schedule.enabled = False
        schedule.next_run = (datetime.now() - timedelta(hours=2)).isoformat()
        schedule.catch_up_policy = "execute_once"

        scheduler = self._make_scheduler([schedule])
        compensated = asyncio.run(hook.scan_and_compensate(scheduler))
        self.assertEqual(compensated, [])

    def test_future_next_run_skipped(self):
        """未过期的调度不补偿。"""
        hook = CatchUpHook(config={"default_policy": "execute_once"})
        future_dt = datetime.now() + timedelta(hours=2)
        schedule = MagicMock()
        schedule.id = "s1"
        schedule.enabled = True
        schedule.next_run = future_dt.isoformat()
        schedule.catch_up_policy = "execute_once"

        scheduler = self._make_scheduler([schedule])
        compensated = asyncio.run(hook.scan_and_compensate(scheduler))
        self.assertEqual(compensated, [])

    def test_invalid_next_run_skipped(self):
        """next_run 非 ISO 字符串时跳过（不抛异常）。"""
        hook = CatchUpHook(config={"default_policy": "execute_once"})
        schedule = MagicMock()
        schedule.id = "s1"
        schedule.enabled = True
        schedule.next_run = "not-a-date"
        schedule.catch_up_policy = "execute_once"

        scheduler = self._make_scheduler([schedule])
        compensated = asyncio.run(hook.scan_and_compensate(scheduler))
        self.assertEqual(compensated, [])

    def test_multiple_schedules_serial(self):
        """Q9: 多个过期调度串行 await（非并发）。"""
        hook = CatchUpHook(config={"default_policy": "execute_once"})
        call_order = []

        async def mock_run(sched):
            call_order.append(sched.id)

        past_dt = datetime.now() - timedelta(hours=2)
        schedules = []
        for i in range(3):
            s = MagicMock()
            s.id = f"s{i}"
            s.enabled = True
            s.next_run = past_dt.isoformat()
            s.catch_up_policy = "execute_once"
            schedules.append(s)

        cron_expr = MagicMock()
        cron_expr.next_run.return_value = datetime.now() + timedelta(days=1)
        scheduler = self._make_scheduler(schedules, run_direct_mock=mock_run)
        scheduler._cron_exprs = {f"s{i}": cron_expr for i in range(3)}

        compensated = asyncio.run(hook.scan_and_compensate(scheduler))
        self.assertEqual(compensated, ["s0", "s1", "s2"])
        self.assertEqual(call_order, ["s0", "s1", "s2"])  # 串行顺序


class TestRunScheduleDirect(unittest.TestCase):
    """_run_schedule_direct 是 thin wrapper（spec 5.1 P1 修复）。"""

    def test_delegates_to_run_schedule_with_injected_params(self):
        from hermes.tasks.scheduler import CronScheduler

        scheduler = CronScheduler.__new__(CronScheduler)
        scheduler._orchestrator = MagicMock()
        scheduler._run_schedule = AsyncMock()

        mock_schedule = MagicMock()
        asyncio.run(scheduler._run_schedule_direct(mock_schedule))

        scheduler._run_schedule.assert_awaited_once()
        call_args = scheduler._run_schedule.call_args
        self.assertEqual(len(call_args[0]), 3)
        self.assertIs(call_args[0][0], scheduler._orchestrator)
        self.assertIs(call_args[0][1], mock_schedule)
        self.assertIsInstance(call_args[0][2], datetime)


class TestRunLoopStartupCatchUp(unittest.TestCase):
    """run_loop 启动时调用 CatchUpHook.scan_and_compensate（spec 5.1）。"""

    def test_run_loop_invokes_scan_and_compensate_before_first_iteration(self):
        """run_loop 首次循环前调用 hooks.catchup.scan_and_compensate。"""
        from hermes.tasks.scheduler import CronScheduler

        scheduler = CronScheduler.__new__(CronScheduler)
        scheduler._schedules = []
        scheduler._cron_exprs = {}
        scheduler._last_triggered_minute = {}
        scheduler._stop_event = asyncio.Event()
        # 让 run_loop 在第一次 wait_for 后立即退出
        scheduler._stop_event.set()

        mock_orchestrator = MagicMock()
        scheduler.hooks = MagicMock()
        scheduler.hooks.hooks = {}  # 无 catchup hook
        # 当 hooks.hooks 含 catchup 时应被调用
        called = {"scan": False}

        async def mock_scan(s):
            called["scan"] = True
            return []

        mock_catchup = MagicMock()
        mock_catchup.scan_and_compensate = mock_scan
        scheduler.hooks.hooks = {"catchup": mock_catchup}

        asyncio.run(scheduler.run_loop(mock_orchestrator))

        self.assertTrue(called["scan"], "run_loop 启动应调用 scan_and_compensate")


if __name__ == "__main__":
    unittest.main()
