"""HealthChecker 单元测试。

覆盖 CheckResult 构造、全组件健康→healthy、组件缺失→unhealthy/ degraded、
异常安全（run_all 不抛异常）、日志断言。
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.monitoring.health import (  # noqa: E402
    CheckResult,
    HealthChecker,
    HealthSummary,
)


def _make_mock_orchestrator(**overrides) -> object:
    """创建一个全属性为 "已初始化" 的 mock orchestrator。

    调用方可传入 overrides 覆盖特定属性为 None（模拟组件缺失）。
    """
    from unittest.mock import MagicMock

    # 默认所有组件都存在
    attrs = {
        "llm_client": MagicMock(),
        "tool_registry": MagicMock(),
        "react_loop": MagicMock(),
        "policy_engine": MagicMock(),
        "approval_manager": MagicMock(),
        "history_buffer": MagicMock(),
        "condenser": MagicMock(),
        "chroma_store": MagicMock(),
        "session_logger": MagicMock(),
        "memory_md_manager": MagicMock(),
        "decay": MagicMock(),
        "memory_retriever": MagicMock(),
        "context_manager": MagicMock(),
        "consolidation_engine": MagicMock(),
        "task_manager": MagicMock(),
        "todo_registry": MagicMock(),
        "file_registry": MagicMock(),
        "cron_scheduler": MagicMock(),
        "cron_tool_registry": MagicMock(),
    }
    attrs.update(overrides)
    obj = MagicMock(**attrs)

    # chroma_store.collection.count() 默认返回 42
    if attrs.get("chroma_store") is not None:
        obj.chroma_store.collection.count.return_value = 42

    # llm_client 子客户端
    if attrs.get("llm_client") is not None:
        obj.llm_client._main_client = MagicMock()
        obj.llm_client._consolidation_client = MagicMock()

    # cron_scheduler.list_schedules 默认返回空列表
    if attrs.get("cron_scheduler") is not None:
        obj.cron_scheduler.list_schedules.return_value = []

    # cron_tool_registry.list_tool_names 默认返回空列表
    if attrs.get("cron_tool_registry") is not None:
        obj.cron_tool_registry.list_tool_names.return_value = []

    # consolidation_engine 默认值
    if attrs.get("consolidation_engine") is not None:
        obj.consolidation_engine.threshold = 15
        obj.consolidation_engine.info_counter = 3

    return obj


class TestCheckResult(unittest.TestCase):
    """CheckResult dataclass 构造与字段访问。"""

    def test_fields(self):
        r = CheckResult("ok", "一切正常", {"key": "val"})
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.message, "一切正常")
        self.assertEqual(r.detail, {"key": "val"})

    def test_default_detail_none(self):
        r = CheckResult("critical", "挂掉了")
        self.assertIsNone(r.detail)

    def test_status_types(self):
        for s in ("ok", "warning", "critical"):
            r = CheckResult(s, "test")
            self.assertEqual(r.status, s)


class TestHealthSummary(unittest.TestCase):
    """HealthSummary 计数。"""

    def test_defaults(self):
        s = HealthSummary()
        self.assertEqual(s.total, 0)
        self.assertEqual(s.ok, 0)
        self.assertEqual(s.warning, 0)
        self.assertEqual(s.critical, 0)

    def test_counts(self):
        s = HealthSummary(total=10, ok=7, warning=2, critical=1)
        self.assertEqual(s.total, 10)
        self.assertEqual(s.ok, 7)
        self.assertEqual(s.warning, 2)
        self.assertEqual(s.critical, 1)


class TestHealthCheckerAllOk(unittest.TestCase):
    """全组件已初始化 → healthy。"""

    def setUp(self):
        self.mock_o = _make_mock_orchestrator()
        self.checker = HealthChecker(
            orchestrator=self.mock_o,
            session_logger_global=unittest.mock.MagicMock(),
            mcp_manager=unittest.mock.MagicMock(),
            skill_loader=unittest.mock.MagicMock(),
            metrics_collector=unittest.mock.MagicMock(),
            proposal_store=unittest.mock.MagicMock(),
        )
        # 使 mock 对象有预期的行为
        self.checker._mcp._clients = {"server1": None}
        self.checker._skill.discover.return_value = []
        self.checker._proposal.list.return_value = []

    def test_overall_healthy(self):
        result = self.checker.run_all()
        self.assertEqual(result["status"], "healthy")

    def test_26_checks(self):
        result = self.checker.run_all()
        # 26 个检查项
        self.assertEqual(result["summary"]["total"], 26)
        self.assertEqual(result["summary"]["ok"], 26)
        self.assertEqual(result["summary"]["warning"], 0)
        self.assertEqual(result["summary"]["critical"], 0)

    def test_response_shape(self):
        result = self.checker.run_all()
        self.assertIn("status", result)
        self.assertIn("timestamp", result)
        self.assertIn("summary", result)
        self.assertIn("checks", result)
        # checks 包含所有子系统名称
        expected_checks = [
            "orchestrator", "llm_client", "react_loop", "session_logger",
            "tool_registry", "chroma_store", "history_buffer",
            "consolidation_engine", "memory_retriever", "memory_md_manager",
            "context_manager", "condenser", "decay", "policy_engine",
            "approval_manager", "audit_logger", "file_registry",
            "todo_registry", "task_manager", "cron_scheduler",
            "cron_tool_registry", "proposal_store", "skill_loader",
            "mcp_manager", "metrics_collector", "disk",
        ]
        for name in expected_checks:
            self.assertIn(name, result["checks"], f"缺少检查项: {name}")

    def test_each_check_has_required_fields(self):
        result = self.checker.run_all()
        for name, check in result["checks"].items():
            with self.subTest(check=name):
                self.assertIn("status", check, f"{name} 缺 status")
                self.assertIn("message", check, f"{name} 缺 message")
                self.assertIn("detail", check, f"{name} 缺 detail")


class TestHealthCheckerCritical(unittest.TestCase):
    """Critical 组件缺失 → unhealthy。"""

    def test_orchestrator_none(self):
        checker = HealthChecker(orchestrator=None)
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertGreaterEqual(result["summary"]["critical"], 1)
        self.assertEqual(result["checks"]["orchestrator"]["status"], "critical")
        # 全部其他检查也应为非 ok（因为 orchestrator 为 None 时依赖它的检查也无法通过）
        self.assertLess(result["summary"]["ok"], 26)

    def test_llm_client_none(self):
        mock_o = _make_mock_orchestrator(llm_client=None)
        checker = HealthChecker(orchestrator=mock_o)
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["checks"]["llm_client"]["status"], "critical")

    def test_react_loop_none(self):
        mock_o = _make_mock_orchestrator(react_loop=None)
        checker = HealthChecker(orchestrator=mock_o)
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["checks"]["react_loop"]["status"], "critical")

    def test_session_logger_none(self):
        mock_o = _make_mock_orchestrator()
        checker = HealthChecker(orchestrator=mock_o, session_logger_global=None)
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["checks"]["session_logger"]["status"], "critical")

    def test_session_logger_query_fails(self):
        logger_mock = unittest.mock.MagicMock()
        logger_mock.list_sessions.side_effect = RuntimeError("DB locked")
        mock_o = _make_mock_orchestrator()
        checker = HealthChecker(orchestrator=mock_o, session_logger_global=logger_mock)
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertIn("DB locked", result["checks"]["session_logger"]["message"])

    def test_llm_main_client_none(self):
        mock_o = _make_mock_orchestrator()
        mock_o.llm_client._main_client = None
        mock_o.llm_client._consolidation_client = unittest.mock.MagicMock()
        checker = HealthChecker(orchestrator=mock_o)
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["checks"]["llm_client"]["status"], "critical")


class TestHealthCheckerWarning(unittest.TestCase):
    """Warning 组件缺失 → degraded 但非 unhealthy。"""

    def _make_checker(self, **orchestrator_overrides):
        """创建 HealthChecker，默认传全全局 mock 避免 critical。"""
        mock_o = _make_mock_orchestrator(**orchestrator_overrides)
        return HealthChecker(
            orchestrator=mock_o,
            session_logger_global=unittest.mock.MagicMock(),
            mcp_manager=unittest.mock.MagicMock(),
            skill_loader=unittest.mock.MagicMock(),
            metrics_collector=unittest.mock.MagicMock(),
            proposal_store=unittest.mock.MagicMock(),
        )

    def test_chroma_store_none(self):
        checker = self._make_checker(chroma_store=None)
        result = checker.run_all()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["chroma_store"]["status"], "warning")

    def test_tool_registry_none(self):
        checker = self._make_checker(tool_registry=None)
        result = checker.run_all()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["tool_registry"]["status"], "warning")

    def test_cron_scheduler_none(self):
        checker = self._make_checker(cron_scheduler=None)
        result = checker.run_all()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["cron_scheduler"]["status"], "warning")

    def test_proposal_store_none(self):
        checker = self._make_checker()
        checker._proposal = None
        result = checker.run_all()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["proposal_store"]["status"], "warning")

    def test_skill_loader_none(self):
        checker = self._make_checker()
        checker._skill = None
        result = checker.run_all()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["skill_loader"]["status"], "warning")

    def test_mcp_manager_none(self):
        checker = self._make_checker()
        checker._mcp = None
        result = checker.run_all()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["mcp_manager"]["status"], "warning")

    def test_disk_low_space(self):
        checker = self._make_checker()
        with unittest.mock.patch("shutil.disk_usage") as mock_du:
            mock_du.return_value.total = 100 * 1024 ** 3  # 100GB
            mock_du.return_value.free = 500 * 1024 * 1024  # 500MB
            result = checker.run_all()
        self.assertIn(result["status"], ("degraded", "unhealthy"))
        self.assertEqual(result["checks"]["disk"]["status"], "warning")
        self.assertIn("不足", result["checks"]["disk"]["message"])


class TestHealthCheckerExceptionSafety(unittest.TestCase):
    """run_all 永不抛异常，异常降级为 CheckResult。"""

    def test_chroma_store_count_raises(self):
        mock_o = _make_mock_orchestrator()
        mock_o.chroma_store.collection.count.side_effect = RuntimeError("connection refused")
        checker = HealthChecker(orchestrator=mock_o)
        # 不应抛异常
        result = checker.run_all()
        self.assertEqual(result["checks"]["chroma_store"]["status"], "warning")

    def test_skill_discover_raises(self):
        mock_o = _make_mock_orchestrator()
        skill_mock = unittest.mock.MagicMock()
        skill_mock.discover.side_effect = OSError("permission denied")
        checker = HealthChecker(orchestrator=mock_o, skill_loader=skill_mock)
        result = checker.run_all()
        self.assertEqual(result["checks"]["skill_loader"]["status"], "warning")

    def test_proposal_list_raises(self):
        mock_o = _make_mock_orchestrator()
        prop_mock = unittest.mock.MagicMock()
        prop_mock.list.side_effect = ValueError("corrupted state")
        checker = HealthChecker(orchestrator=mock_o, proposal_store=prop_mock)
        result = checker.run_all()
        self.assertEqual(result["checks"]["proposal_store"]["status"], "warning")

    def test_check_fn_itself_raises(self):
        """检查函数内部抛异常时，run_all 应兜底为 CheckResult。"""
        def _broken_check():
            raise RuntimeError("unexpected crash")
        mock_o = _make_mock_orchestrator()
        checker = HealthChecker(orchestrator=mock_o)
        # 替换 _checks 列表，验证 run_all 兜底机制
        checker._checks = [("broken", "critical", _broken_check)]
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertIn("异常", result["checks"]["broken"]["message"])


class TestHealthCheckerLogging(unittest.TestCase):
    """日志输出断言：ok 不输出，warning/critical 输出。"""

    def test_healthy_no_warning_logs(self):
        """全部 ok 时不应有 WARNING 或 ERROR 日志。"""
        import io

        mock_o = _make_mock_orchestrator()
        checker = HealthChecker(
            orchestrator=mock_o,
            session_logger_global=unittest.mock.MagicMock(),
            mcp_manager=unittest.mock.MagicMock(),
            skill_loader=unittest.mock.MagicMock(),
            metrics_collector=unittest.mock.MagicMock(),
            proposal_store=unittest.mock.MagicMock(),
        )
        checker._mcp._clients = {"s1": None}
        checker._skill.discover.return_value = []
        checker._proposal.list.return_value = []

        # 接管 logger，捕获所有 WARNING+ 输出
        logger = logging.getLogger("src.monitoring.health")
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        try:
            checker.run_all()
            output = buf.getvalue()
            self.assertEqual(output, "", f"不应有 WARNING/ERROR 日志: {output!r}")
        finally:
            logger.removeHandler(handler)

    def test_warning_produces_log(self):
        mock_o = _make_mock_orchestrator(chroma_store=None)
        checker = HealthChecker(orchestrator=mock_o)
        with self.assertLogs("src.monitoring.health", level="WARNING") as cm:
            checker.run_all()
        self.assertTrue(
            any("chroma" in msg for msg in cm.output),
            f"应包含 chroma 相关日志: {cm.output}",
        )

    def test_critical_produces_error_log(self):
        checker = HealthChecker(orchestrator=None)
        with self.assertLogs("src.monitoring.health", level="WARNING") as cm:
            checker.run_all()
        self.assertTrue(
            any("orchestrator" in msg for msg in cm.output),
            f"应包含 orchestrator 相关日志: {cm.output}",
        )


class TestHealthCheckerEdgeCases(unittest.TestCase):
    """边界情况。"""

    def test_only_orchestrator_no_globals(self):
        """仅传 orchestrator，其余全局 None → 部分 warning 但不会崩溃。"""
        mock_o = _make_mock_orchestrator()
        checker = HealthChecker(orchestrator=mock_o)
        result = checker.run_all()
        # 不应抛异常，status 应为 degraded 或 unhealthy
        self.assertIn(result["status"], ("degraded", "unhealthy"))
        # orchestrator 本身是 ok 的
        self.assertEqual(result["checks"]["orchestrator"]["status"], "ok")

    def test_all_none(self):
        """连 orchestrator 都是 None → unhealthy + critical。"""
        checker = HealthChecker(orchestrator=None)
        result = checker.run_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertGreaterEqual(result["summary"]["critical"], 4)

    def test_session_logger_db_path(self):
        """session_logger 的 db_path 属性应出现在 detail 中。"""
        mock_o = _make_mock_orchestrator()
        logger_mock = unittest.mock.MagicMock()
        logger_mock.db_path = "data/test.db"
        checker = HealthChecker(orchestrator=mock_o, session_logger_global=logger_mock)
        result = checker.run_all()
        self.assertEqual(result["checks"]["session_logger"]["detail"]["db_path"], "data/test.db")


if __name__ == "__main__":
    unittest.main()
