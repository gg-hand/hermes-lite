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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入以避免循环依赖
    from ..llm.client import LLMClient

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
        # 信息计数器：user / assistant / tool 各算 1 条，用于触发 consolidation
        self._info_count: int = 0

    def get_info_count(self) -> int:
        """返回当前信息计数器值。"""
        return self._info_count

    def reset_info_count(self) -> None:
        """重置信息计数器为 0。"""
        self._info_count = 0

    # ------------------------------------------------------------------
    # Phase 9 Task 7.3: 单工具重试检测辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_params_hash(tool_input: dict) -> str:
        """计算工具输入参数的 hash（用于重试检测去重）。

        使用 ``md5(json.dumps(tool_input, sort_keys=True))`` 保证同一 dict
        任意顺序的 key 都得到相同 hash。``tool_input`` 为 ``None`` 或非
        dict 时降级为空 dict 处理。

        参数:
            tool_input: 工具输入参数 dict。

        返回:
            32 字符的十六进制 md5 摘要字符串。
        """
        try:
            payload = json.dumps(tool_input or {}, sort_keys=True)
        except (TypeError, ValueError):
            # 不可序列化对象降级为字符串表示
            payload = repr(tool_input or {})
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _detect_tool_stuck(
        tool_name: str,
        params_hash: str,
        recent_calls: List[Tuple[str, str, Optional[str]]],
        window_size: int = 5,
        threshold: int = 3,
    ) -> Tuple[bool, str]:
        """检测工具是否陷入重复调用卡死。

        在最近 ``window_size`` 次工具调用记录中，按语义分类判断卡死：
        1. 历史上同工具同参数已触发 ANTI_CRAWLER → 立即卡死（"anti_crawler"）
        2. 历史上同工具同参数已触发 PERMANENT → 立即卡死（"permanent"）
        3. 否则按同参数出现次数 ≥ ``threshold`` 判定卡死（默认 ""）

        参数:
            tool_name: 当前待执行的工具名。
            params_hash: 当前调用的参数 hash（``_compute_params_hash`` 输出）。
            recent_calls: 最近工具调用记录列表，每项为
                ``(tool_name, params_hash, error_class)`` 三元组，
                ``error_class`` 可为 ``None``（执行前记录）、``ErrorClass.value`` 字符串。
            window_size: 滑动窗口大小，默认 5。
            threshold: 触发卡死的重复次数阈值，默认 3。

        返回:
            ``(is_stuck, reason)`` 二元组：
            ``is_stuck`` 为 ``True`` 表示卡死需终止；
            ``reason`` 为 ``"anti_crawler"`` / ``"permanent"`` / ``""``（无特殊原因）。
        """
        if not recent_calls or threshold <= 0:
            return False, ""

        # 仅检查最近 window_size 条记录
        window = recent_calls[-window_size:]

        # Phase 9+：扫描历史记录中同参数的语义分类
        for name, phash, ec_str in window:
            if name == tool_name and phash == params_hash:
                if ec_str in ("anti_crawler",):
                    return True, "anti_crawler"
                if ec_str in ("permanent",):
                    return True, "permanent"

        # 统计与当前 (tool_name, params_hash) 完全匹配的历史记录数
        matches = sum(
            1
            for name, phash, _ic in window
            if name == tool_name and phash == params_hash
        )
        return matches >= (threshold - 1), ""

    @staticmethod
    def _build_stuck_message(tool_name: str, reason: str = "") -> str:
        """构造卡死终止消息（run() 返回值用）。

        参数:
            tool_name: 卡死的工具名。
            reason: 卡死原因，支持 ``"anti_crawler"`` / ``"permanent"`` / ``""``。
        """
        if reason == "anti_crawler":
            return (
                f"工具 {tool_name} 触发了目标网站的反爬虫机制，已终止重试。"
                f"建议：更换获取方式、添加请求头、或询问用户。"
            )
        if reason == "permanent":
            return f"工具 {tool_name} 返回了永久性错误，无需重试，已终止。"
        return f"检测到工具 {tool_name} 重复调用卡死，已终止"

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
        """评估工具调用的策略决策。

        若 policy_engine 为 None（向后兼容），返回 allow 决策跳过拦截。
        policy_engine.check 抛异常时降级为 allow，避免策略层故障阻断主流程。

        参数:
            tool_name: 待评估的工具名。
            tool_input: 工具输入参数 dict。
            session_id: v2 可选会话 ID，透传给 PolicyEngine 做参数感知决策
                （write_file / delete_file 按文件状态决策）。

        返回:
            Decision 实例，action 为 allow / confirm / deny 三选一。
        """
        if self.policy_engine is None:
            return Decision("allow", "", "low")
        try:
            return self.policy_engine.check(
                tool_name, tool_input, session_id=session_id
            )
        except Exception as e:
            logger.warning("策略评估异常，降级为 allow: %s", e)
            return Decision("allow", "", "low")

    @staticmethod
    def _extract_schedule_id(session_id: Optional[str]) -> Optional[str]:
        """从 session_id 提取 cron 调度项 ID（Phase 8 Task 4.4）。

        ``session_id`` 以 ``cron:`` 开头时返回前缀之后的部分（调度项 ID）；
        其他值或 ``None`` 返回 ``None``。用于审计日志的 ``schedule_id``
        冗余字段，加速按调度项查询。

        参数:
            session_id: 会话 ID。

        返回:
            调度项 ID（cron 会话）或 ``None``（用户会话）。
        """
        if session_id and session_id.startswith("cron:"):
            return session_id[5:]
        return None

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
        """记录工具调用审计日志（Phase 8 Task 4.4 增强）。

        封装 ``audit_logger.log_tool_call``，自动从 ``session_id`` 提取
        ``schedule_id``、从 ``decision`` 读取 ``decision_source``，简化
        调用方代码。``audit_logger`` 为 ``None`` 时静默跳过。

        参数:
            session_id: 会话 ID。
            tool_name: 工具名称。
            tool_input: 工具输入参数 dict。
            result: 工具执行结果字符串。
            is_error: 是否出错。
            duration_ms: 调用耗时（毫秒）。
            decision: 策略评估结果（含 ``decision_source``），为 ``None``
                时 ``decision_source`` 使用默认值 ``"default_rule"``。
        """
        if self.audit_logger is None:
            return
        decision_source = (
            decision.decision_source if decision is not None else "default_rule"
        )
        schedule_id = self._extract_schedule_id(session_id)
        self.audit_logger.log_tool_call(
            session_id=session_id or "",
            tool_name=tool_name,
            tool_input=tool_input,
            result=result,
            is_error=is_error,
            duration_ms=duration_ms,
            decision_source=decision_source,
            schedule_id=schedule_id,
        )

    def _execute_tool_with_dispatch(
        self, tool_name: str, tool_input: dict,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """执行工具调用，按 cron_tool_registry → tool_registry 顺序派发。

        Phase 8 Task 5.7。``tools_override`` 仅覆盖 LLM 可见 schema，工具
        执行仍需派发：cron_tool 名称不在全局 ToolRegistry 中，需路由到
        :class:`CronToolRegistry`（子进程执行）；其他工具走原
        ``tool_registry.execute_tool`` 路径。

        Phase 9+ 中断上下文：通过 ``current_cancel_event`` ContextVar 将
        ``cancel_event`` 传播到同步工具 handler 内部（如 http_request）。

        派发顺序：
        1. ``cron_tool_registry`` 非 None 且 ``has_tool(tool_name)`` → 走
           ``cron_tool_registry.execute_tool``（子进程执行，返回结果字符串）。
        2. 否则走 ``tool_registry.execute_tool``（全局 registry，含内置工具）。
        3. ``tool_registry`` 为 None 时返回错误字符串（理论上不会发生，
           因为 cron 路径下 tool_registry 必非 None）。

        两个 registry 的 ``execute_tool`` 均保证不抛异常（返回错误字符串），
        因此本方法也不抛异常。

        参数:
            tool_name: 工具名称。
            tool_input: 工具输入参数 dict。
            cancel_event: 可选的取消事件，通过 ContextVar 传播到工具 handler。

        返回:
            执行结果字符串。
        """
        # Phase 9+：设置 ContextVar，传播 cancel_event 到同步工具 handler
        token = None
        try:
            from ._cancel_context import current_cancel_event
            token = current_cancel_event.set(cancel_event)
        except ImportError:
            pass

        try:
            # 1. cron_tool_registry 派发（仅 cron 会话路径注入了 cron_tool_registry）
            cron_reg = getattr(self, "cron_tool_registry", None)
            if cron_reg is not None:
                try:
                    if cron_reg.has_tool(tool_name):
                        return cron_reg.execute_tool(tool_name, tool_input)
                except Exception as exc:
                    logger.error(
                        "cron_tool %s 派发执行失败，回退到 tool_registry: %s",
                        tool_name,
                        exc,
                    )
            # 2. 全局 tool_registry 派发
            if self.tool_registry is None:
                return f"未注册的工具: {tool_name}（tool_registry 未注入）"
            return self.tool_registry.execute_tool(tool_name, tool_input)
        finally:
            if token is not None:
                try:
                    from ._cancel_context import current_cancel_event
                    current_cancel_event.reset(token)
                except ImportError:
                    pass

    def run(
        self,
        user_input: str,
        history: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        session_id: Optional[str] = None,
        tools_override: Optional[List[Dict[str, Any]]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[str, List[Dict[str, Any]], bool]:
        """执行 React 循环。

        参数:
            user_input: 当前用户输入文本。
            history: 历史消息列表 [{role, content}]，可为 None。
                     history 中的消息不参与信息计数器累加。
            system: 系统提示词，若为 None 则不发送 system 字段。
            session_id: 可选会话 ID，由 orchestrator 透传，
                     非流式模式仅作占位（不 yield 事件），保留以便日志关联。
            tools_override: Phase 8 Task 5.7。工具 schema 覆盖列表。为 ``None``
                     时使用 ``self.tool_registry.get_tools_schema()``（默认
                     用户会话路径）。非 ``None`` 时直接使用此列表，用于 cron
                     调度会话按 ``active_tools_snapshot`` 过滤后的请求级隔离。
            cancel_event: Phase 9+ 可选取消事件（threading.Event），由
                     Orchestrator/StreamManager 透传。被 set 时在循环的三个
                     检测点终止执行，返回 is_complete=False。

        返回:
            (final_response, messages_used, is_complete):
                final_response: 最终回复文本（若全程无文本则返回空串）。
                messages_used: 整个循环中使用过的完整消息列表（含历史与新增）。
                is_complete: 本轮是否自然完成。Phase 9 Task 7.4：
                    - ``True``：模型返回 end_turn 自然结束；
                    - ``False``：达到 max_loops、卡死终止或被取消，由 orchestrator
                      检查 TodoList 决定是否自动续接。
        """
        # 1. 构建 messages = history + [user_input]
        messages: List[Dict[str, Any]] = []
        if history:
            # 浅拷贝避免外部修改影响传入历史
            messages.extend(dict(m) for m in history)
        messages.append({"role": "user", "content": user_input})

        # 用户输入计入信息计数器
        self._info_count += 1

        # 2. 获取工具 schema（tool_registry 为 None 时纯对话模式）
        # Phase 8 Task 5.7: tools_override 优先（cron 路径请求级过滤）
        tools: Optional[List[Dict[str, Any]]] = None
        if tools_override is not None:
            # 浅拷贝避免外部修改污染调用方持有的列表
            tools = list(tools_override)
        elif self.tool_registry is not None:
            try:
                tools = self.tool_registry.get_tools_schema()
            except Exception as e:
                logger.warning("获取工具 schema 失败，降级为纯对话模式: %s", e)
                tools = None

        last_text: str = ""

        # Phase 9 Task 7.3 + Phase 9+ 错误分类: 单工具重试检测滑动窗口
        # 维护最近 5 次工具调用记录 (tool_name, params_hash, error_class)，
        # error_class 为 ErrorClass.value（"anti_crawler"/"permanent"等）或 None。
        # 窗口在本次 run() 内维护，不跨 run 调用持久化。
        recent_tool_calls: List[Tuple[str, str, Optional[str]]] = []

        for loop_idx in range(self.max_loops):
            # 检测点 1：每轮开始前检查 cancel_event
            if cancel_event and cancel_event.is_set():
                return last_text, messages, False

            # 3. 调用主对话 LLM
            try:
                response = self.llm_client.chat_main(
                    messages=messages,
                    tools=tools,
                    system=system,
                )
            except Exception as e:
                logger.error("LLM 调用失败 (loop=%d): %s", loop_idx, e)
                # 若已有文本回复，降级返回；否则向上抛出
                if last_text:
                    return last_text, messages, False
                raise

            # 解析响应
            content_blocks = getattr(response, "content", []) or []
            stop_reason = getattr(response, "stop_reason", None)

            # 提取文本与工具调用块，并将原始 block 转为 dict 便于回传
            text_parts: List[str] = []
            tool_use_blocks: List[Dict[str, Any]] = []
            assistant_content: List[Dict[str, Any]] = []

            for block in content_blocks:
                block_dict = self._block_to_dict(block)
                assistant_content.append(block_dict)
                btype = block_dict.get("type")
                if btype == "text":
                    text = block_dict.get("text", "")
                    if text:
                        text_parts.append(text)
                elif btype == "tool_use":
                    tool_use_blocks.append(block_dict)

            # assistant 响应计入信息计数器
            self._info_count += 1

            # 将 assistant 完整响应（含 text 与 tool_use 块）加入 messages
            messages.append({"role": "assistant", "content": assistant_content})

            if text_parts:
                last_text = "".join(text_parts)

            # 4. 判断是否需要工具调用
            # stop_reason != "tool_use" 或无 tool_use 块时，视为最终回复
            if stop_reason != "tool_use" or not tool_use_blocks:
                # 自然结束（end_turn）→ is_complete=True
                return last_text, messages, True

            # 响应包含 tool_use，但未提供 tool_registry：终止循环
            if self.tool_registry is None:
                logger.warning(
                    "模型请求工具调用但未提供 tool_registry，返回当前文本回复"
                )
                # 清理末尾未执行的 assistant(tool_calls)，避免下轮 400
                messages = self._drop_trailing_orphan_tool_calls(messages)
                return last_text, messages, False

            # 5. 执行工具调用，将 tool_result 作为 user 消息回传
            tool_results: List[Dict[str, Any]] = []
            for tb in tool_use_blocks:
                tool_name = tb.get("name", "")
                tool_input = tb.get("input", {}) or {}
                tool_use_id = tb.get("id", "")
                is_error = False
                t0 = time.perf_counter()

                # Phase 9 Task 7.3 + Phase 9+ 错误分类: 工具执行前检测卡死
                # （仅对实际会执行的 allow 分支检测，deny/confirm 拒绝路径不进入滑动窗口）。
                params_hash = self._compute_params_hash(tool_input)
                is_stuck, stuck_reason = self._detect_tool_stuck(
                    tool_name, params_hash, recent_tool_calls
                )
                if is_stuck:
                    logger.warning(
                        "检测到工具 %s 重复调用卡死 (loop=%d, reason=%s)，终止本轮循环",
                        tool_name,
                        loop_idx,
                        stuck_reason or "count",
                    )
                    # 清理末尾未执行的 assistant(tool_calls)，避免下轮 400
                    messages = self._drop_trailing_orphan_tool_calls(messages)
                    return (
                        self._build_stuck_message(tool_name, stuck_reason),
                        messages,
                        False,
                    )

                # Phase 5: 策略评估
                decision = self._evaluate_policy(
                    tool_name, tool_input, session_id=session_id
                )

                if decision.action == "deny":
                    # 拒绝：不调用 execute_tool，返回明确拒绝提示
                    reason_text = decision.reason or "用户未提供原因"
                    result = (
                        f"[用户已拒绝] 工具 {tool_name} 的执行请求"
                        f"（原因：{reason_text}）。"
                        f"请停止重试该工具，改为询问用户意图或换一种方式完成任务。"
                    )
                    duration_ms = (time.perf_counter() - t0) * 1000
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": result,
                        }
                    )
                    # 审计与指标上报（拒绝也算一次工具调用记录）
                    self._log_audit(
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        result=result,
                        is_error=False,
                        duration_ms=duration_ms,
                        decision=decision,
                    )
                    if self.metrics is not None:
                        self.metrics.observe_tool_call(tool_name, True, duration_ms)
                    self._info_count += 1
                    # 拒绝路径不计入重试检测窗口
                    continue
                elif decision.action == "confirm":
                    # 非流式模式不支持 HIL，自动拒绝
                    logger.warning(
                        "非流式 run() 命中 confirm 但不支持 HIL，自动拒绝: tool=%s",
                        tool_name,
                    )
                    reason_text = "非流式模式不支持 HIL 审批，请使用 /chat/stream"
                    result = (
                        f"[用户已拒绝] 工具 {tool_name} 的执行请求"
                        f"（原因：{reason_text}）。"
                        f"请停止重试该工具，改为询问用户意图或换一种方式完成任务。"
                    )
                    duration_ms = (time.perf_counter() - t0) * 1000
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": result,
                        }
                    )
                    self._log_audit(
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        result=result,
                        is_error=False,
                        duration_ms=duration_ms,
                        decision=decision,
                    )
                    if self.metrics is not None:
                        self.metrics.observe_tool_call(tool_name, True, duration_ms)
                    self._info_count += 1
                    # 拒绝路径不计入重试检测窗口
                    continue

                # action == "allow"：正常执行
                # 检测点 2：工具执行前检查 cancel_event
                if cancel_event and cancel_event.is_set():
                    messages = self._drop_trailing_orphan_tool_calls(messages)
                    return last_text, messages, False

                # Phase 8 Task 5.7: 通过 _execute_tool_with_dispatch 派发到
                # cron_tool_registry（子进程）或 tool_registry（全局）。
                try:
                    result = self._execute_tool_with_dispatch(tool_name, tool_input, cancel_event)
                except Exception as e:
                    is_error = True
                    logger.error("工具执行失败 %s: %s", tool_name, e)
                    result = f"工具执行出错: {e}"

                # Phase 9+ 错误分类：调用 ErrorClassifier 分类，记录到滑动窗口
                error_class: Optional[str] = None
                if ErrorClassifier is not None and not is_error:
                    try:
                        ec, _ec_reason = ErrorClassifier.classify(
                            tool_name, tool_input, result
                        )
                        if ec is not ErrorClass.SUCCESS and ec is not ErrorClass.UNKNOWN:
                            error_class = ec.value
                            # 添加策略提示
                            if ec is ErrorClass.ANTI_CRAWLER:
                                result += (
                                    "\n\n[系统提示：该请求触发了目标网站的反爬虫机制，"
                                    "请不要再对相同目标使用相同参数重试。可尝试："
                                    "添加 Cookie/Referer 请求头，或换用其他方式。]"
                                )
                            elif ec is ErrorClass.PERMANENT:
                                result += (
                                    "\n\n[系统提示：该请求返回了永久性错误"
                                    "（如资源不存在），重试无法解决。]"
                                )
                            elif ec is ErrorClass.TRANSIENT:
                                result += (
                                    "\n\n[系统提示：该请求遇到了临时性错误，"
                                    "可稍后重试或检查目标是否正常。]"
                                )
                    except Exception as exc:
                        logger.debug("ErrorClassifier 分类失败: %s", exc)
                # 执行后记录到滑动窗口（含 error_class）
                recent_tool_calls.append((tool_name, params_hash, error_class))

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result,
                        **({"is_error": True} if is_error else {}),
                    }
                )
                duration_ms = (time.perf_counter() - t0) * 1000
                self._log_audit(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    result=result,
                    is_error=is_error,
                    duration_ms=duration_ms,
                    decision=decision,
                )
                if self.metrics is not None:
                    self.metrics.observe_tool_call(tool_name, not is_error, duration_ms)
                # 每条 tool_result 计入信息计数器
                self._info_count += 1

            # 检测点 3：工具执行后、下一轮 LLM 调用前检查 cancel_event
            if cancel_event and cancel_event.is_set():
                return last_text, messages, False

            messages.append({"role": "user", "content": tool_results})
            # 继续下一轮循环

        # 达到 max_loops 仍未完成，触发总结调用
        logger.warning(
            "React 循环达到最大次数 %d，触发总结调用", self.max_loops
        )
        summary_text = self._generate_max_loops_summary(
            messages, last_text, session_id
        )
        # max_loops 耗尽 → is_complete=False（orchestrator 检查 TodoList 决定续接）
        return summary_text, messages, False

    async def run_stream(
        self,
        user_input: str,
        history: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        session_id: Optional[str] = None,
        tools_override: Optional[List[Dict[str, Any]]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        """流式执行 React 循环，异步生成器逐个 yield 事件 dict。

        与 :meth:`run` 等价的循环逻辑，区别在于 LLM 调用使用流式接口，
        文本增量以事件形式实时 yield 给调用方（前端可逐字显示）。

        事件格式：
            - ``{"type": "round_start", "loop_idx": int}``：
              每轮循环开始事件，前端应据此创建独立的 streamMsg
              以隔离多轮文本与工具卡片，避免跨轮累加与 done 覆盖丢失。
              第 1 轮 loop_idx=0，第 2 轮 loop_idx=1，以此类推。
            - ``{"type": "text", "text": "<增量文本>"}``：
              LLM 输出的文本增量，调用方应在当前轮的 streamMsg 中累加显示。
            - ``{"type": "tool", "name": str, "input": dict, "result": str,
              "is_error": bool, "session_id": str|None}``：
              工具调用事件，单次工具执行完成后发出（不流式）。
              ``result`` 为工具执行结果字符串；``is_error`` 为 True 时表示执行异常；
              ``session_id`` 为当前会话 ID（可能为 None），供前端关联 todo 卡片。
            - ``{"type": "approval_request", "approval_id": str, "tool_name": str,
              "tool_input": dict, "reason": str, "risk_level": str}``：
              Phase 5 HIL 审批请求事件，策略命中 confirm 时发出，前端应弹出
              审批确认框并调用 ApprovalManager.resolve 提交决定。
            - ``{"type": "approval_resolved", "approval_id": str, "decision": str,
              "reason": str}``：审批决定已提交事件，``decision`` 为 approve/deny。
            - ``{"type": "done", "response": str, "messages": list,
              "is_complete": bool}``：
              整个 React 循环结束事件，``response`` 为最后一轮的回复文本
              （前端 T8 不再用其覆盖已显示的各轮文本，仅用于标记流结束），
              ``messages`` 为完整消息列表（含历史与新增）。
              Phase 9 Task 7.5：``is_complete`` 标识本轮是否自然完成
              （end_turn → True；max_loops 耗尽 / 卡死 → False），orchestrator
              据此决定是否发起自动续接。

        参数与 :meth:`run` 一致（含 Phase 8 Task 5.7 ``tools_override``）；
        ``session_id`` 用于在 tool 事件中透传会话 ID。
        """
        # 1. 构建 messages = history + [user_input]
        messages: List[Dict[str, Any]] = []
        if history:
            messages.extend(dict(m) for m in history)
        messages.append({"role": "user", "content": user_input})

        # 用户输入计入信息计数器
        self._info_count += 1

        # 2. 获取工具 schema（Phase 8 Task 5.7: tools_override 优先）
        tools: Optional[List[Dict[str, Any]]] = None
        if tools_override is not None:
            tools = list(tools_override)
        elif self.tool_registry is not None:
            try:
                tools = self.tool_registry.get_tools_schema()
            except Exception as e:
                logger.warning("获取工具 schema 失败，降级为纯对话模式: %s", e)
                tools = None

        last_text: str = ""

        # Phase 9 Task 7.3: 单工具重试检测滑动窗口（与 run() 等价语义）
        recent_tool_calls: List[Tuple[str, str, Optional[str]]] = []

        for loop_idx in range(self.max_loops):
            # 🔴 检测点 1：每轮循环开始前检测中断
            if cancel_event and cancel_event.is_set():
                yield {
                    "type": "done",
                    "response": last_text,
                    "messages": messages,
                    "is_complete": False,
                }
                return

            # 每轮循环开始：发出 round_start 事件，前端据此创建独立的
            # streamMsg 以隔离该轮的文本与工具卡片，避免跨轮累加与
            # done 覆盖导致的中间轮 assistant 文本丢失。
            yield {"type": "round_start", "loop_idx": loop_idx}

            # 发出 thinking 状态，告知前端 LLM 正在处理
            yield {"type": "status", "status": "thinking", "message": "正在思考..."}

            # 3. 流式调用主对话 LLM
            content_blocks: List[Dict[str, Any]] = []
            stop_reason: str = "end_turn"
            current_round_text: str = ""  # ← 追踪本轮累积文本
            try:
                for event in self.llm_client.chat_main_stream(
                    messages=messages,
                    tools=tools,
                    system=system,
                    cancel_event=cancel_event,  # ← 透传 cancel_event
                ):
                    # 🔴 检测点 2：每 token 检测中断（立即停止 LLM 流）
                    if cancel_event and cancel_event.is_set():
                        # 将 partial 内容纳入 messages，确保持久化不丢失
                        if current_round_text:
                            messages.append(
                                {"role": "assistant", "content": current_round_text}
                            )
                        yield {
                            "type": "done",
                            "response": current_round_text or last_text,
                            "messages": messages,
                            "is_complete": False,
                        }
                        return

                    etype = event.get("type")
                    if etype == "text":
                        current_round_text += event.get("text", "")  # ← 累积
                        # 透传文本增量
                        yield event
                    elif etype == "done":
                        stop_reason = event.get("stop_reason", "end_turn") or "end_turn"
                        content_blocks = event.get("content_blocks", []) or []
            except StreamCancelled:
                # 后端 SDK 检测到中断后抛出的异常
                if current_round_text:
                    messages.append(
                        {"role": "assistant", "content": current_round_text}
                    )
                yield {
                    "type": "done",
                    "response": current_round_text or last_text,
                    "messages": messages,
                    "is_complete": False,
                }
                return
            except Exception as e:
                logger.error("LLM 流式调用失败 (loop=%d): %s", loop_idx, e)
                if last_text:
                    yield {
                        "type": "done",
                        "response": last_text,
                        "messages": messages,
                        "is_complete": False,
                    }
                    return
                raise

            # 解析 content_blocks
            text_parts: List[str] = []
            tool_use_blocks: List[Dict[str, Any]] = []
            for block in content_blocks:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        text_parts.append(text)
                elif btype == "tool_use":
                    tool_use_blocks.append(block)

            # assistant 响应计入信息计数器
            self._info_count += 1

            # 将 assistant 完整响应加入 messages
            messages.append({"role": "assistant", "content": content_blocks})

            if text_parts:
                last_text = "".join(text_parts)

            # 4. 判断是否需要工具调用
            if stop_reason != "tool_use" or not tool_use_blocks:
                # 自然结束（end_turn）→ is_complete=True
                yield {
                    "type": "done",
                    "response": last_text,
                    "messages": messages,
                    "is_complete": True,
                }
                return

            # 响应包含 tool_use，但未提供 tool_registry：终止循环
            if self.tool_registry is None:
                logger.warning(
                    "模型请求工具调用但未提供 tool_registry，返回当前文本回复"
                )
                # 清理末尾未执行的 assistant(tool_calls)，避免下轮 400
                messages = self._drop_trailing_orphan_tool_calls(messages)
                yield {
                    "type": "done",
                    "response": last_text,
                    "messages": messages,
                    "is_complete": False,
                }
                return

            # 🔴 检测点 3：工具执行前检测中断
            # 注：已开始的工具会执行完毕（原子性），不半途取消
            if cancel_event and cancel_event.is_set():
                messages = self._drop_trailing_orphan_tool_calls(messages)
                yield {
                    "type": "done",
                    "response": last_text,
                    "messages": messages,
                    "is_complete": False,
                }
                return

            # 5. 执行工具调用，发出 tool 事件，并将 tool_result 回传
            tool_results: List[Dict[str, Any]] = []
            for tb in tool_use_blocks:
                tool_name = tb.get("name", "")
                tool_input = tb.get("input", {}) or {}
                tool_use_id = tb.get("id", "")
                is_error = False
                t0 = time.perf_counter()

                # Phase 9 Task 7.3 + Phase 9+ 错误分类: 工具执行前检测卡死
                params_hash = self._compute_params_hash(tool_input)
                is_stuck, stuck_reason = self._detect_tool_stuck(
                    tool_name, params_hash, recent_tool_calls
                )
                if is_stuck:
                    logger.warning(
                        "检测到工具 %s 重复调用卡死 (loop=%d, reason=%s)，终止本轮循环",
                        tool_name,
                        loop_idx,
                        stuck_reason or "count",
                    )
                    # 清理末尾未执行的 assistant(tool_calls)，避免下轮 400
                    messages = self._drop_trailing_orphan_tool_calls(messages)
                    yield {
                        "type": "done",
                        "response": self._build_stuck_message(tool_name, stuck_reason),
                        "messages": messages,
                        "is_complete": False,
                    }
                    return

                # Phase 5: 策略评估
                decision = self._evaluate_policy(
                    tool_name, tool_input, session_id=session_id
                )

                if decision.action == "deny":
                    reason_text = decision.reason or "用户未提供原因"
                    result = (
                        f"[用户已拒绝] 工具 {tool_name} 的执行请求"
                        f"（原因：{reason_text}）。"
                        f"请停止重试该工具，改为询问用户意图或换一种方式完成任务。"
                    )
                    duration_ms = (time.perf_counter() - t0) * 1000
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": result,
                        }
                    )
                    self._log_audit(
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        result=result,
                        is_error=False,
                        duration_ms=duration_ms,
                        decision=decision,
                    )
                    if self.metrics is not None:
                        self.metrics.observe_tool_call(tool_name, True, duration_ms)
                    yield {
                        "type": "tool",
                        "name": tool_name,
                        "input": tool_input,
                        "result": result,
                        "is_error": False,
                        "session_id": session_id,
                        "tool_use_id": tool_use_id,
                    }
                    self._info_count += 1
                    # 拒绝路径不计入重试检测窗口
                    continue
                elif decision.action == "confirm":
                    # 流式模式：抛出审批请求，等待用户决定
                    if self.approval_manager is None:
                        logger.error(
                            "命中 confirm 但 approval_manager 未初始化，降级为 deny: tool=%s",
                            tool_name,
                        )
                        reason_text = "审批管理器未初始化"
                        result = (
                            f"[用户已拒绝] 工具 {tool_name} 的执行请求"
                            f"（原因：{reason_text}）。"
                            f"请停止重试该工具，改为询问用户意图或换一种方式完成任务。"
                        )
                        duration_ms = (time.perf_counter() - t0) * 1000
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": result,
                            }
                        )
                        self._log_audit(
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            result=result,
                            is_error=False,
                            duration_ms=duration_ms,
                            decision=decision,
                        )
                        if self.metrics is not None:
                            self.metrics.observe_tool_call(tool_name, True, duration_ms)
                        yield {
                            "type": "tool",
                            "name": tool_name,
                            "input": tool_input,
                            "result": result,
                            "is_error": False,
                            "session_id": session_id,
                            "tool_use_id": tool_use_id,
                        }
                        self._info_count += 1
                        continue

                    # 创建审批请求
                    approval_id = self.approval_manager.create_request(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        reason=decision.reason,
                        risk_level=decision.risk_level,
                    )
                    # 抛出 approval_request 事件给前端
                    yield {
                        "type": "approval_request",
                        "approval_id": approval_id,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "reason": decision.reason,
                        "risk_level": decision.risk_level,
                    }
                    # 等待用户决定（async generator 可直接 await）
                    decision_str, wait_reason = (
                        await self.approval_manager.wait_for_decision(approval_id)
                    )
                    # 抛出 approval_resolved 事件
                    yield {
                        "type": "approval_resolved",
                        "approval_id": approval_id,
                        "decision": decision_str,
                        "reason": wait_reason,
                    }

                    if decision_str == "approve":
                        # 用户批准，先发 tool_start 事件
                        yield {
                            "type": "tool_start",
                            "tool_use_id": tool_use_id,
                            "name": tool_name,
                            "input": tool_input,
                        }
                        # 记录到滑动窗口（与 allow 分支一致，补 None 作为 error_class）
                        recent_tool_calls.append((tool_name, params_hash, None))
                        try:
                            result = self.tool_registry.execute_tool(
                                tool_name, tool_input
                            )
                        except Exception as e:
                            logger.error("工具执行失败 %s: %s", tool_name, e)
                            result = f"工具执行出错: {e}"
                            is_error = True
                        duration_ms = (time.perf_counter() - t0) * 1000
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": result,
                                **({"is_error": True} if is_error else {}),
                            }
                        )
                        self._log_audit(
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            result=result,
                            is_error=is_error,
                            duration_ms=duration_ms,
                            decision=decision,
                        )
                        if self.metrics is not None:
                            self.metrics.observe_tool_call(
                                tool_name, not is_error, duration_ms
                            )
                        yield {
                            "type": "tool",
                            "name": tool_name,
                            "input": tool_input,
                            "result": result,
                            "is_error": is_error,
                            "session_id": session_id,
                            "tool_use_id": tool_use_id,
                        }
                        self._info_count += 1
                    else:
                        # 用户拒绝或超时
                        reason_text = (
                            wait_reason or decision.reason or "用户未提供原因"
                        )
                        result = (
                            f"[用户已拒绝] 工具 {tool_name} 的执行请求"
                            f"（原因：{reason_text}）。"
                            f"请停止重试该工具，改为询问用户意图或换一种方式完成任务。"
                        )
                        duration_ms = (time.perf_counter() - t0) * 1000
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": result,
                            }
                        )
                        self._log_audit(
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            result=result,
                            is_error=False,
                            duration_ms=duration_ms,
                            decision=decision,
                        )
                        if self.metrics is not None:
                            self.metrics.observe_tool_call(
                                tool_name, True, duration_ms
                            )
                        yield {
                            "type": "tool",
                            "name": tool_name,
                            "input": tool_input,
                            "result": result,
                            "is_error": False,
                            "session_id": session_id,
                            "tool_use_id": tool_use_id,
                        }
                        self._info_count += 1
                    continue

                # action == "allow"：正常执行
                # 先发 tool_start 事件，前端立即显示"⏳ 运行中"
                yield {
                    "type": "tool_start",
                    "tool_use_id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input,
                }
                # Phase 8 Task 5.7: 通过 _execute_tool_with_dispatch 派发到
                # cron_tool_registry（子进程）或 tool_registry（全局）。
                try:
                    result = self._execute_tool_with_dispatch(tool_name, tool_input, cancel_event)
                except Exception as e:
                    logger.error("工具执行失败 %s: %s", tool_name, e)
                    result = f"工具执行出错: {e}"
                    is_error = True

                # Phase 9+ 错误分类：调用 ErrorClassifier 分类，记录到滑动窗口
                error_class: Optional[str] = None
                if ErrorClassifier is not None and not is_error:
                    try:
                        ec, _ec_reason = ErrorClassifier.classify(
                            tool_name, tool_input, result
                        )
                        if ec is not ErrorClass.SUCCESS and ec is not ErrorClass.UNKNOWN:
                            error_class = ec.value
                            if ec is ErrorClass.ANTI_CRAWLER:
                                result += (
                                    "\n\n[系统提示：该请求触发了目标网站的反爬虫机制，"
                                    "请不要再对相同目标使用相同参数重试。可尝试："
                                    "添加 Cookie/Referer 请求头，或换用其他方式。]"
                                )
                            elif ec is ErrorClass.PERMANENT:
                                result += (
                                    "\n\n[系统提示：该请求返回了永久性错误"
                                    "（如资源不存在），重试无法解决。]"
                                )
                            elif ec is ErrorClass.TRANSIENT:
                                result += (
                                    "\n\n[系统提示：该请求遇到了临时性错误，"
                                    "可稍后重试或检查目标是否正常。]"
                                )
                    except Exception as exc:
                        logger.debug("ErrorClassifier 分类失败: %s", exc)
                # 执行后记录到滑动窗口（含 error_class）
                recent_tool_calls.append((tool_name, params_hash, error_class))

                duration_ms = (time.perf_counter() - t0) * 1000
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result,
                        **({"is_error": True} if is_error else {}),
                    }
                )
                # 审计与指标上报（非 None 时）
                self._log_audit(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    result=result,
                    is_error=is_error,
                    duration_ms=duration_ms,
                    decision=decision,
                )
                if self.metrics is not None:
                    self.metrics.observe_tool_call(tool_name, not is_error, duration_ms)
                # 发出工具调用事件（前端可显示工具执行结果）
                yield {
                    "type": "tool",
                    "name": tool_name,
                    "input": tool_input,
                    "result": result,
                    "is_error": is_error,
                    "session_id": session_id,
                    "tool_use_id": tool_use_id,
                }
                # 每条 tool_result 计入信息计数器
                self._info_count += 1

            # 检测点 4：工具执行后检查 cancel_event（run_stream 专用）
            if cancel_event and cancel_event.is_set():
                yield {
                    "type": "done",
                    "response": last_text,
                    "messages": messages,
                    "is_complete": False,
                }
                return

            messages.append({"role": "user", "content": tool_results})
            # 继续下一轮循环（LLM 会基于工具结果再次流式输出）

        # 达到 max_loops 仍未完成，触发总结调用
        logger.warning(
            "React 循环达到最大次数 %d，触发总结调用", self.max_loops
        )
        summary_text = self._generate_max_loops_summary(
            messages, last_text, session_id
        )
        # max_loops 耗尽 → is_complete=False
        yield {
            "type": "done",
            "response": summary_text,
            "messages": messages,
            "is_complete": False,
        }

    def _generate_max_loops_summary(
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
            response = self.llm_client.chat_main(
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
        """将 anthropic 响应的 content block 转为可序列化的 dict。

        anthropic SDK 返回的 content block 是类型对象（如 TextBlock、
        ToolUseBlock），回传给 messages.create 时虽支持对象，但统一转为
        dict 便于日志、序列化与一致处理。

        参数:
            block: anthropic content block 对象或 dict。

        返回:
            转换后的 dict。
        """
        if isinstance(block, dict):
            return block

        block_type = getattr(block, "type", None)
        if block_type == "text":
            return {
                "type": "text",
                "text": getattr(block, "text", ""),
            }
        if block_type == "tool_use":
            return {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}) or {},
            }
        # 未知 block 类型，尽量保留可访问字段
        return {"type": block_type or "unknown"}
