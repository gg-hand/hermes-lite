"""Agent 文件工具 + PolicyEngine 单元测试。

覆盖：file_list_uploads / file_query / file_read_uploaded 三个工具
的行为，以及 PolicyEngine 规则、工具注册。

运行方式：python -m unittest tests.test_file_tools -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

from src.files.upload_manager import UploadManager
from src.files.etl_engine import ETLEngine
from src.agent.tool_registry import ToolRegistry
from src.agent.policy import PolicyEngine, DEFAULT_RULES
from src.agent.builtin_tools import register_file_tools


def _make_um(tmpdir):
    db_path = os.path.join(tmpdir, "test.db")
    udir = os.path.join(tmpdir, "uploads")
    os.makedirs(udir, exist_ok=True)
    return UploadManager(db_path=db_path, upload_dir=udir)


class TestFileListUploads(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.um = _make_um(self.tmpdir)
        self.engine = MagicMock(spec=ETLEngine)
        self.registry = ToolRegistry()
        self.session_id = "test-session"
        register_file_tools(
            self.registry, self.engine, self.um,
            get_session_id=lambda: self.session_id,
        )
        self.handler = self.registry._core_tools["file_list_uploads"].handler

    def tearDown(self):
        self.um.close()

    def test_01_with_files(self):
        self.um.save("a.txt", b"hello", self.session_id)
        result = self.handler()
        data = json.loads(result)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "a.txt")

    def test_02_no_files(self):
        result = self.handler()
        self.assertIn("无已上传文件", result)

    def test_03_session_isolation(self):
        self.um.save("s1.txt", b"s1", self.session_id)
        self.um.save("s2.txt", b"s2", "other-session")
        result = self.handler()
        data = json.loads(result)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "s1.txt")


class TestFileQuery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.um = _make_um(self.tmpdir)
        self.engine = MagicMock(spec=ETLEngine)
        self.engine.query_hybrid.return_value = [
            {"chunk_id": "f1_c0", "file_id": "f1",
             "content": "mock result", "score": 0.9, "source": "vector"}
        ]
        self.registry = ToolRegistry()
        register_file_tools(
            self.registry, self.engine, self.um,
            get_session_id=lambda: "test-session",
        )
        self.handler = self.registry._core_tools["file_query"].handler

    def test_04_query_with_results(self):
        result = self.handler(query="test query")
        data = json.loads(result)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

    def test_05_query_with_file_id(self):
        result = self.handler(query="test", file_id="f1")
        self.engine.query_hybrid.assert_called_with(
            query="test", file_id="f1", top_k=5, offset=0
        )

    def test_06_pagination(self):
        self.handler(query="test", top_k=3, offset=3)
        self.engine.query_hybrid.assert_called_with(
            query="test", file_id=None, top_k=3, offset=3
        )

    def test_07_no_match(self):
        self.engine.query_hybrid.return_value = []
        result = self.handler(query="xyznonexistent")
        self.assertIn("无匹配", result)


class TestFileReadUploaded(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.um = _make_um(self.tmpdir)
        self.engine = MagicMock(spec=ETLEngine)
        self.engine.get_parsed_text.return_value = "Full document text content for testing."
        self.registry = ToolRegistry()
        register_file_tools(
            self.registry, self.engine, self.um,
            get_session_id=lambda: "test-session",
        )
        self.handler = self.registry._core_tools["file_read_uploaded"].handler

    def tearDown(self):
        self.um.close()

    def test_08_read_text(self):
        fid, _ = self.um.save("read.txt", b"test", "s1")
        self.um.update_etl_status(fid, "done")
        result = self.handler(file_id=fid)
        self.assertIn("Full document text", result)

    def test_09_max_chars_truncation(self):
        self.engine.get_parsed_text.return_value = "A" * 1000
        fid, _ = self.um.save("long.txt", b"test", "s1")
        self.um.update_etl_status(fid, "done")
        result = self.handler(file_id=fid, max_chars=100)
        self.assertLessEqual(len(result), 115)  # 100 + "...（内容已截断）"
        self.assertIn("已截断", result)

    def test_10_read_image(self):
        self.um.ocr_enabled = True
        fid, _ = self.um.save("img.png", b"\x89PNG", "s1")
        self.um.update_img_text(fid, "OCR extracted text from photo")
        self.um.update_etl_status(fid, "done")
        result = self.handler(file_id=fid)
        self.assertIn("⬤", result)
        self.assertIn("OCR extracted text", result)

    def test_11_file_not_found(self):
        result = self.handler(file_id="nonexistent")
        self.assertIn("文件不存在", result)

    def test_12_disk_expired(self):
        fid, _ = self.um.save("exp.txt", b"test", "s1")
        self.um.mark_disk_expired(fid)
        result = self.handler(file_id=fid)
        self.assertIn("已到期", result)
        self.assertIn("file_query", result)

    def test_13_failed_status(self):
        fid, _ = self.um.save("fail.txt", b"test", "s1")
        self.um.update_error(fid, "parser crashed")
        result = self.handler(file_id=fid)
        self.assertIn("处理失败", result)
        self.assertIn("parser crashed", result)

    def test_14_processing(self):
        fid, _ = self.um.save("proc.txt", b"test", "s1")
        self.um.update_etl_status(fid, "processing")
        result = self.handler(file_id=fid)
        self.assertIn("正在处理", result)


class TestPolicyEngineFileTools(unittest.TestCase):
    """PolicyEngine 对文件工具的策略检查。"""

    def setUp(self):
        self.pe = PolicyEngine(enabled=True, rules=DEFAULT_RULES)

    def test_15_file_query_allow(self):
        d = self.pe.check("file_query", {"query": "test"})
        self.assertEqual(d.action, "allow")

    def test_16_file_list_uploads_allow(self):
        d = self.pe.check("file_list_uploads", {})
        self.assertEqual(d.action, "allow")

    def test_17_file_read_uploaded_allow(self):
        d = self.pe.check("file_read_uploaded", {"file_id": "f1"})
        self.assertEqual(d.action, "allow")

    def test_18_cron_session_allow(self):
        d = self.pe.check("file_query", {"query": "test"}, session_id="cron:abc")
        self.assertEqual(d.action, "allow")


class TestRegisterFileTools(unittest.TestCase):
    """工具注册验证。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.engine = MagicMock()
        self.um = MagicMock()
        register_file_tools(self.registry, self.engine, self.um)

    def test_19_three_tools_registered(self):
        names = set(self.registry._core_tools.keys())
        for tool in ("file_list_uploads", "file_query", "file_read_uploaded"):
            self.assertIn(tool, names)

    def test_20_schema_has_required_fields(self):
        for name in ("file_list_uploads", "file_query", "file_read_uploaded"):
            tool = self.registry._core_tools[name]
            self.assertTrue(hasattr(tool, "description"))
            self.assertTrue(hasattr(tool, "input_schema"))
            self.assertTrue(hasattr(tool, "handler"))

    def test_21_handler_callable(self):
        handler = self.registry._core_tools["file_list_uploads"].handler
        result = handler()
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
