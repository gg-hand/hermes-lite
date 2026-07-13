"""Workflow 适配器单元测试（Task 6.4）。

覆盖 6 个用例（每个旧模板各自包装为单 step WorkflowSpec）：
1. directory_watch 包装
2. summary 包装
3. email_notify 包装（deterministic）
4. cleanup_suggest 包装
5. research 包装（react 类型推断）
6. custom 包装
"""

from __future__ import annotations

import unittest

from hermes.tasks.workflow.adapter import (
    TEMPLATE_STEP_TYPE_MAP,
    is_adapter_wrapped,
    wrap_template_as_workflow,
)
from hermes.tasks.workflow.spec import WorkflowSpec


class TestWorkflowAdapterWrapDirectoryWatch(unittest.TestCase):
    """1. directory_watch 包装为 WorkflowSpec。"""

    def test_wrap_directory_watch(self):
        spec = wrap_template_as_workflow(
            "directory_watch",
            config={"watch_path": "/tmp/test", "recursive": True},
        )
        self.assertIsInstance(spec, WorkflowSpec)
        self.assertEqual(spec.name, "directory_watch")
        self.assertTrue(spec.is_multi_step_mode())
        self.assertEqual(len(spec.steps), 1)
        step = spec.steps[0]
        self.assertEqual(step.type, "deterministic")
        self.assertEqual(step.config.get("template"), "directory_watch")
        self.assertEqual(step.config.get("watch_path"), "/tmp/test")
        self.assertEqual(step.config.get("recursive"), True)
        # 默认 on_failure=fallback
        self.assertEqual(step.on_failure.action, "fallback")
        self.assertIn("fallback_mode", step.on_failure.fallback_config)
        # adapter 标记
        self.assertTrue(is_adapter_wrapped(spec))
        # 推断的 step 类型（元信息）
        self.assertEqual(spec.metadata.get("inferred_step_type"), "llm")


class TestWorkflowAdapterWrapSummary(unittest.TestCase):
    """2. summary 包装为 WorkflowSpec。"""

    def test_wrap_summary(self):
        spec = wrap_template_as_workflow(
            "summary",
            config={"session_id": "abc123"},
            workflow_name="自定义工作流",
        )
        self.assertEqual(spec.name, "自定义工作流")
        self.assertEqual(len(spec.steps), 1)
        step = spec.steps[0]
        self.assertEqual(step.config.get("template"), "summary")
        self.assertEqual(step.config.get("session_id"), "abc123")
        self.assertTrue(is_adapter_wrapped(spec))
        self.assertEqual(spec.metadata.get("inferred_step_type"), "llm")


class TestWorkflowAdapterWrapEmailNotify(unittest.TestCase):
    """3. email_notify 包装为 WorkflowSpec（推断 deterministic）。"""

    def test_wrap_email_notify(self):
        spec = wrap_template_as_workflow(
            "email_notify",
            config={
                "to": "user@example.com",
                "subject": "测试通知",
                "body": "这是一条测试邮件",
            },
        )
        self.assertEqual(spec.name, "email_notify")
        step = spec.steps[0]
        self.assertEqual(step.config.get("template"), "email_notify")
        self.assertEqual(step.config.get("to"), "user@example.com")
        # email_notify 推断为 deterministic（无 LLM）
        self.assertEqual(spec.metadata.get("inferred_step_type"), "deterministic")
        # 但包装后的 step.type 仍是 deterministic（适配器统一包装）
        self.assertEqual(step.type, "deterministic")


class TestWorkflowAdapterWrapCleanupSuggest(unittest.TestCase):
    """4. cleanup_suggest 包装为 WorkflowSpec。"""

    def test_wrap_cleanup_suggest(self):
        spec = wrap_template_as_workflow(
            "cleanup_suggest",
            config={"threshold": 0.3, "namespace": "test"},
        )
        self.assertEqual(spec.name, "cleanup_suggest")
        step = spec.steps[0]
        self.assertEqual(step.config.get("template"), "cleanup_suggest")
        self.assertEqual(step.config.get("threshold"), 0.3)
        self.assertEqual(spec.metadata.get("inferred_step_type"), "llm")


class TestWorkflowAdapterWrapResearch(unittest.TestCase):
    """5. research 包装为 WorkflowSpec（推断 react）。"""

    def test_wrap_research(self):
        spec = wrap_template_as_workflow(
            "research",
            config={"topic": "AI Agent 框架对比", "max_loops": 10},
        )
        self.assertEqual(spec.name, "research")
        step = spec.steps[0]
        self.assertEqual(step.config.get("template"), "research")
        self.assertEqual(step.config.get("topic"), "AI Agent 框架对比")
        self.assertEqual(step.config.get("max_loops"), 10)
        # research 推断为 react
        self.assertEqual(spec.metadata.get("inferred_step_type"), "react")


class TestWorkflowAdapterWrapCustom(unittest.TestCase):
    """6. custom 包装为 WorkflowSpec。"""

    def test_wrap_custom(self):
        spec = wrap_template_as_workflow(
            "custom",
            config={"tool_name": "cron_tool_1", "tool_input": {"arg": "value"}},
        )
        self.assertEqual(spec.name, "custom")
        step = spec.steps[0]
        self.assertEqual(step.config.get("template"), "custom")
        self.assertEqual(step.config.get("tool_name"), "cron_tool_1")
        self.assertEqual(spec.metadata.get("inferred_step_type"), "llm")


class TestWorkflowAdapterEdgeCases(unittest.TestCase):
    """边界场景补充。"""

    def test_empty_template_name_raises(self):
        with self.assertRaises(ValueError):
            wrap_template_as_workflow("")

    def test_none_config_uses_empty_dict(self):
        spec = wrap_template_as_workflow("research", config=None)
        self.assertEqual(spec.steps[0].config.get("template"), "research")
        # 仅有 template 字段，无其他配置
        self.assertEqual(len(spec.steps[0].config), 1)

    def test_unknown_template_uses_default_step_type(self):
        """未在 TEMPLATE_STEP_TYPE_MAP 中的模板默认推断为 llm。"""
        spec = wrap_template_as_workflow("_unknown_template")
        self.assertEqual(spec.metadata.get("inferred_step_type"), "llm")
        self.assertTrue(is_adapter_wrapped(spec))

    def test_template_step_type_map_completeness(self):
        """TEMPLATE_STEP_TYPE_MAP 含全部 6 个内置模板。"""
        builtin_templates = (
            "directory_watch",
            "summary",
            "email_notify",
            "cleanup_suggest",
            "research",
            "custom",
        )
        for name in builtin_templates:
            self.assertIn(name, TEMPLATE_STEP_TYPE_MAP)

    def test_on_failure_default_fallback_with_single_turn(self):
        """默认 on_failure=fallback，fallback_config 含 fallback_mode=single_turn。"""
        spec = wrap_template_as_workflow("research")
        step = spec.steps[0]
        self.assertEqual(step.on_failure.action, "fallback")
        self.assertEqual(
            step.on_failure.fallback_config.get("fallback_mode"),
            "single_turn",
        )
        self.assertEqual(step.on_failure.fallback_type, "llm")

    def test_workflow_spec_roundtrip(self):
        """包装后的 WorkflowSpec 可序列化为 dict 并重新解析。"""
        spec = wrap_template_as_workflow(
            "directory_watch",
            config={"watch_path": "/tmp"},
        )
        d = spec.to_dict()
        spec2 = WorkflowSpec.from_dict(d)
        self.assertEqual(spec2.name, spec.name)
        self.assertEqual(len(spec2.steps), 1)
        self.assertEqual(spec2.steps[0].config.get("template"), "directory_watch")


if __name__ == "__main__":
    unittest.main()
