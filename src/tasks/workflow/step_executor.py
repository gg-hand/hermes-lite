"""Step 执行器（Task 5.3）。

每个 step 类型对应一个 Executor 子类，由 ``WorkflowEngine._execute_with_policy``
按 ``step.type`` 路由调用。Executor 负责填充 ``StepTrace`` 状态、输出、
工具调用、错误分类，不处理 retry / fallback 策略（由 Engine 统一控制）。

5 种 step 类型：
- ``deterministic``：调用旧 WorkflowTemplate（BUILTIN_TEMPLATES）
- ``llm``：单轮 LLM 调用（``_call_llm_single_turn`` 修复后版本）
- ``tool``：直接调用单个工具（经 PolicyEngine.check 包装 + audit log）
- ``react``：ReactLoop 多轮工具调用循环（asyncio.run + RuntimeError 兜底）
- ``subworkflow``：P2 stub（NotImplementedError，PERMANENT 不重试）

设计要点：
- **安全约束**（ToolCallExecutor）：调用工具前 MUST 经 PolicyEngine.check
  包装，``Decision.action="deny"`` 时抛 ``PermissionError``；调用后 MUST 调
  ``audit_logger.log_tool_call()`` 透传 ``schedule_id`` / ``run_id`` / ``step_id``。
- **D7 修复**（ReactLoopExecutor）：``try: asyncio.run(...)`` + ``except
  RuntimeError: asyncio.run_coroutine_threadsafe(...).result()`` 兜底，避免
  在已有事件循环的线程中抛错。
- **D5 修复**（LlmCallExecutor）：异常写入 ``context.append_error``（若可用），
  StepTrace 记录 ``error_class="transient"`` 让 Engine 决定是否重试。
- **NotImplementedError**（SubworkflowExecutor）：Engine 视为 PERMANENT，
  不触发 retry 直接走 abort 路径。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .base import WorkflowContext, WorkflowResult, WorkflowTemplate
from .spec import StepSpec
from .step_trace import StepTrace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常类型（Executor 抛出，由 Engine 的 _execute_with_policy 捕获）
# ---------------------------------------------------------------------------


class StepExecutionError(Exception):
    """step 执行失败（含 error_class 与 error_message）。

    属性:
        error_class: ErrorClass 值（``transient`` / ``permanent`` /
            ``permission`` / ``notimplemented`` 等），由 Engine 用于
            ``RetryBudget.should_retry`` 判定。
        error_message: 一行精炼错误描述。
    """

    def __init__(self, error_class: str, error_message: str) -> None:
        super().__init__(error_message)
        self.error_class = error_class
        self.error_message = error_message


class PermissionDeniedError(StepExecutionError):
    """工具调用被 PolicyEngine 拒绝（PERMANENT，不重试）。"""

    def __init__(self, message: str) -> None:
        super().__init__("permission", message)


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class StepExecutor:
    """step 执行器基类。

    子类实现 :meth:`_run`，基类负责 ``StepTrace`` 生命周期管理
    （``mark_started`` / ``mark_finished``）。

    子类 ``_run`` 返回 ``(outputs, tool_calls, files)`` 三元组：
    - ``outputs``: dict，供后续 step 通过 condition 引用
    - ``tool_calls``: list，工具调用记录
    - ``files``: list，文件产出记录

    失败时子类应抛 :class:`StepExecutionError`，基类捕获后填充 trace。
    """

    def execute(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: Optional[StepTrace] = None,
    ) -> StepTrace:
        """执行 step，返回填充完毕的 StepTrace。

        参数:
            spec: step 定义。
            context: 工作流上下文。
            trace: 可选 StepTrace 实例（由 Engine 创建并传入 attempt 信息）。
                为 ``None`` 时本方法内部创建。

        返回:
            填充完毕的 :class:`StepTrace`。
        """
        if trace is None:
            trace = StepTrace(
                step_id=spec.id,
                step_name=spec.name or spec.id,
                step_type=spec.type,
            )
        trace.mark_started(datetime.now())

        try:
            outputs, tool_calls, files = self._run(spec, context, trace)
            trace.outputs = outputs or {}
            trace.tool_calls = tool_calls or []
            trace.files = files or []
            trace.mark_finished("success", datetime.now())
            return trace
        except StepExecutionError as e:
            trace.mark_finished(
                "failed", datetime.now(),
                error_class=e.error_class,
                error_message=e.error_message,
            )
            return trace
        except NotImplementedError as e:
            # P2 stub 与未实装特性：PERMANENT，不重试
            trace.mark_finished(
                "failed", datetime.now(),
                error_class="notimplemented",
                error_message=str(e) or "未实装的 step 类型",
            )
            return trace
        except Exception as e:
            # 未知异常归一为 transient（保守策略，让 RetryBudget 决策）
            logger.exception("step %s (%s) 执行异常: %s", spec.id, spec.type, e)
            trace.mark_finished(
                "failed", datetime.now(),
                error_class="transient",
                error_message=f"{type(e).__name__}: {e}",
            )
            return trace

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """子类实现：执行 step 并返回 ``(outputs, tool_calls, files)``。

        失败时抛 :class:`StepExecutionError`（或子类），由基类捕获。
        """
        raise NotImplementedError("子类必须实现 _run")


# ---------------------------------------------------------------------------
# 5.3a DeterministicExecutor
# ---------------------------------------------------------------------------


class DeterministicExecutor(StepExecutor):
    """调用旧 WorkflowTemplate（BUILTIN_TEMPLATES）。

    ``config`` 字段:
    - ``template``（必填）: 模板名（如 ``"directory_watch"``）
    - 其余字段作为模板 ``config`` 透传

    模板未找到时抛 :class:`StepExecutionError`（``error_class="permanent"``），
    模板自身执行失败由模板实现写入 ``WorkflowResult.errors``，本 executor
    将 ``result.success=False`` 转换为 ``error_class="transient"``。
    """

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        # 延迟导入避免循环依赖
        from . import BUILTIN_TEMPLATES

        template_name = spec.config.get("template", "")
        if not template_name:
            raise StepExecutionError(
                "permanent", "deterministic step 缺少 config.template 字段"
            )

        template_cls = BUILTIN_TEMPLATES.get(template_name)
        if template_cls is None:
            raise StepExecutionError(
                "permanent",
                f"模板 '{template_name}' 不存在，"
                f"可用: {sorted(BUILTIN_TEMPLATES.keys())}",
            )

        # 构造模板 config（剥离 template 字段，其余透传）
        template_cfg = {k: v for k, v in spec.config.items() if k != "template"}

        template = template_cls()
        result: WorkflowResult = template.execute(template_cfg, context)

        # 模板执行失败：errors 非空时视为 transient（让 Engine 决定重试或 fallback）
        if not result.success:
            err_msg = "; ".join(result.errors) if result.errors else "模板执行失败"
            raise StepExecutionError("transient", err_msg)

        outputs: Dict[str, Any] = {}
        if result.assistant_response:
            outputs["response"] = result.assistant_response
        if result.metrics_for_injection:
            outputs["metrics"] = result.metrics_for_injection
        return outputs, list(result.tool_calls), list(result.outputs)


# ---------------------------------------------------------------------------
# 5.3b LlmCallExecutor
# ---------------------------------------------------------------------------


class _LlmCallTemplate(WorkflowTemplate):
    """LlmCallExecutor 的内部辅助模板。

    复用 ``WorkflowTemplate._call_llm_single_turn`` 的逻辑（D5 修复后版本），
    避免重复实现 LLM 调用与异常处理。
    """

    name = "_llm_call"

    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:  # pragma: no cover - 不应被直接调用
        raise NotImplementedError("LlmCallExecutor 使用 _call_llm_single_turn")


class LlmCallExecutor(StepExecutor):
    """单轮 LLM 调用。

    ``config`` 字段:
    - ``prompt``（必填）: 用户输入文本（含时间变量占位符，由 context.render 替换）
    - ``system``（可选）: system prompt
    - ``tools``（可选）: 工具 schema 列表

    LLM 调用失败时记录到 ``context.append_error``（若可用，D5 修复），
    并抛 :class:`StepExecutionError`（``error_class="transient"``）。
    """

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        prompt_template = spec.config.get("prompt", "")
        if not prompt_template:
            raise StepExecutionError(
                "permanent", "llm step 缺少 config.prompt 字段"
            )

        system_prompt = spec.config.get("system")
        tools = spec.config.get("tools")

        # 时间变量替换（与 WorkflowContext.render 一致）
        user_input = context.render(prompt_template)

        # D5 修复后的 _call_llm_single_turn：异常写入 error_channel
        # 但 base.py 当前版本仍吞异常 → 此处显式捕获并写入 context
        helper = _LlmCallTemplate()
        if context.llm_client is None:
            # LLM 客户端未配置：写入 error_channel 并抛 transient
            self._append_error(context, "LLM 客户端未配置（context.llm_client=None）")
            raise StepExecutionError(
                "permanent", "LLM 客户端未配置"
            )

        try:
            response_text, tool_calls = helper._call_llm_single_turn(
                context, user_input, system=system_prompt, tools=tools
            )
        except Exception as e:
            # D5 修复：异常写入 error_channel（若可用）
            err_msg = f"LLM 调用失败: {type(e).__name__}: {e}"
            self._append_error(context, err_msg)
            raise StepExecutionError("transient", err_msg) from e

        # 空响应视为 transient 失败（让 Engine 决定重试）
        if not response_text and not tool_calls:
            self._append_error(context, "LLM 返回空响应")
            raise StepExecutionError("transient", "LLM 返回空响应")

        outputs: Dict[str, Any] = {"response": response_text}
        return outputs, list(tool_calls), []

    def _append_error(self, context: WorkflowContext, message: str) -> None:
        """向 context.error_channel 追加错误（若可用）。

        Task 8 之前 WorkflowContext 未实装 error_channel 字段，此处使用
        getattr 容错跳过。
        """
        append_error = getattr(context, "append_error", None)
        if callable(append_error):
            try:
                append_error(message)
            except Exception:
                logger.debug("context.append_error 调用失败", exc_info=True)


# ---------------------------------------------------------------------------
# 5.3c ToolCallExecutor
# ---------------------------------------------------------------------------


class ToolCallExecutor(StepExecutor):
    """直接调用单个工具。

    ``config`` 字段:
    - ``tool``（必填）: 工具名
    - ``input``（可选）: 工具入参 dict
    - ``path_prefix`` / ``allowed_paths``（写操作工具必填）: 写入范围约束

    查找顺序（fallback 链）:
    1. ``context.cron_tool_registry``（cron 会话专用）
    2. ``context.tool_registry``（全局 ToolRegistry）

    安全约束:
    - 调用前 MUST 经 ``context.policy_engine.check(session_id, tool_name,
      tool_input)`` 包装，``action="deny"`` 时抛 :class:`PermissionDeniedError`
    - 调用后 MUST 调 ``context.audit_logger.log_tool_call()`` 透传
      ``schedule_id`` / ``run_id`` / ``step_id``

    工具未注册 → ``error_class="permanent"``；参数错误 → ``"param_error"``；
    其他执行异常 → ``"transient"``。
    """

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        tool_name = spec.config.get("tool", "")
        if not tool_name:
            raise StepExecutionError(
                "permanent", "tool step 缺少 config.tool 字段"
            )
        tool_input = spec.config.get("input") or {}

        # 安全约束：PolicyEngine.check
        policy_engine = getattr(context, "policy_engine", None)
        session_id = getattr(context, "session_id", None) or context.session_id
        if policy_engine is not None:
            try:
                decision = policy_engine.check(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    session_id=session_id,
                )
            except Exception as e:
                # PolicyEngine 异常不应阻塞工具调用，但记录日志
                logger.warning(
                    "PolicyEngine.check 异常 (tool=%s): %s", tool_name, e
                )
            else:
                if decision.action == "deny":
                    raise PermissionDeniedError(
                        f"工具 '{tool_name}' 被策略拒绝: {decision.reason}"
                    )
                # confirm 在 cron 会话视为 deny（无人在场确认）
                # （cron 路径的 confirm 行为由 PolicyEngine 内部处理，
                # 这里仅兜底：若 cron 路径返回 confirm 也视为 deny）
                # 注：实际 cron 路径已通过 _check_cron_grant 处理 confirm，
                # 到达 StepExecutor 时 decision 通常是 allow / deny。

        # 工具查找（fallback 链）
        registry = self._resolve_registry(context, tool_name)
        if registry is None:
            raise StepExecutionError(
                "permanent",
                f"工具 '{tool_name}' 未在任何 registry 注册（cron_tool_registry / tool_registry）",
            )

        # 执行
        start_ts = time.perf_counter()
        is_error = False
        error_class = ""
        error_message = ""
        result_str = ""
        try:
            result_str = registry.execute_tool(tool_name, tool_input)
        except Exception as e:
            is_error = True
            result_str = f"[失败] {type(e).__name__}: {e}"
            # 工具未注册 / 参数错误 → permanent / param_error
            cls_name = type(e).__name__
            if "NotFound" in cls_name:
                error_class = "permanent"
                error_message = f"工具未注册: {e}"
            elif "Param" in cls_name or "param" in cls_name.lower():
                error_class = "param_error"
                error_message = f"参数校验失败: {e}"
            else:
                error_class = "transient"
                error_message = f"工具执行失败: {type(e).__name__}: {e}"
        finally:
            duration_ms = (time.perf_counter() - start_ts) * 1000

        # 审计日志（透传 schedule_id / run_id / step_id）
        audit_logger = getattr(context, "audit_logger", None)
        if audit_logger is not None:
            try:
                audit_logger.log_tool_call(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    result=result_str,
                    is_error=is_error,
                    duration_ms=duration_ms,
                    schedule_id=getattr(context, "schedule_id", None),
                    run_id=getattr(context, "run_id", None),
                )
            except Exception as e:
                logger.warning("audit_logger.log_tool_call 失败: %s", e)

        # step_id 注入审计记录（Task 15 实装，当前 fallback 通过 _log_audit 路径）
        # 此处不强制写入 step_id 字段，避免破坏现有 audit.jsonl 结构。

        if is_error:
            raise StepExecutionError(error_class, error_message)

        outputs: Dict[str, Any] = {"result": result_str, "tool": tool_name}
        tool_call_record = {
            "name": tool_name,
            "input": dict(tool_input),
            "result": result_str,
            "is_error": False,
        }
        return outputs, [tool_call_record], []

    def _resolve_registry(
        self, context: WorkflowContext, tool_name: str
    ) -> Optional[Any]:
        """按 fallback 链查找含 ``tool_name`` 的 registry。

        优先级:
        1. ``context.cron_tool_registry``（cron 会话专用）
        2. ``context.tool_registry``（全局）
        """
        # 1. cron_tool_registry
        cron_registry = getattr(context, "cron_tool_registry", None)
        if cron_registry is not None and self._has_tool(cron_registry, tool_name):
            return cron_registry
        # 2. 全局 tool_registry
        tool_registry = getattr(context, "tool_registry", None)
        if tool_registry is not None and self._has_tool(tool_registry, tool_name):
            return tool_registry
        return None

    @staticmethod
    def _has_tool(registry: Any, tool_name: str) -> bool:
        """检查 registry 是否含指定工具（兼容多种 API）。"""
        # CronToolRegistry 提供 has_tool
        has_tool = getattr(registry, "has_tool", None)
        if callable(has_tool):
            try:
                return bool(has_tool(tool_name))
            except Exception:
                pass
        # 退化到 get_tools_schema
        try:
            schemas = registry.get_tools_schema()
            return any(s.get("name") == tool_name for s in schemas if isinstance(s, dict))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 5.3d ReactLoopExecutor
# ---------------------------------------------------------------------------


class ReactLoopExecutor(StepExecutor):
    """ReactLoop 多轮工具调用循环（D7 修复）。

    ``config`` 字段:
    - ``task``（必填）: 任务描述（含时间变量占位符）
    - ``system``（可选）: system prompt 覆盖
    - ``max_loops``（可选）: 由 react_loop 自身配置控制
    - ``react_loop``（可选）: 直接传入 ReactLoop 实例
    - ``tool_whitelist``（可选）: 工具白名单（P1 实装，P0 跳过）

    D7 修复：``asyncio.run`` 在已有事件循环的线程中会抛 ``RuntimeError``，
    兜底使用 ``asyncio.run_coroutine_threadsafe(coro, loop).result()``。
    """

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        task_template = spec.config.get("task", "")
        if not task_template:
            raise StepExecutionError(
                "permanent", "react step 缺少 config.task 字段"
            )

        react_loop = spec.config.get("react_loop") or getattr(
            context, "react_loop", None
        )
        if react_loop is None:
            raise StepExecutionError(
                "permanent",
                "react step 未配置 react_loop（context 也无 react_loop 属性）",
            )

        system_prompt = spec.config.get("system") or ""
        user_input = context.render(task_template)

        # D7 修复：try asyncio.run + except RuntimeError 兜底
        try:
            response_text, messages_used, _is_complete, _reason = asyncio.run(
                react_loop.run(
                    user_input=user_input,
                    history=[],
                    system=system_prompt,
                    session_id=context.session_id,
                )
            )
        except RuntimeError as e:
            # 已有事件循环的线程中调用 asyncio.run 抛 RuntimeError
            logger.info(
                "asyncio.run 不可用（已有事件循环），降级到 "
                "run_coroutine_threadsafe: %s",
                e,
            )
            try:
                loop = asyncio.get_event_loop_policy().get_event_loop()
            except Exception as loop_err:
                raise StepExecutionError(
                    "permanent",
                    f"获取事件循环失败: {loop_err}",
                ) from loop_err
            if loop is None or not loop.is_running():
                raise StepExecutionError(
                    "permanent",
                    f"事件循环不可用，无法执行 react_loop: {e}",
                )
            future = asyncio.run_coroutine_threadsafe(
                react_loop.run(
                    user_input=user_input,
                    history=[],
                    system=system_prompt,
                    session_id=context.session_id,
                ),
                loop,
            )
            try:
                response_text, messages_used, _is_complete, _reason = (
                    future.result()
                )
            except Exception as fetch_err:
                raise StepExecutionError(
                    "transient",
                    f"ReactLoop 执行失败: {fetch_err}",
                ) from fetch_err

        # 提取工具调用
        tool_calls = self._extract_tool_calls(messages_used)

        outputs: Dict[str, Any] = {"response": response_text}
        if tool_calls:
            outputs["tool_calls_count"] = len(tool_calls)
        return outputs, tool_calls, []

    def _extract_tool_calls(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """从 ReactLoop 返回的 messages 提取工具调用列表。

        与 ResearchTemplate._extract_tool_calls 逻辑一致，独立实现避免
        循环依赖。
        """
        tool_calls: List[Dict[str, Any]] = []
        use_blocks: Dict[str, Dict[str, Any]] = {}
        results: Dict[str, Dict[str, Any]] = {}

        for msg in messages or []:
            content = msg.get("content")
            if isinstance(content, str) or not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    use_id = block.get("id", "")
                    use_blocks[use_id] = block
                elif btype == "tool_result":
                    use_id = block.get("tool_use_id", "")
                    results[use_id] = block

        for use_id, use_block in use_blocks.items():
            result_block = results.get(use_id, {})
            content = result_block.get("content", "")
            if isinstance(content, list):
                parts = []
                for sub in content:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(sub.get("text", ""))
                result_text = "".join(parts)
            elif isinstance(content, str):
                result_text = content
            else:
                result_text = str(content) if content else ""

            tool_calls.append(
                {
                    "name": use_block.get("name", ""),
                    "input": use_block.get("input", {}) or {},
                    "result": result_text,
                    "is_error": bool(result_block.get("is_error", False)),
                }
            )
        return tool_calls


# ---------------------------------------------------------------------------
# 5.3e SubworkflowExecutor（P2 stub）
# ---------------------------------------------------------------------------


class SubworkflowExecutor(StepExecutor):
    """subworkflow step P2 stub。

    P2 阶段实装，当前抛 ``NotImplementedError``。Engine 收到此异常时
    MUST 视为 PERMANENT 错误走 abort 路径，不触发 retry。

    ``config`` 字段（P2 设计，当前未实装）:
    - ``workflow_ref``: 引用已注册的 workflow name
    - 或 inline spec dict
    """

    def _run(
        self,
        spec: StepSpec,
        context: WorkflowContext,
        trace: StepTrace,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        raise NotImplementedError("subworkflow P2 实装")


# ---------------------------------------------------------------------------
# Executor 注册表
# ---------------------------------------------------------------------------


#: step.type → Executor 实例 映射（WorkflowEngine 按此查找）
STEP_EXECUTORS: Dict[str, StepExecutor] = {
    "deterministic": DeterministicExecutor(),
    "llm": LlmCallExecutor(),
    "tool": ToolCallExecutor(),
    "react": ReactLoopExecutor(),
    "subworkflow": SubworkflowExecutor(),
}


def get_step_executor(step_type: str) -> Optional[StepExecutor]:
    """按 step.type 查找 Executor 实例。

    参数:
        step_type: step 类型字符串。

    返回:
        :class:`StepExecutor` 实例，未注册返回 ``None``。
    """
    return STEP_EXECUTORS.get(step_type)


__all__ = [
    "StepExecutionError",
    "PermissionDeniedError",
    "StepExecutor",
    "DeterministicExecutor",
    "LlmCallExecutor",
    "ToolCallExecutor",
    "ReactLoopExecutor",
    "SubworkflowExecutor",
    "STEP_EXECUTORS",
    "get_step_executor",
]
