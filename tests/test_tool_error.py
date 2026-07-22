"""统一工具错误异常层次测试（Phase A）。

覆盖：
- ``ToolError`` 基类与 17 个子类的构造与默认值
- ``to_receipt`` / ``to_system_block`` 文本格式
- ``from_exception`` 异常归一化工厂
- ``from_cron_error`` cron_tool 子进程错误映射
- ``ToolRegistry.execute_tool`` schema 校验与异常上抛
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "teage_liu"))

from teage_liu.agent.tool_error import (  # noqa: E402
    EXECUTION_ERROR_CATEGORIES,
    PRE_EXECUTION_ERROR_CATEGORIES,
    AntiCrawlerError,
    AuthRequiredError,
    CancelledError,
    ErrorStage,
    InternalError,
    LLMFailureError,
    NonStreamHILError,
    NotFoundError,
    OrphanToolResultError,
    ParamError,
    PermanentError,
    PermissionDeniedError,
    PolicyDeniedError,
    StuckDetectedError,
    ToolError,
    ToolNotFoundError,
    ToolTimeoutError,
    TransientError,
    UserRejectedError,
    from_cron_error,
    from_exception,
)
from teage_liu.agent.tool_registry import ToolRegistry  # noqa: E402


class TestToolErrorHierarchy(unittest.TestCase):
    """异常类层次与默认值测试。"""

    def test_pre_execution_subclasses(self):
        """7 个 pre_execution 子类的 stage/category 默认值正确。"""
        cases = [
            (ParamError, "param_error"),
            (ToolNotFoundError, "tool_not_found"),
            (PolicyDeniedError, "policy_denied"),
            (UserRejectedError, "user_rejected"),
            (NonStreamHILError, "non_stream_hil"),
            (StuckDetectedError, "stuck_detected"),
            (CancelledError, "cancelled"),
        ]
        for cls, expected_category in cases:
            with self.subTest(cls=cls.__name__):
                err = cls(tool_name="t", reason="r", suggestion="s")
                self.assertEqual(err.stage, ErrorStage.PRE_EXECUTION)
                self.assertEqual(err.category, expected_category)

    def test_execution_subclasses(self):
        """8 个 execution 子类的 stage/category 默认值正确。"""
        cases = [
            (NotFoundError, "not_found"),
            (PermissionDeniedError, "permission"),  # 注意：类名带 Denied，category 是 permission
            (ToolTimeoutError, "timeout"),
            (TransientError, "transient"),
            (PermanentError, "permanent"),
            (AntiCrawlerError, "anti_crawler"),
            (AuthRequiredError, "auth_required"),
            (InternalError, "internal_error"),
        ]
        for cls, expected_category in cases:
            with self.subTest(cls=cls.__name__):
                err = cls(tool_name="t", reason="r", suggestion="s")
                self.assertEqual(err.stage, ErrorStage.EXECUTION)
                self.assertEqual(err.category, expected_category)

    def test_protocol_subclasses(self):
        """2 个 protocol 子类的 stage/category 默认值正确。"""
        cases = [
            (OrphanToolResultError, "orphan_tool_result"),
            (LLMFailureError, "llm_failure"),
        ]
        for cls, expected_category in cases:
            with self.subTest(cls=cls.__name__):
                err = cls(tool_name="t", reason="r", suggestion="s")
                self.assertEqual(err.stage, ErrorStage.PROTOCOL)
                self.assertEqual(err.category, expected_category)

    def test_category_sets_complete(self):
        """category 集合覆盖所有 17 个子类。"""
        self.assertEqual(len(PRE_EXECUTION_ERROR_CATEGORIES), 7)
        self.assertEqual(len(EXECUTION_ERROR_CATEGORIES), 8)
        # 无交集
        self.assertFalse(PRE_EXECUTION_ERROR_CATEGORIES & EXECUTION_ERROR_CATEGORIES)


class TestToolErrorFormatting(unittest.TestCase):
    """to_receipt / to_system_block 文本格式测试。"""

    def test_to_receipt_execution_format(self):
        """execution 阶段收据格式：[失败] 中文\n原因：...\n建议：..."""
        err = NotFoundError(
            tool_name="file_read",
            reason="文件不存在：/tmp/missing.txt",
            suggestion="检查路径或用 file_glob 查找",
        )
        receipt = err.to_receipt()
        self.assertIn("[失败] 资源不存在", receipt)
        self.assertIn("原因：文件不存在：/tmp/missing.txt", receipt)
        self.assertIn("建议：检查路径或用 file_glob 查找", receipt)

    def test_to_system_block_pre_execution_format(self):
        """pre_execution 阶段注入块格式：[拦截] tool → 中文\n原因：...\n建议：..."""
        err = ParamError(
            tool_name="file_read",
            reason="未声明的参数：['limit']",
            suggestion="工具支持参数：['path', 'offset']",
        )
        block = err.to_system_block()
        self.assertIn("[拦截] file_read → 参数错误", block)
        self.assertIn("原因：未声明的参数：['limit']", block)
        self.assertIn("建议：工具支持参数：['path', 'offset']", block)

    def test_str_representation(self):
        """str(err) 返回 [category] reason 格式，便于日志。"""
        err = InternalError(tool_name="t", reason="boom", suggestion="retry")
        self.assertEqual(str(err), "[internal_error] boom")

    def test_category_zh_fallback(self):
        """未知 category 回退为原值。"""
        # 直接构造基类，category 不在映射表中
        err = ToolError(
            tool_name="t",
            stage=ErrorStage.EXECUTION,
            category="custom_unknown",
            reason="r",
            suggestion="s",
        )
        self.assertIn("custom_unknown", err.to_receipt())


class TestFromException(unittest.TestCase):
    """from_exception 异常归一化测试。"""

    def test_file_not_found_error(self):
        """FileNotFoundError → NotFoundError。"""
        # FileNotFoundError(errno, strerror, filename) 三参构造，filename 才会被读取
        exc = FileNotFoundError(2, "No such file or directory", "/tmp/missing.txt")
        err = from_exception("file_read", exc)
        self.assertIsInstance(err, NotFoundError)
        self.assertEqual(err.category, "not_found")
        self.assertIn("/tmp/missing.txt", err.reason)

    def test_permission_error(self):
        """PermissionError → PermissionDeniedError。"""
        exc = PermissionError(13, "Permission denied", "/tmp/protected.txt")
        err = from_exception("file_read", exc)
        self.assertIsInstance(err, PermissionDeniedError)
        self.assertEqual(err.category, "permission")
        self.assertIn("/tmp/protected.txt", err.reason)

    def test_subprocess_timeout_expired(self):
        """subprocess.TimeoutExpired → ToolTimeoutError。"""
        exc = subprocess.TimeoutExpired(cmd="sleep", timeout=30)
        err = from_exception("bash_exec", exc)
        self.assertIsInstance(err, ToolTimeoutError)
        self.assertEqual(err.category, "timeout")
        self.assertIn("30", err.reason)

    def test_type_error(self):
        """TypeError → PermanentError（幻觉性传参兜底）。"""
        exc = TypeError("got an unexpected keyword argument 'limit'")
        err = from_exception("file_read", exc)
        self.assertIsInstance(err, PermanentError)
        self.assertEqual(err.category, "permanent")

    def test_unknown_exception_fallback(self):
        """未知异常 → InternalError。"""
        exc = RuntimeError("something weird")
        err = from_exception("tool", exc)
        self.assertIsInstance(err, InternalError)
        self.assertEqual(err.category, "internal_error")
        self.assertIn("RuntimeError", err.reason)


class TestFromCronError(unittest.TestCase):
    """from_cron_error cron_tool 子进程错误映射测试。"""

    def test_timeout_mapping(self):
        err = from_cron_error("cron_tool_a", {"error": "took too long", "error_type": "timeout"})
        self.assertIsInstance(err, ToolTimeoutError)
        self.assertIn("took too long", err.reason)

    def test_not_found_mapping(self):
        err = from_cron_error("cron_tool_b", {"error": "no such file", "error_type": "not_found"})
        self.assertIsInstance(err, NotFoundError)

    def test_unknown_error_type_fallback(self):
        err = from_cron_error("cron_tool_c", {"error": "weird", "error_type": "unknown_type"})
        self.assertIsInstance(err, InternalError)


class TestToolRegistrySchemaValidation(unittest.TestCase):
    """ToolRegistry.execute_tool 的 schema 校验与异常上抛测试。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register_core(
            "file_read",
            "读取文件",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                },
                "required": ["path"],
            },
            lambda path, offset=0: f"content of {path}",
        )

    def test_valid_call_returns_result(self):
        """合法参数调用正常返回结果。"""
        result = self.registry.execute_tool("file_read", {"path": "/tmp/a.txt"})
        self.assertEqual(result, "content of /tmp/a.txt")

    def test_hallucinated_param_raises_param_error(self):
        """幻觉性参数（schema 未声明）触发 ParamError。"""
        with self.assertRaises(ParamError) as ctx:
            self.registry.execute_tool(
                "file_read", {"path": "/tmp/a.txt", "limit": 10}
            )
        err = ctx.exception
        self.assertEqual(err.category, "param_error")
        self.assertIn("limit", err.reason)

    def test_missing_required_raises_param_error(self):
        """缺必填参数触发 ParamError。"""
        with self.assertRaises(ParamError) as ctx:
            self.registry.execute_tool("file_read", {"offset": 0})
        err = ctx.exception
        self.assertEqual(err.category, "param_error")
        self.assertIn("path", err.reason)

    def test_required_field_with_falsy_value_not_missing(self):
        """必填字段值为 0/False/"" 时不应被误判为缺失（回归 step_id=0 bug）。

        历史 bug：``not (tool_input or {}).get(k)`` 会把 ``step_id=0`` 误判为缺失，
        导致 LLM 反复重试 plan_update_step 工具，最终触发卡死截停。
        """
        self.registry.register_core(
            "plan_update_step",
            "更新步骤",
            {
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "result": {"type": "string"},
                },
                "required": ["step_id", "status"],
            },
            lambda step_id, status, result="": f"step {step_id} -> {status}",
        )
        # step_id=0 是合法值，不应触发 ParamError
        result = self.registry.execute_tool(
            "plan_update_step", {"step_id": 0, "status": "completed"}
        )
        self.assertEqual(result, "step 0 -> completed")
        # 带 result 字段也合法
        result2 = self.registry.execute_tool(
            "plan_update_step",
            {"step_id": 0, "status": "completed", "result": "已搜索"},
        )
        self.assertEqual(result2, "step 0 -> completed")

    def test_unregistered_tool_raises_not_found(self):
        """未注册工具触发 ToolNotFoundError。"""
        with self.assertRaises(ToolNotFoundError) as ctx:
            self.registry.execute_tool("missing_tool", {})
        err = ctx.exception
        self.assertEqual(err.category, "tool_not_found")

    def test_handler_exception_normalized(self):
        """handler 抛非 ToolError 异常时由 from_exception 归一化。"""
        def _failing_handler(path):
            raise FileNotFoundError(path)

        self.registry.register_core(
            "fail_read",
            "fails",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            _failing_handler,
        )
        with self.assertRaises(NotFoundError) as ctx:
            self.registry.execute_tool("fail_read", {"path": "/tmp/missing.txt"})
        err = ctx.exception
        self.assertEqual(err.category, "not_found")

    def test_tool_error_subclass_propagates(self):
        """handler 抛 ToolError 子类时原样上抛，不被归一化。"""
        def _raising_handler(path):
            raise PermanentError(
                tool_name="custom",
                reason="custom failure",
                suggestion="custom suggestion",
            )

        self.registry.register_core(
            "custom_tool",
            "custom",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            _raising_handler,
        )
        with self.assertRaises(PermanentError) as ctx:
            self.registry.execute_tool("custom_tool", {"path": "x"})
        self.assertEqual(ctx.exception.category, "permanent")
        self.assertEqual(ctx.exception.reason, "custom failure")


if __name__ == "__main__":
    unittest.main()
