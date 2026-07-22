"""DocumentChunker 单元测试。

覆盖：正常分块、段落感知、边界条件、overlap。

运行方式：
    python -m unittest tests.test_chunker -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402
install_mocks()

from teage_liu.files.chunker import DocumentChunker  # noqa: E402


class TestDocumentChunker(unittest.TestCase):
    """分块器测试。"""

    def setUp(self):
        self.chunker = DocumentChunker(chunk_size=512, chunk_overlap=64)

    def test_01_normal_chunking(self):
        text = "First paragraph.\n\n" * 50
        chunks = self.chunker.chunk(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 512)

    def test_02_paragraph_aware(self):
        text = "Short.\n\n" + "x" * 600 + "\n\nTail."
        chunks = self.chunker.chunk(text)
        # 短段落和长段落分别处理
        self.assertGreater(len(chunks), 0)

    def test_03_short_text(self):
        chunks = self.chunker.chunk("short text")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], "short text")

    def test_04_empty_text(self):
        self.assertEqual(self.chunker.chunk(""), [])
        self.assertEqual(self.chunker.chunk("   \n\n  "), [])

    def test_05_exact_chunk_size(self):
        text = "a" * 512
        chunks = self.chunker.chunk(text)
        self.assertEqual(len(chunks), 1)

    def test_06_long_single_paragraph(self):
        text = "x" * 2000
        chunks = self.chunker.chunk(text)
        self.assertGreater(len(chunks), 1)

    def test_07_overlap_present(self):
        self.chunker = DocumentChunker(chunk_size=200, chunk_overlap=30)
        text = "A paragraph with some content.\n\n" * 20
        chunks = self.chunker.chunk(text)
        if len(chunks) > 1:
            # 检查 overlap 标记
            self.assertTrue(
                any("..." in c for c in chunks[1:]),
                "后续块应含 overlap 分隔符 '...'"
            )

    def test_08_overlap_clamp(self):
        # overlap >= chunk_size 应被 clamp
        c = DocumentChunker(chunk_size=100, chunk_overlap=200)
        self.assertLess(c.chunk_overlap, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
