"""调度执行历史 + 调度隔离记忆端点单元测试（Phase 8 Task 1.8 + 1.9）。

验证 ``src/server.py`` 新增的 3 个端点：
- ``GET /schedules/{id}/history?limit=10``：按 session_id="cron:{id}" 取最近 N
  条 assistant 回复
- ``GET /schedules/{id}/memories?limit=20``：按 namespace=cron + cron_id 取
  隔离记忆
- ``DELETE /schedules/{id}/memories/{memory_id}``：删除单条隔离记忆，校验
  cron_id 一致防止跨调度项误删

setup 要点（参考 ``tests/test_task_api.py`` / ``tests/test_memory_dashboard_api.py``）：
- 使用 ``tests/_mock_deps.py`` 注入 chromadb / numpy 等 mock。
- ``cron_scheduler`` 用真实 CronScheduler 指向临时文件。
- ``session_logger`` 用真实 SessionLogger 指向临时 SQLite 文件，预置 assistant
  消息后验证 history 端点。
- ``orchestrator.chroma_store`` 用自定义 mock，支持 namespace/cron_id 参数的
  ``get_all_memories``，验证 memories 端点隔离性。

运行方式:
    python -m unittest tests.test_schedule_history_memories -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 添加 src/ 到 sys.path，使 `from app import ...` 与 routes/*.py 使用同一模块对象
# （FastAPI dependency_overrides 按函数对象身份匹配，导入路径不一致会导致 override 失效）
_SRC_DIR = os.path.join(_PROJECT_ROOT, "teage_liu")
from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

# 注意：必须从 `app`（而非 `src.app`）导入 get_* 函数，因为 routes/*.py 使用
# `from app import get_xxx`，FastAPI dependency_overrides 按函数对象身份匹配。
# 若导入路径不一致，override 不会生效。
from teage_liu.app import (  # noqa: E402
    get_cron_scheduler,
    get_orchestrator,
    get_session_logger,
)
from teage_liu.server import app  # noqa: E402
from teage_liu.storage.sqlite_log import SessionLogger  # noqa: E402
from teage_liu.tasks.scheduler import CronScheduler  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Mock 工具：支持 namespace + cron_id 的 chroma_store
# ---------------------------------------------------------------------------


class _MockChromaStore:
    """支持 namespace/cron_id 过滤的 chroma_store mock。

    与真实 ``ChromaMemoryStore.get_all_memories`` 接口一致：
    - ``get_all_memories(namespace=None, cron_id=None)`` -> list of dict
    - ``delete_memory(memory_id)`` -> None

    内部用 ``self._store`` 维护 id -> {id, content, metadata} 映射，按
    metadata.namespace / metadata.cron_id 过滤，模拟真实隔离层行为。
    """

    def __init__(self, memories: Optional[List[Dict[str, Any]]] = None) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}
        for m in memories or []:
            self._store[m["id"]] = {
                "id": m["id"],
                "content": m.get("content", ""),
                "metadata": m.get("metadata", {}),
            }
        self.delete_calls: List[str] = []

    def get_all_memories(
        self,
        namespace: Optional[str] = None,
        cron_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按 namespace + cron_id 过滤返回记忆列表。

        - namespace="cron"：只返回 metadata.namespace=="cron" 且 cron_id 匹配的条目
        - namespace="user"：只返回 metadata.namespace=="user" 或缺失 namespace 的条目
        - namespace=None：返回全部（管理员视图）
        """
        output = []
        for item in self._store.values():
            meta = item.get("metadata", {})
            if namespace == "cron":
                if meta.get("namespace") != "cron":
                    continue
                if cron_id is not None and meta.get("cron_id") != cron_id:
                    continue
            elif namespace == "user":
                if meta.get("namespace", "user") != "user":
                    continue
            output.append({
                "id": item["id"],
                "content": item["content"],
                "metadata": meta,
            })
        return output

    def delete_memory(self, memory_id: str) -> None:
        self.delete_calls.append(memory_id)
        self._store.pop(memory_id, None)


def _make_mock_orchestrator(chroma_store: _MockChromaStore) -> MagicMock:
    """构造 mock orchestrator，仅暴露 chroma_store 属性。"""
    mock = MagicMock()
    mock.chroma_store = chroma_store
    return mock


# ===========================================================================
# 测试类：GET /schedules/{id}/history
# ===========================================================================


class TestScheduleHistoryEndpoint(unittest.TestCase):
    """GET /schedules/{id}/history 端点测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")
        self.db_path = os.path.join(self.tmpdir, "sessions.db")
        self.cs = CronScheduler(schedules_file=self.sched_file)
        # 创建一个调度项
        self.sched_id = self.cs.add_schedule({
            "name": "测试调度", "cron": "* * * * *", "task": "hello",
        })
        # 真实 SessionLogger 指向临时 DB
        self.logger = SessionLogger(self.db_path)
        cron_session_id = f"cron:{self.sched_id}"
        self.logger.create_session(cron_session_id)
        # 预置 3 条 assistant + 2 条 user 消息（混入 user 验证过滤）
        self.logger.log_message(cron_session_id, "user", "task input 1")
        self.logger.log_message(cron_session_id, "assistant", "first reply")
        self.logger.log_message(cron_session_id, "user", "task input 2")
        self.logger.log_message(cron_session_id, "assistant", "second reply")
        self.logger.log_message(cron_session_id, "assistant", "third reply")
        # 另一个调度项的 cron session（验证不串扰）
        self.other_id = self.cs.add_schedule({
            "name": "其他调度", "cron": "0 * * * *", "task": "other",
        })
        other_session = f"cron:{self.other_id}"
        self.logger.create_session(other_session)
        self.logger.log_message(other_session, "assistant", "other reply")

        self._patches = []
        app.dependency_overrides[get_cron_scheduler] = lambda: self.cs
        app.dependency_overrides[get_session_logger] = lambda: self.logger

    def tearDown(self):
        app.dependency_overrides.pop(get_cron_scheduler, None)
        app.dependency_overrides.pop(get_session_logger, None)
        self.logger.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_history_returns_assistant_only(self):
        """history 端点只返回 assistant 角色消息。"""
        client = self._client()
        resp = client.get(f"/schedules/{self.sched_id}/history")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 3)
        for item in data["history"]:
            # 每条含 id / content / created_at / tool_name
            self.assertIn("id", item)
            self.assertIn("content", item)
            self.assertIn("created_at", item)
            self.assertIn("tool_name", item)

    def test_history_returns_newest_first(self):
        """history 默认按 id 倒序（最新在前）。"""
        client = self._client()
        resp = client.get(f"/schedules/{self.sched_id}/history")
        data = resp.json()
        contents = [item["content"] for item in data["history"]]
        self.assertEqual(contents, ["third reply", "second reply", "first reply"])

    def test_history_limit_param_truncates(self):
        """limit=2 截断到 2 条。"""
        client = self._client()
        resp = client.get(f"/schedules/{self.sched_id}/history?limit=2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 2)

    def test_history_session_isolation(self):
        """A 调度项不可见 B 调度项的执行历史。"""
        client = self._client()
        resp_a = client.get(f"/schedules/{self.sched_id}/history")
        resp_b = client.get(f"/schedules/{self.other_id}/history")
        contents_a = [item["content"] for item in resp_a.json()["history"]]
        contents_b = [item["content"] for item in resp_b.json()["history"]]
        self.assertIn("first reply", contents_a)
        self.assertNotIn("first reply", contents_b)
        self.assertIn("other reply", contents_b)
        self.assertNotIn("other reply", contents_a)

    def test_history_schedule_not_found_returns_404(self):
        """调度项不存在返回 404。"""
        client = self._client()
        resp = client.get("/schedules/nonexistent/history")
        self.assertEqual(resp.status_code, 404)

    def test_history_empty_session(self):
        """无执行历史的调度项返回空列表。"""
        new_id = self.cs.add_schedule({
            "name": "空历史", "cron": "0 0 * * *", "task": "empty",
        })
        client = self._client()
        resp = client.get(f"/schedules/{new_id}/history")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 0)
        self.assertEqual(resp.json()["history"], [])


# ===========================================================================
# 测试类：GET /schedules/{id}/memories
# ===========================================================================


class TestScheduleMemoriesEndpoint(unittest.TestCase):
    """GET /schedules/{id}/memories 端点测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")
        self.cs = CronScheduler(schedules_file=self.sched_file)
        self.sched_id_a = self.cs.add_schedule({
            "name": "调度A", "cron": "* * * * *", "task": "a",
        })
        self.sched_id_b = self.cs.add_schedule({
            "name": "调度B", "cron": "0 * * * *", "task": "b",
        })
        # 预置记忆：A 的 cron 记忆 2 条 + B 的 cron 记忆 1 条 + user 记忆 1 条
        self.memories = [
            {
                "id": "cron-a-1",
                "content": "调度A的事实1",
                "metadata": {"namespace": "cron", "cron_id": self.sched_id_a,
                             "type": "fact", "importance": 0.7},
            },
            {
                "id": "cron-a-2",
                "content": "调度A的事实2",
                "metadata": {"namespace": "cron", "cron_id": self.sched_id_a,
                             "type": "fact", "importance": 0.5},
            },
            {
                "id": "cron-b-1",
                "content": "调度B的事实1",
                "metadata": {"namespace": "cron", "cron_id": self.sched_id_b,
                             "type": "fact", "importance": 0.6},
            },
            {
                "id": "user-1",
                "content": "用户事实",
                "metadata": {"namespace": "user", "type": "fact",
                             "importance": 0.8},
            },
        ]
        self.store = _MockChromaStore(self.memories)
        self.orch = _make_mock_orchestrator(self.store)
        self._patches = []
        app.dependency_overrides[get_cron_scheduler] = lambda: self.cs
        app.dependency_overrides[get_orchestrator] = lambda: self.orch

    def tearDown(self):
        app.dependency_overrides.pop(get_cron_scheduler, None)
        app.dependency_overrides.pop(get_orchestrator, None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_memories_returns_only_own_namespace(self):
        """memories 端点只返回 namespace=cron 且 cron_id 匹配的条目。"""
        client = self._client()
        resp = client.get(f"/schedules/{self.sched_id_a}/memories")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 2)
        ids = [m["id"] for m in data["memories"]]
        self.assertIn("cron-a-1", ids)
        self.assertIn("cron-a-2", ids)
        self.assertNotIn("cron-b-1", ids)
        self.assertNotIn("user-1", ids)

    def test_memories_session_isolation(self):
        """A 调度项不可见 B 调度项的隔离记忆。"""
        client = self._client()
        resp_a = client.get(f"/schedules/{self.sched_id_a}/memories")
        resp_b = client.get(f"/schedules/{self.sched_id_b}/memories")
        self.assertEqual(resp_a.json()["total"], 2)
        self.assertEqual(resp_b.json()["total"], 1)
        self.assertEqual(resp_b.json()["memories"][0]["id"], "cron-b-1")

    def test_memories_does_not_leak_user_namespace(self):
        """memories 端点不返回 user 命名空间的记忆。"""
        client = self._client()
        resp = client.get(f"/schedules/{self.sched_id_a}/memories")
        ids = [m["id"] for m in resp.json()["memories"]]
        self.assertNotIn("user-1", ids)

    def test_memories_limit_param_truncates(self):
        """limit=1 截断到 1 条。"""
        client = self._client()
        resp = client.get(f"/schedules/{self.sched_id_a}/memories?limit=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 1)

    def test_memories_schedule_not_found_returns_404(self):
        """调度项不存在返回 404。"""
        client = self._client()
        resp = client.get("/schedules/nonexistent/memories")
        self.assertEqual(resp.status_code, 404)

    def test_memories_empty_result(self):
        """无隔离记忆的调度项返回空列表。"""
        new_id = self.cs.add_schedule({
            "name": "空记忆", "cron": "0 0 * * *", "task": "empty",
        })
        client = self._client()
        resp = client.get(f"/schedules/{new_id}/memories")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 0)


# ===========================================================================
# 测试类：DELETE /schedules/{id}/memories/{memory_id}
# ===========================================================================


class TestDeleteScheduleMemoryEndpoint(unittest.TestCase):
    """DELETE /schedules/{id}/memories/{memory_id} 端点测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")
        self.cs = CronScheduler(schedules_file=self.sched_file)
        self.sched_id_a = self.cs.add_schedule({
            "name": "调度A", "cron": "* * * * *", "task": "a",
        })
        self.sched_id_b = self.cs.add_schedule({
            "name": "调度B", "cron": "0 * * * *", "task": "b",
        })
        self.memories = [
            {
                "id": "cron-a-1",
                "content": "调度A的事实1",
                "metadata": {"namespace": "cron", "cron_id": self.sched_id_a},
            },
            {
                "id": "cron-b-1",
                "content": "调度B的事实1",
                "metadata": {"namespace": "cron", "cron_id": self.sched_id_b},
            },
        ]
        self.store = _MockChromaStore(self.memories)
        self.orch = _make_mock_orchestrator(self.store)
        self._patches = []
        app.dependency_overrides[get_cron_scheduler] = lambda: self.cs
        app.dependency_overrides[get_orchestrator] = lambda: self.orch

    def tearDown(self):
        app.dependency_overrides.pop(get_cron_scheduler, None)
        app.dependency_overrides.pop(get_orchestrator, None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_delete_own_memory_success(self):
        """删除自己调度项的记忆成功。"""
        client = self._client()
        resp = client.delete(f"/schedules/{self.sched_id_a}/memories/cron-a-1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted_id"], "cron-a-1")
        self.assertIn("cron-a-1", self.store.delete_calls)
        # 验证已从 store 移除
        remaining = self.store.get_all_memories(namespace="cron", cron_id=self.sched_id_a)
        self.assertEqual(len(remaining), 0)

    def test_delete_cross_schedule_memory_returns_404(self):
        """跨调度项删除返回 404（cron_id 不匹配）。"""
        client = self._client()
        # cron-b-1 属于调度B，尝试通过调度A删除应被拒绝
        resp = client.delete(f"/schedules/{self.sched_id_a}/memories/cron-b-1")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("cron-b-1", self.store.delete_calls)
        # cron-b-1 仍在 store 中
        remaining_b = self.store.get_all_memories(namespace="cron", cron_id=self.sched_id_b)
        self.assertEqual(len(remaining_b), 1)

    def test_delete_nonexistent_memory_returns_404(self):
        """删除不存在的 memory_id 返回 404。"""
        client = self._client()
        resp = client.delete(f"/schedules/{self.sched_id_a}/memories/nonexistent")
        self.assertEqual(resp.status_code, 404)

    def test_delete_schedule_not_found_returns_404(self):
        """调度项不存在返回 404。"""
        client = self._client()
        resp = client.delete("/schedules/nonexistent/memories/any-id")
        self.assertEqual(resp.status_code, 404)


# ===========================================================================
# 测试类：SessionLogger.get_recent_role_messages 单元测试
# ===========================================================================


class TestSessionLoggerGetRecentRoleMessages(unittest.TestCase):
    """SessionLogger.get_recent_role_messages 方法测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "sessions.db")
        self.logger = SessionLogger(self.db_path)

    def tearDown(self):
        self.logger.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_only_specified_role(self):
        """只返回指定角色的消息。"""
        self.logger.create_session("sess-1")
        self.logger.log_message("sess-1", "user", "u1")
        self.logger.log_message("sess-1", "assistant", "a1")
        self.logger.log_message("sess-1", "user", "u2")
        self.logger.log_message("sess-1", "assistant", "a2")
        rows = self.logger.get_recent_role_messages("sess-1", "assistant", limit=10)
        self.assertEqual(len(rows), 2)
        contents = [r["content"] for r in rows]
        self.assertIn("a1", contents)
        self.assertIn("a2", contents)
        for r in rows:
            self.assertEqual(r["role"], "assistant")

    def test_returns_newest_first(self):
        """按 id 倒序返回（最新在前）。"""
        self.logger.create_session("sess-1")
        self.logger.log_message("sess-1", "assistant", "first")
        self.logger.log_message("sess-1", "assistant", "second")
        self.logger.log_message("sess-1", "assistant", "third")
        rows = self.logger.get_recent_role_messages("sess-1", "assistant", limit=10)
        self.assertEqual(rows[0]["content"], "third")
        self.assertEqual(rows[1]["content"], "second")
        self.assertEqual(rows[2]["content"], "first")

    def test_limit_truncates(self):
        """limit 截断返回条数。"""
        self.logger.create_session("sess-1")
        for i in range(5):
            self.logger.log_message("sess-1", "assistant", f"reply-{i}")
        rows = self.logger.get_recent_role_messages("sess-1", "assistant", limit=2)
        self.assertEqual(len(rows), 2)
        # 最新两条
        self.assertEqual(rows[0]["content"], "reply-4")
        self.assertEqual(rows[1]["content"], "reply-3")

    def test_session_isolation(self):
        """不同 session_id 互不干扰。"""
        self.logger.create_session("sess-a")
        self.logger.create_session("sess-b")
        self.logger.log_message("sess-a", "assistant", "a-reply")
        self.logger.log_message("sess-b", "assistant", "b-reply")
        rows_a = self.logger.get_recent_role_messages("sess-a", "assistant", limit=10)
        rows_b = self.logger.get_recent_role_messages("sess-b", "assistant", limit=10)
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["content"], "a-reply")
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0]["content"], "b-reply")

    def test_empty_session_returns_empty_list(self):
        """无消息的 session 返回空列表。"""
        self.logger.create_session("empty-sess")
        rows = self.logger.get_recent_role_messages("empty-sess", "assistant", limit=10)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
