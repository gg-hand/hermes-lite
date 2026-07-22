"""OutputFilter 单元测试 — 验证输出侧 PII 过滤器的检测与替换逻辑。

覆盖 Phase 9 Task 3 spec 中所有场景：
- 各类 PII 检测与替换（手机号 / 身份证 / 邮箱 / 银行卡）
- 无 PII 时不修改文本
- 误报订单号接受（11 位数字如 13800138000 被匹配为手机号）
- base64 / 二进制内容跳过（非打印字符占比 >30%）
- 多个 PII 同时存在
- PII 在文本中间位置
- 空字符串（返回原文，0 替换）
- SYSTEM_PROMPT 泄漏检测（可选）

运行方式:
    python -m pytest tests/test_output_filter.py -v
    python -m unittest tests.test_output_filter -v
    python tests/test_output_filter.py
"""

from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.guardrails.output_filter import OutputFilter  # noqa: E402


class TestOutputFilterPhone(unittest.TestCase):
    """手机号 PII 检测与替换。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_phone_basic_replacement(self):
        """标准手机号 13800138000 → 替换为 [手机号已脱敏]。"""
        filtered, n = self.f.filter("联系我: 13800138000")
        self.assertEqual(filtered, "联系我: [手机号已脱敏]")
        self.assertEqual(n, 1)

    def test_phone_at_start(self):
        """手机号在文本开头 → 替换。"""
        filtered, n = self.f.filter("13800138000 是我的手机号")
        self.assertEqual(filtered, "[手机号已脱敏] 是我的手机号")
        self.assertEqual(n, 1)

    def test_phone_in_middle(self):
        """手机号在文本中间 → 替换且前后文本保留。"""
        filtered, n = self.f.filter(
            "请回复邮件或拨打 13800138000 联系我"
        )
        self.assertEqual(
            filtered, "请回复邮件或拨打 [手机号已脱敏] 联系我"
        )
        self.assertEqual(n, 1)

    def test_phone_multiple(self):
        """多个手机号同时存在 → 全部替换，计数正确。"""
        filtered, n = self.f.filter(
            "电话1: 13800138000, 电话2: 15912345678"
        )
        self.assertEqual(
            filtered,
            "电话1: [手机号已脱敏], 电话2: [手机号已脱敏]",
        )
        self.assertEqual(n, 2)

    def test_phone_order_number_false_positive_accepted(self):
        """11 位数字订单号 13800138000 会被匹配为手机号 — 误报可接受。

        spec 明确：误报订单号接受（脱敏优于漏报）。11 位数字
        ``13800138000`` 在结构上与手机号无法区分，统一脱敏。
        """
        filtered, n = self.f.filter("订单号: 13800138000 已发货")
        self.assertEqual(filtered, "订单号: [手机号已脱敏] 已发货")
        self.assertEqual(n, 1)

    def test_phone_prefix_15_to_19(self):
        """号段 15x / 18x / 19x 均匹配（1[3-9] 前缀）。"""
        for phone in ("15012345678", "18812345678", "19912345678"):
            with self.subTest(phone=phone):
                filtered, n = self.f.filter(phone)
                self.assertEqual(filtered, "[手机号已脱敏]")
                self.assertEqual(n, 1)

    def test_phone_invalid_prefix_not_matched(self):
        """号段 1[0-2] 不匹配（如 12012345678 / 11012345678）。"""
        # 11 位数字但不匹配 1[3-9]\d{9}：120 开头不匹配
        # 注：11012345678 不匹配手机号正则，但会匹配银行卡正则（11 位 < 16，也不匹配）
        filtered, n = self.f.filter("编号 12012345678")
        self.assertEqual(filtered, "编号 12012345678")
        self.assertEqual(n, 0)


class TestOutputFilterIdCard(unittest.TestCase):
    """身份证 PII 检测与替换。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_id_card_basic_replacement(self):
        """标准身份证 11010119900101001X → 替换为 [身份证已脱敏]。"""
        filtered, n = self.f.filter("身份证号: 11010119900101001X")
        self.assertEqual(filtered, "身份证号: [身份证已脱敏]")
        self.assertEqual(n, 1)

    def test_id_card_lowercase_x(self):
        """身份证末位小写 x 也匹配。"""
        filtered, n = self.f.filter("ID: 11010119900101001x")
        self.assertEqual(filtered, "ID: [身份证已脱敏]")
        self.assertEqual(n, 1)

    def test_id_card_all_digits(self):
        """身份证末位为数字（非 X）也匹配（\d{17}[\dXx] 含数字）。"""
        filtered, n = self.f.filter("身份证: 110101199001010018")
        self.assertEqual(filtered, "身份证: [身份证已脱敏]")
        self.assertEqual(n, 1)

    def test_id_card_not_truncated_as_phone(self):
        """身份证不被截断为手机号（特异性优先：身份证先于手机号匹配）。

        以 130101 开头的身份证 13010119900101001X，若手机号先匹配
        会取走前 11 位（13010119900）。验证身份证优先匹配后整体替换。
        """
        filtered, n = self.f.filter("ID: 13010119900101001X")
        self.assertEqual(filtered, "ID: [身份证已脱敏]")
        self.assertEqual(n, 1)


class TestOutputFilterEmail(unittest.TestCase):
    """邮箱 PII 检测与替换。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_email_basic_replacement(self):
        """标准邮箱 test@example.com → 替换为 [邮箱已脱敏]。"""
        filtered, n = self.f.filter("邮箱: test@example.com")
        self.assertEqual(filtered, "邮箱: [邮箱已脱敏]")
        self.assertEqual(n, 1)

    def test_email_with_plus_sign(self):
        """邮箱本地部分含 + → 匹配（如 test+label@example.com）。"""
        filtered, n = self.f.filter("邮箱: test+label@example.com")
        self.assertEqual(filtered, "邮箱: [邮箱已脱敏]")
        self.assertEqual(n, 1)

    def test_email_with_dots_in_domain(self):
        """邮箱域名含多点（如 test@mail.example.com）→ 匹配。"""
        filtered, n = self.f.filter("邮箱: test@mail.example.com")
        self.assertEqual(filtered, "邮箱: [邮箱已脱敏]")
        self.assertEqual(n, 1)

    def test_email_with_underscore(self):
        """邮箱本地部分含下划线 → 匹配（\w 含 _）。"""
        filtered, n = self.f.filter("邮箱: test_user@example.com")
        self.assertEqual(filtered, "邮箱: [邮箱已脱敏]")
        self.assertEqual(n, 1)

    def test_email_in_sentence(self):
        """邮箱在句子中 → 替换且前后文本保留。"""
        filtered, n = self.f.filter(
            "请把结果发送到 test@example.com 谢谢"
        )
        self.assertEqual(
            filtered, "请把结果发送到 [邮箱已脱敏] 谢谢"
        )
        self.assertEqual(n, 1)


class TestOutputFilterBankCard(unittest.TestCase):
    """银行卡 PII 检测与替换（可选功能）。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_bank_card_16_digits_replacement(self):
        """16 位银行卡号 → 替换为 [银行卡已脱敏]。"""
        # 注：18 位数字会被身份证正则（\d{17}[\dXx]）优先匹配为身份证，
        # 此处用 16 位确保银行卡正则生效。
        filtered, n = self.f.filter("卡号: 6225880212345678")
        self.assertEqual(filtered, "卡号: [银行卡已脱敏]")
        self.assertEqual(n, 1)

    def test_bank_card_disabled(self):
        """enable_bank_card=False 时不检测银行卡。"""
        f = OutputFilter(enable_bank_card=False)
        filtered, n = f.filter("卡号: 6225880212345678")
        self.assertEqual(filtered, "卡号: 6225880212345678")
        self.assertEqual(n, 0)


class TestOutputFilterNoPII(unittest.TestCase):
    """无 PII 文本应原样返回。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_no_pii_text_unchanged(self):
        """无 PII 文本不修改，返回 0 替换。"""
        text = "这是一段普通文本，不包含任何 PII。"
        filtered, n = self.f.filter(text)
        self.assertEqual(filtered, text)
        self.assertEqual(n, 0)

    def test_short_digits_not_matched(self):
        """短数字（<11 位）不被匹配为手机号 / 银行卡。"""
        filtered, n = self.f.filter("订单: 12345")
        self.assertEqual(filtered, "订单: 12345")
        self.assertEqual(n, 0)

    def test_10_digit_number_not_phone(self):
        """10 位数字不匹配手机号（手机号需 11 位）。"""
        filtered, n = self.f.filter("编号: 1380013800")  # 仅 10 位
        self.assertEqual(filtered, "编号: 1380013800")
        self.assertEqual(n, 0)

    def test_text_without_at_sign_not_email(self):
        """无 @ 符号的字符串不匹配邮箱。"""
        filtered, n = self.f.filter("联系 example.com")
        self.assertEqual(filtered, "联系 example.com")
        self.assertEqual(n, 0)


class TestOutputFilterEmpty(unittest.TestCase):
    """空字符串与 None 输入处理。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_empty_string_returns_empty(self):
        """空字符串 → 返回空字符串，0 替换。"""
        filtered, n = self.f.filter("")
        self.assertEqual(filtered, "")
        self.assertEqual(n, 0)

    def test_none_returns_none(self):
        """None 输入 → 返回 None，0 替换（防御性处理）。"""
        filtered, n = self.f.filter(None)  # type: ignore[arg-type]
        self.assertIsNone(filtered)
        self.assertEqual(n, 0)


class TestOutputFilterMultiplePII(unittest.TestCase):
    """多种 PII 同时存在的混合检测。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_multiple_pii_types_in_one_text(self):
        """同一文本含手机号 + 身份证 + 邮箱 → 全部替换，计数累加。"""
        text = (
            "电话 13800138000，身份证 11010119900101001X，"
            "邮箱 test@example.com"
        )
        filtered, n = self.f.filter(text)
        self.assertEqual(
            filtered,
            "电话 [手机号已脱敏]，身份证 [身份证已脱敏]，"
            "邮箱 [邮箱已脱敏]",
        )
        self.assertEqual(n, 3)

    def test_multiple_pii_with_context(self):
        """PII 嵌入自然语言上下文 → 替换且上下文保留。"""
        text = (
            "用户信息：手机 13800138000 已注册，"
            "备用邮箱 test@example.com，"
            "请勿泄漏身份证 11010119900101001X。"
        )
        filtered, n = self.f.filter(text)
        self.assertEqual(
            filtered,
            "用户信息：手机 [手机号已脱敏] 已注册，"
            "备用邮箱 [邮箱已脱敏]，"
            "请勿泄漏身份证 [身份证已脱敏]。",
        )
        self.assertEqual(n, 3)

    def test_pii_at_text_boundaries(self):
        """PII 在文本开头与结尾同时存在 → 边界替换正确。"""
        text = "13800138000 开头，结尾 test@example.com"
        filtered, n = self.f.filter(text)
        self.assertEqual(
            filtered,
            "[手机号已脱敏] 开头，结尾 [邮箱已脱敏]",
        )
        self.assertEqual(n, 2)


class TestOutputFilterBinaryContent(unittest.TestCase):
    """base64 / 二进制内容跳过 PII 检测。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_binary_content_skipped(self):
        """非打印字符占比 >30% 的二进制内容 → 跳过 PII 正则。

        构造一个含手机号但非打印字符占比 >30% 的字符串（模拟 base64
        编码的字节流），验证不被替换。
        """
        # 1 个可打印字符（手机号开头 '1'）+ 10 个非打印字符
        # 非打印占比 10/11 ≈ 91% > 30%，且整体含 PII 片段
        # 但因占比超阈，整个文本被视为二进制，跳过 PII 检测
        binary_text = "13800138000" + "\x00\x01\x02\x03\x04\x05\x06"
        # 非打印字符占比：7 个非打印 / 18 总长 ≈ 39% > 30%
        filtered, n = self.f.filter(binary_text)
        self.assertEqual(filtered, binary_text)
        self.assertEqual(n, 0)

    def test_mostly_non_printable_skipped(self):
        """几乎全是非打印字符 → 跳过 PII 正则。"""
        # 仅 1 个可打印字符 + 大量非打印字符
        text = "a" + "\x00" * 10
        filtered, n = self.f.filter(text)
        self.assertEqual(filtered, text)
        self.assertEqual(n, 0)

    def test_printable_text_with_pii_not_skipped(self):
        """可打印文本（含 PII）不被误判为二进制 → 正常替换。"""
        # 全部为可打印字符，非打印占比 0%
        text = "电话: 13800138000"
        filtered, n = self.f.filter(text)
        self.assertEqual(filtered, "电话: [手机号已脱敏]")
        self.assertEqual(n, 1)

    def test_text_with_newlines_not_treated_as_binary(self):
        """含换行符的正常文本不视为二进制（\\t \\n \\r 视为可打印）。"""
        text = "行1\n电话: 13800138000\n行3"
        filtered, n = self.f.filter(text)
        self.assertEqual(filtered, "行1\n电话: [手机号已脱敏]\n行3")
        self.assertEqual(n, 1)

    def test_threshold_boundary_just_below(self):
        """非打印占比恰好低于 30% → 不跳过，正常 PII 检测。

        构造 7 个可打印 + 3 个非打印 = 30% 严格不大于阈值（>30% 才跳过）。
        """
        # 7 可打印 + 3 非打印 = 30%，不满足 >30%，应正常检测 PII
        # 用 11 位手机号（13800138000）+ 5 非打印 = 5/16 ≈ 31.25% > 30%
        # 改为：6 非打印 + 14 可打印（含 PII）= 6/20 = 30%，不跳过
        text = "电话13800138000ab" + "\x00\x01\x02\x03\x04\x05"
        # 总长 20，非打印 6，占比 30%（不 > 30%）→ 正常检测
        filtered, n = self.f.filter(text)
        # 手机号 13800138000 应被替换
        self.assertIn("[手机号已脱敏]", filtered)
        self.assertEqual(n, 1)


class TestOutputFilterPromptLeakage(unittest.TestCase):
    """SYSTEM_PROMPT 泄漏检测（detect_prompt_leakage）。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_leakage_detected_with_both_markers(self):
        """响应同时含"指令优先级"与"Teage Liu" → 判定泄漏。"""
        text = (
            "## 指令优先级\n按以下优先级处理...\n"
            "你是一个 AI Agent，名为 Teage Liu。"
        )
        self.assertTrue(self.f.detect_prompt_leakage(text))

    def test_no_leakage_without_priority_marker(self):
        """响应仅含"Teage Liu"但无"指令优先级" → 不判定泄漏。"""
        text = "Teage Liu 是一个 AI Agent 项目。"
        self.assertFalse(self.f.detect_prompt_leakage(text))

    def test_no_leakage_without_hermes_marker(self):
        """响应仅含"指令优先级"但无"Teage Liu" → 不判定泄漏。"""
        text = "任务优先级 vs 指令优先级，需要讨论。"
        self.assertFalse(self.f.detect_prompt_leakage(text))

    def test_no_leakage_normal_text(self):
        """普通文本 → 不判定泄漏。"""
        text = "今天天气不错，适合出门。"
        self.assertFalse(self.f.detect_prompt_leakage(text))

    def test_empty_text_no_leakage(self):
        """空字符串 → 不判定泄漏。"""
        self.assertFalse(self.f.detect_prompt_leakage(""))
        self.assertFalse(self.f.detect_prompt_leakage(None))  # type: ignore[arg-type]


class TestOutputFilterIntegration(unittest.TestCase):
    """集成场景：filter 与 detect_prompt_leakage 协同工作。"""

    def setUp(self) -> None:
        self.f = OutputFilter()

    def test_filter_then_leakage_check(self):
        """先 filter 再 detect_prompt_leakage：泄漏文本含 PII 时双重防护。

        模型返回含 SYSTEM_PROMPT 片段 + 用户 PII 的响应：
        1. filter 先替换 PII（手机号 / 邮箱）
        2. detect_prompt_leakage 仍能检测到 SYSTEM_PROMPT 泄漏
        （因占位符不影响标志性短语）
        """
        text = (
            "## 指令优先级\n联系 13800138000 或 test@example.com。\n"
            "名为 Teage Liu 的 Agent。"
        )
        filtered, n = self.f.filter(text)
        # PII 已替换
        self.assertIn("[手机号已脱敏]", filtered)
        self.assertIn("[邮箱已脱敏]", filtered)
        self.assertEqual(n, 2)
        # SYSTEM_PROMPT 泄漏仍可检测（标志性短语未被 PII 替换影响）
        self.assertTrue(self.f.detect_prompt_leakage(filtered))

    def test_repeatable_filter_idempotent(self):
        """对已过滤文本再次 filter 不再产生替换（幂等）。"""
        text = "电话: 13800138000"
        filtered1, n1 = self.f.filter(text)
        self.assertEqual(n1, 1)
        filtered2, n2 = self.f.filter(filtered1)
        self.assertEqual(n2, 0)
        self.assertEqual(filtered1, filtered2)


if __name__ == "__main__":
    unittest.main()
