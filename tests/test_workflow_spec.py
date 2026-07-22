"""Task 3.3: spec.py 数据模型测试。

覆盖：
- from_dict 解析多步 / 简易模式
- 必填字段校验（id 缺失抛 ValueError）
- OnFailure 默认值（action=abort）
- 嵌套结构（retry.policy / on_failure.fallback_config）
- 简易模式包装（仅 template 字段）
- to_dict / from_dict roundtrip
- type 默认值（llm）
- timeout_seconds 可选
"""

from __future__ import annotations

import unittest

from teage_liu.tasks.workflow.spec import (
    ALLOWED_ON_FAILURE_ACTIONS,
    ALLOWED_STEP_TYPES,
    OnFailure,
    RetryPolicy,
    StepSpec,
    WorkflowSpec,
)


class TestRetryPolicy(unittest.TestCase):
    def test_default_values(self):
        rp = RetryPolicy()
        self.assertEqual(rp.max_attempts, 3)
        self.assertEqual(rp.backoff_strategy, "fixed")
        self.assertEqual(rp.base_delay_ms, 1000)
        self.assertEqual(rp.max_delay_ms, 30000)
        self.assertEqual(rp.retry_on, ["transient", "timeout"])

    def test_from_dict_partial(self):
        rp = RetryPolicy.from_dict({"max_attempts": 5, "backoff_strategy": "exponential"})
        self.assertEqual(rp.max_attempts, 5)
        self.assertEqual(rp.backoff_strategy, "exponential")
        # 缺失字段保留默认
        self.assertEqual(rp.base_delay_ms, 1000)

    def test_from_dict_none_returns_default(self):
        rp = RetryPolicy.from_dict(None)
        self.assertEqual(rp.max_attempts, 3)

    def test_to_dict_roundtrip(self):
        rp = RetryPolicy(max_attempts=7, backoff_strategy="linear", base_delay_ms=500)
        d = rp.to_dict()
        rp2 = RetryPolicy.from_dict(d)
        self.assertEqual(rp2.max_attempts, 7)
        self.assertEqual(rp2.backoff_strategy, "linear")
        self.assertEqual(rp2.base_delay_ms, 500)


class TestOnFailure(unittest.TestCase):
    def test_default_action_is_abort(self):
        of = OnFailure()
        self.assertEqual(of.action, "abort")
        self.assertIsInstance(of.retry, RetryPolicy)

    def test_from_dict_with_retry(self):
        of = OnFailure.from_dict({
            "action": "retry",
            "retry": {"max_attempts": 5, "backoff_strategy": "exponential"},
            "fallback_config": {"fallback_mode": "single_turn"},
        })
        self.assertEqual(of.action, "retry")
        self.assertEqual(of.retry.max_attempts, 5)
        self.assertEqual(of.retry.backoff_strategy, "exponential")
        self.assertEqual(of.fallback_config, {"fallback_mode": "single_turn"})

    def test_from_dict_none_returns_default(self):
        of = OnFailure.from_dict(None)
        self.assertEqual(of.action, "abort")


class TestStepSpec(unittest.TestCase):
    def test_from_dict_minimal(self):
        s = StepSpec.from_dict({"id": "s1"})
        self.assertEqual(s.id, "s1")
        self.assertEqual(s.name, "")
        self.assertEqual(s.type, "llm")  # 默认
        self.assertEqual(s.config, {})
        self.assertEqual(s.depends_on, [])
        self.assertEqual(s.condition, "")
        self.assertIsNone(s.timeout_seconds)
        self.assertIsInstance(s.on_failure, OnFailure)

    def test_from_dict_full(self):
        s = StepSpec.from_dict({
            "id": "research_step",
            "name": "深度研究",
            "type": "react",
            "config": {"task": "研究 workflow 引擎最佳实践", "max_loops": 5},
            "depends_on": ["s1", "s2"],
            "condition": 'steps.s1.outputs.count > 0',
            "on_failure": {"action": "retry", "retry": {"max_attempts": 3}},
            "timeout_seconds": 120,
        })
        self.assertEqual(s.id, "research_step")
        self.assertEqual(s.name, "深度研究")
        self.assertEqual(s.type, "react")
        self.assertEqual(s.config["task"], "研究 workflow 引擎最佳实践")
        self.assertEqual(s.depends_on, ["s1", "s2"])
        self.assertEqual(s.condition, 'steps.s1.outputs.count > 0')
        self.assertEqual(s.on_failure.action, "retry")
        self.assertEqual(s.on_failure.retry.max_attempts, 3)
        self.assertEqual(s.timeout_seconds, 120.0)

    def test_from_dict_missing_id_raises(self):
        with self.assertRaises(ValueError) as ctx:
            StepSpec.from_dict({"type": "llm"})
        self.assertIn("id", str(ctx.exception))

    def test_from_dict_non_dict_raises(self):
        with self.assertRaises(ValueError):
            StepSpec.from_dict("not a dict")  # type: ignore[arg-type]

    def test_to_dict_roundtrip(self):
        s = StepSpec(
            id="s1",
            name="测试",
            type="tool",
            config={"tool": "file_read", "input": {"path": "/tmp"}},
            on_failure=OnFailure(action="skip"),
        )
        d = s.to_dict()
        s2 = StepSpec.from_dict(d)
        self.assertEqual(s2.id, "s1")
        self.assertEqual(s2.type, "tool")
        self.assertEqual(s2.config["tool"], "file_read")
        self.assertEqual(s2.on_failure.action, "skip")


class TestWorkflowSpec(unittest.TestCase):
    def test_multi_step_mode(self):
        wf = WorkflowSpec.from_dict({
            "name": "watch_and_notify",
            "version": 1,
            "steps": [
                {"id": "watch", "type": "deterministic", "config": {"template": "directory_watch"}},
                {"id": "notify", "type": "deterministic", "config": {"template": "email_notify"},
                 "depends_on": ["watch"]},
            ],
            "on_failure": {"action": "abort"},
        })
        self.assertTrue(wf.is_multi_step_mode())
        self.assertFalse(wf.is_simple_mode())
        self.assertEqual(len(wf.steps), 2)
        self.assertEqual(wf.steps[0].id, "watch")
        self.assertEqual(wf.steps[1].depends_on, ["watch"])
        self.assertEqual(wf.on_failure.action, "abort")

    def test_simple_mode_with_template_only(self):
        wf = WorkflowSpec.from_dict({
            "template": "research",
            "template_config": {"topic": "AI workflow best practices"},
        })
        self.assertTrue(wf.is_simple_mode())
        self.assertFalse(wf.is_multi_step_mode())
        self.assertEqual(wf.template, "research")
        self.assertEqual(wf.template_config["topic"], "AI workflow best practices")
        self.assertEqual(wf.steps, [])

    def test_steps_priority_over_template(self):
        """两者同时存在时以 steps 为准（多步模式优先）。"""
        wf = WorkflowSpec.from_dict({
            "template": "research",  # 同时存在 template
            "steps": [{"id": "s1", "type": "llm"}],
        })
        self.assertTrue(wf.is_multi_step_mode())
        self.assertFalse(wf.is_simple_mode())

    def test_from_dict_steps_invalid_raises(self):
        with self.assertRaises(ValueError):
            WorkflowSpec.from_dict({"steps": "not a list"})

    def test_from_dict_step_parse_error_propagates(self):
        with self.assertRaises(ValueError) as ctx:
            WorkflowSpec.from_dict({"steps": [{"type": "llm"}]})  # 缺 id
        self.assertIn("workflow.steps[0]", str(ctx.exception))

    def test_to_dict_roundtrip(self):
        wf = WorkflowSpec(
            name="test_wf",
            version=2,
            steps=[StepSpec(id="s1", type="llm", config={"prompt": "hello"})],
            on_failure=OnFailure(action="retry"),
            timeout_seconds=300,
        )
        d = wf.to_dict()
        wf2 = WorkflowSpec.from_dict(d)
        self.assertEqual(wf2.name, "test_wf")
        self.assertEqual(wf2.version, 2)
        self.assertEqual(len(wf2.steps), 1)
        self.assertEqual(wf2.steps[0].config["prompt"], "hello")
        self.assertEqual(wf2.on_failure.action, "retry")
        self.assertEqual(wf2.timeout_seconds, 300.0)

    def test_from_dict_empty_returns_default(self):
        wf = WorkflowSpec.from_dict({})
        self.assertEqual(wf.name, "")
        self.assertEqual(wf.version, 1)
        self.assertEqual(wf.steps, [])
        self.assertIsNone(wf.template)
        self.assertEqual(wf.on_failure.action, "abort")


class TestAllowedConstants(unittest.TestCase):
    def test_allowed_actions_contains_fallback_skip_abort(self):
        # Q3 决策：移除 "retry"，重试由 RetryHook 接管整次 workflow 重跑
        for action in ("fallback", "skip", "abort"):
            self.assertIn(action, ALLOWED_ON_FAILURE_ACTIONS)
        self.assertNotIn("retry", ALLOWED_ON_FAILURE_ACTIONS)

    def test_allowed_step_types_contains_five_types(self):
        for st in ("deterministic", "llm", "tool", "react", "subworkflow"):
            self.assertIn(st, ALLOWED_STEP_TYPES)


if __name__ == "__main__":
    unittest.main()
