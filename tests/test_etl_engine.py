"""ETLEngine 集成测试。

覆盖：文本 ETL、图片 ETL、失败处理、混合检索、解析缓存、全链路删除。

运行方式：python -m unittest tests.test_etl_engine -v
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

from tests._mock_deps import install_mocks
from tests.test_helpers import minimal_png
install_mocks()

from teage_liu.files.upload_manager import UploadManager
from teage_liu.files.parser import WaterfallParser
from teage_liu.files.chunker import DocumentChunker
from teage_liu.files.etl_engine import ETLEngine


def _make_env():
    """创建测试环境，返回 (tmpdir, um, parser, chunker, engine)"""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    upload_dir = os.path.join(tmpdir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    um = UploadManager(db_path=db_path, upload_dir=upload_dir)
    parser = WaterfallParser()
    chunker = DocumentChunker(chunk_size=512, chunk_overlap=64)

    # mock chroma_store
    chroma_store = MagicMock()
    chroma_store.find_duplicates.return_value = []
    chroma_store.add_memory.return_value = "mock-id"
    chroma_store.query_memory.return_value = [
        {
            "id": "chunk_0",
            "content": "mock content for vector search",
            "metadata": {"file_id": "f1", "namespace": "file"},
            "similarity": 0.92,
        }
    ]

    # mock session_logger
    session_logger = MagicMock()
    session_logger.search_file_chunks.return_value = [
        {"chunk_id": "f1_chunk_0", "file_id": "f1",
         "content": "mock fts content", "original_name": "test.txt"}
    ]

    engine = ETLEngine(
        upload_manager=um,
        chroma_store=chroma_store,
        session_logger=session_logger,
        parser=parser,
        chunker=chunker,
        llm_client=None,
        config={"chroma_namespace": "file", "upload_dir": upload_dir},
    )
    return tmpdir, um, parser, chunker, engine, chroma_store, session_logger


class TestETLProcessText(unittest.TestCase):
    """文本 ETL 流程测试。"""

    def setUp(self):
        self.env = _make_env()
        self.tmpdir, self.um, _, _, self.engine, self.chroma, self.logger = self.env

    def tearDown(self):
        self.um.close()

    def test_01_full_text_etl(self):
        fid, _ = self.um.save("report.txt", b"Hello world.\n\nThis is a test document for ETL pipeline.\n\nIt has multiple paragraphs.", "s1")
        result = self.engine.process_file(fid, "s1")
        self.assertEqual(result["status"], "done")
        self.assertGreater(result["chunk_count"], 0)
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["etl_status"], "done")
        self.assertGreater(meta["chunk_count"], 0)

    def test_02_parsed_cache_written(self):
        fid, _ = self.um.save("cache.txt", b"Cache content test.", "s1")
        self.engine.process_file(fid, "s1")
        cache_path = os.path.join(self.tmpdir, "uploads", f"{fid}.parsed")
        self.assertTrue(os.path.exists(cache_path))
        with open(cache_path, "r", encoding="utf-8") as f:
            self.assertIn("Cache content", f.read())

    def test_03_chromadb_written(self):
        fid, _ = self.um.save("chroma.txt", b"ChromaDB write test content.", "s1")
        self.engine.process_file(fid, "s1")
        self.assertTrue(self.chroma.add_memory.called)
        call_args = self.chroma.add_memory.call_args
        self.assertIn("file", str(call_args))

    def test_04_fts_written(self):
        fid, _ = self.um.save("fts.txt", b"FTS5 write test content.\n\nMore text here.", "s1")
        self.engine.process_file(fid, "s1")
        self.assertTrue(self.logger.insert_file_chunk.called)

    def test_05_dedup_skips_duplicate(self):
        self.chroma.find_duplicates.return_value = [
            {"id": "dup", "content": "dup", "similarity": 0.95}
        ]
        fid, _ = self.um.save("dedup.txt", b"Duplicate content test.", "s1")
        result = self.engine.process_file(fid, "s1")
        self.assertEqual(result["chunk_count"], 0)

    def test_06_summary_fallback_no_llm(self):
        fid, _ = self.um.save("nosum.txt", b"No LLM summary test.", "s1")
        result = self.engine.process_file(fid, "s1")
        self.assertEqual(result["status"], "done")
        meta = self.um.get_metadata(fid)
        # should have simple summary (first 200 chars)
        self.assertIn("No LLM", meta["summary"])


class TestETLProcessImage(unittest.TestCase):
    """图片 ETL 测试。"""

    def setUp(self):
        self.env = _make_env()
        self.tmpdir, self.um, _, _, self.engine, self.chroma, self.logger = self.env
        self.um.ocr_enabled = True

    def tearDown(self):
        self.um.close()

    def test_07_image_etl(self):
        fid, _ = self.um.save("photo.png", minimal_png(), "s1")
        result = self.engine.process_file(fid, "s1")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["chunk_count"], 0)
        meta = self.um.get_metadata(fid)
        self.assertIn("mock ocr", meta["img_text"].lower())

    def test_08_chromadb_not_called_for_image(self):
        fid, _ = self.um.save("photo.jpg", minimal_png(), "s1")
        self.engine.process_file(fid, "s1")
        self.chroma.add_memory.assert_not_called()

    def test_09_fts_not_called_for_image(self):
        fid, _ = self.um.save("photo.gif", minimal_png(), "s1")
        self.engine.process_file(fid, "s1")
        self.logger.insert_file_chunk.assert_not_called()

    def test_image_no_text_still_done(self):
        """图片无文字时仍标记为 done，img_text 为空字符串。"""
        fid, _ = self.um.save("meme.png", minimal_png(), "s1")
        # mock pytesseract 返回空字符串 → parser 抛 ParseError → ETL 捕获并标记 done
        with patch("teage_liu.files.parser.pytesseract.image_to_string", return_value=""):
            result = self.engine.process_file(fid, "s1")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["chunk_count"], 0)
        self.assertIsNone(result["error"])
        meta = self.um.get_metadata(fid)
        self.assertEqual(meta["etl_status"], "done")
        self.assertEqual(meta.get("img_text", None), "")
        self.chroma.add_memory.assert_not_called()
        self.logger.insert_file_chunk.assert_not_called()


class TestETLFailure(unittest.TestCase):
    """ETL 失败处理测试。"""

    def setUp(self):
        self.env = _make_env()
        self.tmpdir, self.um, _, _, self.engine, self.chroma, self.logger = self.env

    def tearDown(self):
        self.um.close()

    def test_10_parse_failure(self):
        fid, _ = self.um.save("bad.txt", b"", "s1")
        result = self.engine.process_file(fid, "s1")
        self.assertEqual(result["status"], "failed")
        self.assertIsNotNone(result["error"])

    def test_11_chromadb_failure(self):
        self.chroma.add_memory.side_effect = Exception("ChromaDB write error")
        fid, _ = self.um.save("fail.txt", b"Test content with chroma error.", "s1")
        result = self.engine.process_file(fid, "s1")
        self.assertEqual(result["status"], "failed")


class TestHybridQuery(unittest.TestCase):
    """混合检索测试。"""

    def setUp(self):
        self.env = _make_env()
        self.tmpdir, self.um, _, _, self.engine, self.chroma, self.logger = self.env

    def tearDown(self):
        self.um.close()

    def test_12_vector_hit(self):
        results = self.engine.query_hybrid("python")
        self.assertGreater(len(results), 0)

    def test_13_fts_hit(self):
        results = self.engine.query_hybrid("contract")
        self.assertGreater(len(results), 0)

    def test_14_rrf_fusion_both_engines(self):
        results = self.engine.query_hybrid("test query")
        for r in results:
            self.assertIn("score", r)
            self.assertGreater(r["score"], 0)

    def test_15_pagination_offset(self):
        r1 = self.engine.query_hybrid("test", top_k=2, offset=0)
        r2 = self.engine.query_hybrid("test", top_k=2, offset=2)
        self.assertLessEqual(len(r1), 2)
        self.assertLessEqual(len(r2), 2)

    def test_16_file_id_filter(self):
        results = self.engine.query_hybrid("test", file_id="f1")
        self.assertGreater(len(results), 0)

    def test_17_no_match(self):
        self.chroma.query_memory.return_value = []
        self.logger.search_file_chunks.return_value = []
        results = self.engine.query_hybrid("xyznonexistent12345")
        self.assertEqual(results, [])


class TestGetParsedText(unittest.TestCase):
    """解析文本读取测试。"""

    def setUp(self):
        self.env = _make_env()
        self.tmpdir, self.um, _, _, self.engine, self.chroma, self.logger = self.env
        self.um.ocr_enabled = True

    def tearDown(self):
        self.um.close()

    def test_18_text_read_cache(self):
        fid, _ = self.um.save("read.txt", b"Read test content.", "s1")
        self.engine.process_file(fid, "s1")
        text = self.engine.get_parsed_text(fid)
        self.assertIsNotNone(text)
        self.assertIn("Read test", text)

    def test_19_image_return_img_text(self):
        fid, _ = self.um.save("img.png", minimal_png(), "s1")
        self.engine.process_file(fid, "s1")
        text = self.engine.get_parsed_text(fid)
        self.assertIsNotNone(text)
        self.assertIn("mock ocr", text.lower())

    def test_20_cache_miss_reparse(self):
        fid, _ = self.um.save("reparse.txt", b"Reparse test content.", "s1")
        self.engine.process_file(fid, "s1")
        # 删除缓存
        cache_path = os.path.join(self.tmpdir, "uploads", f"{fid}.parsed")
        os.remove(cache_path)
        text = self.engine.get_parsed_text(fid)
        self.assertIsNotNone(text)
        self.assertIn("Reparse", text)

    def test_21_both_missing(self):
        text = self.engine.get_parsed_text("nonexistent-id")
        self.assertIsNone(text)


class TestDeleteFileKnowledge(unittest.TestCase):
    """全链路删除测试。"""

    def setUp(self):
        self.env = _make_env()
        self.tmpdir, self.um, _, _, self.engine, self.chroma, self.logger = self.env

    def tearDown(self):
        self.um.close()

    def test_22_full_delete(self):
        fid, _ = self.um.save("del.txt", b"Delete test content.", "s1")
        self.engine.process_file(fid, "s1")
        result = self.engine.delete_file_knowledge(fid)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["details"]["sqlite"])
        # 验证文件已从 SQLite 删除
        self.assertIsNone(self.um.get_metadata(fid))

    def test_23_delete_non_existent(self):
        result = self.engine.delete_file_knowledge("no-such-id")
        self.assertFalse(result["deleted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
