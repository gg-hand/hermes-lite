"""UploadManager 单元测试。

覆盖：校验、存储（去重/并发）、CRUD、持久化、TTL 追踪。

运行方式：
    python -m unittest tests.test_upload_manager -v
"""

from __future__ import annotations

import hashlib
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

from tests._mock_deps import install_mocks  # noqa: E402
install_mocks()

from hermes.files.upload_manager import UploadManager  # noqa: E402


def _make_db_path(tmpdir: str) -> str:
    return os.path.join(tmpdir, "test.db")


def _make_upload_dir(tmpdir: str) -> str:
    path = os.path.join(tmpdir, "uploads")
    os.makedirs(path, exist_ok=True)
    return path


class TestUploadManagerValidate(unittest.TestCase):
    """校验逻辑测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.um = UploadManager(
            db_path=_make_db_path(self.tmpdir),
            upload_dir=_make_upload_dir(self.tmpdir),
        )

    def tearDown(self):
        self.um.close()

    def test_01_valid_txt(self):
        result = self.um.validate("report.txt", b"hello world")
        self.assertIsNone(result)

    def test_02_valid_pdf(self):
        result = self.um.validate("doc.pdf", b"%PDF-1.4 mock content")
        self.assertIsNone(result)

    def test_03_unsupported_extension(self):
        result = self.um.validate("virus.exe", b"malicious")
        self.assertIsNotNone(result)
        self.assertIn(".exe", result)

    def test_04_no_extension(self):
        result = self.um.validate("README", b"content")
        self.assertIsNotNone(result)
        self.assertIn("扩展名", result)

    def test_05_oversized(self):
        big_content = b"x" * (51 * 1024 * 1024)  # 51MB
        result = self.um.validate("big.pdf", big_content)
        self.assertIsNotNone(result)
        self.assertIn("50MB", result)

    def test_06_empty_file(self):
        result = self.um.validate("empty.txt", b"")
        self.assertIsNotNone(result)
        self.assertIn("空", result)

    def test_07_image_ocr_disabled(self):
        result = self.um.validate("photo.png", b"\x89PNG...")
        self.assertIsNotNone(result)
        self.assertIn("OCR", result)

    def test_08_image_ocr_enabled(self):
        self.um.ocr_enabled = True
        result = self.um.validate("photo.png", b"\x89PNG...")
        self.assertIsNone(result)

    def test_09_jpg_ocr_enabled(self):
        self.um.ocr_enabled = True
        result = self.um.validate("scan.jpg", b"\xff\xd8...")
        self.assertIsNone(result)


class TestUploadManagerSave(unittest.TestCase):
    """存储与去重测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.um = UploadManager(
            db_path=_make_db_path(self.tmpdir),
            upload_dir=_make_upload_dir(self.tmpdir),
        )

    def tearDown(self):
        self.um.close()

    def test_10_normal_save(self):
        file_id, is_dup = self.um.save("hello.txt", b"hello world", "s1")
        self.assertIsNotNone(file_id)
        self.assertIsNone(is_dup)
        # 磁盘文件存在
        meta = self.um.get_metadata(file_id)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["original_name"], "hello.txt")
        self.assertTrue(os.path.exists(meta["saved_path"]))

    def test_11_global_dedup_same_session(self):
        content = b"unique content for dedup test"
        file_id1, is_dup1 = self.um.save("file1.txt", content, "s1")
        file_id2, is_dup2 = self.um.save("file2.txt", content, "s1")
        self.assertIsNone(is_dup1)
        self.assertTrue(is_dup2)
        self.assertEqual(file_id1, file_id2)

    def test_12_cross_session_dedup(self):
        content = b"cross session dedup content"
        file_id1, _ = self.um.save("a.txt", content, "s1")
        file_id2, is_dup = self.um.save("b.txt", content, "s2")
        self.assertTrue(is_dup)
        self.assertEqual(file_id1, file_id2)

    def test_13_disk_expired_dedup(self):
        content = b"expired dedup test"
        file_id1, _ = self.um.save("x.txt", content, "s1")
        # 模拟 disk_expired
        self.um.mark_disk_expired(file_id1)
        # 重新上传
        file_id2, is_dup = self.um.save("y.txt", content, "s2")
        self.assertTrue(is_dup)
        self.assertEqual(file_id1, file_id2)

    def test_14_different_content_different_id(self):
        fid1, _ = self.um.save("a.txt", b"content A", "s1")
        fid2, _ = self.um.save("b.txt", b"content B", "s1")
        self.assertNotEqual(fid1, fid2)

    def test_15_binary_content_integrity(self):
        original = b"\x00\x01\x02\xff\xfeHello\x00World"
        file_id, _ = self.um.save("bin.txt", original, "s1")
        read_back = self.um.read_content(file_id)
        self.assertEqual(read_back, original)

    def test_16_concurrent_same_file(self):
        """2 线程同时上传相同内容 → 无 IntegrityError。"""
        content = b"concurrent dedup test"
        results = []
        errors = []

        def upload(session_id):
            try:
                fid, dup = self.um.save("f.txt", content, session_id)
                results.append((fid, dup))
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=upload, args=("sa",))
        t2 = threading.Thread(target=upload, args=("sb",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 2)
        # 至少一个是 is_dup
        self.assertTrue(any(dup for _, dup in results))
        # file_id 相同
        self.assertEqual(results[0][0], results[1][0])


class TestUploadManagerCRUD(unittest.TestCase):
    """元数据 CRUD 测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.um = UploadManager(
            db_path=_make_db_path(self.tmpdir),
            upload_dir=_make_upload_dir(self.tmpdir),
        )

    def tearDown(self):
        self.um.close()

    def test_17_get_metadata_exists(self):
        fid, _ = self.um.save("m.txt", b"metadata test", "s1")
        meta = self.um.get_metadata(fid)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["file_id"], fid)
        self.assertEqual(meta["original_name"], "m.txt")

    def test_18_get_metadata_not_found(self):
        self.assertIsNone(self.um.get_metadata("nonexistent-id"))

    def test_19_get_session_files_with_files(self):
        self.um.save("a.txt", b"a", "s1")
        self.um.save("b.txt", b"b", "s1")
        files = self.um.get_session_files("s1")
        self.assertEqual(len(files), 2)

    def test_20_get_session_files_empty(self):
        self.assertEqual(self.um.get_session_files("no-files"), [])

    def test_21_get_session_files_isolation(self):
        self.um.save("s1.txt", b"s1", "s1")
        self.um.save("s2.txt", b"s2", "s2")
        s1_files = self.um.get_session_files("s1")
        self.assertEqual(len(s1_files), 1)
        self.assertEqual(s1_files[0]["original_name"], "s1.txt")

    def test_22_list_all(self):
        self.um.save("a.txt", b"a", "s1")
        self.um.save("b.txt", b"b", "s2")
        all_files = self.um.list_all()
        self.assertEqual(len(all_files), 2)

    def test_23_update_etl_status(self):
        fid, _ = self.um.save("t.txt", b"test", "s1")
        self.um.update_etl_status(fid, "done", summary="a summary", chunk_count=5)
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["etl_status"], "done")
        self.assertEqual(meta["summary"], "a summary")
        self.assertEqual(meta["chunk_count"], 5)

    def test_24_update_error(self):
        fid, _ = self.um.save("e.txt", b"err", "s1")
        self.um.update_error(fid, "parser failed")
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["etl_status"], "failed")
        self.assertEqual(meta["error_reason"], "parser failed")

    def test_25_update_img_text(self):
        self.um.ocr_enabled = True
        fid, _ = self.um.save("img.png", b"\x89PNG...", "s1")
        self.um.update_img_text(fid, "OCR text")
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["img_text"], "OCR text")

    def test_26_touch_accessed(self):
        fid, _ = self.um.save("t.txt", b"touch", "s1")
        meta_before = self.um.get_metadata(fid)
        old_accessed = meta_before["last_accessed"]
        time.sleep(0.1)
        self.um.touch_accessed(fid)
        meta_after = self.um.get_metadata(fid)
        self.assertNotEqual(meta_after["last_accessed"], old_accessed)

    def test_27_get_expired(self):
        fid, _ = self.um.save("exp.txt", b"expired", "s1")
        # 直接修改 last_accessed 模拟过期
        self.um.conn.execute(
            "UPDATE uploaded_files SET last_accessed = '2020-01-01T00:00:00' "
            "WHERE file_id = ?",
            (fid,),
        )
        self.um.conn.commit()
        expired = self.um.get_expired(30)
        self.assertIn(fid, expired)

    def test_28_get_expired_skips_processing(self):
        fid, _ = self.um.save("proc.txt", b"processing", "s1")
        self.um.update_etl_status(fid, "processing")
        self.um.conn.execute(
            "UPDATE uploaded_files SET last_accessed = '2020-01-01T00:00:00' "
            "WHERE file_id = ?",
            (fid,),
        )
        self.um.conn.commit()
        expired = self.um.get_expired(30)
        self.assertNotIn(fid, expired)

    def test_29_mark_disk_expired(self):
        fid, _ = self.um.save("d.txt", b"disk expired", "s1")
        self.um.mark_disk_expired(fid)
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["etl_status"], "disk_expired")
        self.assertEqual(meta["saved_path"], "")

    def test_30_delete_record(self):
        fid, _ = self.um.save("del.txt", b"delete me", "s1")
        self.assertTrue(self.um.delete_record(fid))
        self.assertIsNone(self.um.get_metadata(fid))
        self.assertFalse(self.um.delete_record(fid))  # 幂等返回 False

    def test_31_read_content_normal(self):
        content = b"file content for reading"
        fid, _ = self.um.save("r.txt", content, "s1")
        self.assertEqual(self.um.read_content(fid), content)

    def test_32_read_content_disk_missing(self):
        fid, _ = self.um.save("r.txt", b"will be deleted", "s1")
        meta = self.um.get_metadata(fid)
        os.remove(meta["saved_path"])
        self.assertIsNone(self.um.read_content(fid))


class TestUploadManagerPersistence(unittest.TestCase):
    """持久化（重启恢复）测试。"""

    def test_33_metadata_survives_restart(self):
        tmpdir = tempfile.mkdtemp()
        db_path = _make_db_path(tmpdir)
        upload_dir = _make_upload_dir(tmpdir)

        um1 = UploadManager(db_path=db_path, upload_dir=upload_dir)
        fid, _ = um1.save("persist.txt", b"persistent", "s1")
        um1.update_etl_status(fid, "done", summary="survived", chunk_count=3)
        um1.close()

        # 模拟重启
        um2 = UploadManager(db_path=db_path, upload_dir=upload_dir)
        meta = um2.get_metadata(fid)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["etl_status"], "done")
        self.assertEqual(meta["summary"], "survived")
        self.assertEqual(meta["chunk_count"], 3)
        um2.close()

    def test_34_file_content_survives_restart(self):
        tmpdir = tempfile.mkdtemp()
        db_path = _make_db_path(tmpdir)
        upload_dir = _make_upload_dir(tmpdir)

        um1 = UploadManager(db_path=db_path, upload_dir=upload_dir)
        content = b"file survives restart"
        fid, _ = um1.save("survive.txt", content, "s1")
        um1.close()

        um2 = UploadManager(db_path=db_path, upload_dir=upload_dir)
        self.assertEqual(um2.read_content(fid), content)
        um2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
