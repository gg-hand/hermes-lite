"""异常类定义测试（9.2）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.agent.tool_error import (
    HookAbortError, ValidationError, WorkflowExecutionError, ToolError, ErrorStage,
)


class TestHookAbortError(unittest.TestCase):

    def test_inherits_tool_error(self):
        err = HookAbortError(reason="校验终止", errors=["err1", "err2"])
        self.assertIsInstance(err, ToolError)

    def test_carries_errors_list(self):
        err = HookAbortError(reason="终止", errors=["e1", "e2"])
        self.assertEqual(err.errors, ["e1", "e2"])

    def test_errors_defaults_empty(self):
        err = HookAbortError(reason="终止")
        self.assertEqual(err.errors, [])

    def test_stage_is_pre_execution(self):
        err = HookAbortError(reason="终止")
        self.assertEqual(err.stage, ErrorStage.PRE_EXECUTION)


class TestValidationError(unittest.TestCase):

    def test_inherits_tool_error(self):
        err = ValidationError(errors=["step1 无效"])
        self.assertIsInstance(err, ToolError)

    def test_carries_errors_list(self):
        err = ValidationError(errors=["e1", "e2"])
        self.assertEqual(err.errors, ["e1", "e2"])

    def test_reason_joined_from_errors(self):
        err = ValidationError(errors=["e1", "e2"])
        self.assertIn("e1", err.reason)
        self.assertIn("e2", err.reason)


class TestWorkflowExecutionError(unittest.TestCase):

    def test_inherits_tool_error(self):
        mock_result = MagicMock()
        mock_result.errors = ["step1 失败"]
        err = WorkflowExecutionError(result=mock_result)
        self.assertIsInstance(err, ToolError)

    def test_carries_result(self):
        mock_result = MagicMock()
        mock_result.errors = ["失败"]
        err = WorkflowExecutionError(result=mock_result)
        self.assertIs(err.result, mock_result)

    def test_stage_is_execution(self):
        mock_result = MagicMock()
        mock_result.errors = []
        err = WorkflowExecutionError(result=mock_result)
        self.assertEqual(err.stage, ErrorStage.EXECUTION)


if __name__ == "__main__":
    unittest.main()
