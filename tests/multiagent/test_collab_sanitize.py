"""collab_sanitize.sanitize_collab_content 单元测试。

覆盖：
- 思考性开头段落剥离（中英文，迁移自 TestFilterMetaLanguage）
- 工具元语言段落剥离（段落级）
- 工具元语言句子剥离（句级，段落内）
- 空结果兜底（保留原文）
- 实质内容不被误伤
"""
from __future__ import annotations

from teage_liu.multiagent.collab_sanitize import SANITIZED_PLACEHOLDER, sanitize_collab_content


class TestStripThinkingPrefix:
    """思考性开头段落剥离（迁移自 TestFilterMetaLanguage，行为一致）。"""

    def test_strips_leading_let_me_paragraph(self):
        text = (
            "Let me check the available tools first.\n\n"
            "[teagent-lu] 我的答案是 42。"
        )
        filtered = sanitize_collab_content(text)
        assert "我的答案是 42" in filtered
        assert "Let me check" not in filtered

    def test_strips_leading_ill_paragraph(self):
        text = (
            "I'll respond to the question now.\n"
            "Since the puzzle is about numbers.\n\n"
            "答案是 7。"
        )
        filtered = sanitize_collab_content(text)
        assert "答案是 7" in filtered
        assert "I'll respond" not in filtered

    def test_strips_leading_chinese_meta(self):
        text = (
            "让我先看看有哪些工具可用。\n\n"
            "根据工具列表查询结果，我可以回答。\n\n"
            "[teagent-liu-2] 谜底是「水」。"
        )
        filtered = sanitize_collab_content(text)
        assert "谜底是「水」" in filtered
        assert "让我先看看" not in filtered
        assert "根据工具列表查询" not in filtered

    def test_strips_actually_looking_since(self):
        text = (
            "Actually, looking at the previous message,\n"
            "Since the other agent asked a question.\n\n"
            "我的回复：猜「云」。"
        )
        filtered = sanitize_collab_content(text)
        assert "我的回复" in filtered
        assert "Actually" not in filtered

    def test_preserves_substantive_content_unchanged(self):
        text = "[teagent-lu] 这是一个直接的协作回复，没有思考性开头。"
        assert sanitize_collab_content(text) == text

    def test_all_meta_language_returns_placeholder(self):
        """全是思考性内容时返回占位符（P3-1：不再保留原文，避免污染协作流）。"""
        text = "Let me think about this.\nSince I have no answer yet."
        assert sanitize_collab_content(text) == SANITIZED_PLACEHOLDER

    def test_strips_multiple_leading_meta_paragraphs(self):
        text = (
            "I'll check the tools.\n\n"
            "Let me look at the history.\n\n"
            "Actually, the answer is clear.\n\n"
            "[teagent-lu] 最终答案：3.14。"
        )
        filtered = sanitize_collab_content(text)
        assert "最终答案：3.14" in filtered
        assert "I'll check" not in filtered
        assert "Let me look" not in filtered
        assert "Actually" not in filtered

    def test_empty_string_returns_empty(self):
        assert sanitize_collab_content("") == ""

    def test_does_not_strip_meta_in_middle(self):
        """中间段落出现的思考性内容不剥离（只剥离开头）。"""
        text = (
            "[teagent-lu] 这是实质回复。\n\n"
            "Let me add: 补充说明。"
        )
        filtered = sanitize_collab_content(text)
        assert "实质回复" in filtered
        assert "补充说明" in filtered


class TestStripToolMetaParagraphs:
    """工具元语言段落级剥离。"""

    def test_strips_leading_tool_call_chinese(self):
        text = (
            "我来调用 send_remote_message 工具向对方发送。\n\n"
            "我的答案：红烧肉。"
        )
        filtered = sanitize_collab_content(text)
        assert "我的答案：红烧肉" in filtered
        assert "我来调用" not in filtered
        assert "send_remote_message" not in filtered

    def test_strips_leading_tool_blocked(self):
        text = (
            "工具调用被拦截，无法发送消息。\n\n"
            "直接回复：今晚吃火锅。"
        )
        filtered = sanitize_collab_content(text)
        assert "今晚吃火锅" in filtered
        assert "工具调用被拦截" not in filtered

    def test_strips_leading_tool_list_not_returned(self):
        text = (
            "tool_list did not return any tools.\n\n"
            "My answer: 42."
        )
        filtered = sanitize_collab_content(text)
        assert "My answer: 42" in filtered
        assert "tool_list" not in filtered

    def test_strips_leading_shell_bash(self):
        text = (
            "需要 shell 命令执行，bash 环境不可用。\n\n"
            "实际回复：无法执行，请直接回答。"
        )
        filtered = sanitize_collab_content(text)
        assert "实际回复" in filtered
        assert "shell" not in filtered
        assert "bash" not in filtered

    def test_strips_leading_english_tool_call(self):
        text = (
            "I'll call the send_remote_message tool now.\n\n"
            "The answer is pizza."
        )
        filtered = sanitize_collab_content(text)
        assert "The answer is pizza" in filtered
        assert "send_remote_message" not in filtered

    def test_all_tool_meta_returns_placeholder(self):
        """全是工具元语言时返回占位符（P3-1：不再保留原文，避免污染协作流）。"""
        text = "我来调用工具。工具调用被拦截。tool_list did not return."
        assert sanitize_collab_content(text) == SANITIZED_PLACEHOLDER


class TestStripToolMetaSentences:
    """工具元语言句级剥离（段落内）。"""

    def test_strips_tool_sentence_in_paragraph(self):
        """段落内整句工具元语言被剥离，保留实质句。"""
        text = (
            "我来调用 send_remote_message 工具。我的答案是烤鸭。"
        )
        filtered = sanitize_collab_content(text)
        assert "我的答案是烤鸭" in filtered
        assert "我来调用" not in filtered

    def test_strips_tool_sentence_between_substantive(self):
        text = (
            "今晚吃面条。我来调用工具查询。明天吃饺子。"
        )
        filtered = sanitize_collab_content(text)
        assert "今晚吃面条" in filtered
        assert "明天吃饺子" in filtered
        assert "我来调用" not in filtered

    def test_strips_english_tool_sentence(self):
        text = (
            "The answer is 42. Let me call the tool to verify. Done."
        )
        filtered = sanitize_collab_content(text)
        assert "The answer is 42" in filtered
        assert "Done" in filtered
        assert "Let me call" not in filtered

    def test_keeps_paragraph_with_substantive_only(self):
        text = "今晚吃火锅。明天吃烧烤。"
        assert sanitize_collab_content(text) == "今晚吃火锅。明天吃烧烤。"

    def test_all_sentences_tool_meta_returns_placeholder(self):
        """整段全为工具元语言时，句级兜底返回占位符（P3-1）。"""
        text = "我来调用工具。工具调用被拦截。"
        assert sanitize_collab_content(text) == SANITIZED_PLACEHOLDER


class TestEdgeCases:
    """边界情况。"""

    def test_none_content_returns_empty(self):
        # type: ignore[arg-type]
        assert sanitize_collab_content(None) == ""  # type: ignore[arg-type]

    def test_whitespace_only_returns_empty(self):
        assert sanitize_collab_content("   ") == "   "

    def test_preserves_newlines_in_substantive(self):
        text = (
            "[teagent-lu] 第一行回复。\n"
            "第二行补充。\n\n"
            "结尾段落。"
        )
        filtered = sanitize_collab_content(text)
        assert "第一行回复" in filtered
        assert "第二行补充" in filtered
        assert "结尾段落" in filtered

    def test_mixed_thinking_and_tool_meta(self):
        text = (
            "Let me check the tools.\n\n"
            "我来调用 send_remote_message。\n\n"
            "最终答案：糖醋排骨。"
        )
        filtered = sanitize_collab_content(text)
        assert "最终答案：糖醋排骨" in filtered
        assert "Let me check" not in filtered
        assert "我来调用" not in filtered


class TestSanitizeAllMetaReturnsPlaceholder:
    """P3-1 兜底漏洞修复：100% 工具元语言内容必须返回占位符，不得原样保留。

    验收失败场景：LLM 输出 100% 是工具元语言时，旧兜底 `return content`
    把元语言原文写入协作流（type=response），违反验收 (c) 无元语言要求。
    debug 铁证：SANITIZE NOCHANGE type=response content_len=238 has_send_remote=True。
    """

    def test_sanitize_all_meta_returns_placeholder(self):
        """100% 工具元语言（含 send_remote_message）→ 占位符，不保留原文。"""
        content = (
            "我来调用 send_remote_message 工具向对方发送消息。"
            "工具调用被拦截，无法发送。"
            "tool_list did not return any tools."
        )
        result = sanitize_collab_content(content)
        assert result == SANITIZED_PLACEHOLDER
        assert result != content
        assert "send_remote_message" not in result
        assert "工具调用被拦截" not in result

    def test_sanitize_all_thinking_returns_placeholder(self):
        """100% 思考性开头段落 → 占位符。"""
        content = (
            "Let me check the available tools first.\n\n"
            "Since I have no substantive answer yet.\n\n"
            "Actually, looking at the context again."
        )
        assert sanitize_collab_content(content) == SANITIZED_PLACEHOLDER

    def test_placeholder_not_empty_and_marks_sanitization(self):
        """占位符非空且语义明确（标注已净化）。"""
        assert SANITIZED_PLACEHOLDER
        assert "净化" in SANITIZED_PLACEHOLDER

    def test_whitespace_only_not_placeholder(self):
        """纯空白输入不应变为占位符（无实质内容可净化）。"""
        assert sanitize_collab_content("   ") == "   "
        assert sanitize_collab_content("\n\n  \n") == "\n\n  \n"

    def test_empty_string_not_placeholder(self):
        """空串返回空串，不返回占位符。"""
        assert sanitize_collab_content("") == ""

    def test_partial_meta_keeps_substantive(self):
        """部分元语言 + 实质内容 → 剥离元语言保留实质，不返回占位符。"""
        content = "我来调用工具查询。今晚吃红烧肉。"
        result = sanitize_collab_content(content)
        assert "今晚吃红烧肉" in result
        assert result != SANITIZED_PLACEHOLDER
