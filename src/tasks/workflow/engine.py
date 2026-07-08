"""WorkflowEngine 通用执行引擎（Task 5.4）。

按拓扑序执行 ``WorkflowSpec.steps``，支持：
- ``retry`` / ``fallback`` / ``skip`` / ``abort`` 四种 on_failure 策略
- ``condition`` 条件跳过（P0 简化版正则）
- ``RetryBudget`` 重试预算（fixed / linear / exponential backoff）
- ``NotImplementedError`` 视为 PERMANENT 不触发 retry
- ``context.error_channel`` 内容合并到 WorkflowResult.errors（D5 修复下游）

执行流程：
1. ``_topo_sort(steps)`` 拓扑排序（含环检测）
2. 逐 step：
   - ``_eval_condition`` 判定是否跳过
   - ``_execute_with_policy`` 调用 StepExecutor + retry/fallback/skip/abort
   - ``StepTrace`` 记录到 result.step_traces
3. ``_aggregate_metrics`` 汇总到 metrics_for_injection
4. ``context.error_channel`` 合并到 result.errors（D5 修复）

不实装（P1+）：
- workflow 级 timeout 强制终止（P2）
- Jinja2 condition 表达式（P2）
- subworkflow 递归执行（P2）
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .base import WorkflowContext, WorkflowResult
from .retry import NON_RETRYABLE_ERRORS, RetryBudget
from .spec import OnFailure, StepSpec, WorkflowSpec
from .step_executor import (
    LlmCallExecutor,
    StepExecutionError,
    StepExecutor,
    get_step_executor,
)
from .step_trace import StepTrace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class WorkflowCycleError(Exception):
    """depends_on 存在环，无法拓扑排序。"""


# ---------------------------------------------------------------------------
# P0 简化版 condition 正则
# ---------------------------------------------------------------------------

# 支持的 condition 形式（P0 简化版）：
# - steps.<id>.outputs.<key> > <number>
# - steps.<id>.outputs.<key> == "<string>"
# - steps.<id>.outputs.<key> == <number>
# - steps.<id>.outputs.<key> != "<string>"
# - steps.<id>.status == "success" / "failed" / "skipped"
# 完整 Jinja2 表达式由 P2 实装。
_CONDITION_RE = re.compile(
    r"^\s*steps\.(?P<step_id>[a-zA-Z0-9_\-]+)\."
    r"(?P<field>outputs|status|attempts)\.(?P<key>[a-zA-Z0-9_\-]+)"
    r"\s*(?P<op>>|>=|<|<=|==|!=)\s*"
    r"(?P<value>[^;\n]+?)\s*$"
)


# ---------------------------------------------------------------------------
# WorkflowEngine
# ---------------------------------------------------------------------------


class WorkflowEngine:
    """通用 workflow 执行引擎。

    用法::

        engine = WorkflowEngine()
        result = engine.execute(spec, context)

    简易模式 spec（仅 template）由 adapter 包装为单 step 后调用本引擎。
    """

    def __init__(self, custom_executors: Optional[Dict[str, StepExecutor]] = None) -> None:
        """初始化引擎。

        参数:
            custom_executors: 自定义 step.type → Executor 映射（覆盖默认）。
                用于测试 mock。
        """
        # 复制默认注册表，避免全局污染
        from .step_executor import STEP_EXECUTORS
        self._executors: Dict[str, StepExecutor] = dict(STEP_EXECUTORS)
        if custom_executors:
            self._executors.update(custom_executors)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def execute(
        self,
        spec: WorkflowSpec,
        context: WorkflowContext,
    ) -> WorkflowResult:
        """执行 workflow，返回 WorkflowResult。

        参数:
            spec: WorkflowSpec 实例。
            context: WorkflowContext 实例。

        返回:
            :class:`WorkflowResult`，含 ``step_traces`` / ``errors`` /
            ``metrics_for_injection``。
        """
        result = WorkflowResult()

        # 简易模式：转交旧模板执行路径（adapter 应已包装，此处兜底）
        if spec.is_simple_mode():
            return self._execute_simple_mode(spec, context)

        # 多步模式
        if not spec.steps:
            result.add_error("workflow 既无 template 也无 steps")
            return result

        # 1. 拓扑排序（含环检测）
        try:
            sorted_steps = self._topo_sort(spec.steps)
        except WorkflowCycleError as e:
            result.add_error(f"workflow depends_on 存在环: {e}")
            return result

        # 2. 逐 step 执行
        step_traces: List[StepTrace] = []
        step_trace_map: Dict[str, StepTrace] = {}
        aborted = False

        workflow_start_time = time.perf_counter()
        workflow_timeout_ms = (
            spec.timeout_seconds * 1000 if spec.timeout_seconds else None
        )

        for step in sorted_steps:
            # workflow 级 timeout 检查（基于累计耗时，非强制中断）
            if workflow_timeout_ms is not None:
                elapsed_ms = (time.perf_counter() - workflow_start_time) * 1000
                if elapsed_ms > workflow_timeout_ms:
                    logger.warning(
                        "workflow '%s' 超时（%dms > %dms），终止剩余 step",
                        spec.name, int(elapsed_ms), int(workflow_timeout_ms),
                    )
                    skipped_trace = StepTrace(
                        step_id=step.id,
                        step_name=step.name or step.id,
                        step_type=step.type,
                        status="skipped",
                        error_class="timeout",
                        error_message="workflow 级超时，step 未执行",
                    )
                    step_traces.append(skipped_trace)
                    step_trace_map[step.id] = skipped_trace
                    continue

            # 2a. condition 判定
            if step.condition:
                if not self._eval_condition(step.condition, step_trace_map):
                    trace = StepTrace(
                        step_id=step.id,
                        step_name=step.name or step.id,
                        step_type=step.type,
                    )
                    trace.mark_started(datetime.now())
                    trace.mark_finished("skipped", datetime.now())
                    step_traces.append(trace)
                    step_trace_map[step.id] = trace
                    continue

            # 2b. 执行 step（含 retry / fallback / skip / abort）
            trace = self._execute_with_policy(step, context, spec)
            step_traces.append(trace)
            step_trace_map[step.id] = trace

            # 2b.1 将 step 产出存入 context.step_outputs，供后续 step 引用
            if trace.outputs:
                context.step_outputs[step.id] = trace.outputs

            # 2c. abort 策略：终止整个 workflow
            # PERMANENT 类错误（含 notimplemented）触发 abort 检查：
            # - NotImplementedError：强制 abort（无论 action 配置）
            # - 其他 PERMANENT：仅当 action=abort 时 abort
            is_permanent = (
                trace.error_class in NON_RETRYABLE_ERRORS
                or trace.error_class == "notimplemented"
            )
            force_abort = trace.error_class == "notimplemented"
            if trace.status == "failed" and is_permanent:
                if force_abort or step.on_failure.action == "abort":
                    aborted = True
                    logger.warning(
                        "workflow '%s' 在 step '%s' 处 abort（%s 错误）",
                        spec.name, step.id, trace.error_class,
                    )
                    break

        # 3. 汇总 metrics
        result.metrics_for_injection = self._aggregate_metrics(
            spec, step_traces, workflow_start_time, aborted
        )

        # 4. 收集 step_traces 与 errors
        # Task 8.1 已实装 WorkflowResult.step_traces 字段，直接赋值
        result.step_traces = step_traces
        for trace in step_traces:
            if trace.status in ("failed", "timeout") and trace.error_message:
                result.errors.append(
                    f"step '{trace.step_id}' ({trace.step_type}) "
                    f"{trace.status}: {trace.error_message}"
                )
            # 聚合每个 step 的 tool_calls 到 result.tool_calls，
            # 供调用方（如 CronScheduler 写 session_logger）按顺序遍历
            if trace.tool_calls:
                result.tool_calls.extend(trace.tool_calls)

        # 5. D5 修复：合并 context.error_channel 到 result.errors
        error_channel = getattr(context, "error_channel", None)
        if error_channel:
            for msg in error_channel:
                result.errors.append(msg)

        # 整体成功判定
        result.success = not aborted and not any(
            t.status in ("failed", "timeout") for t in step_traces
        )

        # 从最后一个成功的 LLM step 提取 assistant_response
        for trace in reversed(step_traces):
            if trace.status == "success" and trace.outputs:
                resp = trace.outputs.get("response") or trace.outputs.get("result")
                if resp:
                    result.assistant_response = resp
                    break

        return result

    # ------------------------------------------------------------------
    # 简易模式兜底（adapter 应已包装，此处用于直接调用场景）
    # ------------------------------------------------------------------

    def _execute_simple_mode(
        self,
        spec: WorkflowSpec,
        context: WorkflowContext,
    ) -> WorkflowResult:
        """简易模式执行：直接调用旧 WorkflowTemplate。

        adapter 应在调用 ``WorkflowEngine.execute`` 前包装简易模式为单 step
        WorkflowSpec，此方法作为直接调用场景的兜底。
        """
        result = WorkflowResult()
        from . import BUILTIN_TEMPLATES

        template_name = spec.template
        if not template_name:
            result.add_error("简易模式 template 为空")
            return result

        template_cls = BUILTIN_TEMPLATES.get(template_name)
        if template_cls is None:
            result.add_error(
                f"模板 '{template_name}' 不存在，"
                f"可用: {sorted(BUILTIN_TEMPLATES.keys())}"
            )
            return result

        template = template_cls()
        template_result = template.execute(spec.template_config, context)
        # 透传字段
        result.success = template_result.success
        result.assistant_response = template_result.assistant_response
        result.tool_calls = list(template_result.tool_calls)
        result.outputs = list(template_result.outputs)
        result.errors = list(template_result.errors)
        result.metrics_for_injection = dict(template_result.metrics_for_injection)

        # error_channel 合并（D5）
        error_channel = getattr(context, "error_channel", None)
        if error_channel:
            result.errors.extend(error_channel)

        return result

    # ------------------------------------------------------------------
    # 拓扑排序（5.4a）
    # ------------------------------------------------------------------

    def _topo_sort(self, steps: List[StepSpec]) -> List[StepSpec]:
        """拓扑排序（DFS），含环检测。

        算法：Kahn 算法变体，按 depends_on 入度递归。
        检测到环时抛 :class:`WorkflowCycleError`。
        """
        step_map: Dict[str, StepSpec] = {s.id: s for s in steps}

        # 校验 depends_on 引用存在
        for step in steps:
            for dep in step.depends_on:
                if dep not in step_map:
                    raise WorkflowCycleError(
                        f"step '{step.id}' depends_on 不存在的 step '{dep}'"
                    )

        # DFS 三色标记法
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {s.id: WHITE for s in steps}
        sorted_list: List[StepSpec] = []

        def dfs(node_id: str, path: List[str]) -> None:
            if color.get(node_id, BLACK) == BLACK:
                return
            if color.get(node_id) == GRAY:
                cycle = " → ".join(path + [node_id])
                raise WorkflowCycleError(f"depends_on 存在环: {cycle}")
            color[node_id] = GRAY
            path.append(node_id)
            for dep in step_map.get(node_id, StepSpec(id=node_id)).depends_on:
                dfs(dep, path)
            path.pop()
            color[node_id] = BLACK
            sorted_list.append(step_map[node_id])

        for step in steps:
            if color[step.id] == WHITE:
                dfs(step.id, [])

        return sorted_list

    # ------------------------------------------------------------------
    # condition 求值（5.4b，P0 简化版正则）
    # ------------------------------------------------------------------

    def _eval_condition(
        self,
        condition: str,
        step_traces: Dict[str, StepTrace],
    ) -> bool:
        """P0 简化版 condition 求值。

        支持的形式（仅支持 ``==`` / ``!=`` / ``>`` / ``>=`` / ``<`` / ``<=``）：
        - ``steps.<id>.outputs.<key> > 0``
        - ``steps.<id>.outputs.<key> == "foo"``
        - ``steps.<id>.status == "success"``
        - ``steps.<id>.attempts.count >= 2``

        参数:
            condition: 条件表达式字符串。
            step_traces: 已完成 step 的 id → StepTrace 映射。

        返回:
            ``True`` 表示应执行当前 step，``False`` 表示跳过。
        """
        if not condition:
            return True

        m = _CONDITION_RE.match(condition)
        if not m:
            logger.warning(
                "condition '%s' 不符合 P0 简化版语法，默认 True（执行）",
                condition,
            )
            return True

        step_id = m.group("step_id")
        field = m.group("field")
        key = m.group("key")
        op = m.group("op")
        raw_value = m.group("value").strip()

        trace = step_traces.get(step_id)
        if trace is None:
            # 引用的 step 尚未执行（不应发生，拓扑序保证）：保守执行
            logger.debug(
                "condition 引用未执行的 step '%s'，默认 True（执行）", step_id
            )
            return True

        # 取值
        if field == "outputs":
            actual_value = trace.outputs.get(key)
        elif field == "status":
            actual_value = trace.status if key == "value" else None
            # P0 不支持 status.<key>，仅支持 status.value（占位）
            # 完整 status 比较由 P1 增强
            if key != "value":
                return True
        elif field == "attempts":
            if key == "count":
                actual_value = trace.attempts
            else:
                return True
        else:
            return True

        # 类型转换与比较
        return self._compare_values(actual_value, op, raw_value)

    def _compare_values(
        self,
        actual: Any,
        op: str,
        raw_value: str,
    ) -> bool:
        """根据 op 比较 actual 与 raw_value（自动类型推断）。"""
        # 字符串字面量：含引号
        if (raw_value.startswith('"') and raw_value.endswith('"')) or (
            raw_value.startswith("'") and raw_value.endswith("'")
        ):
            expected = raw_value[1:-1]
        else:
            # 尝试数字
            try:
                expected = int(raw_value)
            except ValueError:
                try:
                    expected = float(raw_value)
                except ValueError:
                    expected = raw_value

        # 类型对齐
        try:
            if op == "==":
                return actual == expected
            if op == "!=":
                return actual != expected
            if actual is None:
                return False
            if op == ">":
                return actual > expected
            if op == ">=":
                return actual >= expected
            if op == "<":
                return actual < expected
            if op == "<=":
                return actual <= expected
        except TypeError:
            return False
        return False

    # ------------------------------------------------------------------
    # step 执行 + on_failure 策略（5.4c）
    # ------------------------------------------------------------------

    def _execute_with_policy(
        self,
        step: StepSpec,
        context: WorkflowContext,
        workflow_spec: WorkflowSpec,
    ) -> StepTrace:
        """执行单个 step，按 on_failure.action 决策 retry / fallback / skip / abort。

        NotImplementedError 视为 PERMANENT：不重试，直接走 abort 路径
        （无论 action 配置为何）。
        """
        executor = self._executors.get(step.type)
        if executor is None:
            trace = StepTrace(
                step_id=step.id,
                step_name=step.name or step.id,
                step_type=step.type,
            )
            trace.mark_started(datetime.now())
            trace.mark_finished(
                "failed", datetime.now(),
                error_class="permanent",
                error_message=f"未知 step 类型: {step.type}",
            )
            return trace

        on_failure = step.on_failure or workflow_spec.on_failure
        action = on_failure.action

        # 先执行一次（probe），检测 NotImplementedError 决定是否走 abort 路径
        # 注：retry / fallback / skip / abort 共享首次执行结果
        first_trace = executor.execute(step, context, None)

        # NotImplementedError → PERMANENT，不重试直接走 abort
        if first_trace.error_class == "notimplemented":
            logger.info(
                "step '%s' 抛 NotImplementedError（PERMANENT），直接 abort",
                step.id,
            )
            return first_trace

        # 首次成功：直接返回
        if first_trace.status == "success":
            return first_trace

        # 按 action 决策后续处理
        if action == "fallback":
            return self._execute_with_fallback_after_probe(
                step, context, executor, on_failure, first_trace
            )
        if action == "skip":
            # 失败即跳过
            first_trace.status = "skipped"
            return first_trace
        if action == "abort":
            # 失败即终止（Engine 检测到 failed 即 abort）
            return first_trace

        # action=retry：基于首次结果继续重试
        return self._execute_with_retry_after_probe(
            step, context, executor, on_failure, first_trace
        )

    def _execute_with_fallback_after_probe(
        self,
        step: StepSpec,
        context: WorkflowContext,
        executor: StepExecutor,
        on_failure: OnFailure,
        failed_trace: StepTrace,
    ) -> StepTrace:
        """fallback 策略：失败后执行 fallback step（不重试）。"""
        return self._execute_fallback_step(step, context, on_failure, failed_trace)

    def _execute_with_retry_after_probe(
        self,
        step: StepSpec,
        context: WorkflowContext,
        executor: StepExecutor,
        on_failure: OnFailure,
        first_trace: StepTrace,
    ) -> StepTrace:
        """retry 策略：首次已失败，按 RetryPolicy 决定是否重试。

        基于首次执行结果继续 retry 循环（避免重复 probe 调用）。
        """
        budget = RetryBudget(policy=on_failure.retry)
        budget.increment()  # 首次执行计入 attempt

        last_trace = first_trace

        # 首次失败：检查是否应重试
        if not budget.should_retry(first_trace.error_class):
            # 不重试：检查兜底
            return self._apply_fallback_after_retry_exhausted(
                step, context, executor, on_failure, last_trace
            )

        while budget.has_budget():
            backoff_ms = budget.compute_backoff_ms()
            logger.info(
                "step '%s' 第 %d 次失败（%s），%dms 后重试",
                step.id, budget.attempt, last_trace.error_class, backoff_ms,
            )
            budget.sleep_backoff()
            budget.increment()
            retry_trace = StepTrace(
                step_id=step.id,
                step_name=step.name or step.id,
                step_type=step.type,
                attempts=budget.attempt,
            )
            retry_trace = executor.execute(step, context, retry_trace)
            # 合并 attempts
            last_trace = StepTrace(
                step_id=step.id,
                step_name=step.name or step.id,
                step_type=step.type,
                started_at=first_trace.started_at,
                finished_at=retry_trace.finished_at,
                duration_ms=first_trace.duration_ms + retry_trace.duration_ms,
                attempts=budget.attempt,
                status=retry_trace.status,
                error_class=retry_trace.error_class,
                error_message=retry_trace.error_message,
                outputs=retry_trace.outputs,
                tool_calls=retry_trace.tool_calls,
                files=retry_trace.files,
            )

            if retry_trace.status == "success":
                return last_trace

            # PERMANENT / NotImplementedError：不重试
            if (
                retry_trace.error_class in NON_RETRYABLE_ERRORS
                or retry_trace.error_class == "notimplemented"
            ):
                break

            if not budget.should_retry(retry_trace.error_class):
                break

        return self._apply_fallback_after_retry_exhausted(
            step, context, executor, on_failure, last_trace
        )

    def _apply_fallback_after_retry_exhausted(
        self,
        step: StepSpec,
        context: WorkflowContext,
        executor: StepExecutor,
        on_failure: OnFailure,
        last_trace: Optional[StepTrace],
    ) -> StepTrace:
        """重试耗尽后的兜底处理（fallback / skip / abort）。"""
        # 仅当显式配置 fallback_config 时才执行 fallback step。
        # 默认 OnFailure.fallback_type="llm" + 空 fallback_config 不触发 fallback，
        # 避免静默执行未配置的 LLM 调用。
        if on_failure.fallback_config:
            return self._execute_fallback_step(step, context, on_failure, last_trace)

        # 默认行为：标记失败
        if last_trace is None:
            last_trace = StepTrace(
                step_id=step.id,
                step_name=step.name or step.id,
                step_type=step.type,
                status="failed",
                error_class="transient",
                error_message="retry 耗尽且无 fallback 配置",
            )
        else:
            # 保持失败状态
            last_trace.status = "failed"
        return last_trace

    def _execute_fallback_step(
        self,
        step: StepSpec,
        context: WorkflowContext,
        on_failure: OnFailure,
        failed_trace: Optional[StepTrace],
    ) -> StepTrace:
        """执行 fallback step（单轮 LLM 调用）。

        P0 实现：使用 LlmCallExecutor 执行 fallback_config.prompt，
        标记 status=fallback。
        """
        fallback_cfg = on_failure.fallback_config or {}
        fallback_type = on_failure.fallback_type or "llm"

        # 构造 fallback step spec
        fallback_step = StepSpec(
            id=f"{step.id}__fallback",
            name=f"{step.name or step.id} (fallback)",
            type=fallback_type,
            config=dict(fallback_cfg),
        )

        fallback_executor = self._executors.get(fallback_type)
        if fallback_executor is None:
            # 退化：标记 failed
            trace = failed_trace or StepTrace(
                step_id=step.id,
                step_name=step.name or step.id,
                step_type=step.type,
            )
            trace.status = "failed"
            trace.error_class = "permanent"
            trace.error_message = (
                f"fallback 类型 '{fallback_type}' 未注册 executor"
            )
            return trace

        # 执行 fallback
        fallback_trace = fallback_executor.execute(fallback_step, context, None)
        # 合并到原 step trace（保留 step_id）
        merged_trace = StepTrace(
            step_id=step.id,
            step_name=step.name or step.id,
            step_type=step.type,
            started_at=fallback_trace.started_at,
            finished_at=fallback_trace.finished_at,
            duration_ms=fallback_trace.duration_ms,
            attempts=(failed_trace.attempts if failed_trace else 0) + 1,
            status="fallback",
            error_class=failed_trace.error_class if failed_trace else "",
            error_message=(
                f"原 step 失败（{failed_trace.error_message if failed_trace else ''}），"
                f"已执行 fallback"
            ),
            outputs=fallback_trace.outputs,
            tool_calls=fallback_trace.tool_calls,
            files=fallback_trace.files,
        )
        return merged_trace

    # ------------------------------------------------------------------
    # metrics 汇总（5.4d）
    # ------------------------------------------------------------------

    def _aggregate_metrics(
        self,
        spec: WorkflowSpec,
        step_traces: List[StepTrace],
        workflow_start_time: float,
        aborted: bool,
    ) -> Dict[str, Any]:
        """汇总 step_traces 到 metrics_for_injection。"""
        total = len(step_traces)
        success_count = sum(1 for t in step_traces if t.status == "success")
        failed_count = sum(1 for t in step_traces if t.status == "failed")
        skipped_count = sum(1 for t in step_traces if t.status == "skipped")
        fallback_count = sum(1 for t in step_traces if t.status == "fallback")
        timeout_count = sum(1 for t in step_traces if t.status == "timeout")
        total_tool_calls = sum(len(t.tool_calls) for t in step_traces)
        total_attempts = sum(t.attempts for t in step_traces)
        total_duration_ms = int((time.perf_counter() - workflow_start_time) * 1000)

        return {
            "workflow_name": spec.name,
            "step_count": total,
            "step_success": success_count,
            "step_failed": failed_count,
            "step_skipped": skipped_count,
            "step_fallback": fallback_count,
            "step_timeout": timeout_count,
            "total_tool_calls": total_tool_calls,
            "total_attempts": total_attempts,
            "total_duration_ms": total_duration_ms,
            "aborted": aborted,
        }

    # ------------------------------------------------------------------
    # StepTrace 写入 WorkflowResult（Task 8 已实装正式字段）
    # ------------------------------------------------------------------
    # Task 8.1 已为 WorkflowResult 添加 step_traces 字段，
    # execute() 中直接 `result.step_traces = step_traces` 赋值，
    # 不再需要 setattr 兜底。


__all__ = [
    "WorkflowCycleError",
    "WorkflowEngine",
]
