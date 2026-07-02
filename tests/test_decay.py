"""MemoryDecay 三因子衰减计算单元测试（Phase 7 Task 1）。

覆盖：
1. recency 衰减正确性（老记忆 importance 下降）
2. frequency 强化正确性（高频记忆 importance 上升）
3. 三因子组合（recency × frequency × importance）
4. last_accessed 缺失时回退到当前时间（不衰减）
5. access_count 缺失时回退到 0（frequency_factor = 1）
6. 参数解析异常时回退到静态 importance
7. reinforce 后 decayed_importance 提升（集成 ChromaMemoryStore）
8. decay_rate / frequency_weight 参数影响

运行方式：
    python -m pytest tests/test_decay.py -v
    python -m unittest tests.test_decay -v
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 在导入 src 模块前，先为缺失的可选依赖（chromadb/numpy/sentence_transformers）注入 mock
from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.memory.decay import MemoryDecay  # noqa: E402
from src.storage.chroma_store import ChromaMemoryStore  # noqa: E402


# ===========================================================================
# Scenario 1: recency 衰减正确性
# ===========================================================================

class TestRecencyDecay(unittest.TestCase):
    """验证 recency_factor = exp(-days × decay_rate) 的计算正确性。"""

    def test_fresh_memory_no_decay(self):
        """刚写入的记忆（last_accessed = now）recency_factor ≈ 1，不衰减。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        # importance=0.8, last_accessed=now, access_count=0
        # recency_factor = exp(0) = 1, frequency_factor = log(1)*0.5+1 = 1
        # decayed = 0.8 * 1 * 1 = 0.8
        result = decay.decayed_importance(0.8, now_iso, 0)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="刚写入的记忆应不衰减")

    def test_old_memory_decays(self):
        """100 天前的记忆应被衰减：0.8 × exp(-1.0) ≈ 0.294。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        old_dt = datetime.now() - timedelta(days=100)
        old_iso = old_dt.isoformat()
        # recency_factor = exp(-100 * 0.01) = exp(-1) ≈ 0.3679
        # frequency_factor = log(1)*0.5+1 = 1
        # decayed = 0.8 * 0.3679 * 1 ≈ 0.2943
        result = decay.decayed_importance(0.8, old_iso, 0)
        expected = 0.8 * math.exp(-1.0)
        self.assertAlmostEqual(result, expected, places=5,
                               msg="100 天前的记忆应按 exp(-1) 衰减")
        self.assertLess(result, 0.8, "老记忆的 decayed_importance 应小于原 importance")

    def test_older_memory_decays_more(self):
        """越久没访问的记忆衰减越多（单调递减）。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now = datetime.now()
        # 1天、10天、100天、365天
        deltas = [1, 10, 100, 365]
        results = []
        for d in deltas:
            la = (now - timedelta(days=d)).isoformat()
            results.append(decay.decayed_importance(0.8, la, 0))
        # 验证单调递减
        for i in range(len(results) - 1):
            self.assertGreater(
                results[i], results[i + 1],
                f"{deltas[i]} 天前的记忆应比 {deltas[i+1]} 天前的记忆权重高"
            )


# ===========================================================================
# Scenario 2: frequency 强化正确性
# ===========================================================================

class TestFrequencyReinforce(unittest.TestCase):
    """验证 frequency_factor = log(1 + access_count) × frequency_weight + 1。"""

    def test_zero_access_count_no_boost(self):
        """access_count=0 时 frequency_factor = log(1)*0.5+1 = 1，不强化。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        result = decay.decayed_importance(0.8, now_iso, 0)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="access_count=0 时不应强化")

    def test_high_access_count_boosts(self):
        """access_count=20 时 frequency_factor ≈ log(21)*0.5+1 ≈ 2.514。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        # frequency_factor = log(1+20)*0.5+1 = log(21)*0.5+1 ≈ 3.0445*0.5+1 ≈ 2.522
        result = decay.decayed_importance(0.8, now_iso, 20)
        expected_ff = math.log(21) * 0.5 + 1.0
        expected = 0.8 * 1.0 * expected_ff  # recency_factor = 1 (fresh)
        self.assertAlmostEqual(result, expected, places=5,
                               msg="高频记忆应被 frequency_factor 强化")
        self.assertGreater(result, 0.8, "高频记忆的 decayed_importance 应大于原 importance")

    def test_frequency_monotonic_increase(self):
        """access_count 越大 frequency_factor 越大（单调递增）。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        counts = [0, 1, 5, 10, 20, 50, 100]
        results = [decay.decayed_importance(0.8, now_iso, c) for c in counts]
        for i in range(len(results) - 1):
            self.assertGreater(
                results[i + 1], results[i],
                f"access_count={counts[i+1]} 应比 {counts[i]} 强化更多"
            )


# ===========================================================================
# Scenario 3: 三因子组合
# ===========================================================================

class TestThreeFactorCombination(unittest.TestCase):
    """验证 importance × recency_factor × frequency_factor 三因子组合。"""

    def test_three_factor_combo(self):
        """构造场景：importance=0.8, 100天前, access_count=20。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        old_dt = datetime.now() - timedelta(days=100)
        old_iso = old_dt.isoformat()
        # recency_factor = exp(-100*0.01) = exp(-1) ≈ 0.3679
        # frequency_factor = log(21)*0.5+1 ≈ 2.522
        # decayed = 0.8 * 0.3679 * 2.522 ≈ 0.7424
        result = decay.decayed_importance(0.8, old_iso, 20)
        expected = 0.8 * math.exp(-1.0) * (math.log(21) * 0.5 + 1.0)
        self.assertAlmostEqual(result, expected, places=5,
                               msg="三因子组合应正确计算")

    def test_old_high_freq_beats_old_low_freq(self):
        """同样 100 天未访问，高频记忆（count=20）应优于低频记忆（count=0）。

        Spec 场景：C 的 frequency_factor ≈ 2.5，D 的 frequency_factor = 1.0，
        C 的 decayed_importance 是 D 的约 2.5 倍。
        """
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        old_dt = datetime.now() - timedelta(days=100)
        old_iso = old_dt.isoformat()
        c_val = decay.decayed_importance(0.8, old_iso, 20)
        d_val = decay.decayed_importance(0.8, old_iso, 0)
        # C 应大于 D
        self.assertGreater(c_val, d_val,
                           "高频老记忆应优于低频老记忆")
        # 比值应接近 frequency_factor 之比
        ratio = c_val / d_val
        expected_ratio = (math.log(21) * 0.5 + 1.0) / (math.log(1) * 0.5 + 1.0)
        self.assertAlmostEqual(ratio, expected_ratio, places=4,
                               msg="比值应等于 frequency_factor 之比")

    def test_fresh_low_importance_beats_old_high_importance(self):
        """Spec 场景：B（importance=0.5, 今天访问）应排在 A（importance=0.8, 100天前）前面。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        old_dt = datetime.now() - timedelta(days=100)
        old_iso = old_dt.isoformat()
        # A: importance=0.8, 100天前, count=0 → 0.8 * exp(-1) * 1 ≈ 0.294
        a_val = decay.decayed_importance(0.8, old_iso, 0)
        # B: importance=0.5, 今天, count=0 → 0.5 * 1 * 1 = 0.5
        b_val = decay.decayed_importance(0.5, now_iso, 0)
        self.assertGreater(b_val, a_val,
                           "新的低 importance 记忆应排在老的高 importance 记忆前面")


# ===========================================================================
# Scenario 4: last_accessed 缺失时回退
# ===========================================================================

class TestLastAccessedFallback(unittest.TestCase):
    """last_accessed 缺失或非法时回退到当前时间（不衰减）。"""

    def test_last_accessed_none(self):
        """last_accessed=None 时回退到当前时间，recency_factor=1。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        result = decay.decayed_importance(0.8, None, 0)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="last_accessed=None 应不衰减")

    def test_last_accessed_empty_string(self):
        """last_accessed='' 时回退到当前时间。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        result = decay.decayed_importance(0.8, "", 0)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="last_accessed='' 应不衰减")

    def test_last_accessed_invalid_string(self):
        """last_accessed 为非法字符串时回退到当前时间。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        result = decay.decayed_importance(0.8, "not-a-date", 0)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="非法 last_accessed 应不衰减")

    def test_last_accessed_datetime_object(self):
        """last_accessed 为 datetime 对象时正常解析。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        old_dt = datetime.now() - timedelta(days=100)
        # 传 datetime 对象而非字符串
        result = decay.decayed_importance(0.8, old_dt, 0)
        expected = 0.8 * math.exp(-1.0)
        self.assertAlmostEqual(result, expected, places=5,
                               msg="datetime 对象应被正确解析")


# ===========================================================================
# Scenario 5: access_count 缺失时回退
# ===========================================================================

class TestAccessCountFallback(unittest.TestCase):
    """access_count 缺失或非法时回退到 0（frequency_factor = 1）。"""

    def test_access_count_none(self):
        """access_count=None 时回退到 0。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        result = decay.decayed_importance(0.8, now_iso, None)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="access_count=None 时 frequency_factor 应为 1")

    def test_access_count_invalid_string(self):
        """access_count 为非数值字符串时回退到 0。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        result = decay.decayed_importance(0.8, now_iso, "abc")
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="非法 access_count 应回退到 0")

    def test_access_count_negative_clamped_to_zero(self):
        """access_count 为负数时回退到 0。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        result = decay.decayed_importance(0.8, now_iso, -5)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="负数 access_count 应回退到 0")

    def test_access_count_string_numeric(self):
        """access_count 为数字字符串时应被正确解析。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        # "20" 应被解析为 20
        result = decay.decayed_importance(0.8, now_iso, "20")
        expected_ff = math.log(21) * 0.5 + 1.0
        expected = 0.8 * expected_ff
        self.assertAlmostEqual(result, expected, places=5,
                               msg="数字字符串 access_count 应被正确解析")


# ===========================================================================
# Scenario 6: 参数解析异常时回退到静态 importance
# ===========================================================================

class TestExceptionFallback(unittest.TestCase):
    """参数解析异常时回退到静态 importance，保证排序稳定。"""

    def test_invalid_importance_falls_back(self):
        """importance 为非数值时回退到 0.5。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        # importance="high" 无法解析 → 回退到 0.5
        result = decay.decayed_importance("high", now_iso, 0)
        self.assertAlmostEqual(result, 0.5, places=5,
                               msg="非法 importance 应回退到 0.5")

    def test_importance_none_falls_back(self):
        """importance=None 时回退到 0.5。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        now_iso = datetime.now().isoformat()
        result = decay.decayed_importance(None, now_iso, 0)
        self.assertAlmostEqual(result, 0.5, places=5,
                               msg="importance=None 应回退到 0.5")

    def test_all_params_invalid_falls_back(self):
        """所有参数都非法时应回退到静态 importance（0.5）。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        # importance=None, last_accessed=None, access_count=None
        # importance 回退到 0.5, last_accessed 回退到 now, access_count 回退到 0
        # 结果应为 0.5 * 1 * 1 = 0.5
        result = decay.decayed_importance(None, None, None)
        self.assertAlmostEqual(result, 0.5, places=5,
                               msg="所有参数非法时应回退到 0.5")


# ===========================================================================
# Scenario 7: reinforce 后 decayed_importance 提升
# ===========================================================================

class TestReinforceImprovement(unittest.TestCase):
    """验证 reinforce 后记忆的 decayed_importance 提升。

    使用 ChromaMemoryStore（基于 mock chromadb）验证完整流程：
    1. 写入记忆（access_count=0, last_accessed=写入时间）
    2. 模拟时间流逝（构造一个 old timestamp 的记忆）
    3. reinforce 前计算 decayed_importance（应较低）
    4. 调用 reinforce（更新 last_accessed=now, access_count=1）
    5. reinforce 后计算 decayed_importance（应较高）
    """

    def setUp(self):
        """每个测试用例使用独立的 ChromaMemoryStore 实例。"""
        self._tmpdir = tempfile.mkdtemp(prefix="decay_test_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_reinforce_improves_decayed_importance(self):
        """reinforce 后 decayed_importance 应提升。"""
        # 1. 写入一条记忆，模拟 100 天前写入（手动构造老 metadata）
        old_dt = datetime.now() - timedelta(days=100)
        old_iso = old_dt.isoformat()
        memory_id = self.store.add_memory(
            "python 编程知识",
            metadata={
                "type": "fact",
                "importance": 0.8,
                "timestamp": old_iso,
                "last_accessed": old_iso,
                "access_count": 0,
            },
        )

        # 2. reinforce 前的 decayed_importance（100天前，count=0）
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        before = decay.decayed_importance(0.8, old_iso, 0)
        # before ≈ 0.8 * exp(-1) * 1 ≈ 0.294

        # 3. 调用 reinforce
        self.store.reinforce(memory_id)

        # 4. 读取 reinforce 后的 metadata
        all_memories = self.store.get_all_memories()
        target = None
        for m in all_memories:
            if m["id"] == memory_id:
                target = m
                break
        self.assertIsNotNone(target, "reinforce 后记忆应仍存在")
        new_meta = target["metadata"]
        self.assertEqual(int(new_meta["access_count"]), 1,
                         "reinforce 后 access_count 应为 1")
        # last_accessed 应被更新为当前时间（接近 now）
        new_last_accessed = new_meta["last_accessed"]
        new_dt = datetime.fromisoformat(new_last_accessed)
        # 容忍几秒延迟
        self.assertLess(
            abs((datetime.now() - new_dt).total_seconds()), 60,
            "reinforce 后 last_accessed 应接近当前时间"
        )

        # 5. reinforce 后的 decayed_importance
        after = decay.decayed_importance(0.8, new_last_accessed, 1)
        # after ≈ 0.8 * exp(0) * (log(2)*0.5+1) ≈ 0.8 * 1 * 1.346 ≈ 1.077

        # 6. 验证 after > before
        self.assertGreater(after, before,
                           "reinforce 后 decayed_importance 应提升")

    def test_reinforce_nonexistent_id_silent(self):
        """reinforce 不存在的 memory_id 应静默返回（不抛异常）。"""
        # 不应抛异常
        self.store.reinforce("nonexistent-id-12345")
        # 验证记忆列表未受影响（仍为空）
        all_memories = self.store.get_all_memories()
        self.assertEqual(len(all_memories), 0,
                         "reinforce 不存在的 id 不应影响记忆库")

    def test_reinforce_increments_access_count(self):
        """多次 reinforce 应递增 access_count。"""
        memory_id = self.store.add_memory(
            "python 编程知识",
            metadata={"type": "fact", "importance": 0.8},
        )
        # reinforce 3 次
        for _ in range(3):
            self.store.reinforce(memory_id)
        # 读取 access_count
        all_memories = self.store.get_all_memories()
        target = next(m for m in all_memories if m["id"] == memory_id)
        self.assertEqual(int(target["metadata"]["access_count"]), 3,
                         "3 次 reinforce 后 access_count 应为 3")


# ===========================================================================
# Scenario 8: decay_rate / frequency_weight 参数影响
# ===========================================================================

class TestParameterInfluence(unittest.TestCase):
    """验证 decay_rate / frequency_weight 参数对计算结果的影响。"""

    def test_decay_rate_zero_means_no_decay(self):
        """decay_rate=0 时 recency_factor=exp(0)=1，不衰减。"""
        decay = MemoryDecay(decay_rate=0.0, frequency_weight=0.5)
        old_dt = datetime.now() - timedelta(days=365)
        old_iso = old_dt.isoformat()
        result = decay.decayed_importance(0.8, old_iso, 0)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="decay_rate=0 时不应衰减")

    def test_higher_decay_rate_decays_more(self):
        """decay_rate 越大衰减越快。"""
        old_dt = datetime.now() - timedelta(days=100)
        old_iso = old_dt.isoformat()
        # decay_rate=0.01 vs decay_rate=0.05
        decay_slow = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        decay_fast = MemoryDecay(decay_rate=0.05, frequency_weight=0.5)
        slow_val = decay_slow.decayed_importance(0.8, old_iso, 0)
        fast_val = decay_fast.decayed_importance(0.8, old_iso, 0)
        self.assertGreater(slow_val, fast_val,
                           "decay_rate=0.01 应比 0.05 衰减更慢（值更高）")

    def test_frequency_weight_zero_means_no_boost(self):
        """frequency_weight=0 时 frequency_factor=log(1+count)*0+1=1，不强化。"""
        decay = MemoryDecay(decay_rate=0.01, frequency_weight=0.0)
        now_iso = datetime.now().isoformat()
        # 即使 access_count=20，frequency_factor 仍为 1
        result = decay.decayed_importance(0.8, now_iso, 20)
        self.assertAlmostEqual(result, 0.8, places=5,
                               msg="frequency_weight=0 时不应强化")

    def test_higher_frequency_weight_boosts_more(self):
        """frequency_weight 越大强化越显著。"""
        now_iso = datetime.now().isoformat()
        # frequency_weight=0.5 vs frequency_weight=2.0
        decay_low = MemoryDecay(decay_rate=0.01, frequency_weight=0.5)
        decay_high = MemoryDecay(decay_rate=0.01, frequency_weight=2.0)
        low_val = decay_low.decayed_importance(0.8, now_iso, 20)
        high_val = decay_high.decayed_importance(0.8, now_iso, 20)
        self.assertGreater(high_val, low_val,
                           "frequency_weight=2.0 应比 0.5 强化更多")

    def test_default_parameters_match_spec(self):
        """默认参数应为 decay_rate=0.01, frequency_weight=0.5。"""
        decay = MemoryDecay()
        self.assertEqual(decay.decay_rate, 0.01,
                         "默认 decay_rate 应为 0.01")
        self.assertEqual(decay.frequency_weight, 0.5,
                         "默认 frequency_weight 应为 0.5")


# ===========================================================================
# 辅助场景：query_memory 的 reinforce 参数
# ===========================================================================

class TestQueryMemoryReinforceParam(unittest.TestCase):
    """验证 query_memory 的 reinforce 参数行为。"""

    def setUp(self):
        """每个测试用例使用独立的 ChromaMemoryStore 实例。"""
        self._tmpdir = tempfile.mkdtemp(prefix="query_reinforce_test_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_query_reinforce_true_updates_access_count(self):
        """query_memory(reinforce=True) 应更新 access_count。"""
        self.store.add_memory("python 编程知识")
        # reinforce=True（默认）
        results = self.store.query_memory("python", top_k=5, reinforce=True)
        self.assertGreater(len(results), 0, "应检索到记忆")
        # 检查 access_count 是否被更新
        all_memories = self.store.get_all_memories()
        target = all_memories[0]
        self.assertEqual(int(target["metadata"]["access_count"]), 1,
                         "reinforce=True 后 access_count 应为 1")

    def test_query_reinforce_false_skips_update(self):
        """query_memory(reinforce=False) 不应更新 access_count。"""
        self.store.add_memory("python 编程知识")
        # reinforce=False
        results = self.store.query_memory("python", top_k=5, reinforce=False)
        self.assertGreater(len(results), 0, "应检索到记忆")
        # 检查 access_count 应仍为 0
        all_memories = self.store.get_all_memories()
        target = all_memories[0]
        self.assertEqual(int(target["metadata"]["access_count"]), 0,
                         "reinforce=False 时 access_count 应保持 0")

    def test_query_returns_id_field(self):
        """query_memory 返回结果应包含 id 字段（Phase 7 新增）。"""
        self.store.add_memory("python 编程知识")
        results = self.store.query_memory("python", top_k=5, reinforce=False)
        self.assertGreater(len(results), 0)
        for item in results:
            self.assertIn("id", item, "每条记忆应包含 id 字段")
            self.assertTrue(item["id"], "id 字段不应为空")


# ===========================================================================
# 辅助场景：add_memory 默认 metadata 字段
# ===========================================================================

class TestAddMemoryDefaultMetadata(unittest.TestCase):
    """验证 add_memory 默认补全 last_accessed / access_count 字段。"""

    def test_add_memory_sets_default_last_accessed(self):
        """add_memory 不传 last_accessed 时应默认设为 timestamp。"""
        store = ChromaMemoryStore(persist_path=tempfile.mkdtemp(prefix="add_test_"))
        memory_id = store.add_memory(
            "python 编程",
            metadata={"type": "fact", "importance": 0.8},
        )
        all_memories = store.get_all_memories()
        target = next(m for m in all_memories if m["id"] == memory_id)
        meta = target["metadata"]
        self.assertIn("last_accessed", meta, "应包含 last_accessed 字段")
        self.assertIn("access_count", meta, "应包含 access_count 字段")
        self.assertEqual(int(meta["access_count"]), 0,
                         "新记忆的 access_count 应为 0")
        # last_accessed 应等于 timestamp（写入时间）
        self.assertEqual(meta["last_accessed"], meta["timestamp"],
                         "last_accessed 应默认等于 timestamp")

    def test_add_memory_respects_explicit_last_accessed(self):
        """add_memory 传入 last_accessed 时应保留用户值。"""
        store = ChromaMemoryStore(persist_path=tempfile.mkdtemp(prefix="add_test_"))
        old_iso = (datetime.now() - timedelta(days=50)).isoformat()
        memory_id = store.add_memory(
            "python 编程",
            metadata={
                "type": "fact",
                "importance": 0.8,
                "last_accessed": old_iso,
                "access_count": 5,
            },
        )
        all_memories = store.get_all_memories()
        target = next(m for m in all_memories if m["id"] == memory_id)
        meta = target["metadata"]
        self.assertEqual(meta["last_accessed"], old_iso,
                         "应保留用户传入的 last_accessed")
        self.assertEqual(int(meta["access_count"]), 5,
                         "应保留用户传入的 access_count")


if __name__ == "__main__":
    unittest.main(verbosity=2)
