"""_run_schedule 主流程集成测试（3.6）。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.tasks.hooks.base import RetryDecision
from hermes.agent.tool_error import ValidationError


class TestRunScheduleHookIntegration(unittest.TestCase):
    """_run_schedule 在 3 个接入点调用 hooks。"""

    def setUp(self):
        """构造最小 CronScheduler 实例。"""
        from hermes.tasks.scheduler import CronScheduler
        self.scheduler = CronScheduler.__new__(CronScheduler)
        self.scheduler.hooks = MagicMock()
        self.scheduler.hooks.before_execute = AsyncMock(return_value=MagicMock(validation_errors=[]))
        self.scheduler.hooks.on_failure = AsyncMock(return_value=RetryDecision.give_up())
        self.scheduler.hooks.after_execute = AsyncMock()
        self.scheduler.hooks.get_retry_max = MagicMock(return_value=3)
        self.scheduler._failure_counts = {}
        self.scheduler._orchestrator = MagicMock()
        self.scheduler._schedules = []
        self.scheduler._cron_exprs = {}
        self.scheduler.runs_store = MagicMock()
        self.scheduler.workflow_context_factory = None
        # Mock _clear_cron_history / _build_cron_archive_callback as no-ops
        self.scheduler._clear_cron_history = MagicMock()
        self.scheduler._build_cron_archive_callback = MagicMock(return_value=None)
        self.scheduler._parse_last_run_time = MagicMock(return_value=None)
        self.scheduler._persist = MagicMock()
        self.scheduler._append_run_summary = MagicMock()

    def test_validation_error_routes_to_finalize_failure(self):
        """ValidationError 路由到 _finalize_failure（含 after_execute）。"""
        from hermes.tasks.scheduler import CronScheduler
        from hermes.agent.tool_error import ValidationError

        # 构造 ctx 抛 ValidationError
        mock_ctx = MagicMock()
        mock_ctx.validation_errors = ["step1 工具不存在"]
        self.scheduler.hooks.before_execute = AsyncMock(return_value=mock_ctx)

        # mock _finalize_failure 验证被调用
        self.scheduler._finalize_failure = AsyncMock()
        self.scheduler._build_workflow_context = MagicMock(return_value=mock_ctx)

        mock_schedule = MagicMock()
        mock_schedule.id = "test"
        mock_schedule.name = "test"
        mock_schedule.workflow = None
        mock_schedule.task = "test task"

        asyncio.run(self.scheduler._run_schedule(
            self.scheduler._orchestrator, mock_schedule, datetime.now()
        ))

        self.scheduler._finalize_failure.assert_awaited_once()

    def test_after_execute_called_on_success(self):
        """成功路径也调用 after_execute（P0 修复）。"""
        from hermes.tasks.workflow.base import WorkflowResult

        mock_ctx = MagicMock()
        mock_ctx.validation_errors = []
        mock_ctx.schedule = None
        mock_ctx.retry_max = 3
        mock_ctx.retry_count = 0
        mock_ctx.last_error = None
        self.scheduler.hooks.before_execute = AsyncMock(return_value=mock_ctx)
        self.scheduler._build_workflow_context = MagicMock(return_value=mock_ctx)

        # mock _execute_workflow 返回成功 result
        success_result = WorkflowResult(success=True, assistant_response="ok")
        self.scheduler._execute_workflow = MagicMock(return_value=success_result)

        mock_schedule = MagicMock()
        mock_schedule.id = "test"
        mock_schedule.name = "test"
        mock_schedule.workflow = {"name": "wf", "steps": []}
        mock_schedule.task = "test task"
        mock_schedule.generate_llm_summary = False

        asyncio.run(self.scheduler._run_schedule(
            self.scheduler._orchestrator, mock_schedule, datetime.now()
        ))

        self.scheduler.hooks.after_execute.assert_awaited_once()


class TestRunScheduleDirect(unittest.TestCase):
    """_run_schedule_direct 是 thin wrapper。"""

    def test_delegates_to_run_schedule(self):
        from hermes.tasks.scheduler import CronScheduler
        scheduler = CronScheduler.__new__(CronScheduler)
        scheduler._orchestrator = MagicMock()
        scheduler._run_schedule = AsyncMock()

        mock_schedule = MagicMock()
        asyncio.run(scheduler._run_schedule_direct(mock_schedule))

        scheduler._run_schedule.assert_awaited_once()
        call_args = scheduler._run_schedule.call_args
        self.assertIs(call_args[0][0], scheduler._orchestrator)
        self.assertIs(call_args[0][1], mock_schedule)
        self.assertIsInstance(call_args[0][2], datetime)


if __name__ == "__main__":
    unittest.main()
