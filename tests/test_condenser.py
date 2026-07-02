"""Condenser 历史压缩器单元测试。

覆盖 spec ``add-condenser-history-management`` 中所有压缩器场景：
- MaskingCondenser：未超阈值不压缩 / 正常压缩 / tool_use 保留 / tool_result
  占位符格式 / keep_first+keep_recent_n 保护区 / 纯文本保留 / tool_name
  映射缺失回退 unknown / 不修改入参 / 幂等
- LLMSummarizingCondenser：未超阈值不触发 LLM / 超阈值触发摘要 /
  LLM 失败降级 / 无 llm_client 降级 / 旧区为空跳过摘要
- create_condenser_from_config 工厂：None / 禁用 / masking / llm_summary /
  未知 strategy

运行方式：
    python -m unittest tests.test_condenser -v
    python tests/test_condenser.py
"""

from __future__ import annotations

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.memory.condenser import (  # noqa: E402
    Condenser,
    LLMSummarizingCondenser,
    MaskingCondenser,
    create_condenser_from_config,
)


# ---------------------------------------------------------------------------
# 测试夹具：构造含工具调用的典型消息序列
# ---------------------------------------------------------------------------


def _make_tool_messages():
    """构造 11 条消息：2 条 keep_first + 旧区（tool_use + tool_result + 文本）
    + 6 条 keep_recent_n。

    结构：
        [0] user 首条            ← keep_first
        [1] assistant 首条        ← keep_first
        [2] assistant tool_use    ← 旧区（应保留 tool_use 原样）
        [3] user tool_result      ← 旧区（应 masking）
        [4] user 中间文本         ← 旧区（纯文本保留）
        [5] assistant 中间回复    ← 旧区（纯文本保留）
        [6-11] 最近 6 条          ← keep_recent_n（完整保留）
    """
    return [
        {"role": "user", "content": "首条 user"},
        {"role": "assistant", "content": "首条 assistant"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "file_read",
             "input": {"path": "src/x.py"}}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "content": "x" * 5000}
        ]},
        {"role": "user", "content": "中间文本"},
        {"role": "assistant", "content": "中间回复"},
        {"role": "user", "content": "最近1"},
        {"role": "assistant", "content": "最近2"},
        {"role": "user", "content": "最近3"},
        {"role": "assistant", "content": "最近4"},
        {"role": "user", "content": "最近5"},
        {"role": "assistant", "content": "最近6"},
    ]


class _FakeLLM:
    """记录调用并返回固定摘要文本的 LLM mock。"""

    def __init__(self, summary: str = "摘要内容"):
        self.calls = []
        self._summary = summary

    def chat_consolidation(self, messages, system=None):
        self.calls.append({"messages": messages, "system": system})

        class _R:
            content = [{"type": "text", "text": self._summary}]

        return _R()


class _FailLLM:
    """始终抛异常的 LLM mock，用于测试降级。"""

    def chat_consolidation(self, messages, system=None):
        raise RuntimeError("LLM 故障")


# ---------------------------------------------------------------------------
# MaskingCondenser 测试
# ---------------------------------------------------------------------------


class TestMaskingCondenserNoCompressUnderThreshold(unittest.TestCase):
    """1. 消息数 ≤ keep_recent_n 时原样返回浅拷贝。"""

    def test_short_history_returns_shallow_copy(self):
        mc = MaskingCondenser(keep_recent_n=6, keep_first=2)
        small = [{"role": "user", "content": "hi"}] * 4
        out = mc.condense(small)

        self.assertEqual(len(out), 4)
        self.assertEqual(out[0]["content"], "hi")
        # 应返回新列表（浅拷贝），不与入参同对象
        self.assertIsNot(out, small)


class TestMaskingCondenserNormalCompression(unittest.TestCase):
    """2. 正常压缩：tool_result masking、tool_use 保留、保护区完整。"""

    def test_compresses_tool_result_and_preserves_tool_use(self):
        mc = MaskingCondenser(keep_recent_n=6, keep_first=2)
        msgs = _make_tool_messages()
        out = mc.condense(msgs)

        # 条数不变
        self.assertEqual(len(out), len(msgs))

        # keep_first：前 2 条完整保留
        self.assertEqual(out[0]["content"], "首条 user")
        self.assertEqual(out[1]["content"], "首条 assistant")

        # keep_recent_n：最后 6 条完整保留
        self.assertEqual(out[-1]["content"], "最近6")
        self.assertEqual(out[-6]["content"], "最近1")

        # 旧区 tool_use 原样保留（含 input 参数）
        tool_use_msg = out[2]
        self.assertIsInstance(tool_use_msg["content"], list)
        tu_block = tool_use_msg["content"][0]
        self.assertEqual(tu_block["type"], "tool_use")
        self.assertEqual(tu_block["name"], "file_read")
        self.assertEqual(tu_block["input"]["path"], "src/x.py")

        # 旧区纯文本消息完整保留
        self.assertEqual(out[4]["content"], "中间文本")
        self.assertEqual(out[5]["content"], "中间回复")


class TestMaskingCondenserToolResultPlaceholderFormat(unittest.TestCase):
    """3. tool_result 占位符格式：is_compressed + tool_name + char 数。"""

    def test_placeholder_format(self):
        mc = MaskingCondenser(keep_recent_n=6, keep_first=2)
        msgs = _make_tool_messages()
        out = mc.condense(msgs)

        tool_result_msg = out[3]
        self.assertIsInstance(tool_result_msg["content"], list)
        block = tool_result_msg["content"][0]

        # 类型与 tool_use_id 保留
        self.assertEqual(block["type"], "tool_result")
        self.assertEqual(block["tool_use_id"], "tu_1")
        # 标记为已压缩
        self.assertIs(block["is_compressed"], True)
        # 占位符包含工具名与原始字符数
        self.assertIn("file_read", block["content"])
        self.assertIn("5000 chars", block["content"])
        self.assertIn("已归档", block["content"])


class TestMaskingCondenserToolNameMissingFallback(unittest.TestCase):
    """4. tool_use_id 在 tool_use 块中找不到时回退 unknown。"""

    def test_unknown_tool_name_when_mapping_missing(self):
        mc = MaskingCondenser(keep_recent_n=6, keep_first=2)
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "missing_id",
                 "content": "y" * 100}
            ]},
            {"role": "user", "content": "r1"},
            {"role": "user", "content": "r2"},
            {"role": "user", "content": "r3"},
            {"role": "user", "content": "r4"},
            {"role": "user", "content": "r5"},
            {"role": "user", "content": "r6"},
        ]
        out = mc.condense(msgs)
        block = out[2]["content"][0]
        self.assertIn("unknown", block["content"])


class TestMaskingCondenserDoesNotMutateInput(unittest.TestCase):
    """5. condense 不修改入参列表与入参消息字典。"""

    def test_input_preserved_after_condense(self):
        mc = MaskingCondenser(keep_recent_n=6, keep_first=2)
        msgs = _make_tool_messages()
        # 深拷贝作为对照
        import copy
        original = copy.deepcopy(msgs)

        _ = mc.condense(msgs)

        # 入参列表本身未被修改
        self.assertEqual(msgs, original)
        # 特别是旧区 tool_result 的 content 仍是原始 5000 个 x
        self.assertEqual(msgs[3]["content"][0]["content"], "x" * 5000)


class TestMaskingCondenserIdempotent(unittest.TestCase):
    """6. 幂等：对已压缩结果再次 condense 不二次破坏。

    masking 后 tool_result 的 content 变为字符串占位符（不再是 list of
    blocks），二次 masking 时该消息 content 为 str，走纯文本保留分支。
    """

    def test_double_condense_stable(self):
        mc = MaskingCondenser(keep_recent_n=6, keep_first=2)
        msgs = _make_tool_messages()
        out1 = mc.condense(msgs)
        out2 = mc.condense(out1)

        # 二次压缩结果与一次压缩结果一致
        self.assertEqual(out1, out2)


class TestMaskingCondenserKeepsTextOnlyMessages(unittest.TestCase):
    """7. 旧区纯文本消息（content 为 str）完整保留。"""

    def test_text_messages_intact_in_old_zone(self):
        mc = MaskingCondenser(keep_recent_n=4, keep_first=1)
        msgs = [
            {"role": "user", "content": "首条"},
            {"role": "user", "content": "旧文本1"},
            {"role": "assistant", "content": "旧回复1"},
            {"role": "user", "content": "旧文本2"},
            {"role": "assistant", "content": "最近1"},
            {"role": "user", "content": "最近2"},
            {"role": "assistant", "content": "最近3"},
            {"role": "user", "content": "最近4"},
        ]
        out = mc.condense(msgs)

        # 旧区纯文本完整保留
        self.assertEqual(out[1]["content"], "旧文本1")
        self.assertEqual(out[2]["content"], "旧回复1")
        self.assertEqual(out[3]["content"], "旧文本2")


# ---------------------------------------------------------------------------
# LLMSummarizingCondenser 测试
# ---------------------------------------------------------------------------


class TestLLMSummarizingCondenserNoTriggerUnderThreshold(unittest.TestCase):
    """8. masking 后 token 数 ≤ 阈值时不触发 LLM，返回 masking 结果。"""

    def test_no_llm_call_under_threshold(self):
        fake = _FakeLLM()
        sc = LLMSummarizingCondenser(
            keep_recent_n=6, keep_first=2,
            llm_summary_threshold=100000, llm_client=fake,
        )
        msgs = _make_tool_messages()
        out = sc.condense(msgs)

        # 不应调用 LLM
        self.assertEqual(len(fake.calls), 0)
        # 结果与 masking 一致（tool_result 已压缩）
        self.assertEqual(len(out), len(msgs))
        self.assertIs(
            out[3]["content"][0].get("is_compressed"), True
        )


class TestLLMSummarizingCondenserTriggersSummary(unittest.TestCase):
    """9. masking 后 token 数超阈值时触发 LLM 摘要。

    结构：keep_first(2) + 摘要(1) + keep_recent_n(6) = 9 条。
    """

    def test_triggers_llm_summary_when_over_threshold(self):
        fake = _FakeLLM(summary="结构化摘要内容")
        sc = LLMSummarizingCondenser(
            keep_recent_n=6, keep_first=2,
            llm_summary_threshold=10, llm_client=fake,
        )
        msgs = _make_tool_messages()
        out = sc.condense(msgs)

        # 应调用 LLM 1 次
        self.assertEqual(len(fake.calls), 1)
        # 结构：2 + 1 + 6 = 9
        self.assertEqual(len(out), 9)
        # 摘要消息位于 keep_first 之后
        self.assertEqual(out[2]["role"], "user")
        self.assertIn("[历史摘要]", out[2]["content"])
        self.assertIn("结构化摘要内容", out[2]["content"])
        # keep_first 保留
        self.assertEqual(out[0]["content"], "首条 user")
        self.assertEqual(out[1]["content"], "首条 assistant")
        # keep_recent_n 保留
        self.assertEqual(out[-1]["content"], "最近6")


class TestLLMSummarizingCondenserFailureDegrades(unittest.TestCase):
    """10. LLM 调用失败时降级为纯 masking 结果（不抛异常）。"""

    def test_llm_failure_falls_back_to_masking(self):
        sc = LLMSummarizingCondenser(
            keep_recent_n=6, keep_first=2,
            llm_summary_threshold=10, llm_client=_FailLLM(),
        )
        msgs = _make_tool_messages()
        out = sc.condense(msgs)

        # 降级为 masking，条数与原 msgs 相同
        self.assertEqual(len(out), len(msgs))
        # tool_result 仍被 masking
        self.assertIs(
            out[3]["content"][0].get("is_compressed"), True
        )


class TestLLMSummarizingCondenserNoClientDegrades(unittest.TestCase):
    """11. llm_client=None 时即使超阈值也降级为 masking。"""

    def test_no_llm_client_falls_back_to_masking(self):
        sc = LLMSummarizingCondenser(
            keep_recent_n=6, keep_first=2,
            llm_summary_threshold=10, llm_client=None,
        )
        msgs = _make_tool_messages()
        out = sc.condense(msgs)

        # 降级为 masking，条数不变
        self.assertEqual(len(out), len(msgs))
        self.assertIs(
            out[3]["content"][0].get("is_compressed"), True
        )


class TestLLMSummarizingCondenserEmptyOldZone(unittest.TestCase):
    """12. 旧区为空（recent_start <= keep_first）时不触发摘要。"""

    def test_empty_old_zone_skips_summary(self):
        fake = _FakeLLM()
        # msgs 共 12 条，keep_recent_n=6 → recent_start = 12 - 6 = 6
        # 令 keep_first=6 → recent_start == keep_first，旧区 masked[6:6] 为空
        sc = LLMSummarizingCondenser(
            keep_recent_n=6, keep_first=6,
            llm_summary_threshold=10, llm_client=fake,
        )
        msgs = _make_tool_messages()
        out = sc.condense(msgs)

        # 旧区为空，不调用 LLM
        self.assertEqual(len(fake.calls), 0)
        # 返回 masking 结果
        self.assertEqual(len(out), len(msgs))


class TestLLMSummarizingCondenserTokenCounterError(unittest.TestCase):
    """13. token_counter 抛异常时降级为 masking（不触发 LLM）。"""

    def test_token_counter_error_falls_back_to_masking(self):
        fake = _FakeLLM()

        def bad_counter(messages):
            raise ValueError("counter 故障")

        sc = LLMSummarizingCondenser(
            keep_recent_n=6, keep_first=2,
            llm_summary_threshold=10, llm_client=fake,
            token_counter=bad_counter,
        )
        msgs = _make_tool_messages()
        out = sc.condense(msgs)

        # token 计数失败，跳过 LLM 摘要
        self.assertEqual(len(fake.calls), 0)
        self.assertEqual(len(out), len(msgs))


class TestLLMSummarizingCondenserEmptySummaryDegrades(unittest.TestCase):
    """14. LLM 返回空摘要时降级为 masking。"""

    def test_empty_summary_falls_back_to_masking(self):
        fake = _FakeLLM(summary="")
        sc = LLMSummarizingCondenser(
            keep_recent_n=6, keep_first=2,
            llm_summary_threshold=10, llm_client=fake,
        )
        msgs = _make_tool_messages()
        out = sc.condense(msgs)

        # LLM 被调用但返回空摘要，降级为 masking
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(out), len(msgs))


# ---------------------------------------------------------------------------
# create_condenser_from_config 工厂测试
# ---------------------------------------------------------------------------


class TestCreateCondenserFromConfig(unittest.TestCase):
    """15. 工厂函数：None / 禁用 / masking / llm_summary / 未知 strategy。"""

    def test_none_config_returns_none(self):
        self.assertIsNone(create_condenser_from_config(None))

    def test_disabled_returns_none(self):
        self.assertIsNone(
            create_condenser_from_config({"enabled": False})
        )

    def test_masking_strategy(self):
        c = create_condenser_from_config({
            "enabled": True, "strategy": "masking",
            "keep_recent_n": 8, "keep_first": 3,
        })
        self.assertIsInstance(c, MaskingCondenser)
        self.assertEqual(c.keep_recent_n, 8)
        self.assertEqual(c.keep_first, 3)

    def test_llm_summary_strategy(self):
        fake = _FakeLLM()
        c = create_condenser_from_config(
            {"enabled": True, "strategy": "llm_summary",
             "keep_recent_n": 7, "keep_first": 2,
             "llm_summary_threshold": 50000},
            llm_client=fake,
        )
        self.assertIsInstance(c, LLMSummarizingCondenser)
        self.assertEqual(c.keep_recent_n, 7)
        self.assertEqual(c.keep_first, 2)
        self.assertEqual(c.llm_summary_threshold, 50000)

    def test_unknown_strategy_returns_none(self):
        c = create_condenser_from_config({
            "enabled": True, "strategy": "unknown_strategy",
        })
        self.assertIsNone(c)

    def test_defaults_when_keys_missing(self):
        c = create_condenser_from_config({"enabled": True})
        # 缺省 strategy=masking
        self.assertIsInstance(c, MaskingCondenser)
        # 缺省 keep_recent_n=6, keep_first=2
        self.assertEqual(c.keep_recent_n, 6)
        self.assertEqual(c.keep_first, 2)


# ---------------------------------------------------------------------------
# Condenser 抽象基类测试
# ---------------------------------------------------------------------------


class TestCondenserAbstractBase(unittest.TestCase):
    """16. Condenser 基类 condense 未实现时抛 NotImplementedError。"""

    def test_base_condense_raises_not_implemented(self):
        base = Condenser()
        with self.assertRaises(NotImplementedError):
            base.condense([])


if __name__ == "__main__":
    unittest.main()
