"""FileContextInjector 单元测试。

覆盖：文本/图片/混合注入、空会话、截断、session 隔离。

运行方式：python -m unittest tests.test_context_injector -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
from tests.test_helpers import minimal_png
install_mocks()

from src.files.upload_manager import UploadManager
from src.files.context_injector import FileContextInjector


def _make_env():
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    upload_dir = os.path.join(tmpdir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    um = UploadManager(db_path=db_path, upload_dir=upload_dir)
    um.ocr_enabled = True
    injector = FileContextInjector(um, max_files=5, max_tokens=1000)
    return tmpdir, um, injector


class TestFileContextInjector(unittest.TestCase):
    def setUp(self):
        self.env = _make_env()
        self.tmpdir, self.um, self.injector = self.env

    def tearDown(self):
        self.um.close()

    def _add_done_file(self, name, content, session, summary=None, is_image=False):
        if is_image:
            self.um.ocr_enabled = True
        fid, _ = self.um.save(name, content, session)
        if is_image:
            self.um.update_img_text(fid, "mock OCR text from image")
        self.um.update_etl_status(fid, "done", summary=summary or f"摘要: {name}")
        return fid

    def test_01_text_injection(self):
        self._add_done_file("report.pdf", b"report content", "s1",
                            summary="包含营收数据和市场分析预测")
        text = self.injector.get_injection_text("s1")
        self.assertIn("已上传文件", text)
        self.assertIn("report.pdf", text)
        self.assertIn("营收数据", text)

    def test_02_image_injection(self):
        self._add_done_file("photo.png", minimal_png(), "s1", is_image=True)
        text = self.injector.get_injection_text("s1")
        self.assertIn("已上传文件", text)
        self.assertIn("photo.png", text)
        self.assertIn("⬤", text)
        self.assertIn("mock OCR text from image", text)

    def test_03_mixed_injection(self):
        self._add_done_file("doc.pdf", b"doc", "s1", summary="文档摘要")
        self._add_done_file("img.png", minimal_png(), "s1", is_image=True)
        text = self.injector.get_injection_text("s1")
        self.assertIn("doc.pdf", text)
        self.assertIn("img.png", text)
        self.assertIn("⬤", text)
        self.assertIn("文档摘要", text)

    def test_04_no_done_files(self):
        fid, _ = self.um.save("pending.txt", b"test", "s1")
        # status is 'pending', not 'done'
        text = self.injector.get_injection_text("s1")
        self.assertEqual(text, "")

    def test_05_empty_session(self):
        text = self.injector.get_injection_text("empty-session")
        self.assertEqual(text, "")

    def test_06_max_files_truncation(self):
        injector = FileContextInjector(self.um, max_files=3, max_tokens=10000)
        for i in range(5):
            self._add_done_file(f"file{i}.txt", b"x", "s1", summary=f"文件{i}")
        text = injector.get_injection_text("s1")
        lines = text.strip().split("\n")
        file_lines = [l for l in lines if l.startswith("- ")]
        self.assertLessEqual(len(file_lines), 3)

    def test_07_token_budget_truncation(self):
        injector = FileContextInjector(self.um, max_files=10, max_tokens=200)
        for i in range(5):
            self._add_done_file(f"long{i}.txt", b"x", "s1",
                                summary="A" * 100)
        text = injector.get_injection_text("s1")
        # budget 很小，但至少第一个文件应能注入
        self.assertNotEqual(text, "")
        self.assertIn("已上传文件", text)

    def test_08_session_isolation(self):
        self._add_done_file("s1.txt", b"s1", "s1", summary="s1 file")
        self._add_done_file("s2.txt", b"s2", "s2", summary="s2 file")
        text = self.injector.get_injection_text("s1")
        self.assertIn("s1.txt", text)
        self.assertNotIn("s2.txt", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
