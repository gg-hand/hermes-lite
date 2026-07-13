"""ChromaMemoryStore namespace + cron_id 隔离层测试（Phase 8 Task 1.1）。

验证：
- ``add_memory`` 写入时注入 ``metadata.namespace`` 与 ``metadata.cron_id``
- ``query_memory`` 按 namespace + cron_id 过滤召回
- ``find_duplicates`` 按 namespace + cron_id 过滤去重
- ``get_all_memories`` 支持 namespace + cron_id 过滤
- 旧数据兼容：缺失 namespace 字段视为 user
- user/cron 写入互不干扰、cron_id 间隔离

mock 策略：
- 使用 tests/_mock_deps.py 的 mock chromadb（强制注入，不依赖真实 chromadb）
- mock embedding 规则（2 维向量）：文本含 "python" → [1,0]；含 "java" → [0,1]；其他 → [0.5,0.5]

运行方式:
    python -m unittest tests.test_chroma_namespace -v
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


class TestNamespaceWrite(unittest.TestCase):
    """验证 add_memory 注入 namespace + cron_id 到 metadata。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="chroma_ns_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_user_default_namespace(self):
        """默认 namespace=user，metadata.namespace='user'。"""
        mid = self.store.add_memory("user content")
        all_mems = self.store.get_all_memories()
        self.assertEqual(len(all_mems), 1)
        self.assertEqual(all_mems[0]["metadata"]["namespace"], "user")
        # user 命名空间不强制写 cron_id 字段（默认空串兼容旧查询）
        self.assertEqual(all_mems[0]["metadata"].get("cron_id", ""), "")

    def test_cron_namespace_injects_cron_id(self):
        """namespace=cron 时 metadata.cron_id 写入指定值。"""
        mid = self.store.add_memory(
            "cron content", namespace="cron", cron_id="sched_A"
        )
        all_mems = self.store.get_all_memories()
        self.assertEqual(len(all_mems), 1)
        self.assertEqual(all_mems[0]["metadata"]["namespace"], "cron")
        self.assertEqual(all_mems[0]["metadata"]["cron_id"], "sched_A")

    def test_user_explicit_namespace(self):
        """显式 namespace=user 等同默认。"""
        self.store.add_memory("explicit user", namespace="user")
        all_mems = self.store.get_all_memories()
        self.assertEqual(all_mems[0]["metadata"]["namespace"], "user")


class TestNamespaceQueryIsolation(unittest.TestCase):
    """验证 query_memory 按 namespace + cron_id 过滤。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="chroma_q_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        # 准备跨命名空间数据
        # python 关键词使 embedding 一致，确保相似度匹配，专注验证命名空间过滤
        self.store.add_memory(
            "python user fact", namespace="user"
        )
        self.store.add_memory(
            "python cron A fact", namespace="cron", cron_id="sched_A"
        )
        self.store.add_memory(
            "python cron B fact", namespace="cron", cron_id="sched_B"
        )

    def test_user_query_excludes_cron(self):
        """user 命名空间查询不返回 cron 条目。"""
        results = self.store.query_memory(
            "python", top_k=10, namespace="user", reinforce=False
        )
        namespaces = {r["metadata"].get("namespace") for r in results}
        self.assertEqual(namespaces, {"user"})
        self.assertEqual(len(results), 1)

    def test_cron_query_isolates_by_cron_id(self):
        """cron 命名空间查询只返回匹配 cron_id 的条目。"""
        # 查 sched_A
        results_a = self.store.query_memory(
            "python", top_k=10, namespace="cron", cron_id="sched_A",
            reinforce=False,
        )
        namespaces_a = {r["metadata"].get("namespace") for r in results_a}
        cron_ids_a = {r["metadata"].get("cron_id") for r in results_a}
        self.assertEqual(namespaces_a, {"cron"})
        self.assertEqual(cron_ids_a, {"sched_A"})
        self.assertEqual(len(results_a), 1)

        # 查 sched_B
        results_b = self.store.query_memory(
            "python", top_k=10, namespace="cron", cron_id="sched_B",
            reinforce=False,
        )
        cron_ids_b = {r["metadata"].get("cron_id") for r in results_b}
        self.assertEqual(cron_ids_b, {"sched_B"})

    def test_cron_query_no_match_returns_empty(self):
        """cron 命名空间查询不存在的 cron_id 返回空列表。"""
        results = self.store.query_memory(
            "python", top_k=10, namespace="cron", cron_id="nonexistent",
            reinforce=False,
        )
        self.assertEqual(results, [])

    def test_namespace_none_returns_all(self):
        """namespace=None 跨命名空间检索（管理员视图）。"""
        results = self.store.query_memory(
            "python", top_k=10, namespace=None, reinforce=False
        )
        # 至少命中 3 条（user + cron A + cron B）
        self.assertGreaterEqual(len(results), 3)
        namespaces = {r["metadata"].get("namespace") for r in results}
        self.assertEqual(namespaces, {"user", "cron"})


class TestFindDuplicatesNamespace(unittest.TestCase):
    """验证 find_duplicates 按 namespace + cron_id 过滤。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="chroma_dup_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        # user 命名空间已有 python 条目
        self.store.add_memory("python user fact", namespace="user")
        # cron A 命名空间已有 python 条目
        self.store.add_memory(
            "python cron A fact", namespace="cron", cron_id="sched_A"
        )
        # cron B 命名空间已有 python 条目
        self.store.add_memory(
            "python cron B fact", namespace="cron", cron_id="sched_B"
        )

    def test_user_find_duplicates_excludes_cron(self):
        """user 命名空间去重不返回 cron 条目。"""
        dups = self.store.find_duplicates(
            "python new", threshold=0.85, namespace="user"
        )
        namespaces = {d["metadata"].get("namespace") for d in dups}
        self.assertEqual(namespaces, {"user"})

    def test_cron_find_duplicates_scoped_to_cron_id(self):
        """cron 命名空间去重只命中同 cron_id 的条目。"""
        dups_a = self.store.find_duplicates(
            "python new", threshold=0.85, namespace="cron", cron_id="sched_A"
        )
        for d in dups_a:
            self.assertEqual(d["metadata"].get("namespace"), "cron")
            self.assertEqual(d["metadata"].get("cron_id"), "sched_A")

        dups_b = self.store.find_duplicates(
            "python new", threshold=0.85, namespace="cron", cron_id="sched_B"
        )
        for d in dups_b:
            self.assertEqual(d["metadata"].get("namespace"), "cron")
            self.assertEqual(d["metadata"].get("cron_id"), "sched_B")

    def test_cron_find_duplicates_no_cross_isolation(self):
        """不同 cron_id 之间互不干扰（隔离验证）。"""
        # 写入 cron A 的新内容，不应在 cron B 中检测到重复
        dups = self.store.find_duplicates(
            "python cron A fact", threshold=0.85,
            namespace="cron", cron_id="sched_B",
        )
        # cron B 中只有 "python cron B fact"，与 "python cron A fact"
        # 都含 python 关键词（embedding [1,0]），相似度 1.0
        # 此处验证即使 embedding 相同，因 cron_id 不匹配也不会被命中
        for d in dups:
            self.assertEqual(d["metadata"].get("cron_id"), "sched_B")


class TestGetAllMemoriesNamespace(unittest.TestCase):
    """验证 get_all_memories 支持 namespace + cron_id 过滤。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="chroma_all_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.store.add_memory("user1", namespace="user")
        self.store.add_memory("user2", namespace="user")
        self.store.add_memory(
            "cronA1", namespace="cron", cron_id="sched_A"
        )
        self.store.add_memory(
            "cronA2", namespace="cron", cron_id="sched_A"
        )
        self.store.add_memory(
            "cronB1", namespace="cron", cron_id="sched_B"
        )

    def test_get_all_no_filter(self):
        """无过滤参数返回全部条目。"""
        all_mems = self.store.get_all_memories()
        self.assertEqual(len(all_mems), 5)

    def test_get_all_user_namespace(self):
        """namespace=user 只返回用户条目（2 条）。"""
        user_mems = self.store.get_all_memories(namespace="user")
        self.assertEqual(len(user_mems), 2)
        for m in user_mems:
            self.assertEqual(m["metadata"].get("namespace"), "user")

    def test_get_all_cron_namespace_with_cron_id(self):
        """namespace=cron + cron_id 精确过滤。"""
        a_mems = self.store.get_all_memories(namespace="cron", cron_id="sched_A")
        self.assertEqual(len(a_mems), 2)
        for m in a_mems:
            self.assertEqual(m["metadata"].get("namespace"), "cron")
            self.assertEqual(m["metadata"].get("cron_id"), "sched_A")

        b_mems = self.store.get_all_memories(namespace="cron", cron_id="sched_B")
        self.assertEqual(len(b_mems), 1)
        self.assertEqual(b_mems[0]["metadata"].get("cron_id"), "sched_B")


class TestBackwardsCompatOldData(unittest.TestCase):
    """验证旧数据兼容：缺失 namespace 字段视为 user。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="chroma_old_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_old_data_without_namespace_treated_as_user(self):
        """直接通过 collection.add 写入无 namespace 字段的旧数据，
        query_memory(namespace='user') 应能召回。"""
        # 模拟旧数据：直接操作 collection，metadata 不含 namespace
        embedding = self.store._embed("python legacy")
        self.store.collection.add(
            ids=["legacy-1"],
            documents=["python legacy fact"],
            metadatas=[{"type": "fact", "importance": 0.5}],
            embeddings=[embedding],
        )
        # user 命名空间查询应能召回旧数据
        results = self.store.query_memory(
            "python", top_k=10, namespace="user", reinforce=False
        )
        ids = {r["id"] for r in results}
        self.assertIn("legacy-1", ids)

    def test_old_data_not_leaked_to_cron(self):
        """旧数据（无 namespace）不应被 cron 命名空间召回。"""
        embedding = self.store._embed("python legacy")
        self.store.collection.add(
            ids=["legacy-2"],
            documents=["python legacy fact"],
            metadatas=[{"type": "fact"}],
            embeddings=[embedding],
        )
        # cron 命名空间查询不应召回旧数据
        results = self.store.query_memory(
            "python", top_k=10, namespace="cron", cron_id="any",
            reinforce=False,
        )
        ids = {r["id"] for r in results}
        self.assertNotIn("legacy-2", ids)
        self.assertEqual(results, [])

    def test_old_data_in_find_duplicates_user(self):
        """旧数据在 user 命名空间的 find_duplicates 中应被检测到。"""
        embedding = self.store._embed("python legacy")
        self.store.collection.add(
            ids=["legacy-3"],
            documents=["python legacy dup"],
            metadatas=[{"type": "fact"}],
            embeddings=[embedding],
        )
        dups = self.store.find_duplicates(
            "python new", threshold=0.85, namespace="user"
        )
        ids = {d["id"] for d in dups}
        self.assertIn("legacy-3", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
