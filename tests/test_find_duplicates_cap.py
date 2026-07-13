"""P2-6 find_duplicates n_results 上限测试 — 验证 cap 不影响结果正确性。

运行方式:
    python -m unittest tests.test_find_duplicates_cap -v

测试目标:
- 小集合（<50 条）cap 不影响结果
- 相似度阈值过滤仍正确工作
- 与未 cap 的行为一致
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.storage.chroma_store import ChromaMemoryStore


class TestFindDuplicatesCapContract(unittest.TestCase):
    """验证 n_results=50 cap 不影响现有行为。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="find_dups_cap_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_find_duplicates_empty_store(self):
        """空库返回空列表。"""
        result = self.store.find_duplicates("test")
        self.assertEqual(result, [])

    def test_find_duplicates_with_match_small(self):
        """小集合下 cap 不影响结果。"""
        self.store.add_memory("python 编程")
        self.store.add_memory("python 数据分析")

        result = self.store.find_duplicates("python 开发", threshold=0.85)
        self.assertGreater(len(result), 0)
        for item in result:
            self.assertIn("id", item)
            self.assertIn("content", item)
            self.assertIn("metadata", item)
            self.assertIn("similarity", item)

    def test_find_duplicates_no_match(self):
        """不相似内容不被匹配。"""
        self.store.add_memory("java 编程")
        result = self.store.find_duplicates("python 开发", threshold=0.85)
        # mock 下 java → [0,1], python → [1,0], 相似度=0 < 0.85
        self.assertEqual(len(result), 0)

    def test_find_duplicates_multiple_matches_ordered(self):
        """多条匹配按相似度降序排列。"""
        self.store.add_memory("python 编程")
        self.store.add_memory("python 数据分析")
        self.store.add_memory("java 编程")

        result = self.store.find_duplicates("python 开发", threshold=0.5)
        self.assertGreater(len(result), 0)
        similarities = [item["similarity"] for item in result]
        self.assertEqual(similarities, sorted(similarities, reverse=True))

    def test_find_duplicates_exact_match(self):
        """完全相同内容应匹配（similarity=1.0）。"""
        self.store.add_memory("完全相同的文本")
        result = self.store.find_duplicates("完全相同的文本", threshold=0.99)
        self.assertGreater(len(result), 0)
        self.assertAlmostEqual(result[0]["similarity"], 1.0, places=5)

    def test_find_duplicates_threshold_boundary(self):
        """threshold=0.0 时任何匹配都算重复。"""
        self.store.add_memory("some content")
        result = self.store.find_duplicates("other content", threshold=0.0)
        self.assertGreater(len(result), 0)

    def test_find_duplicates_threshold_one(self):
        """threshold=1.0 时仅完全相同算重复。"""
        self.store.add_memory("unique content")
        # 不同的文本 similarity < 1.0
        result = self.store.find_duplicates("different content", threshold=1.0)
        self.assertEqual(len(result), 0)

    def test_find_duplicates_with_namespace_filter(self):
        """命名空间过滤不受 cap 影响。"""
        self.store.add_memory("python 编程", namespace="user")
        self.store.add_memory("python 数据分析", namespace="cron")

        result_user = self.store.find_duplicates("python 开发", threshold=0.5,
                                                  namespace="user")
        result_cron = self.store.find_duplicates("python 开发", threshold=0.5,
                                                  namespace="cron")

        self.assertGreater(len(result_user), 0)
        self.assertGreater(len(result_cron), 0)


class TestFindDuplicatesCapLargeSet(unittest.TestCase):
    """模拟较大集合下 cap 行为。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="find_dups_large_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

        # 插入 60 条记忆（超过默认 cap 50）
        topics = ["python", "java", "rust", "go", "js"] * 12
        for i, topic in enumerate(topics):
            self.store.add_memory(f"{topic} 编程话题 {i}",
                                  metadata={"index": i})

    def test_find_duplicates_still_finds_matches(self):
        """大集合下 find_duplicates 仍能找到匹配。"""
        result = self.store.find_duplicates("python 开发", threshold=0.5)
        self.assertGreater(len(result), 0)

    def test_find_duplicates_result_count_capped(self):
        """返回结果数不超过 cap（50）。"""
        result = self.store.find_duplicates("a", threshold=0.0)
        self.assertLessEqual(len(result), 50,
                             "find_duplicates 返回结果不应超过 50")

    def test_find_duplicates_no_false_positives(self):
        """高阈值下不相关内容不被匹配。"""
        # 使用 threshold=1.0（仅完全相同才匹配）
        result = self.store.find_duplicates("completely unrelated text",
                                            threshold=1.0)
        self.assertEqual(len(result), 0)


class TestFindDuplicatesCapEdgeCases(unittest.TestCase):
    """边界情况。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="find_dups_edge_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_single_item_store(self):
        """单条记录的 store。"""
        self.store.add_memory("single item")
        result = self.store.find_duplicates("single item", threshold=0.9)
        self.assertGreater(len(result), 0)

    def test_exactly_50_items(self):
        """正好 50 条记录。"""
        for i in range(50):
            self.store.add_memory(f"memory {i}")
        result = self.store.find_duplicates("memory", threshold=0.0)
        self.assertLessEqual(len(result), 50)

    def test_find_duplicates_empty_threshold(self):
        """threshold=0 的默认值。"""
        self.store.add_memory("test content")
        # 使用默认 threshold
        result = self.store.find_duplicates("test content")
        self.assertGreater(len(result), 0)


if __name__ == "__main__":
    unittest.main()
