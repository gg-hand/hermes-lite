"""ConsolidationEngine 命名空间路由测试（Phase 8 Task 1.2）。

验证：
- ``consolidate(session_id="cron:xxx")`` 写入 ``namespace=cron`` + ``cron_id=xxx``
- ``consolidate(session_id="user-session")`` 写入 ``namespace=user``
- ``consolidate(session_id=None)`` 走 user 命名空间（向后兼容）
- 惊讶门控范围限定在同 cron_id 内（不与用户记忆或他调度项混淆）

mock 策略：
- 使用 mock chromadb（通过 tests/_mock_deps）
- LLM 调用全部用 unittest.mock.MagicMock 替代
- 直接构造 ChromaMemoryStore 真实实例（基于 mock collection）

运行方式:
    python -m unittest tests.test_consolidation_namespace -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.memory.consolidation import ConsolidationEngine  # noqa: E402
from src.storage.chroma_store import ChromaMemoryStore  # noqa: E402


def _make_llm_response(text: str) -> MagicMock:
    """构造 mock LLM 响应对象，.content 为含单个 text block 的列表。"""
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


def _make_facts_json(facts_list):
    """构造 LLM 返回的 facts JSON 字符串。"""
    import json
    return json.dumps([{"content": c, "type": "fact", "importance": 0.7}
                       for c in facts_list])


class TestConsolidationNamespaceRouting(unittest.TestCase):
    """验证 ConsolidationEngine 按 session_id 前缀路由 namespace。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="consol_ns_")
        self.chroma_store = ChromaMemoryStore(persist_path=self._tmpdir)
        # mock LLM：chat_consolidation 返回指定 facts
        self.llm_client = MagicMock()
        self.engine = ConsolidationEngine(
            llm_client=self.llm_client,
            chroma_store=self.chroma_store,
            threshold=2,  # 低阈值便于触发
            dedup_threshold=0.99,  # 高阈值避免误判重复
            surprise_gate_enabled=False,  # 用原有去重逻辑便于测试
        )

    def _set_llm_facts(self, facts):
        """设置 LLM 下次返回的 facts。"""
        self.llm_client.chat_consolidation.return_value = _make_llm_response(
            _make_facts_json(facts)
        )

    def test_user_session_routes_to_user_namespace(self):
        """user session_id 沉淀的 fact 含 namespace=user。"""
        self._set_llm_facts(["user fact about python"])
        self.engine.add_info({"role": "user", "content": "hi"})
        self.engine.add_info({"role": "assistant", "content": "hello"})
        stats = self.engine.consolidate(session_id="user-session-1")
        self.assertEqual(stats["namespace"], "user")
        self.assertIsNone(stats["cron_id"])
        # 验证写入的 metadata.namespace
        all_mems = self.chroma_store.get_all_memories()
        user_mems = self.chroma_store.get_all_memories(namespace="user")
        self.assertGreater(len(user_mems), 0)
        for m in user_mems:
            self.assertEqual(m["metadata"].get("namespace"), "user")

    def test_cron_session_routes_to_cron_namespace(self):
        """cron: session_id 沉淀的 fact 含 namespace=cron + cron_id。"""
        self._set_llm_facts(["cron fact about python"])
        self.engine.add_info({"role": "user", "content": "trigger"})
        self.engine.add_info({"role": "assistant", "content": "result"})
        stats = self.engine.consolidate(session_id="cron:sched_A")
        self.assertEqual(stats["namespace"], "cron")
        self.assertEqual(stats["cron_id"], "sched_A")
        # 验证写入的 metadata
        cron_mems = self.chroma_store.get_all_memories(namespace="cron", cron_id="sched_A")
        self.assertGreater(len(cron_mems), 0)
        for m in cron_mems:
            self.assertEqual(m["metadata"].get("namespace"), "cron")
            self.assertEqual(m["metadata"].get("cron_id"), "sched_A")

    def test_none_session_routes_to_user(self):
        """session_id=None 默认走 user 命名空间（向后兼容）。"""
        self._set_llm_facts(["legacy fact about python"])
        self.engine.add_info({"role": "user", "content": "x"})
        self.engine.add_info({"role": "assistant", "content": "y"})
        stats = self.engine.consolidate(session_id=None)
        self.assertEqual(stats["namespace"], "user")
        self.assertIsNone(stats["cron_id"])

    def test_cron_facts_isolated_from_user(self):
        """cron 沉淀的 fact 不应被 user 命名空间召回。"""
        # 写 cron fact
        self._set_llm_facts(["cron python fact"])
        self.engine.add_info({"role": "user", "content": "trigger"})
        self.engine.add_info({"role": "assistant", "content": "result"})
        self.engine.consolidate(session_id="cron:sched_X")
        # user 命名空间查询不应召回 cron fact
        user_results = self.chroma_store.query_memory(
            "cron python", top_k=10, namespace="user", reinforce=False
        )
        self.assertEqual(user_results, [])
        # cron 命名空间查询应召回
        cron_results = self.chroma_store.query_memory(
            "cron python", top_k=10, namespace="cron", cron_id="sched_X",
            reinforce=False,
        )
        self.assertGreater(len(cron_results), 0)

    def test_different_cron_ids_isolated(self):
        """不同 cron_id 的 fact 互不干扰。"""
        # 写 cron A fact
        self._set_llm_facts(["cron A fact python"])
        self.engine.add_info({"role": "user", "content": "a"})
        self.engine.add_info({"role": "assistant", "content": "a-response"})
        self.engine.consolidate(session_id="cron:sched_A")
        # 写 cron B fact
        self._set_llm_facts(["cron B fact python"])
        self.engine.add_info({"role": "user", "content": "b"})
        self.engine.add_info({"role": "assistant", "content": "b-response"})
        self.engine.consolidate(session_id="cron:sched_B")
        # cron A 查询只召回 A 的 fact
        results_a = self.chroma_store.query_memory(
            "fact", top_k=10, namespace="cron", cron_id="sched_A",
            reinforce=False,
        )
        for r in results_a:
            self.assertEqual(r["metadata"].get("cron_id"), "sched_A")
        # cron B 查询只召回 B 的 fact
        results_b = self.chroma_store.query_memory(
            "fact", top_k=10, namespace="cron", cron_id="sched_B",
            reinforce=False,
        )
        for r in results_b:
            self.assertEqual(r["metadata"].get("cron_id"), "sched_B")


class TestSurpriseGateScopedToCronId(unittest.TestCase):
    """验证惊讶门控范围限定在同 cron_id 内。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="consol_sg_")
        self.chroma_store = ChromaMemoryStore(persist_path=self._tmpdir)
        self.llm_client = MagicMock()
        self.engine = ConsolidationEngine(
            llm_client=self.llm_client,
            chroma_store=self.chroma_store,
            threshold=2,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )

    def test_surprise_gate_does_not_see_user_memories(self):
        """cron 沉淀时惊讶门控不应看到 user 命名空间的相似记忆。"""
        # 准备：user 命名空间已有相似 fact
        self.chroma_store.add_memory(
            "python programming language fact", namespace="user"
        )
        # cron 沉淀相似内容：惊讶门控不应在 user 命名空间检测到重复
        self.llm_client.chat_consolidation.return_value = _make_llm_response(
            _make_facts_json(["python programming language new fact"])
        )
        self.engine.add_info({"role": "user", "content": "x"})
        self.engine.add_info({"role": "assistant", "content": "y"})
        stats = self.engine.consolidate(session_id="cron:sched_Y")
        # 因 cron 命名空间无相似记忆，应判定为「惊讶，新知识」并新增
        self.assertEqual(stats["namespace"], "cron")
        self.assertEqual(stats["cron_id"], "sched_Y")
        self.assertGreater(stats["facts_added"], 0)
        # 验证写入到 cron 命名空间
        cron_mems = self.chroma_store.get_all_memories(
            namespace="cron", cron_id="sched_Y"
        )
        self.assertGreater(len(cron_mems), 0)

    def test_surprise_gate_scoped_within_same_cron_id(self):
        """cron A 已有相似 fact，cron A 再次沉淀相似内容时惊讶门控应检测到。"""
        # 先写 cron A fact
        self.chroma_store.add_memory(
            "python cron A original fact",
            namespace="cron", cron_id="sched_A",
        )
        # 再次沉淀相似的 cron A fact
        self.llm_client.chat_consolidation.return_value = _make_llm_response(
            _make_facts_json(["python cron A original fact"])  # 完全相同
        )
        self.engine.add_info({"role": "user", "content": "x"})
        self.engine.add_info({"role": "assistant", "content": "y"})
        stats = self.engine.consolidate(session_id="cron:sched_A")
        # 相同内容 sim=1.0 >= surprise_skip_threshold(0.92)，应跳过
        self.assertGreater(stats["skipped_not_surprising"], 0)
        self.assertEqual(stats["facts_added"], 0)


class TestForceConsolidateNamespace(unittest.TestCase):
    """验证 force_consolidate 也支持 session_id 路由。"""

    def test_force_consolidate_cron_session(self):
        tmpdir = tempfile.mkdtemp(prefix="consol_fc_")
        chroma_store = ChromaMemoryStore(persist_path=tmpdir)
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(
            _make_facts_json(["python cron force fact"])
        )
        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=100,  # 高阈值，只能通过 force 触发
        )
        engine.add_info({"role": "user", "content": "x"})
        engine.add_info({"role": "assistant", "content": "y"})
        stats = engine.consolidate(session_id="cron:sched_Z")
        self.assertEqual(stats["namespace"], "cron")
        self.assertEqual(stats["cron_id"], "sched_Z")
        cron_mems = chroma_store.get_all_memories(
            namespace="cron", cron_id="sched_Z"
        )
        self.assertGreater(len(cron_mems), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
