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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入以避免循环依赖
    from ..llm.client import LLMClient
    from ..llm.reasoning_profiles import ReasoningConfig

try:
    from ..stream_manager import StreamCancelled
except ImportError:
    import sys
    from pathlib import Path
    _SRC_DIR = str(Path(__file__).resolve().parent.parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from stream_manager import StreamCancelled  # type: ignore
    try:
        from ..monitoring.metrics import MetricsCollector
        from ..agent.audit import AuditLogger
        from ..agent.policy import PolicyEngine, Decision
        from ..agent.approval import ApprovalManager
    except ImportError:
        from monitoring.metrics import MetricsCollector  # type: ignore
        from agent.audit import AuditLogger  # type: ignore
        from agent.policy import PolicyEngine, Decision  # type: ignore
        from agent.approval import ApprovalManager  # type: ignore

# Phase 9+ 错误分类模块
try:
    from .error_classifier import ErrorClassifier, ErrorClass
except ImportError:
    try:
        from agent.error_classifier import ErrorClassifier, ErrorClass  # type: ignore
    except ImportError:
        ErrorClassifier = None  # type: ignore
        ErrorClass = None  # type: ignore

# 统一工具错误异常层次（Phase A：替换字符串错误载体）
try:
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
except ImportError:
    try:
        from agent.tool_error import (  # type: ignore
            EXECUTION_ERROR_CATEGORIES,
            ErrorStage,
            NonStreamHILError,
            PolicyDeniedError,
            StuckDetectedError,
            ToolError,
            UserRejectedError,
            from_exception,
        )
    except ImportError:  # pragma: no cover
        ToolError = None  # type: ignore
        ErrorStage = None  # type: ignore
        from_exception = None  # type: ignore
        PolicyDeniedError = None  # type: ignore
        NonStreamHILError = None  # type: ignore
        UserRejectedError = None  # type: ignore
        StuckDetectedError = None  # type: ignore
        EXECUTION_ERROR_CATEGORIES = frozenset()  # type: ignore

# Phase 9 Task 6: GuardrailEngine（fail-open 软护栏，工具返回值脱敏）
# 与 monitoring / agent 模块同样降级为 None，由 __init__ 内部降级为 noop。
try:
    from ..guardrails import GuardrailEngine
except ImportError:
    try:
        from guardrails import GuardrailEngine  # type: ignore
    except ImportError:
        GuardrailEngine = None  # type: ignore

# Phase 5: 策略与审批模块（运行时需要 Decision 实例化）
# react_loop.py 本身位于 agent 目录下，相对导入用 from .policy
try:
    from .policy import Decision
except ImportError:  # pragma: no cover
    try:
        from agent.policy import Decision  # type: ignore
    except ImportError:
        # 降级：定义占位 Decision，避免 import 失败阻断
        from dataclasses import dataclass
        from typing import Literal

        @dataclass
        class Decision:  # type: ignore
            action: Literal["allow", "confirm", "deny"]
            reason: str
            risk_level: Literal["low", "medium", "high"]

logger = logging.getLogger(__name__)

# Task 21: 工具执行逻辑提取到 ToolExecutor
try:
    from .tool_executor import (
        ToolExecutor,
        compute_params_hash as _compute_params_hash_fn,
        detect_tool_stuck as _detect_tool_stuck_fn,
        build_stuck_message as _build_stuck_message_fn,
        extract_schedule_id as _extract_schedule_id_fn,
    )
except ImportError:  # pragma: no cover
    try:
        from agent.tool_executor import (  # type: ignore
            ToolExecutor,
            compute_params_hash as _compute_params_hash_fn,
            detect_tool_stuck as _detect_tool_stuck_fn,
            build_stuck_message as _build_stuck_message_fn,
            extract_schedule_id as _extract_schedule_id_fn,
        )
    except ImportError:  # pragma: no cover
        ToolExecutor = None  # type: ignore
        _compute_params_hash_fn = None  # type: ignore
        _detect_tool_stuck_fn = None  # type: ignore
        _build_stuck_message_fn = None  # type: ignore
        _extract_schedule_id_fn = None  # type: ignore

# Task 22: 流式处理辅助函数提取到 stream_handler
try:
    from .stream_handler import (
        block_to_dict as _block_to_dict_fn,
        build_done_event as _build_done_event_fn,
        build_reasoning_stats as _build_reasoning_stats_fn,
    )
except ImportError:  # pragma: no cover
    try:
        from agent.stream_handler import (  # type: ignore
            block_to_dict as _block_to_dict_fn,
            build_done_event as _build_done_event_fn,
            build_reasoning_stats as _build_reasoning_stats_fn,
        )
    except ImportError:  # pragma: no cover
        _block_to_dict_fn = None  # type: ignore
        _build_done_event_fn = None  # type: ignore
        _build_reasoning_stats_fn = None  # type: ignore

# Task 13: 流式执行逻辑提取到 StreamRunner
try:
    from .stream_runner import StreamRunner
except ImportError:  # pragma: no cover
    try:
        from agent.stream_runner import StreamRunner  # type: ignore
    except ImportError:  # pragma: no cover
        StreamRunner = None  # type: ignore

# Task 14: 同步执行逻辑提取到 SyncRunner
try:
    from .sync_runner import SyncRunner
except ImportError:  # pragma: no cover
    try:
        from agent.sync_runner import SyncRunner  # type: ignore
    except ImportError:  # pragma: no cover
        SyncRunner = None  # type: ignore


class ToolRegistry(Protocol):
    """工具注册器接口约定（占位 Protocol）。

    任何提供 get_tools_schema 与 execute_tool 方法的对象均可作为
    ReactLoop 的 tool_registry 注入，实现解耦。
    """

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        """返回工具 schema 列表（Anthropic tool use 格式）。"""
        ...

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """执行工具调用，返回结果字符串。"""
        ...


class _NoopGuardrail:
    """GuardrailEngine 模块不可用时的 noop 占位（Phase 9 Task 6）。

    提供 ``sanitize_tool_result`` / ``scan_input`` / ``filter_output`` 三个
    方法，全部直返原值，保证 react_loop 调用方不抛异常。仅在 GuardrailEngine
    模块导入失败（``GuardrailEngine is None``）时使用，正常路径下
    ``__init__`` 会用 ``GuardrailEngine.create_noop()`` 替代。
    """

    def sanitize_tool_result(self, result: Any, tool_name: str) -> Any:
        """直返原结果（noop）。"""
        return result

    def scan_input(self, text: str):  # type: ignore[no-untyped-def]
        """返回 allow（noop）。

        与 GuardrailEngine.ScanResult 兼容的最简占位，避免引入对 ScanResult
        的硬依赖（GuardrailEngine 模块可能不可用）。
        """
        # 局部 import：仅在调用时尝试导入 ScanResult，失败时返回简单 namedtuple
        try:
            from ..guardrails import ScanResult  # type: ignore
            return ScanResult(action="allow", matched_patterns=[], reason="noop")
        except Exception:
            # 兜底：返回一个轻量 dataclass 实例
            from dataclasses import dataclass, field
            from typing import List as _List

            @dataclass
            class _ScanResultFallback:
                action: str = "allow"
                matched_patterns: _List[str] = field(default_factory=list)
                reason: str = "noop"

            return _ScanResultFallback()

    def filter_output(self, text: str):  # type: ignore[no-untyped-def]
        """直返 (text, 0)（noop）。"""
        return text, 0


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
        tool_registry: Optional[ToolRegistry] = None,
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
        """触发 Agent 自画像信号入池（连续失败≥2 次 或 PERMANENT 错误类）。

        通过 ``orchestrator_ref`` 弱引用访问 ``signal_pool``，调用
        ``signal_pool.add(target="agent", section="Agent 自画像", content=...)``
        将失败模式作为 Agent 自画像信号入池。signal_pool 内部按 target 分组
        去重 + 计数累加，达阈值（7 次）后写入 ``## Agent 自画像`` section。

        cron 会话（session_id 以 ``cron:`` 开头）跳过，避免污染用户画像
        （与 L1 路径的 cron 隔离约束一致）。

        参数:
            session_id: 会话 ID（用于 cron 隔离判断）。
            tool_name: 失败的工具名。
            error_class: 错误分类（``ErrorClass.value`` 字符串），可为 None。
            consecutive_failures: 当前连续失败次数。

        返回:
            True 表示已成功触发入池；False 表示因 signal_pool 不可用或
            cron 会话而跳过。
        """
        # cron 会话跳过：避免 cron 自动任务污染 Agent 自画像
        if session_id and isinstance(session_id, str) and session_id.startswith("cron:"):
            return False
        orch = self._orchestrator_ref() if self._orchestrator_ref else None
        if orch is None or getattr(orch, "signal_pool", None) is None:
            logger.debug(
                "signal_pool 不可用，Agent 失败信号未入池（tool=%s, failures=%d）",
                tool_name, consecutive_failures,
            )
            return False
        # 构造失败模式描述（精炼，避免长文本污染画像）
        ec_part = f"，错误类型={error_class}" if error_class else ""
        content = (
            f"Agent 在工具 {tool_name} 上连续失败 {consecutive_failures} 次{ec_part}"
        )
        try:
            orch.signal_pool.add(
                content=content,
                source="L1_agent_failure",
                section="Agent 自画像",
                target="agent",
            )
            logger.info(
                "Agent 失败信号入池（target=agent）: tool=%s failures=%d ec=%s",
                tool_name, consecutive_failures, error_class,
            )
            return True
        except Exception as e:
            logger.warning("Agent 失败信号入池异常: %s", e)
            return False

    def _check_user_failure_feedback(self, user_input: str, session_id: Optional[str]) -> None:
        """检测用户对失败的口头反馈（"又错了"/"上次说过"等），触发 Agent 信号入池。

        用户说"又错了"/"上次说过"等表明 Agent 重复犯错，应作为 Agent 自画像
        信号入池。本方法在 run()/run_stream() 入口处调用，触发后不阻塞主流程。

        参数:
            user_input: 用户输入文本。
            session_id: 会话 ID（用于 cron 隔离判断）。
        """
        if not user_input:
            return
        # 关键词匹配：用户明确表达 Agent 又错了/重复犯错
        feedback_keywords = ("又错了", "上次说过", "不是说过", "说过不要", "重复犯")
        for kw in feedback_keywords:
            if kw in user_input:
                self._maybe_trigger_agent_failure_signal(
                    session_id=session_id,
                    tool_name="unknown",
                    error_class="user_feedback",
                    consecutive_failures=1,
                )
                return

    # ------------------------------------------------------------------
    # Phase 9 Task 7.3: 单工具重试检测辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_params_hash(tool_input: dict) -> str:
        """计算工具输入参数的 hash（委托到 tool_executor.compute_params_hash）。"""
        return _compute_params_hash_fn(tool_input)

    @staticmethod
    def _detect_tool_stuck(
        tool_name: str,
        params_hash: str,
        recent_calls: List[Tuple[str, str, Optional[str]]],
        window_size: int = 5,
        threshold: int = 3,
    ) -> Tuple[bool, str]:
        """检测工具是否陷入重复调用卡死（委托到 tool_executor.detect_tool_stuck）。"""
        return _detect_tool_stuck_fn(
            tool_name, params_hash, recent_calls, window_size, threshold
        )

    @staticmethod
    def _build_stuck_message(tool_name: str, reason: str = "") -> str:
        """构造卡死终止消息（委托到 tool_executor.build_stuck_message）。"""
        return _build_stuck_message_fn(tool_name, reason)

    @staticmethod
    def _drop_trailing_orphan_tool_calls(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """弹出末尾未执行的 ``assistant(tool_calls)`` 消息。

        Phase 9 修复：卡死终止 / tool_registry 缺失等分支在 LLM 返回
        ``assistant(tool_use)`` 后、工具执行前提前 return，导致 messages
        末尾留下未匹配 tool_result 的孤立 ``assistant(tool_calls)``。
        该 messages 会被持久化进 session db，下一轮请求加载历史时触发
        DeepSeek/OpenAI 400：

            ``An assistant message with 'tool_calls' must be followed by
            tool messages responding to each 'tool_call_id'.``

        本方法从末尾向前弹出连续的孤立 assistant(tool_calls)（content
        为 list 且含 tool_use 块），直到遇到非 assistant 或纯 text
        assistant 为止。返回新列表，不修改原列表。

        注意：仅清理末尾连续孤立项，不动中间消息（中间孤立意味着消息
        序列有更严重问题，删除可能破坏配对）。
        """
        if not messages:
            return messages
        cleaned = list(messages)
        while cleaned:
            last = cleaned[-1]
            if last.get("role") != "assistant":
                break
            content = last.get("content")
            if not isinstance(content, list):
                break
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )
            if not has_tool_use:
                break
            cleaned.pop()
        if len(cleaned) != len(messages):
            logger.warning(
                "清理末尾孤立 assistant(tool_calls): 原始 %d 条 → 清理后 %d 条",
                len(messages),
                len(cleaned),
            )
        return cleaned

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

    @staticmethod
    def _extract_schedule_id(session_id: Optional[str]) -> Optional[str]:
        """从 session_id 提取 cron 调度项 ID（委托到 tool_executor.extract_schedule_id）。"""
        return _extract_schedule_id_fn(session_id)

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
        """达到 max_loops 时调用 LLM 生成总结性回复。

        不传 tools 参数，强制 LLM 返回纯文本总结。失败时降级返回
        ``last_text`` 并记录 error 日志。

        参数:
            messages: 当前循环的完整消息列表。
            last_text: 最后的文本回复（用于降级与 prompt 提示）。
            session_id: 可选会话 ID，仅用于日志关联。

        返回:
            总结性回复文本；调用失败或返回空时降级返回 ``last_text``。
        """
        try:
            summary_prompt = (
                "已达循环上限，请总结当前进展与未完成原因，不要调用工具。"
                f"最后回复：{last_text}"
            )
            summary_messages = messages + [
                {"role": "user", "content": summary_prompt}
            ]
            response = await self.llm_client.chat_main(
                messages=summary_messages,
                system=None,
                tools=None,
            )
            # 提取文本 block（兼容 content 为 dict 列表或对象列表）
            content_blocks = getattr(response, "content", []) or []
            text_parts: List[str] = []
            for block in content_blocks:
                block_dict = self._block_to_dict(block)
                if block_dict.get("type") == "text":
                    text = block_dict.get("text", "")
                    if text:
                        text_parts.append(text)
            summary_text = "".join(text_parts)
            if not summary_text:
                logger.warning(
                    "max_loops 总结调用返回空文本，降级返回 last_text"
                )
                return last_text
            return summary_text
        except Exception as e:
            logger.error("max_loops 总结调用失败，降级返回 last_text: %s", e)
            return last_text

    @staticmethod
    def _block_to_dict(block: Any) -> Dict[str, Any]:
        """将 content block 转为 dict（委托到 stream_handler.block_to_dict）。"""
        return _block_to_dict_fn(block)

    def _build_done_event(
        self,
        *,
        response: str,
        messages: List[Dict[str, Any]],
        is_complete: bool,
        termination_reason: str,
        usage: Optional[Dict[str, Any]] = None,
        content_blocks: Optional[List[Dict[str, Any]]] = None,
        stop_reason: Optional[str] = None,
        reasoning_cfg: Optional["ReasoningConfig"] = None,
        current_round_text: str = "",
        current_round_reasoning: str = "",
    ) -> Dict[str, Any]:
        """统一构造 done 事件（委托到 stream_handler.build_done_event）。"""
        return _build_done_event_fn(
            response=response,
            messages=messages,
            is_complete=is_complete,
            termination_reason=termination_reason,
            usage=usage,
            content_blocks=content_blocks,
            stop_reason=stop_reason,
            reasoning_cfg=reasoning_cfg,
            current_round_text=current_round_text,
            current_round_reasoning=current_round_reasoning,
        )

    @staticmethod
    def _build_reasoning_stats(
        reasoning_cfg: Optional["ReasoningConfig"],
        usage: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """构造 reasoning_stats 字段（委托到 stream_handler.build_reasoning_stats）。"""
        return _build_reasoning_stats_fn(reasoning_cfg, usage)
