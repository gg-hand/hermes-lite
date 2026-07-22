# tests/test_depends_functions.py
"""测试 app.py Depends 函数正确返回容器组件。

spec 2026-07-13 阶段 2：添加 11+ 个 Depends 函数供路由类型安全注入。
"""
from __future__ import annotations
import sys
import os
from unittest.mock import MagicMock

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "teage_liu")
class TestDependsFunctions:

    def test_get_container_or_raise_uninitialized(self):
        """容器未初始化时 get_container_or_raise 抛 RuntimeError。"""
        from teage_liu.app import get_container
        # 确保容器未初始化
        import app
        app._container = None
        try:
            app.get_container_or_raise()
            assert False, "应抛 RuntimeError"
        except RuntimeError as e:
            assert "未初始化" in str(e)

    def test_get_orchestrator(self):
        """get_orchestrator 返回容器中的 orchestrator。"""
        import app
        mock_container = MagicMock()
        mock_orch = MagicMock()
        mock_container.get.return_value = mock_orch
        app._container = mock_container

        result = app.get_orchestrator()
        mock_container.get.assert_called_once_with("orchestrator")
        assert result is mock_orch

    def test_get_session_logger(self):
        """get_session_logger 返回容器中的 session_logger。"""
        import app
        mock_container = MagicMock()
        mock_logger = MagicMock()
        mock_container.get.return_value = mock_logger
        app._container = mock_container

        result = app.get_session_logger()
        mock_container.get.assert_called_once_with("session_logger")
        assert result is mock_logger

    def test_all_depends_functions_exist(self):
        """验证所有 15 个 Depends 函数都存在。"""
        import app
        expected = [
            "get_container_or_raise",
            "get_orchestrator",
            "get_session_logger",
            "get_metrics_collector",
            "get_metrics_store",
            "get_audit_logger",
            "get_approval_manager",
            "get_task_manager",
            "get_stream_manager",
            "get_skill_loader",
            "get_mcp_manager",
            "get_upload_manager",
            "get_etl_engine",
            "get_cron_scheduler",
            "get_proposal_store",
            "get_health_checker",
        ]
        for name in expected:
            assert hasattr(app, name), f"app.{name} 不存在"
            assert callable(getattr(app, name)), f"app.{name} 不可调用"
