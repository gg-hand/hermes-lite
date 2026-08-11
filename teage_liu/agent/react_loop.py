"""React（Reasoning + Acting）循环引擎。

通过 LLM 进行推理与工具调用的迭代循环：
1. 将用户输入与历史拼入 messages，调用主对话 LLM；
2. 若模型返回 tool_use 块，则执行工具并将 tool_result 回传，继续循环；
3. 若模型返回最终文本（end_turn），则结束循环并返回回复；
4. 达到 max_loops 仍未完成时，返回当前最后的文本回复。

Anthropic tool_use 响应格式：
- response.content 是 block 列表，包含 {type: "text", text} 与
  {type: "tool_use", id, name, input} 两种块。
- 工具结果回传格式：
  {role: "user", content: [{type: "tool_result", tool_use_id, content}]}

Phase 5 新增策略拦截层：在工具执行前调用 PolicyEngine 评估，按
allow / confirm / deny 三态路由。allow 直接执行；deny 拒绝并返回
拒绝原因；confirm 在流式模式下（run_stream）通过 ApprovalManager
发起 Human-in-the-Loop 审批，非流式模式（run）自动拒绝。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入以避免循环依赖
    from ..llm.client import LLMClient
    from ..llm.reasoning_profiles import ReasoningConfig

try:
    from ..stream_manager import StreamCancelled
    from ..monitoring.metrics import MetricsCollector
    from ..agent.audit import AuditLogger
    from ..agent.policy import PolicyEngine, Decision
    from ..agent.approval import ApprovalManager
except ImportError:
    from teage_liu.stream_manager import StreamCancelled  # type: ignore
    from teage_liu.monitoring.metrics import MetricsCollector  # type: ignore
    from teage_liu.agent.audit import AuditLogger  # type: ignore
    from teage_liu.agent.policy import PolicyEngine, Decision  # type: ignore
    from teage_liu.agent.approval import ApprovalManager  # type: ignore
from .error_classifier import ErrorClassifier, ErrorClass
from .tool_error import (
    EXECUTION_ERROR_CATEGORIES,
    ErrorStage,
    NonStreamHILError,
    PolicyDeniedError,
    StuckDetectedError,
    ToolError,
    UserRejectedError,
    from_exception,
)
from teage_liu.guardrails import GuardrailEngine
from .tool_executor import (
    ToolExecutor,
    compute_params_hash as _compute_params_hash_fn,
    detect_tool_stuck as _detect_tool_stuck_fn,
    build_stuck_message as _build_stuck_message_fn,
    extract_schedule_id as _extract_schedule_id_fn,
    drop_trailing_orphan_tool_calls as _drop_trailing_orphan_tool_calls_fn,
)
from .stream_handler import (
    block_to_dict as _block_to_dict_fn,
    build_done_event as _build_done_event_fn,
    build_reasoning_stats as _build_reasoning_stats_fn,
    generate_max_loops_summary as _generate_max_loops_summary_fn,
)
from .stream_runner import StreamRunner
from .sync_runner import SyncRunner
from .noop_guardrail import NoopGuardrail as _NoopGuardrail
from .meta_cognition import (
    maybe_trigger_agent_failure_signal as _maybe_trigger_agent_failure_signal_fn,
    check_user_failure_feedback as _check_user_failure_feedback_fn,
)

logger = logging.getLogger(__name__)


class ReactLoop:
    """React（Reasoning + Acting）循环引擎。

    通过 LLM 进行推理与工具调用的迭代循环，直至模型返回最终文本回复
    或达到最大循环次数。

    Attributes:
        llm_client: LLMClient 实例，提供 chat_main 调用。
        tool_registry: 工具注册器，为 None 时进入纯对话模式（不传 tools）。
        max_loops: 最大循环次数，默认 50。Phase 9 Task 7：从 10 提至 50，
            配合 orchestrator 自动续接（总熔断 200 轮）与单工具重试检测，
            彻底消除用户手动"继续"。
        audit_logger: 可选审计日志记录器，非 None 时记录每次工具调用。
        metrics: 可选指标采集器，非 None 时上报工具调用指标。
        policy_engine: 可选策略评估器，非 None 时在工具执行前评估策略，
            按 allow/confirm/deny 三态路由；为 None 时直接放行（向后兼容）。
        approval_manager: 可选审批管理器，非 None 时在流式模式下处理
            confirm 决策的 Human-in-the-Loop 审批流程。
    """

    def __init__(
        self,
        llm_client: "LLMClient",
        tool_registry: Optional[Any] = None,
        max_loops: int = 50,
        audit_logger: Optional["AuditLogger"] = None,
        metrics: Optional["MetricsCollector"] = None,
        policy_engine: Optional["PolicyEngine"] = None,
        approval_manager: Optional["ApprovalManager"] = None,
        cron_tool_registry: Optional[Any] = None,
        guardrail_engine: Optional["GuardrailEngine"] = None,
        orchestrator_ref: Optional[Any] = None,
    ) -> None:
        """初始化 React 循环引擎。

        参数:
            llm_client: LLMClient 实例。
            tool_registry: 工具注册器，为 None 时纯对话模式。
            max_loops: 最大循环次数。
            audit_logger: 可选审计日志记录器，默认 None 不记录审计。
            metrics: 可选指标采集器，默认 None 不上报指标。
            policy_engine: 可选策略评估器，默认 None 时跳过策略拦截（向后兼容）。
            approval_manager: 可选审批管理器，默认 None 时流式模式命中 confirm
                将降级为 deny。
            cron_tool_registry: Phase 8 Task 5.7。可选的 CronToolRegistry 实例，
                用于派发 cron_tool 调用（子进程执行）。当 LLM 调用的工具名在
                cron_tool_registry 中已注册时，走 ``cron_tool_registry.execute_tool``
                路径；否则回退到 ``tool_registry.execute_tool``。为 ``None`` 时
                （用户会话路径）所有工具调用走 tool_registry，向后兼容。
            guardrail_engine: Phase 9 Task 6。可选的 GuardrailEngine 实例，
                用于对外部工具返回值做脱敏（fail-open 软护栏）。为 ``None`` 时
                降级为 ``GuardrailEngine.create_noop()``（所有方法空操作），
                避免 react_loop 空指针。
            orchestrator_ref: 可选的 Orchestrator 实例引用，用于 stash 中断提示
                到 ``_pending_interrupt_notices``，使 stuck/cancel 等终止性错误
                在下一轮用户消息时浮出。使用 ``weakref`` 存储避免循环引用
                （Orchestrator → ReactLoop → Orchestrator）导致 GC 无法回收
                （见边界 9.18）。为 ``None`` 时 stuck 路径仅记 warning 日志。
        """
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_loops = max_loops
        self.audit_logger = audit_logger
        self.metrics = metrics
        self.policy_engine = policy_engine
        self.approval_manager = approval_manager
        # Phase 8 Task 5.7: cron_tool 派发 registry（仅 cron 会话路径注入）
        self.cron_tool_registry = cron_tool_registry
        # Phase 9 Task 6: GuardrailEngine 引用（工具返回值脱敏）
        # None 时降级为 noop，避免每次调用前 None 判断。
        if guardrail_engine is not None:
            self.guardrail_engine = guardrail_engine
        elif GuardrailEngine is not None:
            self.guardrail_engine = GuardrailEngine.create_noop()
        else:
            # GuardrailEngine 模块不可用时构造一个最简 noop 占位对象，
            # 提供 sanitize_tool_result / scan_input / filter_output 三个方法
            # 全部直返原值，保证调用方不抛异常。
            self.guardrail_engine = _NoopGuardrail()
        # Phase B-4: Orchestrator 弱引用（用于 _stash_interrupt_notice）
        # weakref 避免 Orchestrator → ReactLoop → Orchestrator 循环引用导致
        # GC 无法回收（见边界 9.18）。orchestrator_ref 为 None 时 _stash
        # 路径降级为 warning 日志，不抛异常。
        self._orchestrator_ref: Optional[weakref.ReferenceType] = (
            weakref.ref(orchestrator_ref) if orchestrator_ref is not None else None
        )
        # 信息计数器：user / assistant / tool 各算 1 条，用于触发 consolidation
        self._info_count: int = 0
        # Task 21: 工具执行逻辑委托到 ToolExecutor
        if ToolExecutor is not None:
            self._tool_executor = ToolExecutor(
                tool_registry=tool_registry,
                cron_tool_registry=cron_tool_registry,
                policy_engine=policy_engine,
                audit_logger=audit_logger,
            )
        else:
            self._tool_executor = None
        # Task 13: 流式执行逻辑委托到 StreamRunner
        if StreamRunner is not None:
            self.stream_runner = StreamRunner(self)
        else:
            self.stream_runner = None
        # Task 14: 同步执行逻辑委托到 SyncRunner
        if SyncRunner is not None:
            self.sync_runner = SyncRunner(self)
        else:
            self.sync_runner = None

        # 批次 2.4: 最近一次 run() 累积的 LLM usage（input/output tokens 之和）
        # 每次 run() 开始时重置为 None，避免跨调用污染
        # chat_handler / scheduler 读取此属性回填 messages.token_count
        self.last_usage: Optional[Dict[str, Any]] = None

    def get_info_count(self) -> int:
        """返回当前信息计数器值。"""
        return self._info_count

    def reset_info_count(self) -> None:
        """重置信息计数器为 0。"""
        self._info_count = 0

    # ------------------------------------------------------------------
    # Phase B-4: 跨轮中断提示 stash（stuck/cancel 等终止性错误用）
    # ------------------------------------------------------------------
    def _stash_interrupt_notice(self, session_id: str, text: str) -> None:
        """通过 orchestrator 引用 stash 中断提示，下一轮 chat() 注入到 messages[0] 动态区。

        stuck/cancel 等终止性错误当轮无法注入（循环立即 return），通过此通道
        在下一轮用户消息时浮出。复用 orchestrator._pending_interrupt_notices
        既有机制（TTL 300s 自动过期清理）。

        参数:
            session_id: 会话 ID。
            text: 中断提示文本（通常是 ``ToolError.to_system_block()`` 输出）。

        说明:
            - orchestrator_ref 为 None 或已被 GC 回收时，降级为 warning 日志，
              不抛异常（见边界 9.18）。
            - stash 格式与 orchestrator 既有路径一致：``{"content": str, "timestamp": float}``
              参见 orchestrator.py L2343。
        """
        orch = self._orchestrator_ref() if self._orchestrator_ref else None
        if orch is None:
            logger.warning(
                "orchestrator_ref 不可用，中断提示将丢失（session=%s）: %s",
                session_id, text[:80],
            )
            return
        orch._pending_interrupt_notices[session_id] = {
            "content": text,
            "timestamp": time.time(),
        }
        logger.info(
            "InterruptNotice 已 stash (session=%s): %s", session_id, text[:80]
        )

    # ------------------------------------------------------------------
    # Phase 元认知: Agent 自画像信号触发（连续失败/PERMANENT 错误）
    # ------------------------------------------------------------------
    def _maybe_trigger_agent_failure_signal(
        self,
        session_id: Optional[str],
        tool_name: str,
        error_class: Optional[str],
        consecutive_failures: int,
    ) -> bool:
        """触发 Agent 失败信号入池（委托到 meta_cognition.maybe_trigger_agent_failure_signal）。"""
        return _maybe_trigger_agent_failure_signal_fn(
            self._orchestrator_ref, session_id, tool_name,
            error_class, consecutive_failures,
        )

    def _check_user_failure_feedback(self, user_input: str, session_id: Optional[str]) -> None:
        """检测用户失败反馈并触发信号（委托到 meta_cognition.check_user_failure_feedback）。"""
        _check_user_failure_feedback_fn(
            self._orchestrator_ref, user_input, session_id,
        )

    # ------------------------------------------------------------------
    # Phase 9 Task 7.3: 单工具重试检测辅助方法
    # ------------------------------------------------------------------
    def _evaluate_policy(
        self,
        tool_name: str,
        tool_input: dict,
        session_id: Optional[str] = None,
    ) -> "Decision":
        """评估工具调用的策略决策（委托到 ToolExecutor.evaluate_policy）。"""
        executor = getattr(self, "_tool_executor", None)
        if executor is not None:
            return executor.evaluate_policy(
                tool_name, tool_input, session_id=session_id
            )
        return Decision("allow", "", "low")

    def _log_audit(
        self,
        session_id: Optional[str],
        tool_name: str,
        tool_input: dict,
        result: str,
        is_error: bool,
        duration_ms: float,
        decision: Optional["Decision"] = None,
    ) -> None:
        """记录工具调用审计日志（委托到 ToolExecutor.log_audit）。"""
        executor = getattr(self, "_tool_executor", None)
        if executor is not None:
            executor.log_audit(
                session_id, tool_name, tool_input, result,
                is_error, duration_ms, decision,
            )

    def _execute_tool_with_dispatch(
        self, tool_name: str, tool_input: dict,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """执行工具调用（委托到 ToolExecutor.execute_tool_with_dispatch）。"""
        executor = getattr(self, "_tool_executor", None)
        if executor is None:
            # 兼容通过 __new__ 创建的测试实例（未走 __init__）
            if ToolExecutor is not None:
                executor = ToolExecutor(
                    tool_registry=getattr(self, "tool_registry", None),
                    cron_tool_registry=getattr(self, "cron_tool_registry", None),
                    policy_engine=getattr(self, "policy_engine", None),
                    audit_logger=getattr(self, "audit_logger", None),
                )
            else:
                raise ToolNotFoundError(
                    tool_name=tool_name,
                    reason=f"ToolExecutor 未导入，无法执行 {tool_name}",
                    suggestion="检查 ReactLoop 初始化配置",
                )
        return executor.execute_tool_with_dispatch(
            tool_name, tool_input, cancel_event
        )

    def tool_is_blocking(self, tool_name: str) -> bool:
        """判断工具是否为阻塞型（需 asyncio.to_thread 执行）。

        委托到 tool_registry.is_blocking_tool；tool_registry 缺失时返回 False。
        """
        registry = getattr(self, "tool_registry", None)
        if registry is None:
            return False
        is_blocking = getattr(registry, "is_blocking_tool", None)
        if is_blocking is None:
            return False
        try:
            return bool(is_blocking(tool_name))
        except Exception as e:  # noqa: BLE001
            logger.debug("tool_is_blocking 查询失败 %s: %s", tool_name, e)
            return False

    async def execute_tool_with_dispatch_async(
        self,
        tool_name: str,
        tool_input: dict,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """异步执行阻塞型工具（to_thread 桥接）。

        委托到 ToolExecutor.execute_tool_with_dispatch_async；_tool_executor
        缺失（__new__ 测试实例）时降级为 asyncio.to_thread 包同步方法。
        """
        executor = getattr(self, "_tool_executor", None)
        if executor is not None and hasattr(executor, "execute_tool_with_dispatch_async"):
            return await executor.execute_tool_with_dispatch_async(
                tool_name, tool_input, cancel_event
            )
        return await asyncio.to_thread(
            self._execute_tool_with_dispatch, tool_name, tool_input, cancel_event
        )

    async def run(
        self,
        user_input: str,
        history: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        session_id: Optional[str] = None,
        tools_override: Optional[List[Dict[str, Any]]] = None,
        cancel_event: Optional[threading.Event] = None,
        reasoning_cfg: Optional["ReasoningConfig"] = None,
        is_cron: bool = False,
    ) -> Tuple[str, List[Dict[str, Any]], bool, str]:
        """同步 React 循环（委托到 SyncRunner）。"""
        return await self.sync_runner.run(
            user_input=user_input,
            history=history,
            system=system,
            session_id=session_id,
            tools_override=tools_override,
            cancel_event=cancel_event,
            reasoning_cfg=reasoning_cfg,
            is_cron=is_cron,
        )

    async def run_stream(
        self,
        user_input: str,
        history: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        session_id: Optional[str] = None,
        tools_override: Optional[List[Dict[str, Any]]] = None,
        cancel_event: Optional[threading.Event] = None,
        reasoning_cfg: Optional["ReasoningConfig"] = None,
        is_cron: bool = False,
        stream_manager: Optional[Any] = None,
    ):
        """流式执行 React 循环（委托到 StreamRunner）。"""
        async for event in self.stream_runner.run_stream(
            user_input=user_input,
            history=history,
            system=system,
            session_id=session_id,
            tools_override=tools_override,
            cancel_event=cancel_event,
            reasoning_cfg=reasoning_cfg,
            is_cron=is_cron,
            stream_manager=stream_manager,
        ):
            yield event

    async def _generate_max_loops_summary(
        self,
        messages: List[Dict[str, Any]],
        last_text: str,
        session_id: Optional[str] = None,
    ) -> str:
        """达到 max_loops 时生成总结性回复（委托到 stream_handler.generate_max_loops_summary）。"""
        return await _generate_max_loops_summary_fn(
            self.llm_client, messages, last_text, session_id,
        )

    # ── 静态/实例 wrapper（保留为公共 API，供 tests/chat_handler 外部调用） ──
    @staticmethod
    def _compute_params_hash(tool_input: dict) -> str:
        return _compute_params_hash_fn(tool_input)

    @staticmethod
    def _detect_tool_stuck(tool_name, params_hash, recent_calls, window_size=5, threshold=3):
        return _detect_tool_stuck_fn(tool_name, params_hash, recent_calls, window_size, threshold)

    @staticmethod
    def _build_stuck_message(tool_name: str, reason: str = "") -> str:
        return _build_stuck_message_fn(tool_name, reason)

    @staticmethod
    def _drop_trailing_orphan_tool_calls(messages):
        return _drop_trailing_orphan_tool_calls_fn(messages)

    @staticmethod
    def _extract_schedule_id(session_id):
        return _extract_schedule_id_fn(session_id)

    @staticmethod
    def _block_to_dict(block: Any) -> Dict[str, Any]:
        return _block_to_dict_fn(block)

    def _build_done_event(self, *, response, messages, is_complete, termination_reason,
                          usage=None, content_blocks=None, stop_reason=None,
                          reasoning_cfg=None, current_round_text="", current_round_reasoning=""):
        return _build_done_event_fn(
            response=response, messages=messages, is_complete=is_complete,
            termination_reason=termination_reason, usage=usage,
            content_blocks=content_blocks, stop_reason=stop_reason,
            reasoning_cfg=reasoning_cfg, current_round_text=current_round_text,
            current_round_reasoning=current_round_reasoning,
        )

    @staticmethod
    def _build_reasoning_stats(reasoning_cfg, usage):
        return _build_reasoning_stats_fn(reasoning_cfg, usage)

