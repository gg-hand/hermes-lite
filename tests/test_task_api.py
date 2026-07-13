"""调度 API 端点单元测试（Phase 6 Task 7）。

验证 ``src/server.py`` 的 /schedules 端点。使用 FastAPI TestClient。

注：原 /tasks 端点（GET/POST/DELETE 等）已在 T7 删除——plan 模式下任务
管理改用 TodoList 内存对象，通过 SSE 事件（todo_init / todo_update /
todo_complete）实时推送，不再需要 REST 端点。TaskManager 仍以只读归档
解析器形式保留（lifespan 中初始化），但其写接口与对应 REST 端点均已移除。
本测试文件仅覆盖 /schedules 端点（Cron 调度独立于 plan 模式，全部保留）。

setup 要点：
- 参考 ``tests/test_policy_engine.py`` / ``tests/test_config_update.py`` 的 mock 模式。
- 使用 ``tests/_mock_deps.py`` 注入 chromadb/numpy 等 mock。
- patch 模块级全局 ``cron_scheduler`` / ``orchestrator`` 为真实（指向临时
  文件）或 mock 实例，避免真实 LLM 调用与 lifespan 依赖。
- 每个测试用例使用独立临时 schedules.yaml 文件，避免相互污染。

运行方式:
    python -m unittest tests.test_task_api -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.server import app  # noqa: E402
from hermes.tasks.scheduler import CronScheduler  # noqa: E402
# Task 11: server.py 全局变量已删除，通过 app.dependency_overrides 注入 mock
from hermes.app import get_orchestrator, get_cron_scheduler  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


class MockOrchestrator:
    """真实可调用的 mock orchestrator，记录 chat 调用。

    不使用 unittest.mock.MagicMock，因为 asyncio.to_thread 需要真实可调用对象。
    """

    def __init__(self):
        self.calls = []

    def chat(self, session_id, user_input):
        self.calls.append((session_id, user_input))
        return "mock response"


class TestScheduleApi(unittest.TestCase):
    """调度 API 端点测试，每例使用独立临时文件并 patch 全局组件。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")
        # 真实 CronScheduler 指向临时文件，保证端点逻辑被真实执行
        self.cs = CronScheduler(schedules_file=self.sched_file)
        self.orch = MockOrchestrator()
        # Task 11: 通过 DI overrides 注入 mock（不再 patch src.server 全局变量）
        app.dependency_overrides[get_cron_scheduler] = lambda: self.cs
        app.dependency_overrides[get_orchestrator] = lambda: self.orch
        self._patches = []

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        app.dependency_overrides.pop(get_cron_scheduler, None)
        app.dependency_overrides.pop(get_orchestrator, None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    # ---------- /schedules 端点 ----------

    def test_get_schedules_empty(self):
        """初始 GET /schedules 返回空列表。"""
        client = self._client()
        resp = client.get("/schedules")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["schedules"], [])

    def test_create_schedule_invalid_cron_returns_400(self):
        """非法 cron 返回 400。"""
        client = self._client()
        resp = client.post("/schedules", json={
            "name": "非法", "cron": "99 * * * *", "task": "x",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cron", resp.json()["detail"])

    def test_create_schedule_valid(self):
        """合法调度项创建成功。"""
        client = self._client()
        resp = client.post("/schedules", json={
            "name": "每分钟", "cron": "* * * * *", "task": "hello",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["schedule_id"])
        self.assertIn("已创建", data["message"])
        # 列表中可见
        schedules = client.get("/schedules").json()["schedules"]
        self.assertEqual(len(schedules), 1)

    def test_update_schedule_enabled_hot_update(self):
        """更新 enabled 即时生效，needs_restart=False。"""
        client = self._client()
        create_resp = client.post("/schedules", json={
            "name": "九点", "cron": "0 9 * * *", "task": "morning",
        })
        sched_id = create_resp.json()["schedule_id"]
        resp = client.put(f"/schedules/{sched_id}", json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["needs_restart"])
        # 验证 enabled 已更新
        schedules = client.get("/schedules").json()["schedules"]
        self.assertFalse(schedules[0]["enabled"])

    def test_delete_schedule(self):
        """删除调度项。"""
        client = self._client()
        create_resp = client.post("/schedules", json={
            "name": "待删", "cron": "* * * * *", "task": "x",
        })
        sched_id = create_resp.json()["schedule_id"]
        resp = client.delete(f"/schedules/{sched_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(client.get("/schedules").json()["schedules"], [])

    def test_trigger_schedule_async(self):
        """验证端点返回 {"status":"ok"}（不验证实际触发）。"""
        client = self._client()
        create_resp = client.post("/schedules", json={
            "name": "触发", "cron": "* * * * *", "task": "fire",
        })
        sched_id = create_resp.json()["schedule_id"]
        resp = client.post(f"/schedules/{sched_id}/trigger")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
