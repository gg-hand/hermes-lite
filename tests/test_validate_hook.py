"""ValidateHook + validate_workflow_spec 测试（4.1）。"""
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

from hermes.tasks.hooks.validate_hook import ValidateHook
from hermes.tasks.workflow.validator import validate_workflow_spec
from hermes.tasks.workflow.spec import WorkflowSpec, StepSpec


class TestValidateWorkflowSpec(unittest.TestCase):
    """validate_workflow_spec 工具存在性 + step_type 校验。"""

    def test_valid_spec_returns_empty_errors(self):
        spec = WorkflowSpec(name="test", steps=[
            StepSpec(id="s1", name="step1", type="llm"),
        ])
        errors = validate_workflow_spec(spec=spec, cron_tool_registry=None, tool_registry=None)
        self.assertEqual(errors, [])

    def test_invalid_step_type_returns_error(self):
        spec = WorkflowSpec(name="test", steps=[
            StepSpec(id="s1", name="step1", type="invalid_type"),
        ])
        errors = validate_workflow_spec(spec=spec, cron_tool_registry=None, tool_registry=None)
        self.assertTrue(any("invalid_type" in e for e in errors))

    def test_tool_step_missing_tool_config_returns_error(self):
        spec = WorkflowSpec(name="test", steps=[
            StepSpec(id="s1", name="step1", type="tool", config={}),
        ])
        errors = validate_workflow_spec(spec=spec, cron_tool_registry=None, tool_registry=None)
        self.assertTrue(any("未配置" in e or "config.tool" in e for e in errors))

    def test_tool_step_with_unregistered_tool_returns_error(self):
        spec = WorkflowSpec(name="test", steps=[
            StepSpec(id="s1", name="step1", type="tool", config={"tool": "nonexistent"}),
        ])
        mock_cron_reg = MagicMock()
        mock_cron_reg.has_tool.return_value = False
        mock_tool_reg = MagicMock()
        mock_tool_reg.has_tool.return_value = False
        errors = validate_workflow_spec(
            spec=spec, cron_tool_registry=mock_cron_reg, tool_registry=mock_tool_reg
        )
        self.assertTrue(any("nonexistent" in e for e in errors))

    def test_tool_step_with_registered_cron_tool_returns_no_error(self):
        spec = WorkflowSpec(name="test", steps=[
            StepSpec(id="s1", name="step1", type="tool", config={"tool": "cron_tool__echo"}),
        ])
        mock_cron_reg = MagicMock()
        mock_cron_reg.has_tool.return_value = True
        errors = validate_workflow_spec(
            spec=spec, cron_tool_registry=mock_cron_reg, tool_registry=None
        )
        self.assertEqual(errors, [])


class TestValidateHook(unittest.TestCase):
    """ValidateHook.before_execute 填充 ctx.validation_errors。"""

    def test_before_execute_no_schedule_returns_ctx(self):
        hook = ValidateHook(config={})
        ctx = MagicMock()
        ctx.schedule = None
        result = asyncio.run(hook.before_execute(ctx))
        self.assertIs(result, ctx)

    def test_before_execute_no_workflow_returns_ctx(self):
        hook = ValidateHook(config={})
        ctx = MagicMock()
        ctx.schedule = MagicMock(workflow=None)
        result = asyncio.run(hook.before_execute(ctx))
        self.assertIs(result, ctx)

    def test_before_execute_valid_spec_no_errors(self):
        hook = ValidateHook(config={})
        ctx = MagicMock()
        ctx.schedule = MagicMock()
        ctx.schedule.workflow = WorkflowSpec(name="t", steps=[])
        ctx.cron_tool_registry = None
        ctx.tool_registry = None
        ctx.validation_errors = []
        result = asyncio.run(hook.before_execute(ctx))
        self.assertEqual(result.validation_errors, [])

    def test_before_execute_invalid_spec_fills_errors(self):
        hook = ValidateHook(config={})
        ctx = MagicMock()
        ctx.schedule = MagicMock()
        ctx.schedule.workflow = WorkflowSpec(name="t", steps=[
            StepSpec(id="s1", name="s", type="invalid"),
        ])
        ctx.cron_tool_registry = None
        ctx.tool_registry = None
        ctx.validation_errors = []
        result = asyncio.run(hook.before_execute(ctx))
        self.assertTrue(len(result.validation_errors) > 0)


if __name__ == "__main__":
    unittest.main()
