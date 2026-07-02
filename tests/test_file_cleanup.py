"""TTL 清理与并发测试。

覆盖：TTL 正常清理、跳过 processing、知识库不受影响、并发场景。

运行方式：python -m unittest tests.test_file_cleanup -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

from src.files.upload_manager import UploadManager


class TestTTLCleanup(unittest.TestCase):
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

    def _make_expired_file(self, name, session):
        fid, _ = self.um.save(name, b"content", session)
        # 设 last_accessed 为 60 天前
        self.um.conn.execute(
            "UPDATE uploaded_files SET last_accessed = '2020-01-01T00:00:00' "
            "WHERE file_id = ?", (fid,)
        )
        self.um.conn.commit()
        return fid

    def test_01_normal_expiry(self):
        fid = self._make_expired_file("old.txt", "s1")
        saved_path = self.um.get_metadata(fid)["saved_path"]
        expired = self.um.get_expired(30)
        self.assertIn(fid, expired)
        # 模拟 file_cleanup_loop 逻辑
        self.um.mark_disk_expired(fid)
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["etl_status"], "disk_expired")
        self.assertEqual(meta["saved_path"], "")

    def test_02_cache_not_checked_by_upload_manager(self):
        """UploadManager 不追踪缓存文件，仅清理器关心。"""
        fid = self._make_expired_file("cached.txt", "s1")
        expired = self.um.get_expired(30)
        self.assertIn(fid, expired)

    def test_03_skip_processing(self):
        fid = self._make_expired_file("processing.txt", "s1")
        self.um.update_etl_status(fid, "processing")
        expired = self.um.get_expired(30)
        self.assertNotIn(fid, expired)

    def test_04_fresh_file_not_expired(self):
        fid, _ = self.um.save("fresh.txt", b"new", "s1")
        expired = self.um.get_expired(30)
        self.assertNotIn(fid, expired)

    def test_05_chromadb_not_tracked(self):
        """TTL 逻辑完全不涉及 ChromaDB，验证清理后 metadata 保留。"""
        fid = self._make_expired_file("kb.txt", "s1")
        self.um.mark_disk_expired(fid)
        meta = self.um.get_metadata(fid)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["original_name"], "kb.txt")

    def test_06_reupload_after_expiry(self):
        content = b"reupload after expiry"
        fid1, _ = self.um.save("v1.txt", content, "s1")
        self.um.mark_disk_expired(fid1)
        # 重新上传（全局去重应命中）
        fid2, is_dup = self.um.save("v2.txt", content, "s2")
        self.assertTrue(is_dup)
        self.assertEqual(fid1, fid2)


class TestConcurrentCleanup(unittest.TestCase):
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

    def test_07_processing_skipped_during_expiry(self):
        """模拟 ETL 正在处理时 TTL 扫描不冲突。"""
        fid, _ = self.um.save("simul.txt", b"simultaneous", "s1")
        self.um.update_etl_status(fid, "processing")
        # 设 last_accessed 过期
        self.um.conn.execute(
            "UPDATE uploaded_files SET last_accessed = '2020-01-01' "
            "WHERE file_id = ?", (fid,)
        )
        self.um.conn.commit()
        # get_expired 应跳过 processing
        expired = self.um.get_expired(30)
        self.assertNotIn(fid, expired)
        # ETL 完成后可清理
        self.um.update_etl_status(fid, "done")
        expired_after = self.um.get_expired(30)
        self.assertIn(fid, expired_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
