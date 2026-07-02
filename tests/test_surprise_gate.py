"""惊讶门控（写入侧过滤）单元测试 — Phase 7 Task 2。

覆盖 spec ``implement-phase7-memory-enhancement`` 中惊讶门控的所有场景：
- surprise_gate_enabled=False 时走原有去重逻辑（向后兼容）
- sim < surprise_similarity_threshold → 新增（惊讶，新知识）
- surprise_similarity_threshold ≤ sim < surprise_skip_threshold → 更新（惊讶，纠正旧记忆）
- sim ≥ surprise_skip_threshold → 跳过（不惊讶，已有等价记忆）
- user_profile 类事实不受惊讶门控影响（仍走 memory.md 写入路径）
- find_duplicates 异常时降级到原有去重逻辑（不中断主流程）
- stats 中 skipped_not_surprising 计数正确
- 日志记录正确（DEBUG 决策日志 + INFO 汇总日志）

运行方式:
    python -m pytest tests/test_surprise_gate.py -v
    python -m unittest tests.test_surprise_gate -v
    python tests/test_surprise_gate.py

mock 策略:
- LLM 调用全部用 unittest.mock.MagicMock 替代，无网络请求
- chroma_store 用 MagicMock 替代，find_duplicates 返回值由测试控制相似度
- 参考 tests/test_consolidation.py 的测试风格
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.memory.consolidation import ConsolidationEngine  # noqa: E402


def _make_llm_response(text: str) -> MagicMock:
    """构造 mock LLM 响应对象，.content 为含单个 text block 的列表。

    与 test_consolidation.py 保持一致，兼容 ConsolidationEngine
    ._extract_response_text 的 dict block 解析逻辑。
    """
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


def _make_facts_json(facts: list) -> str:
    """构造 facts JSON 字符串。

    参数:
        facts: list of dict，每个 dict 含 content / type / importance 字段。

    返回:
        形如 '{"facts": [...]}' 的 JSON 字符串。
    """
    import json

    return json.dumps({"facts": facts}, ensure_ascii=False)


def _make_duplicate(memory_id: str, content: str, similarity: float) -> dict:
    """构造 find_duplicates 返回的单条重复记忆项。"""
    return {
        "id": memory_id,
        "content": content,
        "metadata": {},
        "similarity": similarity,
    }


# ===========================================================================
# 1. surprise_gate_enabled=False 时走原有去重逻辑（向后兼容）
# ===========================================================================


class TestSurpriseGateDisabled(unittest.TestCase):
    """验证 surprise_gate_enabled=False 时走原有去重逻辑。"""

    def test_disabled_finds_duplicate_and_updates(self):
        """关闭惊讶门控后，find_duplicates 命中（任意相似度）走更新路径。

        原有逻辑：find_duplicates 用 dedup_threshold 检索，命中则更新，
        不区分相似度高低。即使 sim=0.95（在惊讶门控下会被跳过），
        关闭门控后仍走更新。
        """
        facts_json = _make_facts_json(
            [{"content": "user likes Python", "type": "fact", "importance": 0.8}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("existing-id", "user likes Python", 0.95)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=False,
        )
        engine.add_info({"role": "user", "content": "I like Python"})

        stats = engine.consolidate()

        # 走更新路径，不跳过
        self.assertEqual(chroma_store.update_memory.call_count, 1)
        self.assertEqual(chroma_store.add_memory.call_count, 0)
        self.assertEqual(stats["facts_updated"], 1)
        self.assertEqual(stats["facts_added"], 0)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["skipped_not_surprising"], 0)

    def test_disabled_no_duplicate_and_adds(self):
        """关闭惊讶门控后，find_duplicates 未命中走新增路径。"""
        facts_json = _make_facts_json(
            [{"content": "user has a cat", "type": "fact", "importance": 0.7}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=False,
        )
        engine.add_info({"role": "user", "content": "I have a cat"})

        stats = engine.consolidate()

        self.assertEqual(chroma_store.add_memory.call_count, 1)
        self.assertEqual(chroma_store.update_memory.call_count, 0)
        self.assertEqual(stats["facts_added"], 1)
        self.assertEqual(stats["facts_updated"], 0)
        self.assertEqual(stats["skipped_not_surprising"], 0)


# ===========================================================================
# 2. sim < surprise_similarity_threshold → 新增（惊讶，新知识）
# ===========================================================================


class TestSurpriseGateAddNewKnowledge(unittest.TestCase):
    """验证 sim < surprise_similarity_threshold 时新增到向量库。"""

    def test_low_similarity_adds_new_fact(self):
        """sim=0.3 < 0.85 阈值，判定为「惊讶，新知识」→ 新增。"""
        facts_json = _make_facts_json(
            [{"content": "user has a cat", "type": "fact", "importance": 0.7}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # find_duplicates 用 surprise_similarity_threshold=0.85 检索，
        # 返回空表示无 sim >= 0.85 的记忆
        chroma_store.find_duplicates.return_value = []

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "I have a cat"})

        stats = engine.consolidate()

        self.assertEqual(chroma_store.add_memory.call_count, 1)
        self.assertEqual(chroma_store.update_memory.call_count, 0)
        self.assertEqual(stats["facts_added"], 1)
        self.assertEqual(stats["facts_updated"], 0)
        self.assertEqual(stats["skipped_not_surprising"], 0)
        # 惊讶门控路径不 increment duplicates
        self.assertEqual(stats["duplicates"], 0)

    def test_find_duplicates_called_with_surprise_threshold(self):
        """惊讶门控开启时，find_duplicates 使用 surprise_similarity_threshold。"""
        facts_json = _make_facts_json(
            [{"content": "new fact", "type": "fact", "importance": 0.5}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})
        engine.consolidate()

        # 验证 find_duplicates 用 surprise_similarity_threshold=0.85 调用
        chroma_store.find_duplicates.assert_called_once()
        call_args = chroma_store.find_duplicates.call_args
        # call_args 是 (args_tuple, kwargs_dict) 形式
        # 代码中调用方式：find_duplicates(content, threshold=0.85)
        args_tuple, kwargs_dict = call_args[0], call_args[1]
        # threshold 应作为 kwarg 传入
        self.assertEqual(kwargs_dict.get("threshold"), 0.85)


# ===========================================================================
# 3. surprise_similarity_threshold ≤ sim < surprise_skip_threshold → 更新
# ===========================================================================


class TestSurpriseGateUpdateConflict(unittest.TestCase):
    """验证相似度介于两阈值之间时更新已有记忆（惊讶，纠正旧记忆）。"""

    def test_medium_similarity_updates_existing(self):
        """sim=0.87 ∈ [0.85, 0.92) → 更新（惊讶，纠正/补充旧记忆）。"""
        facts_json = _make_facts_json(
            [{"content": "user is learning Rust", "type": "fact", "importance": 0.8}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("old-id", "user only uses Python", 0.87)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "I am learning Rust"})

        stats = engine.consolidate()

        self.assertEqual(chroma_store.update_memory.call_count, 1)
        self.assertEqual(chroma_store.add_memory.call_count, 0)
        self.assertEqual(stats["facts_updated"], 1)
        self.assertEqual(stats["facts_added"], 0)
        self.assertEqual(stats["skipped_not_surprising"], 0)
        # 惊讶门控更新路径不 increment duplicates（与原有逻辑区分）
        self.assertEqual(stats["duplicates"], 0)

    def test_update_uses_highest_similarity_target(self):
        """命中多条时取相似度最高的那条进行更新。"""
        facts_json = _make_facts_json(
            [{"content": "new fact", "type": "fact", "importance": 0.5}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # find_duplicates 按相似度降序排列，第一条最高
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("highest-id", "highest sim content", 0.90),
            _make_duplicate("lower-id", "lower sim content", 0.86),
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})
        engine.consolidate()

        # 验证 update_memory 用最高相似度的 id
        chroma_store.update_memory.assert_called_once()
        call_args = chroma_store.update_memory.call_args
        # 第一个位置参数为 target_id
        self.assertEqual(call_args.args[0], "highest-id")

    def test_boundary_just_below_skip_threshold_updates(self):
        """sim=0.919（刚好低于 skip 阈值 0.92）→ 更新。"""
        facts_json = _make_facts_json(
            [{"content": "fact content", "type": "fact", "importance": 0.5}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("id-1", "old content", 0.919)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})
        stats = engine.consolidate()

        self.assertEqual(stats["facts_updated"], 1)
        self.assertEqual(stats["skipped_not_surprising"], 0)


# ===========================================================================
# 4. sim ≥ surprise_skip_threshold → 跳过（不惊讶，已有等价记忆）
# ===========================================================================


class TestSurpriseGateSkipNotSurprising(unittest.TestCase):
    """验证 sim ≥ surprise_skip_threshold 时跳过写入。"""

    def test_high_similarity_skips(self):
        """sim=0.95 ≥ 0.92 跳过阈值 → 跳过（不惊讶，已有等价记忆）。"""
        facts_json = _make_facts_json(
            [{"content": "user likes Python", "type": "fact", "importance": 0.8}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("existing-id", "user uses Python", 0.95)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "I like Python"})

        stats = engine.consolidate()

        # 跳过：既不新增也不更新
        self.assertEqual(chroma_store.add_memory.call_count, 0)
        self.assertEqual(chroma_store.update_memory.call_count, 0)
        self.assertEqual(stats["skipped_not_surprising"], 1)
        self.assertEqual(stats["facts_added"], 0)
        self.assertEqual(stats["facts_updated"], 0)

    def test_boundary_at_skip_threshold_skips(self):
        """sim=0.92（恰好等于 skip 阈值）→ 跳过（≥ 判定）。"""
        facts_json = _make_facts_json(
            [{"content": "fact content", "type": "fact", "importance": 0.5}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("id-1", "old content", 0.92)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})
        stats = engine.consolidate()

        self.assertEqual(stats["skipped_not_surprising"], 1)
        self.assertEqual(stats["facts_updated"], 0)

    def test_multiple_facts_mixed_decisions(self):
        """多条 fact 混合决策：一条跳过、一条更新、一条新增。"""
        facts_json = _make_facts_json(
            [
                {"content": "fact skip", "type": "fact", "importance": 0.5},
                {"content": "fact update", "type": "fact", "importance": 0.5},
                {"content": "fact add", "type": "fact", "importance": 0.5},
            ]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # 三次 find_duplicates 调用分别返回不同结果
        chroma_store.find_duplicates.side_effect = [
            [_make_duplicate("id-skip", "skip content", 0.95)],  # 跳过
            [_make_duplicate("id-update", "update content", 0.87)],  # 更新
            [],  # 新增
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        stats = engine.consolidate()

        self.assertEqual(stats["skipped_not_surprising"], 1)
        self.assertEqual(stats["facts_updated"], 1)
        self.assertEqual(stats["facts_added"], 1)
        self.assertEqual(chroma_store.update_memory.call_count, 1)
        self.assertEqual(chroma_store.add_memory.call_count, 1)


# ===========================================================================
# 5. user_profile 类事实不受惊讶门控影响
# ===========================================================================


class TestUserProfileNotAffectedBySurpriseGate(unittest.TestCase):
    """验证 user_profile 类事实仍走 memory.md 写入路径，不触发惊讶门控。"""

    def test_user_profile_bypasses_surprise_gate(self):
        """user_profile 事实不触发 find_duplicates，直接走 memory.md。"""
        facts_json = _make_facts_json(
            [
                {"content": "user is engineer", "type": "user_profile", "importance": 0.9},
            ]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # 即使 find_duplicates 返回高相似度，user_profile 也不应触发
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("existing", "engineer", 0.99)
        ]

        memory_md_writer = MagicMock()
        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            memory_md_writer=memory_md_writer,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "I am engineer"})

        stats = engine.consolidate()

        # 等待异步写入线程
        import time
        time.sleep(0.2)

        # user_profile 不触发 find_duplicates（惊讶门控不适用）
        self.assertEqual(chroma_store.find_duplicates.call_count, 0)
        # user_profile 不入向量库
        self.assertEqual(chroma_store.add_memory.call_count, 0)
        self.assertEqual(chroma_store.update_memory.call_count, 0)
        # 走 memory.md 写入
        memory_md_writer.assert_called_once()
        # stats 计数正确
        self.assertEqual(stats["profile_only"], 1)
        self.assertEqual(stats["facts_added"], 0)
        self.assertEqual(stats["facts_updated"], 0)
        self.assertEqual(stats["skipped_not_surprising"], 0)

    def test_mixed_user_profile_and_fact_only_fact_hits_surprise_gate(self):
        """user_profile + fact 混合时，仅 fact 触发惊讶门控。"""
        facts_json = _make_facts_json(
            [
                {"content": "user profile fact", "type": "user_profile", "importance": 0.9},
                {"content": "user likes Python", "type": "fact", "importance": 0.8},
            ]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # fact 的 find_duplicates 返回高相似度（应跳过）
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("id-1", "user uses Python", 0.95)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        stats = engine.consolidate()

        # 仅 fact 触发一次 find_duplicates（user_profile 不触发）
        self.assertEqual(chroma_store.find_duplicates.call_count, 1)
        # fact 被跳过（sim=0.95 >= 0.92）
        self.assertEqual(stats["skipped_not_surprising"], 1)
        self.assertEqual(stats["facts_added"], 0)
        # user_profile 走 memory.md
        self.assertEqual(stats["profile_only"], 1)


# ===========================================================================
# 6. find_duplicates 异常时降级到原有去重逻辑（不中断主流程）
# ===========================================================================


class TestSurpriseGateFallbackOnException(unittest.TestCase):
    """验证惊讶门控 find_duplicates 异常时降级到原有去重逻辑。"""

    def test_surprise_gate_exception_falls_back_to_original_logic(self):
        """惊讶门控 find_duplicates 抛异常 → 降级到原有去重逻辑（用 dedup_threshold）。

        降级后再调 find_duplicates(dedup_threshold)，未命中 → 新增。
        """
        facts_json = _make_facts_json(
            [{"content": "fact content", "type": "fact", "importance": 0.5}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # 第一次（surprise gate）抛异常，第二次（降级原逻辑）返回空
        chroma_store.find_duplicates.side_effect = [
            RuntimeError("vector db error"),
            [],
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
            dedup_threshold=0.85,
        )
        engine.add_info({"role": "user", "content": "msg"})

        stats = engine.consolidate()

        # 降级后走原逻辑：find_duplicates 未命中 → 新增
        self.assertEqual(chroma_store.find_duplicates.call_count, 2)
        self.assertEqual(chroma_store.add_memory.call_count, 1)
        self.assertEqual(stats["facts_added"], 1)
        self.assertEqual(stats["skipped_not_surprising"], 0)

    def test_fallback_exception_does_not_crash(self):
        """两次 find_duplicates 都抛异常时不崩溃，按"无重复"新增。"""
        facts_json = _make_facts_json(
            [{"content": "fact content", "type": "fact", "importance": 0.5}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # 两次都抛异常
        chroma_store.find_duplicates.side_effect = RuntimeError("db down")

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        # 不应抛异常
        stats = engine.consolidate()

        # 降级路径 catch 异常后按"无重复"处理 → 新增
        self.assertEqual(stats["facts_added"], 1)
        self.assertEqual(stats["skipped_not_surprising"], 0)

    def test_disabled_mode_exception_does_not_crash(self):
        """surprise_gate_enabled=False 时 find_duplicates 异常也不崩溃。"""
        facts_json = _make_facts_json(
            [{"content": "fact content", "type": "fact", "importance": 0.5}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.side_effect = RuntimeError("db down")

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=False,
        )
        engine.add_info({"role": "user", "content": "msg"})

        stats = engine.consolidate()

        # 原 logic 也 catch 异常 → 按"无重复"处理 → 新增
        self.assertEqual(stats["facts_added"], 1)


# ===========================================================================
# 7. stats 中 skipped_not_surprising 计数正确
# ===========================================================================


class TestSkippedNotSurprisingStats(unittest.TestCase):
    """验证 stats 中 skipped_not_surprising 计数正确。"""

    def test_multiple_skipped_counted_correctly(self):
        """多条 fact 均被跳过时，skipped_not_surprising 计数正确。"""
        facts_json = _make_facts_json(
            [
                {"content": "fact 1", "type": "fact", "importance": 0.5},
                {"content": "fact 2", "type": "fact", "importance": 0.5},
                {"content": "fact 3", "type": "fact", "importance": 0.5},
            ]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        # 三条都返回高相似度（应全部跳过）
        chroma_store.find_duplicates.side_effect = [
            [_make_duplicate("id-1", "old 1", 0.95)],
            [_make_duplicate("id-2", "old 2", 0.96)],
            [_make_duplicate("id-3", "old 3", 0.97)],
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        stats = engine.consolidate()

        self.assertEqual(stats["skipped_not_surprising"], 3)
        self.assertEqual(stats["facts_added"], 0)
        self.assertEqual(stats["facts_updated"], 0)

    def test_stats_dict_has_skipped_not_surprising_key(self):
        """stats 字典默认包含 skipped_not_surprising 键（值为 0）。"""
        llm_client = MagicMock()
        chroma_store = MagicMock()
        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
        )
        # 无待沉淀消息时返回默认 stats
        stats = engine.consolidate()
        self.assertIn("skipped_not_surprising", stats)
        self.assertEqual(stats["skipped_not_surprising"], 0)


# ===========================================================================
# 8. 日志记录正确（DEBUG 决策日志 + INFO 汇总日志）
# ===========================================================================


class TestSurpriseGateLogging(unittest.TestCase):
    """验证惊讶门控的 DEBUG 决策日志与 INFO 汇总日志。"""

    def test_skip_logs_debug_message(self):
        """跳过时记录 DEBUG 日志「惊讶门控：跳过不惊讶的事实」。"""
        facts_json = _make_facts_json(
            [{"content": "user likes Python", "type": "fact", "importance": 0.8}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("id-1", "user uses Python", 0.95)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        with self.assertLogs(
            "src.memory.consolidation", level="DEBUG"
        ) as cm:
            engine.consolidate()

        # 至少一条 DEBUG 日志包含「跳过不惊讶」
        skip_logs = [
            r for r in cm.records
            if "跳过不惊讶" in r.getMessage() and r.levelno == logging.DEBUG
        ]
        self.assertTrue(len(skip_logs) >= 1, "应记录跳过决策的 DEBUG 日志")

    def test_update_logs_debug_message(self):
        """更新时记录 DEBUG 日志「惊讶门控：更新冲突记忆」。"""
        facts_json = _make_facts_json(
            [{"content": "user is learning Rust", "type": "fact", "importance": 0.8}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("old-id", "user only uses Python", 0.87)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        with self.assertLogs(
            "src.memory.consolidation", level="DEBUG"
        ) as cm:
            engine.consolidate()

        update_logs = [
            r for r in cm.records
            if "更新冲突记忆" in r.getMessage() and r.levelno == logging.DEBUG
        ]
        self.assertTrue(len(update_logs) >= 1, "应记录更新决策的 DEBUG 日志")

    def test_add_logs_debug_message(self):
        """新增时记录 DEBUG 日志「惊讶门控：新增新知识」。"""
        facts_json = _make_facts_json(
            [{"content": "user has a cat", "type": "fact", "importance": 0.7}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = []

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        with self.assertLogs(
            "src.memory.consolidation", level="DEBUG"
        ) as cm:
            engine.consolidate()

        add_logs = [
            r for r in cm.records
            if "新增新知识" in r.getMessage() and r.levelno == logging.DEBUG
        ]
        self.assertTrue(len(add_logs) >= 1, "应记录新增决策的 DEBUG 日志")

    def test_info_summary_log_includes_skipped_count(self):
        """consolidate 结束时 INFO 日志包含 skipped_not_surprising 统计。"""
        facts_json = _make_facts_json(
            [{"content": "user likes Python", "type": "fact", "importance": 0.8}]
        )
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response(facts_json)

        chroma_store = MagicMock()
        chroma_store.find_duplicates.return_value = [
            _make_duplicate("id-1", "user uses Python", 0.95)
        ]

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            threshold=1,
            surprise_gate_enabled=True,
            surprise_similarity_threshold=0.85,
            surprise_skip_threshold=0.92,
        )
        engine.add_info({"role": "user", "content": "msg"})

        with self.assertLogs(
            "src.memory.consolidation", level="INFO"
        ) as cm:
            engine.consolidate()

        # INFO 汇总日志应包含「惊讶门控跳过」字样
        summary_logs = [
            r for r in cm.records
            if "惊讶门控跳过" in r.getMessage() and r.levelno == logging.INFO
        ]
        self.assertTrue(
            len(summary_logs) >= 1,
            "应记录包含 skipped_not_surprising 统计的 INFO 汇总日志",
        )


# ===========================================================================
# 9. 默认参数与配置
# ===========================================================================


class TestSurpriseGateDefaults(unittest.TestCase):
    """验证 ConsolidationEngine 惊讶门控默认参数。"""

    def test_default_params(self):
        """默认 surprise_gate_enabled=True, thresholds=0.85/0.92。"""
        engine = ConsolidationEngine(
            llm_client=MagicMock(),
            chroma_store=MagicMock(),
        )
        self.assertTrue(engine.surprise_gate_enabled)
        self.assertEqual(engine.surprise_similarity_threshold, 0.85)
        self.assertEqual(engine.surprise_skip_threshold, 0.92)

    def test_custom_params(self):
        """自定义参数正确存储。"""
        engine = ConsolidationEngine(
            llm_client=MagicMock(),
            chroma_store=MagicMock(),
            surprise_gate_enabled=False,
            surprise_similarity_threshold=0.70,
            surprise_skip_threshold=0.90,
        )
        self.assertFalse(engine.surprise_gate_enabled)
        self.assertEqual(engine.surprise_similarity_threshold, 0.70)
        self.assertEqual(engine.surprise_skip_threshold, 0.90)


if __name__ == "__main__":
    unittest.main(verbosity=2)
