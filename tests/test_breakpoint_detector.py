"""BreakpointDetector 单元测试。"""
import pytest
from hermes.breakpoint_detector import BreakpointDetector


class TestBreakpointDetector:
    def setup_method(self):
        self.d = BreakpointDetector(threshold=40)

    # -- 正面信号：句子结束 --

    def test_chinese_period_breaks(self):
        assert self.d.should_break("这是一个句子。")

    def test_chinese_exclamation_breaks(self):
        assert self.d.should_break("太棒了！")

    def test_chinese_question_breaks(self):
        assert self.d.should_break("真的吗？")

    def test_english_period_breaks(self):
        assert self.d.should_break("This is a sentence.")

    def test_english_exclamation_breaks(self):
        assert self.d.should_break("Amazing!")

    # -- 正面信号：代码块闭合 --

    def test_code_block_close(self):
        assert self.d.should_break("print(1)\n```\n")

    def test_code_block_close_no_newline(self):
        assert self.d.should_break("print(1)\n```")

    # -- 正面信号：段落结束 --

    def test_paragraph_break(self):
        assert self.d.should_break("第一段。\n\n")

    def test_multi_paragraph(self):
        assert self.d.should_break("第一段。\n\n第二段。\n\n")

    # -- 负面信号：不该中断 --

    def test_middle_of_chinese(self):
        assert not self.d.should_break("正在处理您的请求")

    def test_middle_of_english(self):
        assert not self.d.should_break("the quick brown fox jumps")

    def test_inside_code_block(self):
        assert not self.d.should_break("```\nx = 1\ny = 2\n")

    def test_empty_text(self):
        assert not self.d.should_break("")

    def test_single_char(self):
        assert not self.d.should_break("a")

    # -- 评分验证 --

    def test_sentence_end_score(self):
        score = self.d.score("完成了。")
        assert score >= 40

    def test_code_block_close_score(self):
        score = self.d.score("print(1)\n```\n")
        assert score >= 50

    def test_mid_word_score(self):
        score = self.d.score("in progress")
        assert score <= 0

    def test_inside_code_penalty(self):
        score = self.d.score("```\nx = 1\n")
        assert score < 40

    # -- 配置 --

    def test_custom_threshold_low(self):
        d = BreakpointDetector(threshold=10)
        assert d.should_break("好的。")  # 句号加分足够触发低阈值

    def test_custom_threshold_high(self):
        d = BreakpointDetector(threshold=100)
        assert not d.should_break("句子结束。")
