"""MemoryRetriever.get_injection_text exclude_types 过滤测试（ops-reliability-uplift Task 5.6）。

验证：
- exclude_types={"conversation_turn"} 过滤命中 metadata.type 的记录
- exclude_types=None 不过滤（向后兼容）
- 全部被过滤掉时 injection_text 为空字符串（不报错）
- 仅 fact 类型全部保留

mock 策略：
- 使用 tests/_mock_deps.py 的 mock chromadb（强制注入）
- mock embedding 规则（2 维向量）：文本含 "python" → [1,0]；含 "java" → [0,1]；其他 → [0.5,0.5]

运行方式:
    python -m unittest tests.test_retrieval_exclude_types -v
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
    """构造 MemoryRetriever 实例。"""
    return MemoryRetriever(
        chroma_store=store,
        memory_md_manager=None,
        llm_client=None,
        top_k=top_k,
        enable_rerank=False,
    )


class TestExcludeTypesFilter(unittest.TestCase):
    """验证 get_injection_text 的 exclude_types 参数过滤行为。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="retriever_exclude_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.retriever = _make_retriever(self.store)
        # 写入 cron 命名空间下 3 条记忆（关键词均为 python 保证相似）：
        # 1. type=conversation_turn（应被过滤）
        # 2. type=summary（应保留）
        # 3. type=fact（应保留）
        self.store.add_memory(
            "python cron last response",
            metadata={"type": "conversation_turn"},
            namespace="cron",
            cron_id="sched_A",
        )
        self.store.add_memory(
            "python cron summary text",
            metadata={"type": "summary"},
            namespace="cron",
            cron_id="sched_A",
        )
        self.store.add_memory(
            "python cron fact info",
            metadata={"type": "fact"},
            namespace="cron",
            cron_id="sched_A",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_exclude_conversation_turn_filters_matched(self):
        """exclude_types={"conversation_turn"} 过滤命中项，保留 summary/fact。"""
        text = self.retriever.get_injection_text(
            "python question",
            namespace="cron",
            cron_id="sched_A",
            exclude_types={"conversation_turn"},
        )
        # 应包含 summary 和 fact，不含 conversation_turn 的 "last response"
        self.assertIn("summary text", text)
        self.assertIn("fact info", text)
        self.assertNotIn("last response", text)

    def test_exclude_types_none_no_filter(self):
        """exclude_types=None 不过滤，全部 3 条记忆都注入（向后兼容）。"""
        text = self.retriever.get_injection_text(
            "python question",
            namespace="cron",
            cron_id="sched_A",
            exclude_types=None,
        )
        # 全部 3 条都应出现
        self.assertIn("last response", text)
        self.assertIn("summary text", text)
        self.assertIn("fact info", text)

    def test_exclude_types_empty_set_no_filter(self):
        """exclude_types=set() 空集不过滤（与 None 等价）。"""
        text = self.retriever.get_injection_text(
            "python question",
            namespace="cron",
            cron_id="sched_A",
            exclude_types=set(),
        )
        self.assertIn("last response", text)
        self.assertIn("summary text", text)
        self.assertIn("fact info", text)

    def test_all_filtered_returns_empty_string(self):
        """全部记录被过滤掉时 injection_text 为空字符串（不报错）。"""
        # 同时排除 conversation_turn / summary / fact
        text = self.retriever.get_injection_text(
            "python question",
            namespace="cron",
            cron_id="sched_A",
            exclude_types={"conversation_turn", "summary", "fact"},
        )
        self.assertEqual(text, "")

    def test_only_fact_all_preserved(self):
        """仅有 fact 类型记忆时，exclude_types={"conversation_turn"} 全部保留。"""
        # 新建一个只含 fact 的 store
        tmpdir2 = tempfile.mkdtemp(prefix="retriever_fact_only_")
        try:
            store2 = ChromaMemoryStore(persist_path=tmpdir2)
            retriever2 = _make_retriever(store2)
            store2.add_memory(
                "python fact only one",
                metadata={"type": "fact"},
                namespace="cron",
                cron_id="sched_X",
            )
            text = retriever2.get_injection_text(
                "python question",
                namespace="cron",
                cron_id="sched_X",
                exclude_types={"conversation_turn"},
            )
            self.assertIn("fact only one", text)
        finally:
            import shutil
            shutil.rmtree(tmpdir2, ignore_errors=True)

    def test_exclude_types_case_insensitive(self):
        """exclude_types 大小写不敏感（CONVERSATION_TURN 也能过滤 conversation_turn）。"""
        text = self.retriever.get_injection_text(
            "python question",
            namespace="cron",
            cron_id="sched_A",
            exclude_types={"CONVERSATION_TURN"},
        )
        self.assertNotIn("last response", text)
        self.assertIn("summary text", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
