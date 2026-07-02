"""记忆 Dashboard API 端点单元测试（Phase 7 Task 4）。

验证 ``src/server.py`` 新增的 4 个端点：
- ``GET /memories``：关键词向量检索 + 可选 type 过滤
- ``GET /memories/all``：列出全部记忆 + 可选 type 过滤 + limit 截断
- ``DELETE /memories/{memory_id}``：手动删除单条记忆（不存在返回 404）
- ``GET /profile``：返回 memory.md 全文 + 文件 mtime

setup 要点（参考 ``tests/test_task_api.py`` / ``tests/test_config_update.py``）：
- 使用 ``tests/_mock_deps.py`` 注入 chromadb / numpy 等 mock，避免依赖真实模型权重。
- 用 ``unittest.mock.patch`` 替换 ``src.server.orchestrator`` 全局变量为
  自定义 mock，其 ``chroma_store`` / ``memory_md_manager`` 属性也是 mock，
  避免触发真实 LLM 调用与 lifespan 依赖。
- ``GET /profile`` 测试使用临时文件 + 真实 ``MemoryMdManager``，端到端验证
  文件读取与 mtime 解析逻辑。

运行方式:
    python -m pytest tests/test_memory_dashboard_api.py -v
    python -m unittest tests.test_memory_dashboard_api -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.server import app  # noqa: E402
from src.memory.memory_md import MemoryMdManager  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Mock 工具：构造可控的 chroma_store / orchestrator
# ---------------------------------------------------------------------------


class _MockChromaStore:
    """可控的 chroma_store mock，记录调用并按预设数据返回。

    与真实 ``ChromaMemoryStore`` 接口一致：
    - ``query_memory(query, top_k=5, reinforce=True)`` -> list of dict
    - ``get_all_memories()`` -> list of dict
    - ``delete_memory(memory_id)`` -> None

    通过 ``self._store`` 维护内存数据，使 DELETE 端点的「先查存在性再删除」
    逻辑可以端到端跑通。
    """

    def __init__(self, memories: List[Dict[str, Any]] | None = None) -> None:
        # memories: [{id, content, metadata, similarity?}]
        self._store: Dict[str, Dict[str, Any]] = {}
        for m in memories or []:
            self._store[m["id"]] = {
                "id": m["id"],
                "content": m.get("content", ""),
                "metadata": m.get("metadata", {}),
                "similarity": m.get("similarity", 0.0),
            }
        # 记录调用历史，便于断言
        self.query_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[str] = []

    def query_memory(
        self, query: str, top_k: int = 5, reinforce: bool = True
    ) -> List[Dict[str, Any]]:
        self.query_calls.append({
            "query": query, "top_k": top_k, "reinforce": reinforce,
        })
        # 简单按 top_k 截断返回所有记忆（不做真实向量检索）
        items = list(self._store.values())
        return items[:top_k]

    def get_all_memories(self) -> List[Dict[str, Any]]:
        return list(self._store.values())

    def delete_memory(self, memory_id: str) -> None:
        self.delete_calls.append(memory_id)
        # 模拟真实 chromadb：删除不存在的 id 静默成功
        self._store.pop(memory_id, None)


def _make_mock_orchestrator(
    chroma_store: _MockChromaStore | None = None,
    memory_md_manager: Any = None,
) -> MagicMock:
    """构造 mock orchestrator，暴露 chroma_store / memory_md_manager 属性。

    其他属性（llm_client / consolidation_engine 等）保持 MagicMock 默认行为，
    不影响 Dashboard 端点逻辑。
    """
    mock = MagicMock()
    mock.chroma_store = chroma_store
    mock.memory_md_manager = memory_md_manager
    return mock


# ===========================================================================
# 测试类：GET /memories 搜索
# ===========================================================================


class TestSearchMemories(unittest.TestCase):
    """GET /memories 端点测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # 预置 3 条记忆：2 条 fact + 1 条 conversation_turn
        self.memories = [
            {
                "id": "mem-001",
                "content": "用户主力语言是 Python",
                "metadata": {
                    "type": "fact",
                    "importance": 0.8,
                    "last_accessed": "2026-06-20T10:00:00",
                },
                "similarity": 0.92,
            },
            {
                "id": "mem-002",
                "content": "用户使用 Java 后端",
                "metadata": {
                    "type": "fact",
                    "importance": 0.6,
                    "last_accessed": "2026-06-21T11:00:00",
                },
                "similarity": 0.75,
            },
            {
                "id": "mem-003",
                "content": "用户问过 Python 性能问题",
                "metadata": {
                    "type": "conversation_turn",
                    "importance": 0.3,
                    "last_accessed": "2026-06-22T12:00:00",
                },
                "similarity": 0.65,
            },
        ]
        self.store = _MockChromaStore(self.memories)
        self.orch = _make_mock_orchestrator(chroma_store=self.store)
        self._patches = [patch("src.server.orchestrator", self.orch)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_search_returns_correct_format(self):
        """GET /memories 搜索返回 {memories: [...], total: N} 格式。"""
        client = self._client()
        resp = client.get("/memories", params={"q": "Python", "top_k": 20})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("memories", data)
        self.assertIn("total", data)
        self.assertEqual(data["total"], len(data["memories"]))
        # 每条记忆含必填字段
        for m in data["memories"]:
            self.assertIn("id", m)
            self.assertIn("content", m)
            self.assertIn("similarity", m)
            self.assertIn("metadata", m)

    def test_search_passes_reinforce_false(self):
        """GET /memories 调 query_memory 时显式传 reinforce=False（浏览不强化）。"""
        client = self._client()
        client.get("/memories", params={"q": "Python"})
        self.assertEqual(len(self.store.query_calls), 1)
        call = self.store.query_calls[0]
        self.assertEqual(call["query"], "Python")
        # 浏览场景必须 reinforce=False，避免浏览也触发强化
        self.assertFalse(call["reinforce"])

    def test_search_filter_by_type(self):
        """GET /memories?type=fact 只返回 metadata.type=fact 的记忆。"""
        client = self._client()
        resp = client.get(
            "/memories", params={"q": "Python", "type": "fact"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # 全部预置记忆含 Python 关键词，但只有 2 条 type=fact
        self.assertEqual(data["total"], 2)
        for m in data["memories"]:
            self.assertEqual(m["metadata"]["type"], "fact")

    def test_search_empty_keyword_returns_empty(self):
        """GET /memories?q=（空关键词）返回空列表（不触发检索）。"""
        client = self._client()
        resp = client.get("/memories", params={"q": ""})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["memories"], [])
        # 不应调用 chroma_store.query_memory
        self.assertEqual(len(self.store.query_calls), 0)

    def test_search_chroma_store_not_initialized_returns_503(self):
        """chroma_store 为 None 时返回 503。"""
        self.orch.chroma_store = None
        client = self._client()
        resp = client.get("/memories", params={"q": "Python"})
        self.assertEqual(resp.status_code, 503)


# ===========================================================================
# 测试类：GET /memories/all 列出全部
# ===========================================================================


class TestListAllMemories(unittest.TestCase):
    """GET /memories/all 端点测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # 预置 5 条记忆：3 条 fact + 2 条 conversation_turn
        self.memories = [
            {"id": f"m-{i}", "content": f"事实 {i}", "metadata": {"type": "fact"}}
            for i in range(3)
        ] + [
            {"id": f"c-{i}", "content": f"对话 {i}",
             "metadata": {"type": "conversation_turn"}}
            for i in range(2)
        ]
        self.store = _MockChromaStore(self.memories)
        self.orch = _make_mock_orchestrator(chroma_store=self.store)
        self._patches = [patch("src.server.orchestrator", self.orch)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_list_all_returns_all_memories(self):
        """GET /memories/all 不带过滤参数返回全部记忆。"""
        client = self._client()
        resp = client.get("/memories/all")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 5)
        # 不应包含 similarity 字段（/memories/all 不做向量检索）
        for m in data["memories"]:
            self.assertIn("id", m)
            self.assertIn("content", m)
            self.assertIn("metadata", m)

    def test_list_all_filter_by_type(self):
        """GET /memories/all?type=fact 只返回 fact 类型。"""
        client = self._client()
        resp = client.get("/memories/all", params={"type": "fact"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 3)
        for m in data["memories"]:
            self.assertEqual(m["metadata"]["type"], "fact")

    def test_list_all_with_limit(self):
        """GET /memories/all?limit=2 截断到 2 条。"""
        client = self._client()
        resp = client.get("/memories/all", params={"limit": 2})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["memories"]), 2)

    def test_list_all_filter_and_limit_combined(self):
        """GET /memories/all?type=conversation_turn&limit=1 先过滤再截断。"""
        client = self._client()
        resp = client.get(
            "/memories/all", params={"type": "conversation_turn", "limit": 1}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # 2 条 conversation_turn，limit=1 后剩 1 条
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["memories"][0]["metadata"]["type"], "conversation_turn")

    def test_list_all_empty_store_returns_empty(self):
        """空库时 GET /memories/all 返回空列表。"""
        self.orch.chroma_store = _MockChromaStore([])
        client = self._client()
        resp = client.get("/memories/all")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["memories"], [])


# ===========================================================================
# 测试类：DELETE /memories/{memory_id}
# ===========================================================================


class TestDeleteMemory(unittest.TestCase):
    """DELETE /memories/{memory_id} 端点测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.memories = [
            {"id": "exist-001", "content": "存在的记忆",
             "metadata": {"type": "fact"}},
        ]
        self.store = _MockChromaStore(self.memories)
        self.orch = _make_mock_orchestrator(chroma_store=self.store)
        self._patches = [patch("src.server.orchestrator", self.orch)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_delete_existing_memory_succeeds(self):
        """DELETE /memories/exist-001 成功删除，返回 status=ok。"""
        client = self._client()
        resp = client.delete("/memories/exist-001")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["deleted_id"], "exist-001")
        # 验证 chroma_store.delete_memory 被调用
        self.assertEqual(self.store.delete_calls, ["exist-001"])
        # 验证记忆已从 store 中移除
        self.assertNotIn("exist-001", self.store._store)

    def test_delete_nonexistent_returns_404(self):
        """DELETE /memories/nonexistent 返回 404。"""
        client = self._client()
        resp = client.delete("/memories/nonexistent-id")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("不存在", resp.json()["detail"])
        # 不应调用 delete_memory
        self.assertEqual(self.store.delete_calls, [])

    def test_delete_chroma_store_not_initialized_returns_503(self):
        """chroma_store 为 None 时返回 503。"""
        self.orch.chroma_store = None
        client = self._client()
        resp = client.delete("/memories/any-id")
        self.assertEqual(resp.status_code, 503)


# ===========================================================================
# 测试类：GET /profile 用户画像
# ===========================================================================


class TestGetProfile(unittest.TestCase):
    """GET /profile 端点测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.memory_md_path = os.path.join(self.tmpdir, "memory.md")
        # 使用真实 MemoryMdManager，端到端验证文件读取 + mtime 解析
        self.manager = MemoryMdManager(file_path=self.memory_md_path)
        self.orch = _make_mock_orchestrator(memory_md_manager=self.manager)
        self._patches = [patch("src.server.orchestrator", self.orch)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_profile_returns_memory_md_full_content(self):
        """GET /profile 返回 memory.md 全文 + 非空 updated_at。"""
        # 写入 memory.md
        content = "# 用户画像\n\n## 基本信息\n- 用户名: test\n- 城市: 北京\n"
        with open(self.memory_md_path, "w", encoding="utf-8") as f:
            f.write(content)
        client = self._client()
        resp = client.get("/profile")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["content"], content)
        # updated_at 为非空字符串（ISO 时间戳）
        self.assertTrue(data["updated_at"])

    def test_profile_file_not_exist_returns_empty(self):
        """memory.md 不存在时返回空 content + 空 updated_at。"""
        # 不创建 memory.md 文件
        self.assertFalse(os.path.exists(self.memory_md_path))
        client = self._client()
        resp = client.get("/profile")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["content"], "")
        self.assertEqual(data["updated_at"], "")

    def test_profile_preserves_multiline_format(self):
        """GET /profile 返回的内容保留换行格式（前端 white-space: pre-wrap 渲染）。"""
        content = "## 技术栈\n- Python\n- Java\n- Rust\n\n## 工作习惯\n- 偏好函数式\n"
        with open(self.memory_md_path, "w", encoding="utf-8") as f:
            f.write(content)
        client = self._client()
        resp = client.get("/profile")
        data = resp.json()
        # 多行内容应被原样保留（含 \n）
        self.assertIn("\n", data["content"])
        self.assertEqual(data["content"].count("\n"), content.count("\n"))

    def test_profile_manager_none_returns_empty(self):
        """memory_md_manager 为 None 时返回空 content + 空 updated_at。"""
        self.orch.memory_md_manager = None
        client = self._client()
        resp = client.get("/profile")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["content"], "")
        self.assertEqual(data["updated_at"], "")


# ===========================================================================
# 测试类：orchestrator 未初始化
# ===========================================================================


class TestOrchestratorNotInitialized(unittest.TestCase):
    """orchestrator 为 None 时所有 Dashboard 端点返回 503。"""

    def setUp(self):
        self._patches = [patch("src.server.orchestrator", None)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass

    def _client(self) -> TestClient:
        return TestClient(app)

    def test_memories_returns_503_when_orchestrator_none(self):
        client = self._client()
        resp = client.get("/memories", params={"q": "x"})
        self.assertEqual(resp.status_code, 503)

    def test_memories_all_returns_503_when_orchestrator_none(self):
        client = self._client()
        resp = client.get("/memories/all")
        self.assertEqual(resp.status_code, 503)

    def test_delete_returns_503_when_orchestrator_none(self):
        client = self._client()
        resp = client.delete("/memories/any-id")
        self.assertEqual(resp.status_code, 503)

    def test_profile_returns_503_when_orchestrator_none(self):
        client = self._client()
        resp = client.get("/profile")
        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main(verbosity=2)
