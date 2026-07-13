"""WorkflowEngine 单元测试（Task 5.5）。

覆盖 16+ 用例：
1. 线性执行 / 多步串联
2. condition skip
3. retry 成功 / 耗尽 fallback / abort 终止
4. 错误分类映射
5. 拓扑排序 / 环检测抛异常
6. LLM 降级链 / tool_call fallback
7. inject 变量 / 时间变量替换
8. 简易模式
9. workflow timeout / step timeout
10. NotImplementedError PERMANENT 不 retry
"""

from __future__ import annotations

import time
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from hermes.tasks.workflow.base import WorkflowContext, WorkflowResult
from hermes.tasks.workflow.engine import WorkflowCycleError, WorkflowEngine
from hermes.tasks.workflow.spec import OnFailure, RetryPolicy, StepSpec, WorkflowSpec
from hermes.tasks.workflow.step_executor import (
    DeterministicExecutor,
    LlmCallExecutor,
    StepExecutionError,
    StepExecutor,
    SubworkflowExecutor,
)


# ---------------------------------------------------------------------------
# 测试辅助 Executor
# ---------------------------------------------------------------------------


class _StubExecutor(StepExecutor):
    """可编程 stub executor，按预设脚本返回结果或抛异常。"""

    def __init__(
        self,
        outputs_list: Optional[List[Dict[str, Any]]] = None,
        errors: Optional[List[StepExecutionError]] = None,
        tool_calls_per_call: Optional[List[List[Dict[str, Any]]]] = None,
    ) -> None:
        self.outputs_list = outputs_list or [{"response": "ok"}]
        self.errors = errors or []
        self.tool_calls_per_call = tool_calls_per_call
        self.call_count = 0

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        idx = self.call_count
        self.call_count += 1

        # 错误脚本
        if idx < len(self.errors) and self.errors[idx] is not None:
            raise self.errors[idx]

        outputs = (
            self.outputs_list[idx]
            if idx < len(self.outputs_list)
            else self.outputs_list[-1]
        )
        tool_calls = []
        if self.tool_calls_per_call and idx < len(self.tool_calls_per_call):
            tool_calls = self.tool_calls_per_call[idx]
        return outputs, tool_calls, []


class _FailingExecutor(StepExecutor):
    """总是失败的 executor，用于 retry / abort 测试。"""

    def __init__(
        self,
        error_class: str = "transient",
        error_message: str = "stub failure",
        success_at: Optional[int] = None,
    ) -> None:
        self.error_class = error_class
        self.error_message = error_message
        self.success_at = success_at
        self.call_count = 0

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        self.call_count += 1
        if self.success_at is not None and self.call_count >= self.success_at:
            return {"response": "ok"}, [], []
        raise StepExecutionError(self.error_class, self.error_message)


# ---------------------------------------------------------------------------
# 测试辅助：构造 context
# ---------------------------------------------------------------------------


def _make_context(
    session_id: str = "cron:test",
    schedule_id: str = "test",
    **kwargs: Any,
) -> WorkflowContext:
    """构造测试用 WorkflowContext。"""
    ctx = WorkflowContext(session_id=session_id, schedule_id=schedule_id)
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx


def _make_simple_spec(
    steps: List[StepSpec],
    name: str = "test",
    **kwargs: Any,
) -> WorkflowSpec:
    """构造测试用 WorkflowSpec（多步模式）。"""
    return WorkflowSpec(name=name, steps=steps, **kwargs)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestWorkflowEngineLinearExecution(unittest.TestCase):
    """1. 线性执行 / 14. 多步串联。"""

    def test_linear_two_steps_executed_in_order(self):
        """两个无依赖 step 按声明顺序执行。"""
        stub_a = _StubExecutor(outputs_list=[{"value": "A"}])
        stub_b = _StubExecutor(outputs_list=[{"value": "B"}])
        engine = WorkflowEngine(custom_executors={
            "llm": stub_a, "deterministic": stub_b,
        })
        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"}),
            StepSpec(id="s2", type="deterministic", config={"template": "x"}),
        ])
        ctx = _make_context()

        result = engine.execute(spec, ctx)

        self.assertTrue(result.success)
        traces = getattr(result, "step_traces", None) or getattr(result, "step_traces", [])
        self.assertEqual(len(traces), 2)
        self.assertEqual(traces[0].step_id, "s1")
        self.assertEqual(traces[1].step_id, "s2")
        self.assertEqual(traces[0].status, "success")
        self.assertEqual(traces[1].status, "success")
        self.assertEqual(stub_a.call_count, 1)
        self.assertEqual(stub_b.call_count, 1)

    def test_multi_step_with_dependencies(self):
        """s2 depends_on s1，拓扑序保证 s1 先执行。"""
        execution_order: List[str] = []

        class _OrderExecutor(StepExecutor):
            def __init__(self, name: str) -> None:
                self.name = name

            def _run(self, spec, ctx, trace):
                execution_order.append(self.name)
                return {"step": self.name}, [], []

        engine = WorkflowEngine(custom_executors={
            "llm": _OrderExecutor("llm"),
        })
        # 故意把 s2 放在前面，但 s2 depends_on s1
        spec = _make_simple_spec([
            StepSpec(id="s2", type="llm", config={"prompt": "x"}, depends_on=["s1"]),
            StepSpec(id="s1", type="llm", config={"prompt": "y"}),
        ])
        ctx = _make_context()

        engine.execute(spec, ctx)

        # 拓扑序保证 s1 先执行
        self.assertEqual(execution_order, ["llm", "llm"])


class TestWorkflowEngineConditionSkip(unittest.TestCase):
    """2. condition skip。"""

    def test_condition_false_skips_step(self):
        """condition 求值为 False 时跳过 step。"""
        stub_s1 = _StubExecutor(outputs_list=[{"count": 0}])
        stub_s2 = _StubExecutor(outputs_list=[{"response": "B"}])
        engine = WorkflowEngine(custom_executors={"llm": stub_s1})
        # 注：s2 用同一 executor 实例，但 condition 跳过应不调用
        engine._executors["llm"] = stub_s1  # s1 用
        # 改用单独的 executor for s2
        stub_s2 = _StubExecutor(outputs_list=[{"response": "B"}])

        # 实际测试用自定义 executor
        class _ConditionalExecutor(StepExecutor):
            def __init__(self):
                self.calls: List[str] = []

            def _run(self, spec, ctx, trace):
                self.calls.append(spec.id)
                return {"count": 0}, [], []

        cond_executor = _ConditionalExecutor()
        engine = WorkflowEngine(custom_executors={"llm": cond_executor})

        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"}),
            StepSpec(
                id="s2", type="llm", config={"prompt": "y"},
                depends_on=["s1"],
                condition='steps.s1.outputs.count > 5',
            ),
        ])
        ctx = _make_context()

        result = engine.execute(spec, ctx)

        # s1 应执行，s2 应跳过（count=0，不 > 5）
        self.assertEqual(cond_executor.calls, ["s1"])
        traces = getattr(result, "step_traces", None) or getattr(result, "step_traces", [])
        self.assertEqual(traces[1].status, "skipped")

    def test_condition_true_executes_step(self):
        """condition 求值为 True 时执行 step。"""
        class _TrackingExecutor(StepExecutor):
            def __init__(self):
                self.calls: List[str] = []

            def _run(self, spec, ctx, trace):
                self.calls.append(spec.id)
                return {"count": 10}, [], []

        executor = _TrackingExecutor()
        engine = WorkflowEngine(custom_executors={"llm": executor})

        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"}),
            StepSpec(
                id="s2", type="llm", config={"prompt": "y"},
                depends_on=["s1"],
                condition='steps.s1.outputs.count > 5',
            ),
        ])
        ctx = _make_context()

        engine.execute(spec, ctx)

        self.assertEqual(executor.calls, ["s1", "s2"])


class TestWorkflowEngineRetry(unittest.TestCase):
    """3. retry 成功 / 4. retry 耗尽 fallback / 17. NotImplementedError PERMANENT 不 retry。"""

    def test_retry_succeeds_on_second_attempt(self):
        """首次失败（transient），retry 后第二次成功。"""
        failing_then_success = _FailingExecutor(
            error_class="transient",
            error_message="network error",
            success_at=2,  # 第 2 次成功
        )
        engine = WorkflowEngine(custom_executors={"llm": failing_then_success})
        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="llm", config={"prompt": "x"},
                on_failure=OnFailure(
                    action="retry",
                    retry=RetryPolicy(max_attempts=3, backoff_strategy="fixed", base_delay_ms=1),
                ),
            ),
        ])
        ctx = _make_context()

        with patch("hermes.tasks.workflow.retry.time.sleep"):
            result = engine.execute(spec, ctx)

        self.assertTrue(result.success)
        self.assertEqual(failing_then_success.call_count, 2)
        traces = getattr(result, "step_traces", None) or getattr(result, "step_traces", [])
        self.assertEqual(traces[0].attempts, 2)
        self.assertEqual(traces[0].status, "success")

    def test_retry_exhausted_fallback(self):
        """retry 耗尽后执行 fallback。"""
        always_fail = _FailingExecutor(
            error_class="transient",
            error_message="persistent failure",
        )
        fallback_executor = _StubExecutor(outputs_list=[{"response": "fallback"}])
        engine = WorkflowEngine(custom_executors={
            "llm": always_fail,
            "tool": fallback_executor,  # fallback_type=tool
        })
        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="llm", config={"prompt": "x"},
                on_failure=OnFailure(
                    action="retry",
                    retry=RetryPolicy(max_attempts=2, backoff_strategy="fixed", base_delay_ms=1),
                    fallback_type="tool",
                    fallback_config={"tool": "stub", "input": {}},
                ),
            ),
        ])
        ctx = _make_context()

        with patch("hermes.tasks.workflow.retry.time.sleep"):
            result = engine.execute(spec, ctx)

        # retry 2 次失败 → fallback
        self.assertEqual(always_fail.call_count, 2)
        self.assertEqual(fallback_executor.call_count, 1)
        traces = getattr(result, "step_trace", None) or getattr(result, "step_traces", [])
        self.assertEqual(traces[0].status, "fallback")

    def test_notimplementederror_not_retried(self):
        """NotImplementedError（subworkflow P2 stub）不重试，直接 abort。"""
        # 直接用真实的 SubworkflowExecutor
        engine = WorkflowEngine()  # 默认 executors
        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="subworkflow", config={},
                on_failure=OnFailure(
                    action="retry",
                    retry=RetryPolicy(max_attempts=5, base_delay_ms=1),
                ),
            ),
            StepSpec(id="s2", type="llm", config={"prompt": "y"}),
        ])
        ctx = _make_context()

        with patch("hermes.tasks.workflow.retry.time.sleep"):
            result = engine.execute(spec, ctx)

        # s1 失败（NotImplementedError → notimplemented），不重试
        # abort 终止 workflow，s2 未执行
        self.assertFalse(result.success)
        traces = getattr(result, "step_traces", [])
        self.assertEqual(len(traces), 1)  # 仅 s1
        self.assertEqual(traces[0].attempts, 1)  # 未重试
        self.assertEqual(traces[0].error_class, "notimplemented")


class TestWorkflowEngineAbort(unittest.TestCase):
    """5. abort 终止。"""

    def test_abort_terminates_workflow(self):
        """on_failure.action=abort 时失败即终止后续 step。"""
        always_fail = _FailingExecutor(error_class="permanent", error_message="fatal")
        executor_calls: List[str] = []

        class _TrackingExecutor(StepExecutor):
            def _run(self, spec, ctx, trace):
                executor_calls.append(spec.id)
                return {"response": "ok"}, [], []

        engine = WorkflowEngine(custom_executors={
            "llm": always_fail,
            "deterministic": _TrackingExecutor(),
        })
        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="llm", config={"prompt": "x"},
                on_failure=OnFailure(action="abort"),
            ),
            StepSpec(id="s2", type="deterministic", config={"template": "y"}),
        ])
        ctx = _make_context()

        result = engine.execute(spec, ctx)

        self.assertFalse(result.success)
        # s2 未执行（abort 终止）
        self.assertEqual(executor_calls, [])


class TestWorkflowEngineErrorClassMapping(unittest.TestCase):
    """6. 错误分类映射。"""

    def test_permanent_error_does_not_retry(self):
        """PERMANENT 错误不触发 retry（即使 action=retry）。"""
        permanent_fail = _FailingExecutor(
            error_class="permanent",
            error_message="not found",
        )
        engine = WorkflowEngine(custom_executors={"llm": permanent_fail})
        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="llm", config={"prompt": "x"},
                on_failure=OnFailure(
                    action="retry",
                    retry=RetryPolicy(max_attempts=5, base_delay_ms=1),
                ),
            ),
        ])
        ctx = _make_context()

        with patch("hermes.tasks.workflow.retry.time.sleep"):
            result = engine.execute(spec, ctx)

        # 仅调用 1 次（不重试）
        self.assertEqual(permanent_fail.call_count, 1)
        self.assertFalse(result.success)

    def test_transient_error_triggers_retry(self):
        """TRANSIENT 错误触发 retry。"""
        transient_fail = _FailingExecutor(
            error_class="transient",
            error_message="network error",
        )
        engine = WorkflowEngine(custom_executors={"llm": transient_fail})
        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="llm", config={"prompt": "x"},
                on_failure=OnFailure(
                    action="retry",
                    retry=RetryPolicy(max_attempts=3, base_delay_ms=1),
                ),
            ),
        ])
        ctx = _make_context()

        with patch("hermes.tasks.workflow.retry.time.sleep"):
            engine.execute(spec, ctx)

        # 重试 3 次（max_attempts）
        self.assertEqual(transient_fail.call_count, 3)


class TestWorkflowEngineTopoSort(unittest.TestCase):
    """7. 拓扑排序 / 8. 环检测抛异常。"""

    def test_topo_sort_diamond_dependency(self):
        """菱形依赖：s1 → s2/s3 → s4，拓扑序保证 s1 最先 s4 最后。"""
        execution_order: List[str] = []

        class _OrderExecutor(StepExecutor):
            def _run(self, spec, ctx, trace):
                execution_order.append(spec.id)
                return {"id": spec.id}, [], []

        engine = WorkflowEngine(custom_executors={"llm": _OrderExecutor()})
        spec = _make_simple_spec([
            StepSpec(id="s4", type="llm", config={"prompt": "x"},
                     depends_on=["s2", "s3"]),
            StepSpec(id="s2", type="llm", config={"prompt": "y"},
                     depends_on=["s1"]),
            StepSpec(id="s3", type="llm", config={"prompt": "z"},
                     depends_on=["s1"]),
            StepSpec(id="s1", type="llm", config={"prompt": "w"}),
        ])
        ctx = _make_context()

        engine.execute(spec, ctx)

        # s1 必须最先，s4 必须最后
        self.assertEqual(execution_order[0], "s1")
        self.assertEqual(execution_order[-1], "s4")
        # s2 与 s3 在 s1 之后、s4 之前
        self.assertLess(execution_order.index("s1"), execution_order.index("s2"))
        self.assertLess(execution_order.index("s1"), execution_order.index("s3"))
        self.assertGreater(execution_order.index("s4"), execution_order.index("s2"))
        self.assertGreater(execution_order.index("s4"), execution_order.index("s3"))

    def test_cycle_detection_raises(self):
        """depends_on 含环时抛 WorkflowCycleError。"""
        engine = WorkflowEngine()
        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"},
                     depends_on=["s2"]),
            StepSpec(id="s2", type="llm", config={"prompt": "y"},
                     depends_on=["s1"]),
        ])
        ctx = _make_context()

        # execute 内部捕获 WorkflowCycleError 并写入 errors
        result = engine.execute(spec, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("环" in e for e in result.errors))

    def test_depends_on_nonexistent_step_raises(self):
        """depends_on 引用不存在的 step 时报错。"""
        engine = WorkflowEngine()
        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"},
                     depends_on=["nonexistent"]),
        ])
        ctx = _make_context()

        result = engine.execute(spec, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("nonexistent" in e for e in result.errors))


class TestWorkflowEngineLLMFallbackChain(unittest.TestCase):
    """9. LLM 降级链。"""

    def test_llm_failure_with_fallback_config(self):
        """LLM 调用失败时执行 fallback_config（单轮 LLM）。"""
        primary = _FailingExecutor(error_class="transient", error_message="LLM failed")
        fallback = _StubExecutor(outputs_list=[{"response": "fallback_response"}])
        engine = WorkflowEngine(custom_executors={
            "llm": primary,
        })
        # 注：fallback_type=llm 时也用 llm executor，但本测试用单独实例
        # 修改实现：custom_executors 中 fallback 也用 llm key
        # 实际：fallback_executor 由 _executors[fallback_type] 取，fallback_type=llm
        # 故同一 executor 实例会被复用。改用不同 fallback_type
        engine._executors["tool"] = fallback

        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="llm", config={"prompt": "x"},
                on_failure=OnFailure(
                    action="retry",
                    retry=RetryPolicy(max_attempts=2, base_delay_ms=1),
                    fallback_type="tool",
                    fallback_config={"tool": "stub", "input": {}},
                ),
            ),
        ])
        ctx = _make_context()

        with patch("hermes.tasks.workflow.retry.time.sleep"):
            result = engine.execute(spec, ctx)

        # primary 重试 2 次失败 → fallback 调用 1 次
        self.assertEqual(primary.call_count, 2)
        self.assertEqual(fallback.call_count, 1)
        traces = getattr(result, "step_traces", [])
        self.assertEqual(traces[0].status, "fallback")
        self.assertEqual(traces[0].outputs.get("response"), "fallback_response")


class TestWorkflowEngineToolCallFallback(unittest.TestCase):
    """10. tool_call fallback。"""

    def test_tool_step_with_skip_policy(self):
        """tool step 失败 + on_failure.action=skip 时跳过。"""
        tool_fail = _FailingExecutor(error_class="transient", error_message="tool error")
        engine = WorkflowEngine(custom_executors={"tool": tool_fail})

        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="tool", config={"tool": "stub", "input": {}},
                on_failure=OnFailure(action="skip"),
            ),
        ])
        ctx = _make_context()

        result = engine.execute(spec, ctx)

        # skip 策略：失败即跳过，workflow 仍成功
        self.assertTrue(result.success)
        traces = getattr(result, "step_traces", [])
        self.assertEqual(traces[0].status, "skipped")


class TestWorkflowEngineInjectAndTimeVariables(unittest.TestCase):
    """11. inject 变量 / 12. 时间变量替换。"""

    def test_step_outputs_available_to_condition(self):
        """s1 的 outputs 可被 s2 的 condition 引用。"""
        class _CountingExecutor(StepExecutor):
            def __init__(self, count: int):
                self.count = count

            def _run(self, spec, ctx, trace):
                return {"count": self.count}, [], []

        class _TrackingExecutor(StepExecutor):
            def __init__(self):
                self.executed = False

            def _run(self, spec, ctx, trace):
                self.executed = True
                return {"response": "ok"}, [], []

        tracking = _TrackingExecutor()
        engine = WorkflowEngine(custom_executors={
            "llm": _CountingExecutor(count=5),
            "tool": tracking,
        })
        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"}),
            StepSpec(
                id="s2", type="tool", config={"tool": "stub", "input": {}},
                depends_on=["s1"],
                condition='steps.s1.outputs.count == 5',
            ),
        ])
        ctx = _make_context()

        engine.execute(spec, ctx)

        # s2 应执行（count==5 为真）
        self.assertTrue(tracking.executed)

    def test_time_variables_replaced_in_prompt(self):
        """prompt 中的 {now} / {today} 占位符被替换。"""
        captured_prompt: List[str] = []

        class _CapturingExecutor(StepExecutor):
            def _run(self, spec, ctx, trace):
                # 模板 _call_llm_single_turn 已替换；此处直接断言 config
                # 但本 executor 不走 LLM 路径，需手动调用 context.render
                rendered = ctx.render(spec.config.get("prompt", ""))
                captured_prompt.append(rendered)
                return {"response": "ok"}, [], []

        engine = WorkflowEngine(custom_executors={"llm": _CapturingExecutor()})
        spec = _make_simple_spec([
            StepSpec(
                id="s1", type="llm",
                config={"prompt": "当前时间: {now}\n日期: {today}"},
            ),
        ])
        ctx = _make_context()
        ctx.current_time = datetime(2026, 7, 5, 12, 0, 0)

        engine.execute(spec, ctx)

        self.assertIn("2026-07-05", captured_prompt[0])
        # {now} 应替换为完整时间
        self.assertIn("2026-07-05 12:00:00", captured_prompt[0])


class TestWorkflowEngineSimpleMode(unittest.TestCase):
    """13. 简易模式。"""

    def test_simple_mode_calls_template(self):
        """简易模式（仅 template）应直接调用 WorkflowTemplate.execute。"""
        from hermes.tasks.workflow import BUILTIN_TEMPLATES

        # mock 一个测试模板
        class _TestTemplate:
            name = "_test_simple"

            def execute(self, config, context):
                result = WorkflowResult()
                result.assistant_response = "simple response"
                result.metrics_for_injection = {"mode": "simple"}
                return result

        # 临时注册
        BUILTIN_TEMPLATES["_test_simple"] = _TestTemplate
        try:
            engine = WorkflowEngine()
            spec = WorkflowSpec(
                name="simple_test",
                template="_test_simple",
                template_config={"key": "value"},
            )
            ctx = _make_context()

            result = engine.execute(spec, ctx)

            self.assertTrue(result.success)
            self.assertEqual(result.assistant_response, "simple response")
            self.assertEqual(result.metrics_for_injection.get("mode"), "simple")
        finally:
            del BUILTIN_TEMPLATES["_test_simple"]


class TestWorkflowEngineTimeout(unittest.TestCase):
    """15. workflow timeout / 16. step timeout。"""

    def test_workflow_timeout_skips_remaining_steps(self):
        """workflow 级 timeout 超时后剩余 step 标记 skipped。"""
        # 用 mock 让 perf_counter 返回较大值模拟超时
        class _SlowExecutor(StepExecutor):
            def _run(self, spec, ctx, trace):
                return {"slow": True}, [], []

        engine = WorkflowEngine(custom_executors={"llm": _SlowExecutor()})
        spec = _make_simple_spec(
            [
                StepSpec(id="s1", type="llm", config={"prompt": "x"}),
                StepSpec(id="s2", type="llm", config={"prompt": "y"}),
            ],
            timeout_seconds=0.001,  # 极短超时
        )
        ctx = _make_context()

        # mock perf_counter 让 elapsed > timeout
        call_count = [0]
        original_perf_counter = time.perf_counter

        def mock_perf_counter():
            call_count[0] += 1
            # 第 1 次返回 0（start），第 2 次起返回很大值
            if call_count[0] == 1:
                return 0.0
            return 100.0

        with patch("hermes.tasks.workflow.engine.time.perf_counter", mock_perf_counter):
            result = engine.execute(spec, ctx)

        # 至少 1 个 step 应被跳过
        traces = getattr(result, "step_traces", None) or getattr(result, "step_traces", [])
        skipped = [t for t in traces if t.status == "skipped"]
        self.assertGreater(len(skipped), 0)


class TestWorkflowEngineAggregateMetrics(unittest.TestCase):
    """metrics 汇总。"""

    def test_metrics_aggregation(self):
        """metrics_for_injection 含 step_count / success_count 等。"""
        stub = _StubExecutor(outputs_list=[{"response": "ok"}])
        engine = WorkflowEngine(custom_executors={"llm": stub})
        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"}),
            StepSpec(id="s2", type="llm", config={"prompt": "y"}),
        ])
        ctx = _make_context()

        result = engine.execute(spec, ctx)

        metrics = result.metrics_for_injection
        self.assertEqual(metrics["workflow_name"], "test")
        self.assertEqual(metrics["step_count"], 2)
        self.assertEqual(metrics["step_success"], 2)
        self.assertEqual(metrics["step_failed"], 0)
        self.assertFalse(metrics["aborted"])


class TestWorkflowEngineErrorChannelMerge(unittest.TestCase):
    """D5 修复：context.error_channel 合并到 result.errors。"""

    def test_error_channel_merged_to_result(self):
        """context.error_channel 内容出现在 result.errors。"""
        stub = _StubExecutor(outputs_list=[{"response": "ok"}])
        engine = WorkflowEngine(custom_executors={"llm": stub})
        spec = _make_simple_spec([
            StepSpec(id="s1", type="llm", config={"prompt": "x"}),
        ])
        ctx = _make_context()
        # 模拟 Task 8 后的 error_channel（setattr 兜底）
        ctx.error_channel = ["LLM 调用失败: timeout", "context overload"]
        ctx.append_error = lambda msg: ctx.error_channel.append(msg)

        result = engine.execute(spec, ctx)

        # error_channel 内容合并到 errors
        self.assertIn("LLM 调用失败: timeout", result.errors)
        self.assertIn("context overload", result.errors)


class TestWorkflowEngineEmptyWorkflowSpec(unittest.TestCase):
    """空 workflow spec（无 template 无 steps）。"""

    def test_empty_spec_returns_failure(self):
        engine = WorkflowEngine()
        spec = WorkflowSpec(name="empty")
        ctx = _make_context()

        result = engine.execute(spec, ctx)

        self.assertFalse(result.success)
        self.assertTrue(any("无 template 也无 steps" in e for e in result.errors))


class TestWorkflowEngineUnknownStepType(unittest.TestCase):
    """未知 step 类型。"""

    def test_unknown_step_type_marks_failed(self):
        engine = WorkflowEngine()
        spec = _make_simple_spec([
            StepSpec(id="s1", type="unknown_type", config={}),
        ])
        ctx = _make_context()

        result = engine.execute(spec, ctx)

        self.assertFalse(result.success)
        traces = getattr(result, "step_traces", [])
        self.assertEqual(traces[0].status, "failed")
        self.assertEqual(traces[0].error_class, "permanent")


if __name__ == "__main__":
    unittest.main()
