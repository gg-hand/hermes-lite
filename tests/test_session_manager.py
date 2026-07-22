"""SessionManager 测试:会话创建、标题生成。

从 Orchestrator 提取的会话管理职责:
- ensure_session: 确保 session_logger 中存在会话记录
- generate_title_async: 异步生成会话标题（fire-and-forget）
- 标题缓存: _titled_sessions 避免重复查 DB
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teage_liu"))

import pytest
from unittest.mock import MagicMock, AsyncMock
from teage_liu.agent.session_manager import SessionManager


class TestSessionManagerBasics:
    def test_ensure_session_calls_logger(self):
        """ensure_session 应调用 session_logger.ensure_session 原子创建。"""
        logger = MagicMock()
        mgr = SessionManager(session_logger=logger)
        mgr.ensure_session("test-1")
        logger.ensure_session.assert_called_once_with("test-1")

    def test_ensure_session_handles_none_logger(self):
        """session_logger 为 None 时不抛异常。"""
        mgr = SessionManager(session_logger=None)
        mgr.ensure_session("test-1")  # 不应抛异常

    def test_ensure_session_swallows_logger_errors(self):
        """logger 抛异常时仅记录 warning，不向上传播。"""
        logger = MagicMock()
        logger.ensure_session.side_effect = RuntimeError("db error")
        mgr = SessionManager(session_logger=logger)
        mgr.ensure_session("test-1")  # 不应抛异常


class TestTitleGeneration:
    def test_skip_cron_session(self):
        """cron: 前缀的会话不生成标题。"""
        llm = MagicMock()
        mgr = SessionManager(llm_client=llm, session_logger=MagicMock())
        mgr.generate_title_async("cron:test", "你好")
        # 不应创建任何 task
        assert len(mgr._pending_title_tasks) == 0

    def test_skip_no_llm_client(self):
        """llm_client 为 None 时不生成标题。"""
        mgr = SessionManager(llm_client=None, session_logger=MagicMock())
        mgr.generate_title_async("test-1", "你好")
        assert len(mgr._pending_title_tasks) == 0

    def test_skip_no_session_logger(self):
        """session_logger 为 None 时不生成标题。"""
        llm = MagicMock()
        mgr = SessionManager(llm_client=llm, session_logger=None)
        mgr.generate_title_async("test-1", "你好")
        assert len(mgr._pending_title_tasks) == 0

    def test_skip_already_titled(self):
        """已有标题的会话不重复生成。"""
        llm = MagicMock()
        logger = MagicMock()
        logger.get_session_title.return_value = "已有标题"
        mgr = SessionManager(llm_client=llm, session_logger=logger)
        mgr.generate_title_async("test-1", "你好")
        assert len(mgr._pending_title_tasks) == 0
        # 缓存回填
        assert "test-1" in mgr._titled_sessions

    def test_generate_title_async_creates_task(self):
        """无标题的会话应创建异步任务。"""
        llm = MagicMock()
        logger = MagicMock()
        logger.get_session_title.return_value = None
        mgr = SessionManager(llm_client=llm, session_logger=logger)
        # 需在事件循环内创建 task
        async def _run():
            mgr.generate_title_async("test-1", "你好")
            assert len(mgr._pending_title_tasks) == 1
            # 等待任务完成
            tasks = list(mgr._pending_title_tasks)
            for t in tasks:
                await t
        asyncio.run(_run())


class TestTitleCache:
    def test_titled_sessions_init_empty(self):
        """_titled_sessions 初始为空 set。"""
        mgr = SessionManager()
        assert mgr._titled_sessions == set()

    def test_pending_title_tasks_init_empty(self):
        """_pending_title_tasks 初始为空 set。"""
        mgr = SessionManager()
        assert mgr._pending_title_tasks == set()
