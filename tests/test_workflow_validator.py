"""Task 4.3: WorkflowValidator 测试。

覆盖 13 个用例：
- id 重复
- depends_on 环
- 未知模板
- 未知工具
- 写工具无 path_prefix
- 硬禁止工具（针对 config.tool 字段）
- 缺必填字段
- type 非法
- 简易模式合法
- 多步合法
- warnings 非阻断
- merge 结果
- 简易模式裸 dict 兼容
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

from src.tasks.workflow.spec import OnFailure, RetryPolicy, StepSpec, WorkflowSpec
from src.tasks.workflow.validator import (
    HARD_DISABLED_TOOLS,
    ValidationError,
    ValidationResult,
    WorkflowValidator,
    validate_workflow_dict,
)


class _MockToolRegistry:
    """模拟 ToolRegistry，仅提供 get_tools_schema。"""

    def __init__(self, tool_names: List[str]):
        self._tools = tool_names

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        return [{"name": n, "description": "", "input_schema": {}} for n in self._tools]


class TestWorkflowValidatorErrors(unittest.TestCase):
    def setUp(self):
        self.validator = WorkflowValidator()

    def test_step_id_duplicate(self):
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="llm"),
            StepSpec(id="s1", type="llm"),  # 重复
        ])
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("重复" in e.message for e in result.errors))

    def test_depends_on_cycle(self):
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="llm", depends_on=["s2"]),
            StepSpec(id="s2", type="llm", depends_on=["s1"]),  # 环
        ])
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("环" in e.message for e in result.errors))

    def test_depends_on_unknown_step(self):
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="llm", depends_on=["nonexistent"]),
        ])
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("nonexistent" in e.message for e in result.errors))

    def test_unknown_template(self):
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="deterministic", config={"template": "ghost_template"}),
        ])
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("ghost_template" in e.message for e in result.errors))

    def test_unknown_tool_in_registry(self):
        registry = _MockToolRegistry(["file_read", "web_fetch"])
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="tool", config={"tool": "ghost_tool"}),
        ])
        result = self.validator.validate(spec, tool_registry=registry)
        self.assertFalse(result.valid)
        self.assertTrue(any("ghost_tool" in e.message for e in result.errors))

    def test_write_tool_without_path_prefix(self):
        # file_write 是写操作工具，需声明 path_prefix 或 allowed_paths
        registry = _MockToolRegistry(["file_write"])
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="tool", config={"tool": "file_write", "input": {"path": "/tmp/x"}}),
        ])
        result = self.validator.validate(spec, tool_registry=registry)
        self.assertFalse(result.valid)
        self.assertTrue(any("path_prefix" in e.field for e in result.errors))

    def test_hard_disabled_tool_in_config_tool_field(self):
        """硬禁止工具检测：针对 tool 类型 step 的 config.tool 字段。"""
        # bash_exec 在硬禁止清单
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="tool", config={"tool": "bash_exec", "input": {"cmd": "ls"}}),
        ])
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("硬禁止" in e.message for e in result.errors))
        # 验证所有三个硬禁止工具都被拦截
        for tool in HARD_DISABLED_TOOLS:
            spec2 = WorkflowSpec(steps=[
                StepSpec(id="s1", type="tool", config={"tool": tool}),
            ])
            r2 = self.validator.validate(spec2)
            self.assertFalse(r2.valid, f"{tool} 应被拦截")

    def test_missing_required_field_id(self):
        """StepSpec.from_dict 在缺 id 时抛 ValueError，validator 不直接处理。"""
        with self.assertRaises(ValueError):
            StepSpec.from_dict({"type": "llm"})  # 缺 id

    def test_invalid_step_type(self):
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="ghost_type"),
        ])
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("ghost_type" in e.message for e in result.errors))


class TestWorkflowValidatorValid(unittest.TestCase):
    def setUp(self):
        self.validator = WorkflowValidator()

    def test_simple_mode_valid(self):
        """简易模式：仅 template 字段，模板存在则合法。"""
        spec = WorkflowSpec(template="research", template_config={"topic": "test"})
        result = self.validator.validate(spec)
        self.assertTrue(result.valid, f"简易模式应合法，错误: {[e.message for e in result.errors]}")

    def test_multi_step_valid(self):
        """多步合法 workflow：directory_watch → email_notify。"""
        spec = WorkflowSpec(steps=[
            StepSpec(id="watch", type="deterministic",
                     config={"template": "directory_watch", "watch_path": "/tmp"}),
            StepSpec(id="notify", type="deterministic",
                     config={"template": "email_notify"},
                     depends_on=["watch"]),
        ])
        result = self.validator.validate(spec)
        self.assertTrue(result.valid, f"多步应合法，错误: {[e.message for e in result.errors]}")

    def test_tool_step_with_registered_tool(self):
        """tool 类型 step 使用已注册的读取工具，无 path_prefix 要求。"""
        registry = _MockToolRegistry(["file_read", "file_query"])
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="tool", config={"tool": "file_read", "input": {"path": "/tmp"}}),
        ])
        result = self.validator.validate(spec, tool_registry=registry)
        self.assertTrue(result.valid, f"已注册读取工具应合法，错误: {[e.message for e in result.errors]}")

    def test_write_tool_with_path_prefix(self):
        """写操作工具声明了 path_prefix 应合法。"""
        registry = _MockToolRegistry(["file_write"])
        spec = WorkflowSpec(steps=[
            StepSpec(id="s1", type="tool",
                     config={"tool": "file_write", "path_prefix": "/tmp/reports/",
                             "input": {"path": "/tmp/reports/x.md"}}),
        ])
        result = self.validator.validate(spec, tool_registry=registry)
        self.assertTrue(result.valid, f"声明 path_prefix 的写工具应合法，错误: {[e.message for e in result.errors]}")

    def test_simple_mode_bare_dict_compatible(self):
        """简易模式裸 dict（仅 template + 模板配置字段）通过 schema。"""
        bare_dict = {"template": "research", "topic": "AI workflow"}
        result = validate_workflow_dict(bare_dict)
        self.assertTrue(result.valid, f"裸 dict 应合法，错误: {[e.message for e in result.errors]}")


class TestValidationResult(unittest.TestCase):
    def test_warnings_non_blocking(self):
        """warnings 不影响 valid 状态。"""
        result = ValidationResult()
        result.add_warning("s1", "timeout_seconds", "step 超时超过 workflow 超时")
        self.assertTrue(result.valid)  # 仅有 warning 时仍 valid
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(len(result.errors), 0)

    def test_merge_combines_results(self):
        """merge 合并两个结果。"""
        r1 = ValidationResult()
        r1.add_error("s1", "type", "type 非法")
        r1.add_warning("s1", "timeout", "超时过长")

        r2 = ValidationResult()
        r2.add_error("s2", "config", "config 缺字段")

        r1.merge(r2)
        self.assertFalse(r1.valid)
        self.assertEqual(len(r1.errors), 2)
        self.assertEqual(len(r1.warnings), 1)

    def test_to_dict_serialization(self):
        """to_dict 序列化结构正确。"""
        result = ValidationResult()
        result.add_error("s1", "type", "type 非法")
        result.add_warning("s2", "timeout", "超时")
        d = result.to_dict()
        self.assertFalse(d["valid"])
        self.assertEqual(len(d["errors"]), 1)
        self.assertEqual(len(d["warnings"]), 1)
        self.assertEqual(d["errors"][0]["step_id"], "s1")
        self.assertEqual(d["warnings"][0]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
