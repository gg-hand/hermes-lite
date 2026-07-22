"""RetryHook + _is_permanent_error 测试（5.2）。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

from teage_liu.tasks.hooks.base import RetryDecision
from teage_liu.tasks.hooks.retry_hook import RetryHook, _is_permanent_error
from teage_liu.agent.tool_error import (
    ToolNotFoundError, ParamError, HookAbortError, ValidationError,
    AuthRequiredError, WorkflowExecutionError,
)


class TestIsPermanentError(unittest.TestCase):
    """_is_permanent_error 错误分类（5.2）。"""

    def test_tool_not_found_is_permanent(self):
        err = ToolNotFoundError(tool_name="t", reason="r", suggestion="s")
        self.assertTrue(_is_permanent_error(err))

    def test_param_error_is_permanent(self):
        err = ParamError(tool_name="t", reason="r", suggestion="s")
        self.assertTrue(_is_permanent_error(err))

    def test_validation_error_is_permanent(self):
        err = ValidationError(errors=["e1"])
        self.assertTrue(_is_permanent_error(err))

    def test_hook_abort_error_is_permanent(self):
        err = HookAbortError(reason="abort")
        self.assertTrue(_is_permanent_error(err))

    def test_auth_required_is_permanent(self):
        """P1 修复：AuthRequiredError 加入顶层 isinstance。"""
        err = AuthRequiredError(tool_name="t", reason="r", suggestion="s")
        self.assertTrue(_is_permanent_error(err))

    def test_generic_exception_is_transient(self):
        """未分类异常默认 transient（给一次机会）。"""
        self.assertFalse(_is_permanent_error(ValueError("x")))

    def test_connection_error_is_transient(self):
        self.assertFalse(_is_permanent_error(ConnectionError("net down")))

    def test_workflow_execution_error_with_permanent_step(self):
        """WorkflowExecutionError 含 permanent step trace 视为 permanent。"""
        mock_trace = MagicMock()
        mock_trace.error_class = "permanent"
        mock_result = MagicMock()
        mock_result.step_traces = [mock_trace]
        err = WorkflowExecutionError(result=mock_result)
        self.assertTrue(_is_permanent_error(err))

    def test_workflow_execution_error_with_auth_required_step(self):
        """WorkflowExecutionError 含 auth_required step trace 视为 permanent。"""
        mock_trace = MagicMock()
        mock_trace.error_class = "auth_required"
        mock_result = MagicMock()
        mock_result.step_traces = [mock_trace]
        err = WorkflowExecutionError(result=mock_result)
        self.assertTrue(_is_permanent_error(err))

    def test_workflow_execution_error_with_transient_step(self):
        """WorkflowExecutionError 仅含 transient step trace 视为 transient。"""
        mock_trace = MagicMock()
        mock_trace.error_class = "transient"
        mock_result = MagicMock()
        mock_result.step_traces = [mock_trace]
        err = WorkflowExecutionError(result=mock_result)
        self.assertFalse(_is_permanent_error(err))


class TestRetryHookOnFailure(unittest.TestCase):
    """RetryHook.on_failure 决策逻辑。"""

    def test_permanent_error_gives_up(self):
        hook = RetryHook(config={"max_retries": 3, "interval_seconds": 60})
        ctx = MagicMock()
        ctx.retry_count = 0
        err = ToolNotFoundError(tool_name="t", reason="r", suggestion="s")
        decision = asyncio.run(hook.on_failure(ctx, err))
        self.assertFalse(decision.retry)
        self.assertIn("permanent", decision.reason)

    def test_transient_error_retries(self):
        hook = RetryHook(config={"max_retries": 3, "interval_seconds": 60})
        ctx = MagicMock()
        ctx.retry_count = 0
        decision = asyncio.run(hook.on_failure(ctx, ValueError("x")))
        self.assertTrue(decision.retry)
        self.assertEqual(decision.delay_seconds, 60)

    def test_max_retries_reached_gives_up(self):
        hook = RetryHook(config={"max_retries": 3, "interval_seconds": 60})
        ctx = MagicMock()
        ctx.retry_count = 3  # 已达上限
        decision = asyncio.run(hook.on_failure(ctx, ValueError("x")))
        self.assertFalse(decision.retry)
        self.assertIn("最大重试次数", decision.reason)

    def test_transient_only_false_retries_permanent(self):
        """transient_only=False 时 permanent 也重试。"""
        hook = RetryHook(config={"max_retries": 3, "interval_seconds": 60, "transient_only": False})
        ctx = MagicMock()
        ctx.retry_count = 0
        err = ToolNotFoundError(tool_name="t", reason="r", suggestion="s")
        decision = asyncio.run(hook.on_failure(ctx, err))
        self.assertTrue(decision.retry)

    def test_default_max_retries_is_3(self):
        hook = RetryHook(config={})
        self.assertEqual(hook.max_retries, 3)

    def test_default_interval_is_300(self):
        hook = RetryHook(config={})
        self.assertEqual(hook.interval_seconds, 300)


if __name__ == "__main__":
    unittest.main()
