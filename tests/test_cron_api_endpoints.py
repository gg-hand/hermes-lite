"""cron API 端点测试（7.3）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

# 先 import hermes.server.app 触发完整 app 装配，避免循环导入
from hermes.server import app  # noqa: E402,F401


class TestCronRunsApiEndpoints(unittest.TestCase):
    """5 个 API 端点：执行历史 / 手动重跑 / 单次详情 / 最近记录 / 统计。"""

    def setUp(self):
        self.mock_app = MagicMock()
        self.mock_app.state.cron_scheduler = MagicMock()
        self.mock_app.state.cron_scheduler.runs_store = MagicMock()
        # mock schedule 列表（用于 trigger 与 read_recent_all）
        self.mock_schedule = MagicMock()
        self.mock_schedule.id = "s1"
        self.mock_schedule.name = "test_schedule"
        self.mock_app.state.cron_scheduler.list_schedules.return_value = [
            {"id": "s1", "name": "test_schedule"}
        ]
        # _schedules 用于 trigger_schedule_run 中的查找
        self.mock_app.state.cron_scheduler._schedules = [self.mock_schedule]

    def test_get_schedule_runs_returns_list(self):
        """GET /cron_tools/schedules/{id}/runs 返回执行历史列表。"""
        from hermes.tasks.run_summary import RunSummary
        mock_runs = [
            RunSummary(schedule_id="s1", run_id="r1", success=True),
            RunSummary(schedule_id="s1", run_id="r2", success=False),
        ]
        self.mock_app.state.cron_scheduler.runs_store.read_recent.return_value = mock_runs

        from hermes.routes.cron_tools import get_schedule_runs
        response = get_schedule_runs("s1", limit=20, request=self._make_request())
        self.assertEqual(response.status_code, 200)

    def test_get_schedule_runs_scheduler_none_returns_503(self):
        """scheduler 为 None 时返回 503。"""
        self.mock_app.state.cron_scheduler = None
        from hermes.routes.cron_tools import get_schedule_runs
        response = get_schedule_runs("s1", limit=20, request=self._make_request())
        self.assertEqual(response.status_code, 503)

    def test_get_run_by_id_returns_detail(self):
        """GET /cron_tools/schedules/{id}/runs/{run_id} 返回单次详情。"""
        from hermes.tasks.run_summary import RunSummary
        mock_run = RunSummary(schedule_id="s1", run_id="r1", success=False)
        mock_dict = mock_run.to_dict()
        self.mock_app.state.cron_scheduler.runs_store.read_by_run_id.return_value = mock_dict

        from hermes.routes.cron_tools import get_run_detail
        response = get_run_detail("s1", "r1", request=self._make_request())
        self.assertEqual(response.status_code, 200)

    def test_get_run_by_id_not_found_returns_404(self):
        """run_id 不存在时返回 404。"""
        self.mock_app.state.cron_scheduler.runs_store.read_by_run_id.return_value = None

        from hermes.routes.cron_tools import get_run_detail
        response = get_run_detail("s1", "nonexistent", request=self._make_request())
        self.assertEqual(response.status_code, 404)

    def test_trigger_run_returns_202(self):
        """POST /cron_tools/schedules/{id}/run 异步触发，返回 202。"""
        from hermes.routes.cron_tools import trigger_schedule_run
        # mock _run_schedule_direct 为 AsyncMock 避免实际执行
        self.mock_app.state.cron_scheduler._run_schedule_direct = AsyncMock()
        response = trigger_schedule_run("s1", request=self._make_request())
        self.assertEqual(response.status_code, 202)

    def test_trigger_run_schedule_not_found_returns_404(self):
        """schedule_id 不存在时返回 404。"""
        from hermes.routes.cron_tools import trigger_schedule_run
        response = trigger_schedule_run("nonexistent", request=self._make_request())
        self.assertEqual(response.status_code, 404)

    def test_get_recent_runs_returns_all_schedules(self):
        """GET /cron_tools/runs/recent 返回所有调度最近记录。"""
        from hermes.routes.cron_tools import get_recent_runs
        self.mock_app.state.cron_scheduler.runs_store.read_recent_all.return_value = []
        response = get_recent_runs(limit=50, request=self._make_request())
        self.assertEqual(response.status_code, 200)

    def test_get_run_stats_returns_summary(self):
        """GET /cron_tools/runs/stats 返回今日统计。"""
        from hermes.routes.cron_tools import get_run_stats
        self.mock_app.state.cron_scheduler.runs_store.read_recent_all.return_value = []
        response = get_run_stats(request=self._make_request())
        self.assertEqual(response.status_code, 200)

    def test_get_run_stats_with_data(self):
        """stats 端点正确统计今日成功/失败/耗时。"""
        from datetime import date
        today_str = date.today().isoformat()
        mock_runs = [
            {"started_at": today_str + "T10:00:00", "success": True, "duration_seconds": 5.0},
            {"started_at": today_str + "T11:00:00", "success": False, "duration_seconds": 3.0},
            {"started_at": "2020-01-01T00:00:00", "success": True, "duration_seconds": 1.0},  # 非今日
        ]
        self.mock_app.state.cron_scheduler.runs_store.read_recent_all.return_value = mock_runs

        from hermes.routes.cron_tools import get_run_stats
        import json
        response = get_run_stats(request=self._make_request())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["today_success"], 1)
        self.assertEqual(body["today_failure"], 1)
        self.assertEqual(body["today_total"], 2)
        self.assertEqual(body["total_duration_seconds"], 8.0)

    def _make_request(self):
        request = MagicMock()
        request.app = self.mock_app
        return request


if __name__ == "__main__":
    unittest.main()
