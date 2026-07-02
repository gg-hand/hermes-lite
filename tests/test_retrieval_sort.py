"""MemoryRetriever 排序逻辑测试（importance 参与检索排序）。

覆盖 P5 任务引入的"similarity 分桶 + 桶内 importance 降序"排序策略：

1. test_similarity_close_importance_decides - similarity 接近时 importance 决定顺序
2. test_similarity_gap_high_bucket_first     - similarity 差距大时高桶优先
3. test_truncation_drops_low_bucket_low_importance - 截断从低桶低 importance 末尾开始
4. test_importance_missing_defaults_to_half  - importance 缺失时默认 0.5 不报错
5. test_invalid_importance_falls_back        - 非数值 importance 容错回退

运行方式：
    python -m unittest tests.test_retrieval_sort -v
"""

from __future__ import annotations

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

# 将项目根目录加入 sys.path，使 from src.xxx import yyy 可用
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 在导入 src 模块前，先为缺失的可选依赖（chromadb/numpy/sentence_transformers）注入 mock
from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.memory.retrieval import MemoryRetriever, _sort_key, BUCKET_PRECISION  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助：构造 mock 依赖与记忆条目
# ---------------------------------------------------------------------------

def _make_retriever(max_memory_tokens: int = 1000) -> MemoryRetriever:
    """构造一个不依赖真实 LLM/向量库的 MemoryRetriever 实例。"""

    class MockChromaStore:
        def query_memory(self, query_text, top_k=5):
            return []

    class MockMemoryMdManager:
        def get_summary(self, query=None, max_tokens=500):
            return ""

    return MemoryRetriever(
        chroma_store=MockChromaStore(),
        memory_md_manager=MockMemoryMdManager(),
        max_memory_tokens=max_memory_tokens,
    )


def _make_memory(content: str, similarity: float, importance: float):
    """构造一条记忆条目，importance 放在 metadata.importance 中。"""
    return {
        "content": content,
        "metadata": {"type": "fact", "importance": importance},
        "similarity": similarity,
    }


def _extract_ordered_contents(text: str):
    """从 format_for_prompt 输出中按行提取记忆内容顺序。

    输出格式形如：
        ## 相关记忆
        1. 内容A (相关度: 0.82)
        2. 内容B (相关度: 0.81)
    提取 ["内容A", "内容B"]。
    """
    lines = text.splitlines()
    contents = []
    for line in lines:
        # 形如 "1. 内容A (相关度: 0.82)"
        stripped = line.strip()
        if not stripped or stripped.startswith("##"):
            continue
        # 去掉前导序号 "N. "
        dot_pos = stripped.find(". ")
        if dot_pos < 0:
            continue
        rest = stripped[dot_pos + 2:]
        # 去掉末尾的 " (相关度: x.xx)"
        paren_pos = rest.rfind(" (相关度:")
        if paren_pos >= 0:
            rest = rest[:paren_pos]
        contents.append(rest)
    return contents


# ===========================================================================
# Scenario 1: similarity 接近时 importance 决定顺序
# ===========================================================================

class TestSimilarityCloseImportanceDecides(unittest.TestCase):
    """similarity 在同一桶内时，importance 高的排在前面。"""

    def test_same_bucket_importance_orders(self):
        """0.82 与 0.81 同属 0.80 桶，importance 0.9 的 B 应排在 importance 0.5 的 A 前。"""
        retriever = _make_retriever()
        mem_a = _make_memory("记忆A", similarity=0.82, importance=0.5)
        mem_b = _make_memory("记忆B", similarity=0.81, importance=0.9)
        result = retriever.format_for_prompt({
            "long_term_memories": [mem_a, mem_b],
            "user_profile_summary": "",
            "total_tokens": 0,
        })

        ordered = _extract_ordered_contents(result)
        self.assertEqual(len(ordered), 2, "应输出两条记忆")
        # 期望 B 在 A 前面：同桶内 importance 高的优先
        self.assertEqual(ordered[0], "记忆B", "同桶内 importance 0.9 的 B 应排在前面")
        self.assertEqual(ordered[1], "记忆A", "同桶内 importance 0.5 的 A 应排在后面")

    def test_sort_key_same_bucket(self):
        """直接验证 _sort_key 在同桶内按 importance 比较。"""
        mem_a = _make_memory("记忆A", similarity=0.82, importance=0.5)
        mem_b = _make_memory("记忆B", similarity=0.81, importance=0.9)
        # 0.82/0.05=16.4 → round=16；0.81/0.05=16.2 → round=16，同桶
        self.assertEqual(_sort_key(mem_a)[0], _sort_key(mem_b)[0], "应同属 0.80 桶")
        # importance 比较：B(0.9) > A(0.5)
        self.assertGreater(_sort_key(mem_b)[1], _sort_key(mem_a)[1])


# ===========================================================================
# Scenario 2: similarity 差距大时高桶优先
# ===========================================================================

class TestSimilarityGapHighBucketFirst(unittest.TestCase):
    """similarity 差距大时，高桶整体优先于低桶，importance 不影响桶间顺序。"""

    def test_high_bucket_beats_low_bucket(self):
        """0.90 属桶 18，0.70 属桶 14，A 应排在 B 前，尽管 A 的 importance(0.3) < B 的(1.0)。"""
        retriever = _make_retriever()
        mem_a = _make_memory("记忆A", similarity=0.90, importance=0.3)
        mem_b = _make_memory("记忆B", similarity=0.70, importance=1.0)
        result = retriever.format_for_prompt({
            "long_term_memories": [mem_a, mem_b],
            "user_profile_summary": "",
            "total_tokens": 0,
        })

        ordered = _extract_ordered_contents(result)
        self.assertEqual(len(ordered), 2, "应输出两条记忆")
        # 期望 A 在 B 前面：高桶优先，importance 不影响桶间顺序
        self.assertEqual(ordered[0], "记忆A", "高桶(0.90)应优先于低桶(0.70)")
        self.assertEqual(ordered[1], "记忆B", "低桶(0.70)应在高桶(0.90)之后")

    def test_sort_key_bucket_dominates(self):
        """直接验证 _sort_key 的桶值优先于 importance。"""
        mem_a = _make_memory("记忆A", similarity=0.90, importance=0.3)
        mem_b = _make_memory("记忆B", similarity=0.70, importance=1.0)
        # 0.90/0.05=18；0.70/0.05=14，A 桶高于 B 桶
        self.assertGreater(_sort_key(mem_a)[0], _sort_key(mem_b)[0], "A 桶值应高于 B")
        # 排序后 A 在前
        ordered = sorted([mem_a, mem_b], key=_sort_key, reverse=True)
        self.assertEqual(ordered[0]["content"], "记忆A")
        self.assertEqual(ordered[1]["content"], "记忆B")


# ===========================================================================
# Scenario 3: 截断从低桶低 importance 末尾开始
# ===========================================================================

class TestTruncationDropsLowBucketLowImportance(unittest.TestCase):
    """max_memory_tokens 不足时，从末尾（低桶 + 低 importance）开始剔除。"""

    def test_truncation_keeps_high_priority(self):
        """5 条记忆，max_memory_tokens 设小，被剔除的应是低桶低 importance 的。

        构造：
          - 记忆 HIGH_SIM_HIGH_IMP: sim=0.90, imp=0.9 （桶 18，最高优先级）
          - 记忆 HIGH_SIM_LOW_IMP:  sim=0.88, imp=0.2 （桶 18，桶内低优先级）
          - 记忆 MID_SIM_HIGH_IMP:  sim=0.70, imp=0.9 （桶 14）
          - 记忆 MID_SIM_LOW_IMP:   sim=0.69, imp=0.2 （桶 14，桶内低优先级）
          - 记忆 LOW_SIM_LOW_IMP:   sim=0.50, imp=0.1 （桶 10，最低优先级）

        max_memory_tokens 设为 30（字符数估算约 90 字符内只能保留 1~2 条），
        验证保留的列表中至少包含 HIGH_SIM_HIGH_IMP，且 LOW_SIM_LOW_IMP 应被剔除。
        """
        retriever = _make_retriever(max_memory_tokens=30)
        memories = [
            _make_memory("LOW_SIM_LOW_IMP", similarity=0.50, importance=0.1),
            _make_memory("HIGH_SIM_LOW_IMP", similarity=0.88, importance=0.2),
            _make_memory("HIGH_SIM_HIGH_IMP", similarity=0.90, importance=0.9),
            _make_memory("MID_SIM_LOW_IMP", similarity=0.69, importance=0.2),
            _make_memory("MID_SIM_HIGH_IMP", similarity=0.70, importance=0.9),
        ]
        result = retriever.format_for_prompt({
            "long_term_memories": memories,
            "user_profile_summary": "",
            "total_tokens": 0,
        })

        ordered = _extract_ordered_contents(result)
        # 30 token 远小于全量输出，必然发生截断
        self.assertLess(len(ordered), 5, "应发生截断，保留数量小于 5")
        # 最高优先级 HIGH_SIM_HIGH_IMP 必须保留且排第一
        self.assertGreater(len(ordered), 0, "至少应保留一条记忆")
        self.assertEqual(ordered[0], "HIGH_SIM_HIGH_IMP",
                         "最高优先级记忆应排在第一位且被保留")
        # 最低优先级 LOW_SIM_LOW_IMP 应被剔除
        self.assertNotIn("LOW_SIM_LOW_IMP", ordered,
                         "最低桶最低 importance 的记忆应被截断剔除")

    def test_truncation_order_preserved(self):
        """截断后剩余记忆仍保持"高桶优先 + 桶内高 importance 优先"顺序。"""
        retriever = _make_retriever(max_memory_tokens=60)
        memories = [
            _make_memory("mem_low", similarity=0.50, importance=0.1),
            _make_memory("mem_high_sim_low_imp", similarity=0.88, importance=0.2),
            _make_memory("mem_high_sim_high_imp", similarity=0.90, importance=0.9),
            _make_memory("mem_mid_sim_high_imp", similarity=0.70, importance=0.9),
            _make_memory("mem_mid_sim_low_imp", similarity=0.69, importance=0.2),
        ]
        result = retriever.format_for_prompt({
            "long_term_memories": memories,
            "user_profile_summary": "",
            "total_tokens": 0,
        })

        ordered = _extract_ordered_contents(result)
        # 验证保留的记忆是按桶+importance 降序排列的
        # 即排在前的记忆其 _sort_key 应不小于排在后的
        original_by_content = {m["content"]: m for m in memories}
        keys = [_sort_key(original_by_content[c]) for c in ordered]
        for i in range(len(keys) - 1):
            self.assertGreaterEqual(
                keys[i], keys[i + 1],
                f"位置 {i} 的记忆 ({ordered[i]}) 排序键应不小于位置 {i + 1} ({ordered[i + 1]})",
            )


# ===========================================================================
# 辅助场景：importance 缺失 / 非法值容错
# ===========================================================================

class TestImportanceDefaultsAndFallback(unittest.TestCase):
    """importance 缺失或非法时，_sort_key 应容错回退，不抛异常。"""

    def test_importance_missing_defaults_to_half(self):
        """metadata 中无 importance 字段时默认 0.5，排序正常进行。"""
        mem = {
            "content": "无importance记忆",
            "metadata": {"type": "fact"},
            "similarity": 0.80,
        }
        bucket, imp = _sort_key(mem)
        self.assertEqual(imp, 0.5, "importance 缺失时应默认 0.5")
        # 不应抛异常
        retriever = _make_retriever()
        result = retriever.format_for_prompt({
            "long_term_memories": [mem],
            "user_profile_summary": "",
            "total_tokens": 0,
        })
        self.assertIn("无importance记忆", result)

    def test_metadata_missing_defaults_to_half(self):
        """无 metadata 字段时 importance 默认 0.5。"""
        mem = {"content": "无metadata记忆", "similarity": 0.80}
        _, imp = _sort_key(mem)
        self.assertEqual(imp, 0.5, "metadata 缺失时 importance 应默认 0.5")

    def test_invalid_importance_falls_back(self):
        """importance 为非数值（字符串）时回退到 0.5。"""
        mem = {
            "content": "非法importance记忆",
            "metadata": {"type": "fact", "importance": "high"},
            "similarity": 0.80,
        }
        _, imp = _sort_key(mem)
        self.assertEqual(imp, 0.5, "非法 importance 应回退到 0.5")

    def test_invalid_similarity_falls_back(self):
        """similarity 为非数值时回退到 0.0（桶 0）。"""
        mem = {
            "content": "非法similarity记忆",
            "metadata": {"type": "fact", "importance": 0.8},
            "similarity": "N/A",
        }
        bucket, _ = _sort_key(mem)
        self.assertEqual(bucket, 0, "非法 similarity 应回退到桶 0")


# ===========================================================================
# 辅助场景：模块级常量与默认行为回归
# ===========================================================================

class TestBucketPrecisionConstant(unittest.TestCase):
    """验证模块级常量 BUCKET_PRECISION 已定义且为 0.05。"""

    def test_bucket_precision_value(self):
        self.assertEqual(BUCKET_PRECISION, 0.05,
                         "BUCKET_PRECISION 应为 0.05")

    def test_bucket_boundary_examples(self):
        """验证关键边界桶值：0.82/0.81 同桶，0.90/0.70 不同桶。"""
        # 0.82/0.05 = 16.4 → round = 16；0.81/0.05 = 16.2 → round = 16
        self.assertEqual(_sort_key(_make_memory("a", 0.82, 0.5))[0],
                         _sort_key(_make_memory("b", 0.81, 0.5))[0],
                         "0.82 与 0.81 应同属一桶")
        # 0.90/0.05 = 18；0.70/0.05 = 14
        self.assertNotEqual(_sort_key(_make_memory("a", 0.90, 0.5))[0],
                            _sort_key(_make_memory("b", 0.70, 0.5))[0],
                            "0.90 与 0.70 应分属不同桶")


if __name__ == "__main__":
    unittest.main(verbosity=2)
