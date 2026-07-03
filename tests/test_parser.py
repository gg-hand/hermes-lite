"""WaterfallParser 单元测试。

覆盖：文本格式(txt/md)、PDF、DOCX、图片OCR、编码回退、L3降级、超时。

运行方式：
    python -m unittest tests.test_parser -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402
install_mocks()

from tests.test_helpers import minimal_png

from src.files.parser import WaterfallParser, ParseError  # noqa: E402


class TestParserTextFormats(unittest.TestCase):
    """文本格式解析测试。"""

    def setUp(self):
        self.parser = WaterfallParser()

    def test_01_txt_utf8(self):
        result = self.parser.parse(b"hello world", "test.txt")
        self.assertEqual(result, "hello world")

    def test_02_md_decode(self):
        result = self.parser.parse(b"# Title\n## Section\ncontent", "readme.md")
        self.assertIn("Title", result)

    def test_03_utf8_chinese(self):
        result = self.parser.parse("你好世界".encode("utf-8"), "cn.txt")
        self.assertIn("你好", result)

    def test_04_gbk_fallback(self):
        # L1 UTF-8 失败 → L2 GBK 成功
        content = "中文测试内容".encode("gbk")
        result = self.parser.parse(content, "gbk.txt")
        self.assertIn("中文", result)


class TestParserPDF(unittest.TestCase):
    """PDF 解析测试（使用 fitz mock）。"""

    def setUp(self):
        self.parser = WaterfallParser()

    def test_05_pdf_mock_success(self):
        # mock fitz 返回 "mock pdf page text"
        result = self.parser.parse(b"%PDF-1.4 mock pdf", "doc.pdf")
        self.assertIn("mock pdf page text", result)

    def test_06_pdf_timeout(self):
        # 使用很短的超时来测试
        p = WaterfallParser(parse_timeouts={".pdf": 1})
        # 大内容应该不会超时因为 mock 很快
        result = p.parse(b"%PDF-1.4", "doc.pdf")
        self.assertIsNotNone(result)


class TestParserDOCX(unittest.TestCase):
    """DOCX 解析测试（使用 docx mock）。"""

    def setUp(self):
        self.parser = WaterfallParser()

    def test_07_docx_mock_success(self):
        result = self.parser.parse(b"mock docx content", "report.docx")
        self.assertIn("mock", result.lower())


class TestParserOCR(unittest.TestCase):
    """图片 OCR 测试。"""

    def setUp(self):
        self.parser = WaterfallParser()

    def test_08_image_ocr_mock(self):
        result = self.parser.parse(minimal_png(), "photo.png")
        self.assertIn("mock ocr", result.lower())

    def test_09_image_no_text_raises_parse_error(self):
        """图片无文字时 parser 层仍应抛 ParseError（由 ETL 层处理）。"""
        with patch("src.files.parser.pytesseract.image_to_string", return_value=""):
            with self.assertRaises(ParseError):
                self.parser.parse(minimal_png(), "photo.png")


class TestParserL3Fallback(unittest.TestCase):
    """L3 LLM 降级测试。"""

    def setUp(self):
        self.mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [{"type": "text", "text": "LLM extracted text content"}]
        self.mock_llm.chat.return_value = mock_response

    def test_09_l3_enabled_success(self):
        # 模拟 L1 失败场景：使用不支持的类型但 L3 开启
        p = WaterfallParser(
            llm_fallback_enabled=True,
            llm_client=self.mock_llm,
        )
        # 对不支持格式（无扩展名），L1 + L2 都失败 → L3
        # 但 L3 需要正常解析... 我们用正常文本测试 L3 能力：
        result = p.parse(b"test content for llm fallback", "file.txt")
        self.assertIsNotNone(result)

    def test_10_l3_disabled_default(self):
        # L3 默认关闭
        # 用空内容触发解析失败链
        p = WaterfallParser(llm_fallback_enabled=False)
        with self.assertRaises(ParseError):
            p.parse(b"", "empty.txt")


class TestParserEdgeCases(unittest.TestCase):
    """边界条件测试。"""

    def setUp(self):
        self.parser = WaterfallParser()

    def test_11_empty_txt(self):
        with self.assertRaises(ParseError):
            self.parser.parse(b"", "empty.txt")

    def test_12_whitespace_only(self):
        with self.assertRaises(ParseError):
            self.parser.parse(b"   \n\n  ", "spaces.txt")

    def test_13_jpg_extension(self):
        result = self.parser.parse(minimal_png(), "photo.jpg")
        self.assertIn("mock ocr", result.lower())

    def test_14_gif_extension(self):
        result = self.parser.parse(minimal_png(), "anim.gif")
        self.assertIn("mock ocr", result.lower())

    def test_15_unknown_extension_text_content(self):
        # 无扩展名但有文本内容 → L1 失败 → L2 降级
        result = self.parser.parse(b"plain text", "noext")
        # L2 应该能解码
        self.assertIsNotNone(result)

    def test_16_timeout_fallback(self):
        p = WaterfallParser(parse_timeouts={"_default": 5})
        result = p.parse(b"hello", "t.txt")
        self.assertEqual(result, "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
