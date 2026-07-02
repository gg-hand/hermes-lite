"""安全边界与健壮性测试。

覆盖：路径穿越、MIME伪装、XSS、SQL注入、超大文件名、空文件body。

运行方式：python -m unittest tests.test_file_security -v
"""

from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

from src.files.upload_manager import UploadManager


class TestFileSecurity(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.upload_dir = os.path.join(self.tmpdir, "uploads")
        os.makedirs(self.upload_dir, exist_ok=True)
        self.um = UploadManager(
            db_path=self.db_path,
            upload_dir=self.upload_dir,
        )

    def tearDown(self):
        self.um.close()

    def test_01_path_traversal(self):
        """路径穿越：文件名含 ../ 的 .txt 应存为 uuid 文件。"""
        fid, _ = self.um.save("../../../etc/passwd.txt", b"evil", "s1")
        self.assertIsNotNone(fid)
        meta = self.um.get_metadata(fid)
        saved_path = meta["saved_path"]
        # saved_path 应该在 upload_dir 内，不包含 ".."
        self.assertIn(self.upload_dir, saved_path)
        self.assertNotIn("..", os.path.normpath(saved_path))
        self.assertNotIn("etc", saved_path.split(os.sep))

    def test_02_mime_mismatch(self):
        """MIME 伪装：.txt 扩展名但内容像 PDF，按扩展名校验通过。"""
        error = self.um.validate("fake.txt", b"%PDF-1.4...")
        self.assertIsNone(error)

    def test_03_empty_body_with_valid_content_type(self):
        """空内容：multipart 字段非空但内容为空。"""
        error = self.um.validate("empty.txt", b"")
        self.assertIsNotNone(error)
        self.assertIn("空", error)

    def test_04_sql_injection_in_filename(self):
        """SQL 注入：文件名含 DROP TABLE 语句。"""
        fid, _ = self.um.save(
            "'; DROP TABLE uploaded_files; --.txt",
            b"sql injection test",
            "s1",
        )
        self.assertIsNotNone(fid)
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["original_name"],
                         "'; DROP TABLE uploaded_files; --.txt")
        # 验证表还在（参数化查询防御）
        cur = self.um.conn.execute("SELECT COUNT(*) FROM uploaded_files")
        self.assertGreater(cur.fetchone()[0], 0)

    def test_05_ultra_long_filename(self):
        """超长文件名应被截断或正常处理。"""
        long_name = "A" * 10000 + ".txt"
        try:
            fid, _ = self.um.save(long_name, b"long name", "s1")
            self.assertIsNotNone(fid)
        except Exception:
            pass  # 拒绝也合理（不是安全漏洞）

    def test_06_xss_in_content(self):
        """文件内容含 XSS，仅存盘不应执行。"""
        xss_content = b"<script>alert(1)</script>"
        fid, _ = self.um.save("safe.txt", xss_content, "s1")
        read_back = self.um.read_content(fid)
        self.assertEqual(read_back, xss_content)

    def test_07_duplicate_upload_different_sessions(self):
        """不同 session 上传相同文件，去重正常工作。"""
        content = b"shared content"
        fid_a, _ = self.um.save("a.txt", content, "session-A")
        fid_b, is_dup = self.um.save("b.txt", content, "session-B")
        self.assertTrue(is_dup)
        self.assertEqual(fid_a, fid_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
