"""MemoryRetriever namespace + cron_id 过滤召回测试（Phase 8 Task 1.3）。

验证：
- ``retrieve`` 默认 ``namespace="user"`` 只召回用户命名空间记忆（向后兼容）
- ``retrieve`` 显式 ``namespace="cron"`` + ``cron_id`` 只召回该调度项自己的记忆
- ``get_injection_text`` 支持 namespace + cron_id 透传
- cron 会话不返回用户记忆，不同 cron_id 之间互不可见
- ``namespace=None`` 跨命名空间检索（管理员视图）

mock 策略：
- 使用 tests/_mock_deps.py 的 mock chromadb（强制注入）
- mock embedding 规则（2 维向量）：文本含 "python" → [1,0]；含 "java" → [0,1]；其他 → [0.5,0.5]

运行方式:
    python -m unittest tests.test_retrieval_namespace -v
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

from hermes.memory.retrieval import MemoryRetriever  # noqa: E402
from hermes.storage.chroma_store import ChromaMemoryStore  # noqa: E402


def _make_retriever(store: ChromaMemoryStore, top_k: int = 5) -> MemoryRetriever:
    """构造 MemoryRetriever 实例。

    memory_md_manager 与 llm_client 均传 None（retriever 当前实现不再使用
    memory_md_manager；llm_client 仅在 enable_rerank=True 时使用，本测试不开启）。
    """
    return MemoryRetriever(
        chroma_store=store,
        memory_md_manager=None,
        llm_client=None,
        top_k=top_k,
        enable_rerank=False,
    )


class TestRetrieveNamespaceDefault(unittest.TestCase):
    """验证 retrieve 默认 namespace=user 行为（向后兼容）。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="retriever_default_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.retriever = _make_retriever(self.store)

    def test_default_namespace_returns_only_user_memories(self):
        """默认调用只召回 user 命名空间记忆，不返回 cron 记忆。"""
        # 写入一条 user 记忆 + 一条 cron 记忆，关键词均为 python 保证相似
        self.store.add_memory(
            "python user fact", namespace="user"
        )
        self.store.add_memory(
            "python cron fact", namespace="cron", cron_id="sched_A"
        )
        result = self.retriever.retrieve("python question")
        mems = result["long_term_memories"]
        self.assertEqual(len(mems), 1, "默认应只返回 1 条 user 记忆")
        self.assertEqual(mems[0]["metadata"]["namespace"], "user")
        self.assertIn("user fact", mems[0]["content"])

    def test_no_namespace_argument_backwards_compat(self):
        """不传 namespace 参数时行为与原有一致（user 命名空间）。"""
        self.store.add_memory("python legacy fact", namespace="user")
        # 不传 namespace 参数（向后兼容关键场景）
        result = self.retriever.retrieve("python question")
        self.assertEqual(len(result["long_term_memories"]), 1)
        self.assertEqual(
            result["long_term_memories"][0]["metadata"]["namespace"], "user"
        )


class TestRetrieveCronNamespaceIsolation(unittest.TestCase):
    """验证 retrieve namespace=cron 的隔离性。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="retriever_cron_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.retriever = _make_retriever(self.store)
        # 跨命名空间数据：python 关键词保证 embedding 一致，专注验证命名空间过滤
        self.store.add_memory("python user fact", namespace="user")
        self.store.add_memory(
            "python cron A fact", namespace="cron", cron_id="sched_A"
        )
        self.store.add_memory(
            "python cron B fact", namespace="cron", cron_id="sched_B"
        )

    def test_cron_retrieves_only_own_namespace(self):
        """cron 调用方只召回自己命名空间的记忆，不返回 user 或其他 cron_id。"""
        result = self.retriever.retrieve(
            "python question", namespace="cron", cron_id="sched_A"
        )
        mems = result["long_term_memories"]
        self.assertEqual(len(mems), 1, "应只召回 cron A 自己的 1 条记忆")
        self.assertEqual(mems[0]["metadata"]["namespace"], "cron")
        self.assertEqual(mems[0]["metadata"]["cron_id"], "sched_A")
        self.assertIn("cron A fact", mems[0]["content"])

    def test_cron_does_not_return_user_memories(self):
        """cron 会话绝不召回 user 命名空间记忆。"""
        result = self.retriever.retrieve(
            "python question", namespace="cron", cron_id="sched_A"
        )
        for m in result["long_term_memories"]:
            self.assertNotEqual(m["metadata"]["namespace"], "user")
            self.assertNotIn("user fact", m["content"])

    def test_different_cron_ids_isolated(self):
        """不同 cron_id 之间互不可见。"""
        result_a = self.retriever.retrieve(
            "python question", namespace="cron", cron_id="sched_A"
        )
        result_b = self.retriever.retrieve(
            "python question", namespace="cron", cron_id="sched_B"
        )
        self.assertEqual(len(result_a["long_term_memories"]), 1)
        self.assertEqual(len(result_b["long_term_memories"]), 1)
        self.assertIn(
            "cron A fact", result_a["long_term_memories"][0]["content"]
        )
        self.assertIn(
            "cron B fact", result_b["long_term_memories"][0]["content"]
        )

    def test_cron_namespace_empty_result_for_unknown_id(self):
        """不存在的 cron_id 应返回空结果（不泄露其他命名空间数据）。"""
        result = self.retriever.retrieve(
            "python question", namespace="cron", cron_id="nonexistent"
        )
        self.assertEqual(result["long_term_memories"], [])


class TestRetrieveNamespaceNoneAdminView(unittest.TestCase):
    """验证 namespace=None 跨命名空间检索（管理员视图）。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="retriever_admin_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.retriever = _make_retriever(self.store, top_k=10)
        self.store.add_memory("python user fact", namespace="user")
        self.store.add_memory(
            "python cron A fact", namespace="cron", cron_id="sched_A"
        )
        self.store.add_memory(
            "python cron B fact", namespace="cron", cron_id="sched_B"
        )

    def test_none_namespace_returns_all(self):
        """namespace=None 不按命名空间过滤，跨命名空间返回所有匹配。"""
        result = self.retriever.retrieve(
            "python question", namespace=None
        )
        mems = result["long_term_memories"]
        # 应回全部 3 条（user + cron A + cron B）
        self.assertEqual(len(mems), 3)
        namespaces = {m["metadata"]["namespace"] for m in mems}
        self.assertEqual(namespaces, {"user", "cron"})


class TestGetInjectionTextNamespace(unittest.TestCase):
    """验证 get_injection_text 支持 namespace + cron_id 透传。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="retriever_inject_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.retriever = _make_retriever(self.store)
        self.store.add_memory("python user fact", namespace="user")
        self.store.add_memory(
            "python cron fact", namespace="cron", cron_id="sched_X"
        )

    def test_default_injection_returns_user_only(self):
        """默认 get_injection_text 只注入 user 记忆。"""
        text = self.retriever.get_injection_text("python question")
        self.assertIn("user fact", text)
        self.assertNotIn("cron fact", text)

    def test_cron_injection_returns_own_only(self):
        """cron 路径 get_injection_text 只注入自己命名空间的记忆。"""
        text = self.retriever.get_injection_text(
            "python question", namespace="cron", cron_id="sched_X"
        )
        self.assertIn("cron fact", text)
        self.assertNotIn("user fact", text)

    def test_empty_result_returns_empty_string(self):
        """无匹配记忆时返回空字符串（cron 路径不存在的 cron_id）。"""
        text = self.retriever.get_injection_text(
            "python question", namespace="cron", cron_id="nonexistent"
        )
        self.assertEqual(text, "")


class TestUserProfileFilteringInCronNamespace(unittest.TestCase):
    """验证 user_profile 过滤逻辑在 cron 命名空间下仍生效。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="retriever_profile_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.retriever = _make_retriever(self.store)

    def test_user_profile_filtered_in_cron_namespace(self):
        """type=user_profile 的条目即使在 cron 命名空间也应被过滤。"""
        # 写入 cron 命名空间下的 user_profile 残留 + 普通 fact
        self.store.add_memory(
            "python user profile residue",
            metadata={"type": "user_profile"},
            namespace="cron",
            cron_id="sched_P",
        )
        self.store.add_memory(
            "python cron fact",
            metadata={"type": "fact"},
            namespace="cron",
            cron_id="sched_P",
        )
        result = self.retriever.retrieve(
            "python question", namespace="cron", cron_id="sched_P"
        )
        mems = result["long_term_memories"]
        # user_profile 应被过滤，只剩 1 条 fact
        self.assertEqual(len(mems), 1)
        for m in mems:
            self.assertNotEqual(
                str(m["metadata"].get("type", "")).lower(), "user_profile"
            )


if __name__ == "__main__":
    unittest.main()
