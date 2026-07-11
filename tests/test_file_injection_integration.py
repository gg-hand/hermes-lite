"""文件注入到 orchestrator _build_enhanced_context 的集成测试。

验证 Task 1-3 的端到端链路：FileContextInjector 输出经 ContextManager.get_file_injection
被 orchestrator 正确拼装到 messages[0]，覆盖 pending/done/图片/cron/空会话场景。

运行方式：python -m pytest tests/test_file_injection_integration.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
from tests.test_helpers import minimal_png
install_mocks()

from src.files.upload_manager import UploadManager
from src.files.context_injector import FileContextInjector
from src.memory.context_manager import ContextManager
from src.agent.context_builder import ContextBuilder
from src.agent.cron_isolator import CronIsolator
from src.orchestrator import Orchestrator


def _make_env():
    """创建临时环境：UploadManager + FileContextInjector + ContextManager。"""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    upload_dir = os.path.join(tmpdir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    um = UploadManager(db_path=db_path, upload_dir=upload_dir)
    um.ocr_enabled = True
    injector = FileContextInjector(um, max_files=5, max_tokens=1000)
    cm = ContextManager(file_context_injector=injector)
    return tmpdir, um, injector, cm


def _make_orchestrator(cm: ContextManager) -> Orchestrator:
    """创建 Orchestrator 实例但绕过 __init__。

    仅设置 _build_enhanced_context 路径上需要的属性：
    - context_manager：注入含 file_context_injector 的 ContextManager
    - memory_retriever / task_manager / todo_registry：None 跳过对应注入
    其他属性（condenser / cron_scheduler 等）通过 getattr 兜底为 None。
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch.context_manager = cm
    orch.memory_retriever = None
    orch.task_manager = None
    orch.todo_registry = None
    # 委托管理器（方法对象模式，持有 orch 引用）
    orch.context_builder = ContextBuilder()
    orch.cron_isolator = CronIsolator(orchestrator=orch)
    return orch


def _run_async(coro):
    """同步运行 async 协程，返回结果。"""
    return asyncio.run(coro)


class TestFileInjectionIntegration(unittest.TestCase):
    """验证 orchestrator _build_enhanced_context 正确接入文件注入。"""

    def setUp(self):
        self.tmpdir, self.um, self.injector, self.cm = _make_env()
        self.orch = _make_orchestrator(self.cm)

    def tearDown(self):
        self.um.close()

    def _add_done_file(self, name, content, session, summary=None, is_image=False):
        """添加一个 done 状态文件。"""
        if is_image:
            self.um.ocr_enabled = True
        fid, _ = self.um.save(name, content, session)
        if is_image:
            self.um.update_img_text(fid, "mock OCR text from image")
        self.um.update_etl_status(fid, "done", summary=summary or f"摘要: {name}")
        return fid

    def _add_pending_file(self, name, content, session):
        """添加一个 pending 状态文件（save 后不调 update_etl_status）。"""
        fid, _ = self.um.save(name, content, session)
        return fid

    def _build_context(self, session_id: str, user_input: str = "test") -> tuple:
        """调用 _build_enhanced_context 并返回结果。"""
        history: List[Dict[str, Any]] = []
        return _run_async(
            self.orch._build_enhanced_context(session_id, user_input, history)
        )

    # ------------------------------------------------------------------
    # 测试用例
    # ------------------------------------------------------------------

    def test_01_orchestrator_injects_file_info_into_messages(self):
        """done 状态文件应被注入到 messages[0]，LLM 无需调用工具即可感知。"""
        self._add_done_file("report.txt", b"report content", "s1",
                            summary="包含营收数据和市场分析")
        system_text, enhanced_history, tools_override = self._build_context("s1")

        # messages[0] 是 injection_text 前置的 user 消息
        self.assertGreater(len(enhanced_history), 0)
        messages_zero = enhanced_history[0]
        self.assertEqual(messages_zero["role"], "user")
        content = messages_zero["content"]
        self.assertIn("已上传文件", content)
        self.assertIn("report.txt", content)
        self.assertIn("营收数据", content)

    def test_02_pending_file_shows_processing_marker(self):
        """pending 状态文件应在 messages[0] 显示'处理中'标记。"""
        self._add_pending_file("draft.txt", b"draft", "s1")
        system_text, enhanced_history, tools_override = self._build_context("s1")

        content = enhanced_history[0]["content"]
        self.assertIn("draft.txt", content)
        self.assertIn("处理中", content)
        # pending 文件不应显示摘要或 OCR 标记
        self.assertNotIn("⬤", content)

    def test_03_done_image_file_shows_ocr_text(self):
        """done 状态图片文件应在 messages[0] 显示 OCR 提取的文字。"""
        self._add_done_file("photo.png", minimal_png(), "s1", is_image=True)
        system_text, enhanced_history, tools_override = self._build_context("s1")

        content = enhanced_history[0]["content"]
        self.assertIn("photo.png", content)
        self.assertIn("⬤", content)
        self.assertIn("mock OCR text from image", content)

    def test_04_cron_session_skips_file_injection(self):
        """cron 会话不应注入文件信息（cron 无用户上传绑定）。"""
        # 即便为 cron:xxx 会话添加文件，cron 路径也不应注入
        self._add_done_file("should-not-appear.txt", b"x", "cron:test123",
                            summary="不应被注入")
        system_text, enhanced_history, tools_override = self._build_context("cron:test123")

        # cron 路径可能注入 env_section（非空时 enhanced_history[0] 存在）
        # 但绝不应包含"已上传文件"段
        if enhanced_history:
            content = enhanced_history[0]["content"]
            self.assertNotIn("已上传文件", content)
            self.assertNotIn("should-not-appear.txt", content)

    def test_05_no_uploads_returns_empty_injection(self):
        """无上传会话的 messages[0] 不应包含'已上传文件'段。"""
        system_text, enhanced_history, tools_override = self._build_context("empty-session")

        # 无文件注入时，injection_text 可能为空（enhanced_history 也为空）
        # 或仅含 env_section（不含"已上传文件"）
        if enhanced_history:
            content = enhanced_history[0]["content"]
            self.assertNotIn("已上传文件", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
