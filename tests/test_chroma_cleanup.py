"""chroma_store TTL 清理测试（TDD）。

验证 ChromaMemoryStore.delete_old_entries(days) 能删除超过指定天数的记忆条目。
由于 chromadb metadata 的字符串时间戳不支持 $lt 比较，采用 Python 侧全量扫描方案。

运行方式:
    python -m pytest tests/test_chroma_cleanup.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

# 使用 mock chroma 避免下载 ONNX 模型
import tests._mock_deps  # noqa: E402,F401  # 触发 mock 安装

from teage_liu.storage.chroma_store import ChromaMemoryStore  # noqa: E402


class TestChromaCleanup(unittest.TestCase):
    """验证 delete_old_entries 删除过期记忆。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="hermes_chroma_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_delete_old_entries_removes_expired(self):
        """超过指定天数的条目应被删除。"""
        store = ChromaMemoryStore(persist_path=self._tmp_dir)

        # 插入 5 条"旧"记忆（timestamp 为 100 天前）
        old_ts = (datetime.now() - timedelta(days=100)).isoformat()
        for i in range(5):
            store.add_memory(
                content=f"old memory {i}",
                metadata={"timestamp": old_ts},
            )

        # 插入 5 条"新"记忆（当前时间）
        for i in range(5):
            store.add_memory(
                content=f"new memory {i}",
                metadata={"timestamp": datetime.now().isoformat()},
            )

        # 调用清理（90 天阈值）
        deleted = store.delete_old_entries(days=90)

        # 应删除 5 条旧记忆
        self.assertEqual(deleted, 5, f"应删除 5 条旧记忆，实际 {deleted}")
        # 剩余 5 条新记忆
        remaining = store.get_all_memories()
        self.assertEqual(len(remaining), 5, f"应剩余 5 条，实际 {len(remaining)}")
        # 验证剩余的都是新记忆
        for m in remaining:
            self.assertIn("new memory", m["content"])

    def test_delete_old_entries_keeps_recent(self):
        """未过期的条目不应被删除。"""
        store = ChromaMemoryStore(persist_path=self._tmp_dir)

        # 插入 3 条新记忆（1 天前）
        recent_ts = (datetime.now() - timedelta(days=1)).isoformat()
        for i in range(3):
            store.add_memory(
                content=f"recent {i}",
                metadata={"timestamp": recent_ts},
            )

        # 调用清理（90 天阈值）
        deleted = store.delete_old_entries(days=90)
        self.assertEqual(deleted, 0)
        # 全部保留
        self.assertEqual(len(store.get_all_memories()), 3)

    def test_delete_old_entries_zero_days_deletes_all(self):
        """days=0 删除所有有条目（立即过期）。"""
        store = ChromaMemoryStore(persist_path=self._tmp_dir)

        for i in range(3):
            store.add_memory(
                content=f"memory {i}",
                metadata={"timestamp": datetime.now().isoformat()},
            )

        # days=-1：cutoff 在未来，所有条目都过期
        # （days=0 时 cutoff 与 timestamp 几乎相等，边界不稳定）
        deleted = store.delete_old_entries(days=-1)
        # 应删除全部 3 条
        self.assertEqual(deleted, 3, f"应删除 3 条，实际 {deleted}")

    def test_delete_old_entries_empty_collection(self):
        """空集合调用清理返回 0，不报错。"""
        store = ChromaMemoryStore(persist_path=self._tmp_dir)
        deleted = store.delete_old_entries(days=90)
        self.assertEqual(deleted, 0)

    def test_delete_old_entries_missing_timestamp_field(self):
        """缺失 timestamp 字段的旧数据视为可清理（保守策略）。"""
        store = ChromaMemoryStore(persist_path=self._tmp_dir)

        # 直接通过 collection.add 写入无 timestamp 的条目
        store.collection.add(
            ids=["no_ts_1"],
            documents=["no timestamp memory"],
            metadatas=[{"type": "fact"}],
            embeddings=[[0.1] * 384],  # mock 接受任意向量
        )

        # 90 天阈值：缺失 timestamp 视为远古旧数据
        deleted = store.delete_old_entries(days=90)
        self.assertEqual(deleted, 1, "缺失 timestamp 的条目应被清理")


if __name__ == "__main__":
    unittest.main()
