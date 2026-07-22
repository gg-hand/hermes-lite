"""ConsolidationEngine 单元测试 — 验证阈值触发与 facts JSON 容错解析。

运行方式:
    python -m unittest tests.test_consolidation -v
    python tests/test_consolidation.py

mock 策略:
- LLM 调用全部用 unittest.mock.MagicMock 替代，无网络请求
- chroma_store 用 MagicMock 替代（避免真实 chromadb 初始化开销）
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.memory.consolidation import ConsolidationEngine  # noqa: E402


def _make_llm_response(text: str) -> MagicMock:
    """构造 mock LLM 响应对象，.content 为含单个 text block 的列表。

    _extract_response_text 兼容 dict 类型 block: {"type": "text", "text": "..."}。
    """
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


class TestShouldConsolidate(unittest.TestCase):
    """验证 should_consolidate 的阈值触发逻辑。"""

    def test_below_threshold(self):
        """消息数未达阈值时返回 False。"""
        engine = ConsolidationEngine(
            llm_client=MagicMock(),
            chroma_store=MagicMock(),
            threshold=5,
        )
        for i in range(4):  # threshold - 1
            engine.add_info({"role": "user", "content": f"msg {i}"})
        self.assertFalse(engine.should_consolidate())

    def test_at_threshold(self):
        """消息数达阈值时返回 True。"""
        engine = ConsolidationEngine(
            llm_client=MagicMock(),
            chroma_store=MagicMock(),
            threshold=5,
        )
        for i in range(5):  # exactly threshold
            engine.add_info({"role": "user", "content": f"msg {i}"})
        self.assertTrue(engine.should_consolidate())

    def test_above_threshold(self):
        """消息数超过阈值时返回 True。"""
        engine = ConsolidationEngine(
            llm_client=MagicMock(),
            chroma_store=MagicMock(),
            threshold=5,
        )
        for i in range(8):  # threshold + 3
            engine.add_info({"role": "user", "content": f"msg {i}"})
        self.assertTrue(engine.should_consolidate())

    def test_default_threshold_is_15(self):
        """默认阈值为 15。"""
        engine = ConsolidationEngine(
            llm_client=MagicMock(),
            chroma_store=MagicMock(),
        )
        self.assertEqual(engine.threshold, 15)
        for i in range(14):
            engine.add_info({"role": "user", "content": f"msg {i}"})
        self.assertFalse(engine.should_consolidate())
        engine.add_info({"role": "user", "content": "msg 14"})
        self.assertTrue(engine.should_consolidate())


class TestParseFactsJson(unittest.TestCase):
    """验证 _parse_facts_json 的容错解析。"""

    def test_valid_json(self):
        """合法 JSON（{"facts": [...]} 格式）正确解析。"""
        raw = '{"facts": [{"content": "test fact", "type": "fact", "importance": 0.5}]}'
        facts = ConsolidationEngine._parse_facts_json(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "test fact")

    def test_valid_json_array(self):
        """LLM 直接返回 JSON 数组也能正确解析。"""
        raw = '[{"content": "fact a", "type": "fact"}, {"content": "fact b", "type": "fact"}]'
        facts = ConsolidationEngine._parse_facts_json(raw)
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0]["content"], "fact a")
        self.assertEqual(facts[1]["content"], "fact b")

    def test_with_markdown_fence(self):
        """带 markdown fence 的 JSON 容错解析。"""
        raw = '```json\n{"facts": [{"content": "test", "type": "fact", "importance": 0.5}]}\n```'
        facts = ConsolidationEngine._parse_facts_json(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "test")

    def test_with_plain_markdown_fence(self):
        """带无语言标记的 markdown fence 也能解析。"""
        raw = '```\n[{"content": "fenced", "type": "fact"}]\n```'
        facts = ConsolidationEngine._parse_facts_json(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "fenced")

    def test_invalid_json(self):
        """非法 JSON 返回空列表不抛异常。"""
        raw = 'this is not json at all'
        facts = ConsolidationEngine._parse_facts_json(raw)
        self.assertEqual(facts, [])

    def test_empty_string(self):
        """空字符串与 None 返回空列表。"""
        self.assertEqual(ConsolidationEngine._parse_facts_json(""), [])
        self.assertEqual(ConsolidationEngine._parse_facts_json(None), [])

    def test_filters_invalid_entries(self):
        """过滤掉非 dict 或缺少 content 的条目。"""
        raw = '[{"content": "valid"}, {"type": "fact"}, "not a dict", {"content": ""}]'
        facts = ConsolidationEngine._parse_facts_json(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "valid")

    def test_facts_field_not_list(self):
        """facts 字段非列表时返回空。"""
        raw = '{"facts": "not a list"}'
        facts = ConsolidationEngine._parse_facts_json(raw)
        self.assertEqual(facts, [])


class TestConsolidateWithMockLlm(unittest.TestCase):
    """验证 consolidate() 调用 LLM 并写入 chroma_store。"""

    def test_consolidate_writes_facts_to_chroma_store(self):
        """mock LLM 返回固定 facts JSON，验证 consolidate() 调用 chroma_store.add_memory。

        user_profile 类事实仅写入 memory.md，不入向量库；
        其他类型 fact 走 add_memory。
        """
        # 1. 构造 mock LLM，chat_consolidation 返回固定 JSON
        facts_json = (
            '{"facts": ['
            '{"content": "user likes Python", "type": "fact", "importance": 0.8},'
            '{"content": "user is engineer", "type": "user_profile", "importance": 0.9}'
            ']}'
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        # 2. 构造 mock chroma_store，find_duplicates 返回空（无重复）
        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        # 3. 注入足够消息触发 consolidate
        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=2,
        )
        engine.add_info({"role": "user", "content": "I like Python"})
        engine.add_info({"role": "assistant", "content": "ok"})

        # 4. 调用 consolidate()
        stats = engine.consolidate()

        # 5. 验证 LLM 被调用一次
        self.assertEqual(llm_client.chat_consolidation.call_count, 1)

        # 6. 验证 add_memory 仅被调用 1 次（user_profile 跳过向量库）
        self.assertEqual(chroma_store.add_memory.call_count, 1)
        self.assertEqual(chroma_store.update_memory.call_count, 0)
        # user_profile 也不触发 find_duplicates
        self.assertEqual(chroma_store.find_duplicates.call_count, 1)

        # 7. 验证统计字典
        self.assertEqual(stats["facts_extracted"], 2)
        self.assertEqual(stats["facts_added"], 1)
        self.assertEqual(stats["facts_updated"], 0)
        self.assertEqual(stats["duplicates"], 0)
        self.assertEqual(stats["profile_only"], 1)

        # 8. 验证 consolidate 后状态重置
        self.assertEqual(engine.info_counter, 0)
        self.assertEqual(engine.pending_messages, [])

    def test_consolidate_updates_duplicates(self):
        """find_duplicates 返回重复项时调用 update_memory 而非 add_memory。

        注：默认 surprise_gate_enabled=True 会把 sim>=0.92 的重复判定为「不惊讶」
        而跳过。此处显式关闭惊讶门控以测试原有去重更新逻辑（向后兼容路径）。
        惊讶门控自身的双阈值行为见 tests/test_surprise_gate.py。
        """
        facts_json = (
            '{"facts": ['
            '{"content": "user likes Python", "type": "fact", "importance": 0.8}'
            ']}'
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # 模拟已有重复记忆
        chroma_store.find_duplicates.return_value = [
            {
                "id": "existing-id",
                "content": "user likes Python",
                "metadata": {},
                "similarity": 0.95,
            }
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=False,  # 关闭惊讶门控，走原有去重逻辑
        )
        engine.add_info({"role": "user", "content": "I like Python"})

        stats = engine.consolidate()

        self.assertEqual(chroma_store.add_memory.call_count, 0)
        self.assertEqual(chroma_store.update_memory.call_count, 1)
        self.assertEqual(stats["facts_added"], 0)
        self.assertEqual(stats["facts_updated"], 1)
        self.assertEqual(stats["duplicates"], 1)

    def test_consolidate_empty_buffer_returns_zero_stats(self):
        """无待沉淀消息时返回空统计且不调用 LLM。"""
        llm_client = MagicMock()
        chroma_store = MagicMock()
        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
        )
        stats = engine.consolidate()
        self.assertEqual(stats["facts_extracted"], 0)
        self.assertEqual(stats["facts_added"], 0)
        llm_client.chat_consolidation.assert_not_called()

    def test_consolidate_llm_failure_no_reset(self):
        """LLM 调用失败时不重置缓冲，便于重试。"""
        llm_client = MagicMock()
        llm_client.chat_consolidation.side_effect = RuntimeError("LLM down")
        chroma_store = MagicMock()

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
        )
        engine.add_info({"role": "user", "content": "msg"})

        stats = engine.consolidate()

        self.assertEqual(stats["facts_extracted"], 0)
        # 缓冲未重置，便于后续重试
        self.assertEqual(engine.info_counter, 1)
        self.assertEqual(len(engine.pending_messages), 1)

    def test_consolidate_no_facts_resets_buffer(self):
        """LLM 返回空 facts 时重置缓冲。"""
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response("[]")
        chroma_store = MagicMock()

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
        )
        engine.add_info({"role": "user", "content": "msg"})

        stats = engine.consolidate()

        self.assertEqual(stats["facts_extracted"], 0)
        chroma_store.add_memory.assert_not_called()
        # 缓冲已重置
        self.assertEqual(engine.info_counter, 0)
        self.assertEqual(engine.pending_messages, [])

    def test_consolidate_calls_memory_md_writer_for_profile_facts(self):
        """user_profile 类事实触发 memory_md_writer 回调，且不入向量库。"""
        facts_json = (
            '{"facts": ['
            '{"content": "user is engineer", "type": "user_profile", "importance": 0.9},'
            '{"content": "user likes Python", "type": "fact", "importance": 0.8}'
            ']}'
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)
        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        memory_md_writer = MagicMock()
        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            memory_md_writer=memory_md_writer,
            threshold=1,
        )
        engine.add_info({"role": "user", "content": "I am engineer"})

        stats = engine.consolidate()

        # memory_md_writer 在守护线程中异步调用，短暂等待确保线程执行完毕
        import time
        time.sleep(0.2)
        memory_md_writer.assert_called_once()
        called_facts = memory_md_writer.call_args[0][0]
        # 仅 user_profile 类事实传入
        self.assertEqual(len(called_facts), 1)
        self.assertEqual(called_facts[0]["type"], "user_profile")

        # user_profile 不入向量库：find_duplicates 与 add_memory 各仅 1 次（普通 fact）
        self.assertEqual(chroma_store.find_duplicates.call_count, 1)
        self.assertEqual(chroma_store.add_memory.call_count, 1)
        # profile_only 统计正确
        self.assertEqual(stats["profile_only"], 1)
        self.assertEqual(stats["facts_added"], 1)


class TestPreferenceDualWrite(unittest.TestCase):
    """验证 preference 类型事实的双写：信号池累积 + ChromaDB 检索。

    经 review 发现若把 preference 从 ChromaDB 摘掉，retrieval.py 的
    priority_map 仍把 preference: 2 列为检索类型，会导致 memory_search
    检索不到用户偏好。因此 preference 必须同时进信号池和向量库。
    """

    def test_preference_dual_write_signal_pool_and_chromadb(self):
        """preference 类型事实同时进入信号池和向量库。"""
        facts_json = (
            '{"facts": ['
            '{"content": "用户讨厌 emoji", "type": "preference", "importance": 0.8}'
            ']}'
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation_sync.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        signal_pool = MagicMock()
        signal_pool.THRESHOLD = 7

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            signal_pool=signal_pool,
            threshold=1,
        )
        engine.add_info({"role": "user", "content": "我讨厌 emoji"})

        stats = engine.consolidate()

        # 信号池被调用，source="L3"
        signal_pool.add.assert_called_once()
        call_kwargs = signal_pool.add.call_args.kwargs
        self.assertEqual(call_kwargs["source"], "L3")
        self.assertEqual(call_kwargs["weight"], 2)
        self.assertEqual(call_kwargs["section"], "沉淀笔记")
        self.assertIn("emoji", call_kwargs["content"])

        # 向量库也被调用（双写保留检索能力）
        chroma_store.add_memory.assert_called_once()

        # profile_only 不应增加（preference 不是 profile_only）
        self.assertEqual(stats["profile_only"], 0)
        self.assertEqual(stats["facts_added"], 1)

    def test_user_profile_only_signal_pool(self):
        """回归保护：user_profile 仅走信号池，不入向量库。"""
        facts_json = (
            '{"facts": ['
            '{"content": "user is backend engineer", "type": "user_profile", "importance": 0.9}'
            ']}'
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation_sync.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        signal_pool = MagicMock()
        signal_pool.THRESHOLD = 7

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            signal_pool=signal_pool,
            threshold=1,
        )
        engine.add_info({"role": "user", "content": "I am engineer"})

        stats = engine.consolidate()

        # 信号池被调用
        signal_pool.add.assert_called_once()
        # 向量库未被调用（user_profile 跳过）
        chroma_store.add_memory.assert_not_called()
        # profile_only 计数正确
        self.assertEqual(stats["profile_only"], 1)

    def test_decision_only_chromadb(self):
        """回归保护：decision 仅走向量库，不入信号池。"""
        facts_json = (
            '{"facts": ['
            '{"content": "user decided to migrate from Webpack to Vite", "type": "decision", "importance": 0.9}'
            ']}'
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation_sync.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        signal_pool = MagicMock()
        signal_pool.THRESHOLD = 7

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            signal_pool=signal_pool,
            threshold=1,
        )
        engine.add_info({"role": "user", "content": "I decided to migrate"})

        stats = engine.consolidate()

        # 信号池未被调用（decision 不进信号池）
        signal_pool.add.assert_not_called()
        # 向量库被调用
        chroma_store.add_memory.assert_called_once()
        # profile_only 不增加
        self.assertEqual(stats["profile_only"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
