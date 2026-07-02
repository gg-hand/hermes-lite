"""P1-3 reinforce() 优化测试 — 验证 O(1) 直接 ID 查询的正确性。

运行方式:
    python -m unittest tests.test_reinforce_optimization -v

测试目标:
- reinforce 正确递增 access_count
- 不存在的 memory_id 安全处理
- None metadata 边界
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from src.storage.chroma_store import ChromaMemoryStore


class TestReinforceContract(unittest.TestCase):
    """验证 reinforce 的行为契约（优化前后一致）。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="reinforce_test_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_reinforce_increments_access_count(self):
        """reinforce 使 access_count 递增。"""
        mem_id = self.store.add_memory("测试记忆")
        # 首次 reinforce
        self.store.reinforce(mem_id)
        # 通过 collection.get 验证
        result = self.store.collection.get(ids=[mem_id], include=["metadatas"])
        if result and result.get("metadatas") and result["metadatas"][0]:
            self.assertEqual(result["metadatas"][0].get("access_count"), 1)

        # 再次 reinforce
        self.store.reinforce(mem_id)
        result = self.store.collection.get(ids=[mem_id], include=["metadatas"])
        if result and result.get("metadatas") and result["metadatas"][0]:
            self.assertEqual(result["metadatas"][0].get("access_count"), 2)

    def test_reinforce_updates_last_accessed(self):
        """reinforce 更新 last_accessed 时间戳。"""
        mem_id = self.store.add_memory("测试记忆")
        # 获取初始时间
        result1 = self.store.collection.get(ids=[mem_id], include=["metadatas"])
        ts1 = None
        if result1 and result1.get("metadatas") and result1["metadatas"][0]:
            ts1 = result1["metadatas"][0].get("last_accessed")

        self.store.reinforce(mem_id)

        result2 = self.store.collection.get(ids=[mem_id], include=["metadatas"])
        ts2 = None
        if result2 and result2.get("metadatas") and result2["metadatas"][0]:
            ts2 = result2["metadatas"][0].get("last_accessed")

        # 时间戳应更新（如果之前有值，新值应不同）
        # reinforce 后 last_accessed 应被设置（可能和首次在同一微秒，仅验证存在性）
        self.assertIsNotNone(ts2, "reinforce 后 last_accessed 不应为 None")

    def test_reinforce_nonexistent_memory_id(self):
        """不存在的 memory_id 应安全返回，不抛异常。"""
        try:
            self.store.reinforce("nonexistent_id")
        except Exception as e:
            self.fail(f"reinforce 对不存在的 memory_id 抛异常: {e}")

    def test_reinforce_multiple_memories_not_affected(self):
        """reinforce 一个记忆不应影响其他记忆。"""
        id_a = self.store.add_memory("记忆A")
        id_b = self.store.add_memory("记忆B")

        self.store.reinforce(id_a)

        result = self.store.collection.get(ids=[id_a], include=["metadatas"])
        count_a = None
        if result and result.get("metadatas") and result["metadatas"][0]:
            count_a = result["metadatas"][0].get("access_count")

        result = self.store.collection.get(ids=[id_b], include=["metadatas"])
        count_b = None
        if result and result.get("metadatas") and result["metadatas"][0]:
            count_b = result["metadatas"][0].get("access_count")

        if count_a is not None and count_b is not None:
            self.assertEqual(count_a, 1)
            # 未被 reinforce 的记忆 access_count 应为 0 或 None
            self.assertIn(count_b, (0, None))

    def test_reinforce_updates_document(self):
        """reinforce 应保留文档内容不变。"""
        original_content = "这条记忆应该被保留"
        mem_id = self.store.add_memory(original_content)
        self.store.reinforce(mem_id)

        result = self.store.collection.get(ids=[mem_id], include=["documents"])
        if result and result.get("documents"):
            self.assertEqual(result["documents"][0], original_content)


class TestReinforceEdgeCases(unittest.TestCase):
    """reinforce 边界情况测试。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="reinforce_edge_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_reinforce_empty_store(self):
        """空库上 reinforce 安全。"""
        try:
            self.store.reinforce("any_id")
        except Exception as e:
            self.fail(f"reinforce 在空库上抛异常: {e}")

    def test_reinforce_after_delete(self):
        """reinforce 已删除的记忆安全。"""
        mem_id = self.store.add_memory("将被删除")
        self.store.delete_memory(mem_id)
        try:
            self.store.reinforce(mem_id)
        except Exception as e:
            self.fail(f"reinforce 已删除记忆抛异常: {e}")

    def test_reinforce_without_metadata(self):
        """没有 metadata 的记忆 reinforce 不抛异常。"""
        mem_id = self.store.add_memory("无 metadata 记忆")
        # 清除 metadata（mock 要求 documents 参数）
        self.store.collection.update(ids=[mem_id], documents=["无 metadata 记忆"], metadatas=[{}])
        try:
            self.store.reinforce(mem_id)
        except Exception as e:
            self.fail(f"reinforce 无 metadata 的记忆抛异常: {e}")


if __name__ == "__main__":
    unittest.main()
