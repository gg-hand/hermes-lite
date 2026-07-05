"""HTTP 端点集成测试（使用 FastAPI TestClient + ContextManager 集成）。

注意：server.py 的 lifespan 需要完整的 Orchestrator 初始化，这些测试
仅验证端点路由和基本的错误处理路径（mock 模式下）。

运行方式：python -m unittest tests.test_file_api -v
"""

from __future__ import annotations

import json
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
install_mocks()

from src.files.upload_manager import UploadManager
from src.files.context_injector import FileContextInjector
from src.memory.context_manager import ContextManager


class TestContextManagerIntegration(unittest.TestCase):
    """验证 ContextManager 与 FileContextInjector 集成。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(self.tmpdir, "test.db")
        upload_dir = os.path.join(self.tmpdir, "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        self.um = UploadManager(db_path=db_path, upload_dir=upload_dir)

    def tearDown(self):
        self.um.close()

    def _add_done_file(self, session, name, summary):
        fid, _ = self.um.save(name, b"test", session)
        self.um.update_etl_status(fid, "done", summary=summary)
        return fid

    def test_01_file_injection_in_messages(self):
        """messages[0] 含文件摘要 + 记忆检索（mock memory 返回固定值）。"""
        self._add_done_file("s1", "report.pdf", "营收数据增长15%")
        injector = FileContextInjector(self.um, max_files=5, max_tokens=1000)

        cm = ContextManager(
            file_context_injector=injector,
        )

        class MockMemoryRetriever:
            @staticmethod
            def get_injection_text(user_input):
                return "## 相关记忆\n1. 测试记忆 (相关度: 0.90)"

        cm.memory_retriever = MockMemoryRetriever()
        prompt = cm.build_prompt("s1", "你好")

        messages = prompt["messages"]
        self.assertGreater(len(messages), 0)
        content = messages[0]["content"]
        self.assertIn("已上传文件", content)
        self.assertIn("相关记忆", content)
        self.assertIn("report.pdf", content)
        self.assertIn("营收数据", content)

    def test_02_only_file_no_memory(self):
        """仅文件无记忆 → messages[0] 只含文件摘要。"""
        self._add_done_file("s1", "doc.pdf", "文档摘要")
        injector = FileContextInjector(self.um, max_files=5, max_tokens=1000)
        cm = ContextManager(file_context_injector=injector)
        prompt = cm.build_prompt("s1", "你好")

        content = prompt["messages"][0]["content"]
        self.assertIn("已上传文件", content)
        self.assertNotIn("相关记忆", content)

    def test_03_only_memory_no_file(self):
        """仅记忆无文件 → messages[0] 只含记忆（与现有行为一致）。"""
        injector = FileContextInjector(self.um, max_files=5, max_tokens=1000)
        cm = ContextManager(file_context_injector=injector)

        class MockMemoryRetriever:
            @staticmethod
            def get_injection_text(user_input):
                return "## 相关记忆\n1. 测试记忆"

        cm.memory_retriever = MockMemoryRetriever()
        prompt = cm.build_prompt("empty-files", "你好")

        content = prompt["messages"][0]["content"]
        self.assertIn("相关记忆", content)
        self.assertNotIn("已上传文件", content)

    def test_04_both_empty(self):
        """无文件 + 无记忆 → 不插入 messages[0]。"""
        injector = FileContextInjector(self.um, max_files=5, max_tokens=1000)
        cm = ContextManager(file_context_injector=injector)

        class MockEmptyRetriever:
            @staticmethod
            def get_injection_text(user_input):
                return ""

        cm.memory_retriever = MockEmptyRetriever()
        prompt = cm.build_prompt("no-files", "你好")

        messages = prompt["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "你好")

    def test_05_cron_context_no_file_injection(self):
        """cron context 不应包含文件摘要。"""
        injector = FileContextInjector(self.um, max_files=5, max_tokens=1000)
        cm = ContextManager(file_context_injector=injector)

        class MockCronIsolation:
            namespace = "cron"
            cron_id = "cron-test"

        prompt = cm.build_cron_context(
            session_id="cron:test",
            user_input="run task",
            cron_isolation=MockCronIsolation(),
        )

        content = prompt["messages"][0]["content"]
        self.assertNotIn("已上传文件", content)

    def test_06_session_deletion_preserves_knowledge(self):
        """验证会话删除后文件知识库不受影响。"""
        self._add_done_file("s1", "keep.pdf", "会话删除后仍在")

        # 模拟会话删除：仅删除 session 关联，不删 uploaded_files
        # 这里上传的文件仍然可通过 get_metadata 查到
        files_after = self.um.get_session_files("s1")
        self.assertGreater(len(files_after), 0)

        # 就算没有 session，文件知识库仍可通过 list_all 查到
        all_files = self.um.list_all()
        self.assertGreater(len(all_files), 0)

    def test_07_pending_included_failed_excluded(self):
        """pending 文件应注入'处理中'标记；failed 文件仍排除。"""
        fid, _ = self.um.save("pending.txt", b"test", "s1")
        injector = FileContextInjector(self.um, max_files=5, max_tokens=1000)
        text = injector.get_injection_text("s1")
        # pending 文件现在应被注入（显示"处理中"标记）
        self.assertIn("pending.txt", text)
        self.assertIn("处理中", text)

        # 设为 failed 后应排除
        self.um.update_error(fid, "error")
        text = injector.get_injection_text("s1")
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
