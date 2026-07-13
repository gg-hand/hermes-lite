"""ChromaMemoryStore 单元测试 — 验证 find_duplicates 的 ANN 查询行为。

运行方式：
    python -m unittest tests.test_chroma_store -v
    python tests/test_chroma_store.py

mock 策略：
- 使用 tests/_mock_deps.py 的 mock chromadb（强制注入，不依赖真实 chromadb）
- mock embedding 规则（2 维向量）：
    文本含 "python" → [1.0, 0.0]
    文本含 "java"   → [0.0, 1.0]
    其他            → [0.5, 0.5]
- mock collection.query 按 cosine 距离真实计算（与真实 ChromaDB cosine space 一致）：
    python vs python → distance 0.0  → similarity 1.0
    python vs java   → distance 1.0  → similarity 0.0
    python vs 其他   → distance ≈0.293 → similarity ≈0.707
  据此设计 threshold 边界：
    threshold=0.85 → python vs python（sim 1.0）重复；python vs java（sim 0.0）不重复
    threshold=0.0  → 任意非零相似度都算重复
    threshold=1.0  → 仅 distance=0（完全相同 embedding）才算重复
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.storage.chroma_store import ChromaMemoryStore  # noqa: E402


class TestFindDuplicates(unittest.TestCase):
    """验证 find_duplicates 在不同场景下的行为。"""

    def setUp(self):
        """每个测试用例使用独立的 ChromaMemoryStore 实例。

        mock chromadb 的 _MockChromaClient 每次调用 PersistentClient 都返回
        新实例，因此每个 ChromaMemoryStore 都是空库，测试间互不影响。
        persist_path 使用临时目录（mock 不实际落盘，但构造函数会 os.makedirs）。
        """
        self._tmpdir = tempfile.mkdtemp(prefix="chroma_test_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_find_duplicates_empty_store(self):
        """空库时 find_duplicates 返回空列表。"""
        result = self.store.find_duplicates("任意内容")
        self.assertEqual(result, [])

    def test_find_duplicates_with_match(self):
        """有重复时返回降序列表，每项含 id/content/metadata/similarity。"""
        self.store.add_memory("python 编程")
        self.store.add_memory("python 数据分析")
        # mock：两段都含 python → embedding [1,0]，与查询 [1,0] 的 similarity=1.0 > 0.85
        result = self.store.find_duplicates("python 开发", threshold=0.85)

        self.assertGreater(len(result), 0)
        for item in result:
            self.assertIn("id", item)
            self.assertIn("content", item)
            self.assertIn("metadata", item)
            self.assertIn("similarity", item)
        # 两条已添加记忆都应被匹配
        self.assertEqual(len(result), 2)
        # 验证降序
        similarities = [item["similarity"] for item in result]
        self.assertEqual(similarities, sorted(similarities, reverse=True))

    def test_find_duplicates_no_match(self):
        """不相似内容不应被检测到（python vs java，similarity 0.0 < 0.85）。"""
        self.store.add_memory("java 编程")
        # mock：java=[0,1], python=[1,0] → similarity 0.0 < 0.85 → 不重复
        result = self.store.find_duplicates("python 开发", threshold=0.85)
        self.assertEqual(result, [])

    def test_find_duplicates_threshold_zero(self):
        """threshold=0.0 时任意非零相似度都算重复。

        python=[1,0] vs 其他=[0.5,0.5] → similarity ≈0.707 > 0.0 → 重复。
        """
        self.store.add_memory("python 编程")
        result = self.store.find_duplicates("任意内容", threshold=0.0)
        self.assertGreaterEqual(len(result), 1)

    def test_find_duplicates_threshold_one(self):
        """threshold=1.0 时仅 distance=0（完全相同 embedding）才算重复。

        python=[1,0] vs 其他=[0.5,0.5] → similarity ≈0.707 < 1.0 → 不重复。
        """
        self.store.add_memory("python 编程")
        result = self.store.find_duplicates("任意内容", threshold=1.0)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
