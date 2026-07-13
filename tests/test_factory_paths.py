# tests/test_factory_paths.py
"""测试 app.py 工厂函数导入路径正确性。

spec 2026-07-13 阶段 1：修复 10 个工厂导入路径 + HealthChecker 参数。
"""
from __future__ import annotations
import sys
import os
from unittest.mock import patch, MagicMock

# 确保 src 在 path 中
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "hermes")
class TestFactoryImportPaths:
    """验证每个工厂函数使用正确的导入路径。"""

    def test_session_logger_path(self):
        """_create_session_logger 应从 storage.sqlite_log 导入。"""
        from hermes.app import _create_session_logger
        with patch("hermes.storage.sqlite_log.SessionLogger") as mock_cls:
            _create_session_logger({"storage": {"sqlite_path": "data/test.db"}})
            mock_cls.assert_called_once_with(db_path="data/test.db")

    def test_metrics_store_path(self):
        """_create_metrics_store 应从 monitoring.metrics_store 导入。"""
        from hermes.app import _create_metrics_store
        with patch("hermes.monitoring.metrics_store.MetricsStore") as mock_cls:
            result = _create_metrics_store(
                {"monitoring": {"enabled": True, "daily_persistence": True},
                 "storage": {"sqlite_path": "data/test.db"}}
            )
            mock_cls.assert_called_once_with(db_path="data/test.db")

    def test_audit_logger_path(self):
        """_create_audit_logger 应从 agent.audit 导入。"""
        from hermes.app import _create_audit_logger
        with patch("hermes.agent.audit.AuditLogger") as mock_cls:
            _create_audit_logger({"monitoring": {"enabled": True}})
            mock_cls.assert_called_once()

    def test_approval_manager_path(self):
        """_create_approval_manager 应从 agent.approval 导入。"""
        from hermes.app import _create_approval_manager
        with patch("hermes.agent.approval.ApprovalManager") as mock_cls:
            _create_approval_manager({"security": {"approval_timeout_seconds": 300}})
            mock_cls.assert_called_once()

    def test_stream_manager_path(self):
        """_create_stream_manager 应从 stream_manager 导入（根级模块）。"""
        from hermes.app import _create_stream_manager
        with patch("hermes.stream_manager.StreamManager") as mock_cls:
            _create_stream_manager()
            mock_cls.assert_called_once()

    def test_skill_loader_path(self):
        """_create_skill_loader 应从 skill.loader 导入。"""
        from hermes.app import _create_skill_loader
        with patch("hermes.skill.loader.SkillLoader") as mock_cls:
            _create_skill_loader()
            mock_cls.assert_called_once()

    def test_proposal_store_path(self):
        """_create_proposal_store 应从 agent.cron_proposals 导入。"""
        from hermes.app import _create_proposal_store
        with patch("hermes.agent.cron_proposals.ProposalStore") as mock_cls:
            _create_proposal_store()
            mock_cls.assert_called_once()

    def test_mcp_manager_path(self):
        """_create_mcp_manager 应从 mcp.manager 导入。"""
        from hermes.app import _create_mcp_manager
        with patch("hermes.mcp.manager.MCPManager") as mock_cls:
            _create_mcp_manager({"skills": {}})
            mock_cls.assert_called_once()

    def test_etl_engine_paths(self):
        """_create_etl_engine 应从 files.parser + files.chunker 导入。"""
        from hermes.app import _create_etl_engine
        with patch("hermes.files.parser.WaterfallParser") as mock_parser, \
             patch("hermes.files.chunker.DocumentChunker") as mock_chunker, \
             patch("hermes.files.etl_engine.ETLEngine") as mock_etl:
            mock_orch = MagicMock()
            _create_etl_engine({"files": {}}, upload_manager=None, orchestrator=mock_orch)
            mock_parser.assert_called_once()
            mock_chunker.assert_called_once()
            mock_etl.assert_called_once()

    def test_cron_scheduler_path(self):
        """_create_cron_scheduler 应从 tasks.scheduler 导入。"""
        from hermes.app import _create_cron_scheduler
        with patch("hermes.tasks.scheduler.CronScheduler") as mock_cls:
            _create_cron_scheduler({"tasks": {}})
            mock_cls.assert_called_once()


class TestHealthCheckerFactory:
    """验证 HealthChecker 工厂补全为 6 参数。"""

    def test_health_checker_receives_six_args(self):
        """_create_health_checker 应传入 6 个参数给 HealthChecker。"""
        from hermes.app import _create_health_checker
        with patch("hermes.monitoring.health.HealthChecker") as mock_cls:
            mock_orch = MagicMock()
            mock_session = MagicMock()
            mock_mcp = MagicMock()
            mock_skill = MagicMock()
            mock_metrics = MagicMock()
            mock_proposal = MagicMock()
            _create_health_checker(
                orchestrator=mock_orch,
                session_logger=mock_session,
                mcp_manager=mock_mcp,
                skill_loader=mock_skill,
                metrics_collector=mock_metrics,
                proposal_store=mock_proposal,
            )
            mock_cls.assert_called_once_with(
                orchestrator=mock_orch,
                session_logger_global=mock_session,
                mcp_manager=mock_mcp,
                skill_loader=mock_skill,
                metrics_collector=mock_metrics,
                proposal_store=mock_proposal,
            )
