"""runs.jsonl 持久化失败告警测试（治本脆弱点 6）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.tasks.scheduler import CronScheduler


class TestAppendRunSummaryFailureAlert(unittest.TestCase):
    """runs_store 未初始化或 append 抛异常时记 error 日志，不静默 return。"""

    def _make_schedule(self):
        mock_schedule = MagicMock()
        mock_schedule.id = "test_sched"
        mock_schedule.name = "test_sched"
        mock_schedule.generate_llm_summary = False
        return mock_schedule

    def _call_append(self, scheduler, schedule):
        """以最小合法参数调用 _append_run_summary。"""
        scheduler._append_run_summary(
            schedule,  # schedule
            "",  # started_at
            "",  # finished_at
            0.0,  # duration_seconds
            True,  # success
            "",  # user_input
            "",  # assistant_response
            [],  # tool_calls
            [],  # outputs
            [],  # errors
        )

    def test_runs_store_none_logs_error(self):
        """runs_store 为 None 时记 error 日志。"""
        scheduler = CronScheduler.__new__(CronScheduler)
        scheduler.runs_store = None
        schedule = self._make_schedule()

        with patch("hermes.tasks.scheduler.logger") as mock_logger:
            self._call_append(scheduler, schedule)
            mock_logger.error.assert_called()
            error_msg = mock_logger.error.call_args[0][0]
            self.assertIn("runs_store", error_msg)

    def test_persist_exception_logs_error(self):
        """append 抛异常时记 error 日志，不传播。"""
        scheduler = CronScheduler.__new__(CronScheduler)
        scheduler.runs_store = MagicMock()
        scheduler.runs_store.append.side_effect = OSError("disk full")
        schedule = self._make_schedule()

        with patch("hermes.tasks.scheduler.logger") as mock_logger:
            # 不应抛异常
            self._call_append(scheduler, schedule)
            mock_logger.error.assert_called()


if __name__ == "__main__":
    unittest.main()
