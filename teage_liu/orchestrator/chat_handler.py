"""非流式对话处理器。

从 Orchestrator.chat 提取，负责非流式对话流程：
历史获取 → 上下文构建 → React 循环 → 消息持久化 → 记忆沉淀触发。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..llm.reasoning_profiles import ReasoningConfig

logger = logging.getLogger(__name__)

# 延迟导入：与 orchestrator/__init__.py 保持一致的 try/except 降级策略
from teage_liu.agent.context_builder import ContextBuilder
from teage_liu.agent.msg_persistence import MessagePersistence
from teage_liu.agent.intent_classifier import IntentClassificationResult, classify_intent


def _extract_token_count(usage: Any) -> int:
    """从 LLM usage dict 提取 token 总数（input + output）。

    批次 2.4: 用于回填 messages.token_count，之前所有调用方均未传入导致恒为 0。

    参数:
        usage: LLMResponse.usage 字段，通常为 ``{"input_tokens": N, "output_tokens": M}``。
            None / 非 dict / 缺少字段时返回 0（容错）。

    返回:
        input_tokens + output_tokens，缺字段用 0 兜底。
    """
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


class ChatHandler:
    """非流式对话处理器。

    持有 Orchestrator 引用，通过属性访问组件，避免重复构造。
    """

    def __init__(self, orchestrator: Any) -> None:
        self.orch = orchestrator

    async def chat(
        self,
        session_id: str,
        user_input: str,
        cancel_event: Optional[threading.Event] = None,
        reasoning_cfg: Optional["ReasoningConfig"] = None,
        is_cron: bool = False,
        extra_system_prompt: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
    ) -> str:
        """主对话入口（非流式）。

        流程:
            1. 获取 session 历史；
            2. 调用 enhanced_context_builder 构建上下文；
            3. 调用 react_loop.run 执行 React 循环；
            4. 记录用户输入与 assistant 回复到 session_logger；
            5. 更新 history_buffer；
            6. 触发 consolidation。

        extra_system_prompt: 可选的额外 system 上下文（如 multiagent 协作注入），
            会作为独立 system 消息追加到 enhanced_history 末尾。与
            system_prompt_override 互斥（override 优先）。

        system_prompt_override: 协作会话专用 system prompt 覆盖。
            当非 None 时（如 worker_adapter 注入协作专用 prompt），**完全绕开**
            enhanced_context_builder，跳过主 SYSTEM_PROMPT / 用户画像 / 检索记忆 /
            TodoList / 任务进度等用户会话专属上下文：
            - system_text 直接设为 override
            - enhanced_history 仅含会话历史 + 当前 user_input（不注入画像/记忆/Todo）
            - tools_override 固定 None
            适用场景：多 worker 协作会话（session_id 以 ``multiagent_`` 开头），
            避免双 worker 共享主 SYSTEM_PROMPT 的 "Teage Liu" 自称导致身份混乱。
            override 中应已含 agent_id 身份声明（见 prompts.build_collab_system_prompt）。
        """
        orch = self.orch
        # 记录当前 session_id，供 plan 工具通过 get_session_id 回调获取
        orch._current_session_id = session_id
        # 重置 intent_result，防止上一轮残留（Task 5 P1 修复）
        orch._current_intent_result = None

        # 0. 会话切换检测：若 session_id 变化且 pending 非空，先 flush 旧会话沉淀
        await orch.msg_persistence.maybe_flush_on_switch(session_id)

        # 1. 获取 session 历史
        history: List[Dict[str, Any]] = []
        if orch.history_buffer is not None:
            try:
                history = orch.history_buffer.get_history(session_id) or []
            except Exception as e:
                logger.warning("从 HistoryBuffer 获取历史失败: %s", e)
                history = []

        # 1.5 消费暂存的中断通知（规范 3 Task 8.3-8.6）
        pending_notice = orch._pending_interrupt_notices.pop(session_id, None)
        pending_notice_content: Optional[str] = None
        if pending_notice is not None:
            notice_age = time.time() - pending_notice.get("timestamp", 0)
            if notice_age > 300:  # 5 分钟 TTL
                logger.info(
                    "中断通知已过期（%.0f秒 > 300秒），丢弃: %s",
                    notice_age, session_id,
                )
            else:
                pending_notice_content = pending_notice.get("content")
                logger.info("准备注入 InterruptNotice 到 history: %s", session_id)

        # 1.6 安全网：清理历史中的连续 user 消息（兼容旧 JSONL 文件）
        history = MessagePersistence.sanitize_history(history)

        # 2. 执行 React 循环
        # 构建含用户画像 + 检索记忆的增强上下文
        # 协作会话路径（system_prompt_override 非 None）：完全绕开主 SYSTEM_PROMPT，
        # 不注入用户画像 / 检索记忆 / TodoList / 任务进度等用户会话专属上下文，
        # 直接用 override 作为 system_text，enhanced_history 仅含会话历史 + 当前输入。
        # 这避免双 worker 共享主 SYSTEM_PROMPT 的 "Teage Liu" 自称导致身份混乱。
        if system_prompt_override is not None:
            system_text = system_prompt_override
            # condenser 压缩历史（与用户路径保持一致，避免协作历史膨胀）
            condensed_history = await orch.enhanced_context_builder._apply_condenser(history)
            enhanced_history = list(condensed_history) + [
                {"role": "user", "content": user_input}
            ]
            tools_override = None
            logger.debug(
                "协作会话 %s 走 system_prompt_override 路径，绕开主 SYSTEM_PROMPT "
                "(system_text 长度=%d, enhanced_history 长度=%d)",
                session_id, len(system_text), len(enhanced_history),
            )
        else:
            system_text, enhanced_history, tools_override = await orch.enhanced_context_builder.build(
                session_id, user_input, history
            )
            # 主会话工具隔离：隐藏 worker 协作工具（send_remote_message 等）
            tools_override = self._maybe_filter_main_session_tools(
                session_id, tools_override
            )
        # 规范 3 Task 8.4: 追加独立 system 消息到 enhanced_history
        if pending_notice_content is not None:
            enhanced_history = list(enhanced_history) + [
                {"role": "system", "content": pending_notice_content}
            ]
        # multiagent 协作注入的额外 system 上下文（如 Director 干预、协作队列消息）
        # 注意：协作路径（system_prompt_override 非 None）已绕开 enhanced_context_builder，
        # 此处仍允许 extra_system_prompt 作为补充上下文追加（如 worker_adapter 的
        # _build_urgent_system_prompt 返回的协作上下文），但不与 override 冲突。
        if extra_system_prompt:
            enhanced_history = list(enhanced_history) + [
                {"role": "system", "content": extra_system_prompt}
            ]
        enhanced_history_len = len(enhanced_history)

        # Phase 9 Task 6 接入点 A: 输入扫描（fail-open 软护栏）
        if orch.guardrail_engine is not None:
            guardrail_result = orch.guardrail_engine.scan_input(user_input)
            if guardrail_result.action == "deny":
                if orch.audit_logger is not None:
                    orch.audit_logger.log_guardrail_decision(
                        layer="input_scan",
                        action="deny",
                        reason=guardrail_result.reason,
                        session_id=session_id,
                        matched_patterns=guardrail_result.matched_patterns,
                        risk_level="high",
                    )
                logger.warning(
                    "输入扫描 deny，拦截会话 %s: %s",
                    session_id,
                    guardrail_result.reason,
                )
                return "检测到潜在的安全风险，请重新表述您的请求。"
            if (
                guardrail_result.action == "suspicious"
                and orch.audit_logger is not None
            ):
                orch.audit_logger.log_guardrail_decision(
                    layer="input_scan",
                    action="allow",
                    reason=guardrail_result.reason,
                    session_id=session_id,
                    matched_patterns=guardrail_result.matched_patterns,
                    risk_level="medium",
                )

        # spec agent-metacognition-uplift Task 5: Intent Classifier 前置路由
        intent_result: Optional[IntentClassificationResult] = None
        if is_cron:
            if orch.metrics is not None:
                orch.metrics.observe_intent(is_cron_skip=True)
        elif classify_intent is not None and orch.llm_client is not None:
            _intent_start = time.monotonic()
            intent_result = await classify_intent(
                llm_client=orch.llm_client,
                user_input=user_input,
                history=history,
                cancel_event=cancel_event,
            )
            _intent_latency_ms = (time.monotonic() - _intent_start) * 1000
            logger.info(
                "会话 %s intent_classifier 结果: %s (confidence=%.2f)",
                session_id,
                intent_result.intent.value,
                intent_result.confidence,
            )
            if orch.metrics is not None:
                orch.metrics.observe_intent(
                    intent_type=intent_result.intent.value,
                    confidence=intent_result.confidence,
                    fallback_reason=intent_result.fallback_reason,
                    latency_ms=_intent_latency_ms,
                )
        orch._current_intent_result = intent_result

        # Phase 9 Task 7.6-7.7: 自动续接包装层 + 总熔断 200 轮
        MAX_TOTAL_ROUNDS = 200
        total_rounds = 0
        current_user_input = user_input
        current_history = enhanced_history
        response_text: str = ""
        messages_used: List[Dict[str, Any]] = []

        # 规范 2: 跨 run 空回复检测
        empty_count = orch._consecutive_empty_runs.get(session_id, 0)
        if empty_count >= 2:
            logger.warning(
                "会话 %s 连续 %d 次空回复，直接返回友好提示",
                session_id, empty_count,
            )
            return "抱歉，连续两次未能生成回复，可能是模型异常或上下文冲突。请重试或换种问法。"
        if empty_count == 1:
            system_text = (system_text or "") + (
                "\n\n[系统提示] 上一轮 LLM 返回了空回复。请确保本次明确回应用户问题，"
                "不要返回空内容。"
            )
            logger.info("会话 %s 注入空回复纠偏提示到 system_text", session_id)

        while total_rounds < MAX_TOTAL_ROUNDS:
            response_text, messages_used, is_complete, termination_reason = await orch.react_loop.run(
                user_input=current_user_input,
                history=current_history,
                system=system_text,
                session_id=session_id,
                tools_override=tools_override,
                cancel_event=cancel_event,
                reasoning_cfg=reasoning_cfg,
                is_cron=is_cron,
            )
            if orch.metrics is not None:
                orch.metrics.observe_termination(termination_reason)
            total_rounds += orch.react_loop.max_loops

            if is_complete:
                break

            # is_complete=False：检查 TodoList 是否有未完成步骤
            todo_dict = None
            if orch.todo_registry is not None:
                try:
                    todo_dict = orch.todo_registry.get_todo_dict(session_id)
                except Exception as e:
                    logger.warning("获取 TodoList 失败，跳过自动续接: %s", e)
                    todo_dict = None

            if not ContextBuilder.has_unfinished_steps(todo_dict):
                break

            # 构造续接消息，继续循环（不重置 messages）
            continuation_msg = orch.context_builder.build_continuation_message(todo_dict)
            current_user_input = continuation_msg
            current_history = messages_used
            logger.info(
                "React 循环未完成（total_rounds=%d/%d），TodoList 有未完成步骤，"
                "自动续接",
                total_rounds,
                MAX_TOTAL_ROUNDS,
            )

        if total_rounds >= MAX_TOTAL_ROUNDS:
            response_text = "已达总轮次上限 200，任务终止"
            logger.warning(
                "达到总轮次上限 %d，强制终止", MAX_TOTAL_ROUNDS
            )

        # 空回复计数
        is_empty_response = (
            termination_reason not in ("user_cancel", "tool_permanent_fail")
            and (not response_text or not response_text.strip())
        )
        if is_empty_response:
            orch._consecutive_empty_runs[session_id] = empty_count + 1
            logger.info(
                "会话 %s 空回复计数 %d -> %d",
                session_id, empty_count, empty_count + 1,
            )
        elif empty_count > 0:
            orch._consecutive_empty_runs[session_id] = 0
            logger.info("会话 %s 收到非空回复，重置空回复计数", session_id)

        # Phase 9 Task 6 接入点 B: 输出过滤（PII 脱敏）
        filtered_response: str = response_text
        if orch.guardrail_engine is not None:
            try:
                filtered_response, pii_count = (
                    orch.guardrail_engine.filter_output(response_text)
                )
                if pii_count > 0:
                    logger.info(
                        "输出过滤脱敏 %d 处 PII（会话 %s）",
                        pii_count,
                        session_id,
                    )
            except Exception as e:
                logger.warning("filter_output 异常，fail-open 使用原值: %s", e)
                filtered_response = response_text

        # 3. 记录消息到 session_logger
        if orch.session_logger is not None:
            try:
                orch.session_mgr.ensure_session(session_id)
                orch.session_logger.log_message(
                    session_id=session_id,
                    role="user",
                    content=user_input,
                )
                # 批次 2.4: 从 react_loop.last_usage 提取 token 总数回填
                # 之前所有调用方均未传入 token_count 导致恒为 0
                _assistant_token_count = _extract_token_count(
                    getattr(orch.react_loop, "last_usage", None)
                )
                orch.session_logger.log_message(
                    session_id=session_id,
                    role="assistant",
                    content=response_text,
                    token_count=_assistant_token_count,
                )
            except Exception as e:
                logger.warning("记录会话日志失败: %s", e)

        # 4. 更新历史缓冲
        if orch.history_buffer is not None:
            try:
                new_messages = messages_used[enhanced_history_len:]
                orch.msg_persistence.persist_new_messages(session_id, new_messages, user_input, response_text)
            except Exception as e:
                logger.warning("更新 HistoryBuffer 失败: %s", e)

        # 5. 累加信息到 ConsolidationEngine
        if orch.consolidation_engine is not None:
            try:
                orch.consolidation_engine.add_info(
                    {"role": "user", "content": user_input}
                )
                orch.consolidation_engine.add_info(
                    {"role": "assistant", "content": filtered_response}
                )
                if orch.consolidation_engine.should_consolidate():
                    await orch.msg_persistence.trigger_consolidation(session_id)
            except Exception as e:
                logger.warning("consolidation 信息累加或触发失败: %s", e)

        # 6. 首次对话后异步生成会话标题
        orch.session_mgr.generate_title_async(session_id, user_input)

        return filtered_response

    def _maybe_filter_main_session_tools(
        self,
        session_id: str,
        tools_override: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """主会话工具隔离：主会话隐藏 worker 协作工具。

        对齐"两工具不互通"：主会话协作工具（list_collab_agents /
        request_collaboration）与 worker 协作工具（send_remote_message /
        list_remote_agents）互不可见。

        判定依据：仅当主会话协作工具已注册（即 main_session_collab 激活，
        工具注册门控 multiagent.enabled + main_session_collab.enabled +
        register_tools + a2a.enabled 全满足）时，才从主会话工具集过滤掉
        worker 协作工具。非主会话（cron: / multiagent_ 前缀）不处理；
        worker 协作会话对主会话工具由 request_collaboration handler 的
        contextvar 守卫兜底拒绝。

        参数:
            session_id: 当前会话 ID。
            tools_override: 当前工具集（主会话路径通常为 None → 全量）。

        返回:
            过滤后的工具 schema 列表；无需过滤时原样返回。
        """
        if session_id and (
            session_id.startswith("cron:") or session_id.startswith("multiagent_")
        ):
            return tools_override
        orch = self.orch
        registry = getattr(orch, "tool_registry", None)
        if registry is None:
            return tools_override
        try:
            core = getattr(registry, "_core_tools", {}) or {}
            has_main_tools = "list_collab_agents" in core
        except Exception:
            has_main_tools = False
        if not has_main_tools:
            return tools_override
        full = tools_override
        if full is None:
            try:
                full = registry.get_tools_schema()
            except Exception:
                return tools_override
        hide = {"send_remote_message", "list_remote_agents"}
        filtered = [t for t in full if t.get("name") not in hide]
        return filtered if filtered else full

    async def chat_stream(
        self,
        session_id: str,
        user_input: str,
        cancel_event: Optional[threading.Event] = None,
        reasoning_cfg: Optional["ReasoningConfig"] = None,
        is_cron: bool = False,
        stream_manager: Optional[Any] = None,
        extra_system_prompt: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
    ):
        """流式主对话入口，异步生成器逐个 yield 事件 dict。

        与 :meth:`chat` 等价的会话管理逻辑（历史获取、消息记录、
        history_buffer 更新、consolidation 累加），但调用
        :meth:`ReactLoop.run_stream`，将 LLM 文本增量与工具调用事件
        实时透传给调用方。

        与 :meth:`chat` 不同的是，本方法在流式过程中**实时收集**所有事件
        （text / tool / round_start / done），并在 finally 块中按事件顺序
        **批量写入 session_logger**：
        - 每个 ``tool`` 事件：记录 2 条消息（tool_use + tool_result，附带
          ``tool_name`` / ``tool_call_id`` 字段）
        - 每轮 assistant 文本：在下一轮 ``round_start`` 或 ``done`` 时作为
          一条 assistant 消息提交
        - 兜底：若整个流未发出 ``round_start``（单轮场景）或收集列表末尾
          不是纯文本 assistant 消息，则用 ``response_text`` 补记一条

        这样保证流被中断时（客户端断连 / 异常）finally 块仍会保存已收集
        的消息，避免工具卡片刷新后丢失。

        事件格式（与 ``ReactLoop.run_stream`` 一致，并扩展 todo 事件）：
            - ``{"type": "round_start", "loop_idx": int}``：新一轮循环开始。
            - ``{"type": "text", "text": str}``：LLM 输出的文本增量。
            - ``{"type": "reasoning", "text": str, "signature": str|None}``：
              推理增量（reasoning 模式开启时）。
            - ``{"type": "tool", "name": str, "input": dict, "result": str,
              "is_error": bool, "session_id": str|None}``：工具调用事件。
            - ``{"type": "todo_init", "session_id": str, "todo": dict}``：
              plan_task 工具执行后发射的 todo 初始化事件。
            - ``{"type": "todo_update", "session_id": str, "todo": dict}``：
              update_todo 工具执行后发射的 todo 更新事件。
            - ``{"type": "todo_complete", "session_id": str, "todo": dict}``：
              update_todo 后所有 step 均为 completed 时发射的完成事件。
            - ``{"type": "approval_request", ...}``：HIL 审批请求事件。
            - ``{"type": "approval_resolved", ...}``：审批决定事件。
            - ``{"type": "done", "response": str, "messages": list}``：
              整个对话结束事件。

        Yields:
            事件 dict。
        """
        orch = self.orch
        # 0. 会话切换检测：若 session_id 变化且 pending 非空，先 flush 旧会话沉淀
        yield {"type": "status", "status": "loading_context", "message": "正在加载上下文..."}
        await orch.msg_persistence.maybe_flush_on_switch(session_id)

        # 记录当前 session_id，供 plan 工具通过 get_session_id 回调获取
        orch._current_session_id = session_id
        # 重置 intent_result，防止上一轮残留（Task 5 P1 修复）
        orch._current_intent_result = None

        # 1. 获取 session 历史
        history: List[Dict[str, Any]] = []
        if orch.history_buffer is not None:
            try:
                history = orch.history_buffer.get_history(session_id) or []
            except Exception as e:
                logger.warning("从 HistoryBuffer 获取历史失败: %s", e)
                history = []

        # 1.5 消费暂存的中断通知（规范 3 Task 8.3-8.6）
        # 不再字符串拼接到 user_input，改为在 _build_enhanced_context 之后
        # 追加独立 {"role":"system"} 消息到 enhanced_history
        # TTL 清理：超过 5 分钟未消费的通知自动丢弃（Task 8.6）
        pending_notice = orch._pending_interrupt_notices.pop(session_id, None)
        pending_notice_content: Optional[str] = None
        if pending_notice is not None:
            notice_age = time.time() - pending_notice.get("timestamp", 0)
            if notice_age > 300:  # 5 分钟 TTL
                logger.info(
                    "中断通知已过期（%.0f秒 > 300秒），丢弃: %s",
                    notice_age, session_id,
                )
            else:
                pending_notice_content = pending_notice.get("content")
                logger.info("准备注入 InterruptNotice 到 history（流式）: %s", session_id)

        # 1.6 安全网：清理历史中的连续 user 消息（兼容旧 JSONL 文件）
        history = MessagePersistence.sanitize_history(history)

        # 2. 流式执行 React 循环，透传事件并收集待持久化的消息
        # 构建含用户画像 + 检索记忆的增强上下文
        # Phase 8 Task 5.7: _build_enhanced_context 返回三元组，第三项为
        # tools_override（用户会话固定 None；cron 会话为请求级过滤后的列表）
        if system_prompt_override is not None:
            # 协作/覆盖路径（如 main_collab_{agent_id} 主会话协作响应）：
            # 绕开主 SYSTEM_PROMPT / 用户画像 / 检索记忆，直接用 override 作 system_text
            condensed_history = await orch.enhanced_context_builder._apply_condenser(history)
            system_text = system_prompt_override
            enhanced_history = list(condensed_history) + [
                {"role": "user", "content": user_input}
            ]
            tools_override = None
        else:
            system_text, enhanced_history, tools_override = await orch.enhanced_context_builder.build(
                session_id, user_input, history
            )
            # 主会话工具隔离：隐藏 worker 协作工具（send_remote_message 等）
            tools_override = self._maybe_filter_main_session_tools(
                session_id, tools_override
            )
        # 规范 3 Task 8.4: 追加独立 system 消息到 enhanced_history（流式路径）
        if pending_notice_content is not None:
            enhanced_history = list(enhanced_history) + [
                {"role": "system", "content": pending_notice_content}
            ]
        # 主会话协作引导段（条件注入，extra_system_prompt）：以独立 system
        # 消息追加到 history 末尾，不替换主 SYSTEM_PROMPT。
        if extra_system_prompt:
            enhanced_history = list(enhanced_history) + [
                {"role": "system", "content": extra_system_prompt}
            ]
        enhanced_history_len = len(enhanced_history)

        # Phase 9 Task 6 接入点 D: 输入扫描（流式路径）
        # deny 时 yield 一个 error 事件并 return（不进入 run_stream）；
        # suspicious 时放行并记录审计；allow 时正常处理。
        if orch.guardrail_engine is not None:
            guardrail_result = orch.guardrail_engine.scan_input(user_input)
            if guardrail_result.action == "deny":
                # 审计记录（deny 为高风险）
                if orch.audit_logger is not None:
                    orch.audit_logger.log_guardrail_decision(
                        layer="input_scan",
                        action="deny",
                        reason=guardrail_result.reason,
                        session_id=session_id,
                        matched_patterns=guardrail_result.matched_patterns,
                        risk_level="high",
                    )
                logger.warning(
                    "输入扫描 deny（流式），拦截会话 %s: %s",
                    session_id,
                    guardrail_result.reason,
                )
                yield {
                    "type": "error",
                    "message": "检测到潜在的安全风险，请重新表述您的请求。",
                    "reason": guardrail_result.reason,
                }
                # 直接 yield done 事件，保证前端流正常结束
                yield orch.react_loop._build_done_event(
                    response="检测到潜在的安全风险，请重新表述您的请求。",
                    messages=[],
                    is_complete=True,
                    termination_reason="error",
                )
                return
            # suspicious 或 allow 时放行；suspicious 记录审计（中等风险）
            if (
                guardrail_result.action == "suspicious"
                and orch.audit_logger is not None
            ):
                orch.audit_logger.log_guardrail_decision(
                    layer="input_scan",
                    action="allow",
                    reason=guardrail_result.reason,
                    session_id=session_id,
                    matched_patterns=guardrail_result.matched_patterns,
                    risk_level="medium",
                )

        # spec agent-metacognition-uplift Task 5: Intent Classifier 前置路由（流式路径）
        # 在 _build_enhanced_context 之后、run_stream 之前调用，对用户输入做
        # 轻量意图分类。classify_intent 不依赖 stream_manager，全链路异步，
        # 失败降级为 SIMPLE_QA，低置信度回退到 MULTI_STEP_TASK。
        # intent_result 保存到 self._current_intent_result，供下游 Task 6/7/8
        # 读取（与 chat() 路径保持一致）。
        # cron 会话为固定调度，跳过意图识别节省 LLM 调用成本。
        intent_result: Optional[IntentClassificationResult] = None
        if is_cron:
            # cron 会话跳过意图识别，记录监控指标
            if orch.metrics is not None:
                orch.metrics.observe_intent(is_cron_skip=True)
        elif classify_intent is not None and orch.llm_client is not None:
            _intent_start = time.monotonic()
            intent_result = await classify_intent(
                llm_client=orch.llm_client,
                user_input=user_input,
                history=history,
                cancel_event=cancel_event,
            )
            _intent_latency_ms = (time.monotonic() - _intent_start) * 1000
            logger.info(
                "会话 %s intent_classifier 结果（流式）: %s (confidence=%.2f)",
                session_id,
                intent_result.intent.value,
                intent_result.confidence,
            )
            # 上报 intent 分类监控指标
            if orch.metrics is not None:
                orch.metrics.observe_intent(
                    intent_type=intent_result.intent.value,
                    confidence=intent_result.confidence,
                    fallback_reason=intent_result.fallback_reason,
                    latency_ms=_intent_latency_ms,
                )
        # P1 修复：透传 intent_result 到实例属性（与 chat() 路径一致）。
        orch._current_intent_result = intent_result

        response_text: str = ""
        # done 事件携带的完整 messages（含 history + 本轮新增），
        # 在 finally 块中切出本轮新增部分持久化到 history_buffer。
        # 流中断未收到 done 时保持 None，降级为 user_input + response_text。
        done_messages: Optional[List[Dict[str, Any]]] = None
        # 收集所有要持久化的事件/消息（按时间顺序），每项形如：
        # {"role": "user"/"assistant", "content": str,
        #  "tool_name": Optional[str], "tool_call_id": Optional[str]}
        collected_messages: List[Dict[str, Any]] = []
        # 当前轮的 assistant 文本累加器（在 round_start 时把上一轮累加的文本
        # 作为一条 assistant 消息提交，避免跨轮累加串台）
        current_round_text: str = ""
        # 当前轮的 reasoning（思考）文本累加器，随 assistant 消息一起持久化
        current_round_reasoning: str = ""
        # 工具调用 ID 自增计数器（ReactLoop 当前未在 tool 事件中透传 tool_use_id，
        # 这里用自增 ID 保证 tool_use 与 tool_result 能配对）
        last_tool_use_id_counter: int = 0

        # 规范 2: 跨 run 空回复检测（流式路径）
        # count >= 2 → yield error + done 后 return，不进入 run_stream
        # count == 1 → 追加纠偏提示到 system_text（不持久化，per-call）
        empty_count = orch._consecutive_empty_runs.get(session_id, 0)
        if empty_count >= 2:
            logger.warning(
                "会话 %s 连续 %d 次空回复（流式），直接返回友好提示",
                session_id, empty_count,
            )
            yield {
                "type": "error",
                "message": "抱歉，连续两次未能生成回复，可能是模型异常或上下文冲突。请重试或换种问法。",
            }
            yield orch.react_loop._build_done_event(
                response="抱歉，连续两次未能生成回复，可能是模型异常或上下文冲突。请重试或换种问法。",
                messages=[],
                is_complete=True,
                termination_reason="error",
            )
            return
        if empty_count == 1:
            system_text = (system_text or "") + (
                "\n\n[系统提示] 上一轮 LLM 返回了空回复。请确保本次明确回应用户问题，"
                "不要返回空内容。"
            )
            logger.info("会话 %s 注入空回复纠偏提示到 system_text（流式）", session_id)

        # 规范 2: 捕获 done 事件的 termination_reason，用于 finally 块计数
        stream_termination_reason: str = "normal"
        # 批次 2.4: 捕获 done 事件的 usage，用于 finally 块回填 token_count
        stream_done_usage: Optional[Dict[str, Any]] = None
        try:
            async for event in orch.react_loop.run_stream(
                user_input=user_input,
                history=enhanced_history,
                system=system_text,
                session_id=session_id,
                tools_override=tools_override,
                cancel_event=cancel_event,
                reasoning_cfg=reasoning_cfg,
                is_cron=is_cron,
                stream_manager=stream_manager,
            ):
                etype = event.get("type")

                # T13: 对 plan_task / update_todo 工具，获取变更后的 todo_dict，
                # 用于：(1) 持久化 tool_result content 替换为 JSON（供 T14 前端
                # loadMessages 解析重建卡片）；(2) 发射 todo_init / todo_update /
                # todo_complete 事件。初始为 None，仅在 tool 分支内赋值。
                todo_dict: Optional[dict] = None

                # 收集逻辑（同时透传给 server.py）
                if etype == "round_start":
                    # 新一轮开始：把上一轮累加的 assistant 文本作为一条消息提交
                    if current_round_text:
                        msg_dict: Dict[str, Any] = {
                            "role": "assistant", "content": current_round_text
                        }
                        if current_round_reasoning:
                            msg_dict["reasoning"] = current_round_reasoning
                        collected_messages.append(msg_dict)
                        current_round_text = ""
                        current_round_reasoning = ""
                elif etype == "reasoning":
                    # 累加到当前轮的 reasoning 文本
                    current_round_reasoning += event.get("text", "")
                elif etype == "text":
                    # 累加到当前轮的 assistant 文本
                    current_round_text += event.get("text", "")
                elif etype == "tool":
                    # 先提交当前轮累加的 assistant 文本（LLM 先输出文本，
                    # 再调用工具），保证持久化顺序与事件实际顺序一致：
                    # text → tool_use → tool_result
                    if current_round_text:
                        msg_dict = {
                            "role": "assistant", "content": current_round_text
                        }
                        if current_round_reasoning:
                            msg_dict["reasoning"] = current_round_reasoning
                        collected_messages.append(msg_dict)
                        current_round_text = ""
                        current_round_reasoning = ""
                    # 工具调用：记录 2 条消息（tool_use + tool_result）
                    tool_name = event.get("name", "")
                    tool_input = event.get("input", {}) or {}
                    tool_result = event.get("result", "")
                    tool_is_error = bool(event.get("is_error", False))
                    # 优先用事件中的 tool_use_id（ReactLoop 当前未透传），
                    # 否则用自增 ID
                    tool_call_id = (
                        event.get("tool_use_id")
                        or f"tool_{last_tool_use_id_counter}"
                    )
                    last_tool_use_id_counter += 1

                    # T13: plan_task / update_todo 工具调用后，获取 todo_dict
                    # 用于持久化 content 替换与事件发射。todo_registry 为 None
                    # 或 session 无 plan 时降级跳过（todo_dict 保持 None）。
                    if (
                        tool_name in ("plan_create", "plan_update_step")
                        and orch.todo_registry is not None
                    ):
                        try:
                            todo_dict = orch.todo_registry.get_todo_dict(
                                session_id
                            )
                        except Exception as e:
                            logger.warning("获取 todo dict 失败: %s", e)
                            todo_dict = None

                    # tool_use 消息（assistant 角色，记录调用）
                    collected_messages.append(
                        {
                            "role": "assistant",
                            "content": f"调用工具 {tool_name}: "
                            f"{json.dumps(tool_input, ensure_ascii=False)}",
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                        }
                    )
                    # tool_result 消息（user 角色，记录返回结果）
                    # T13: 对 plan_task / update_todo，持久化的 content 替换为
                    # todo_dict 的 JSON 字符串（前端 loadMessages 可解析重建卡片），
                    # 而非工具返回给 LLM 的简短字符串。
                    if (
                        todo_dict is not None
                        and tool_name in ("plan_create", "plan_update_step")
                    ):
                        persisted_content = json.dumps(
                            todo_dict, ensure_ascii=False
                        )
                    else:
                        persisted_content = tool_result
                    collected_messages.append(
                        {
                            "role": "user",
                            "content": persisted_content,
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "is_error": tool_is_error,
                        }
                    )
                elif etype == "done":
                    # done 事件触发时，把最后一轮累加的文本也提交
                    if current_round_text:
                        msg_dict = {
                            "role": "assistant", "content": current_round_text
                        }
                        if current_round_reasoning:
                            msg_dict["reasoning"] = current_round_reasoning
                        collected_messages.append(msg_dict)
                        current_round_text = ""
                        current_round_reasoning = ""
                    response_text = event.get("response", "") or ""
                    # 批次 2.4: 捕获 done 事件的 usage，用于 finally 块回填 token_count
                    stream_done_usage = event.get("usage")
                    # 捕获完整 messages（含 history + 本轮新增），用于
                    # 在 finally 块中切出本轮新增部分持久化到 history_buffer
                    done_messages = event.get("messages")
                    # Phase 9 Task 7.5: 读取 is_complete 字段（流式路径）
                    # TODO Phase 9+: 流式路径暂未实装自动续接（续接需在
                    # async for 外层包装 while 循环，并管理 collected_messages
                    # 的跨轮累加与 done_messages 切片边界，复杂度较高）。
                    # 当前仅记录 is_complete 供日志观察，达到 max_loops 或卡死
                    # 时流式路径直接结束（与旧行为一致），用户可手动发"继续"
                    # 触发下一轮 chat_stream。非流式路径（chat()）已完整实装
                    # 自动续接 + 总熔断 200 轮。
                    done_is_complete = event.get("is_complete", True)
                    if not done_is_complete:
                        logger.info(
                            "流式 React 循环未自然完成（is_complete=False），"
                            "流式路径暂不支持自动续接，需用户手动继续"
                        )
                    # 规范 2: 捕获 termination_reason，用于 finally 块空回复计数
                    stream_termination_reason = event.get(
                        "termination_reason", "normal"
                    )

                    # 在 done 事件捕获后、yield 前触发标题生成（避免 finally
                    # 块在 GeneratorExit 期间创建 task 失败被静默吞掉的问题）
                    orch.session_mgr.generate_title_async(session_id, user_input)

                yield event  # 透传给 server.py

                # Phase 9 Task 6 接入点 E: done 事件后过滤 PII
                # 在 done 事件透传后，对最终 response 做 PII 脱敏。
                # 若发生替换，额外 yield 一个 output_filtered 事件，
                # 供前端将已显示的响应替换为脱敏版本。
                # 注意：仅在 try 块内（done 事件）yield output_filtered，
                # finally 块中不能可靠 yield（async generator 限制）。
                if etype == "done" and orch.guardrail_engine is not None:
                    try:
                        _done_response = event.get("response", "") or ""
                        (
                            _filtered_done,
                            _done_pii_count,
                        ) = orch.guardrail_engine.filter_output(_done_response)
                        if _filtered_done != _done_response:
                            yield {
                                "type": "output_filtered",
                                "filtered_response": _filtered_done,
                                "replacements_count": _done_pii_count,
                            }
                    except Exception as e:
                        logger.warning(
                            "done 事件 output_filtered 异常，fail-open 跳过: %s",
                            e,
                        )

                # T13: plan_task / update_todo 工具事件透传后，发射 todo 事件。
                # 事件顺序：tool → todo_init / (todo_update → todo_complete)，
                # 在下一轮 text 之前发射，确保前端实时渲染 todo 卡片。
                if etype == "tool" and todo_dict is not None:
                    tool_name = event.get("name", "")
                    if tool_name == "plan_create":
                        yield {
                            "type": "todo_init",
                            "session_id": session_id,
                            "todo": todo_dict,
                        }
                    elif tool_name == "plan_update_step":
                        yield {
                            "type": "todo_update",
                            "session_id": session_id,
                            "todo": todo_dict,
                        }
                        # 所有 step 均为 completed 时额外发射 todo_complete
                        if todo_dict.get("completed"):
                            yield {
                                "type": "todo_complete",
                                "session_id": session_id,
                                "todo": todo_dict,
                            }
        finally:
            # 反馈监控：上报流式终止原因（覆盖正常/异常/取消所有退出路径）
            if orch.metrics is not None:
                orch.metrics.observe_termination(stream_termination_reason)
            # 空回复计数：直接检查 response_text 是否为空
            # 取消/工具失败不计数（避免级联误判）
            is_empty_response_stream = (
                stream_termination_reason not in ("user_cancel", "tool_permanent_fail")
                and (not response_text or not response_text.strip())
            )
            if is_empty_response_stream:
                orch._consecutive_empty_runs[session_id] = empty_count + 1
                logger.info(
                    "会话 %s 空回复计数 %d -> %d（流式）",
                    session_id, empty_count, empty_count + 1,
                )
            elif empty_count > 0:
                orch._consecutive_empty_runs[session_id] = 0
                logger.info("会话 %s 收到非空回复，重置空回复计数（流式）", session_id)

            # 3. 批量记录到 session_logger（即使流被中断也保证保存）
            if orch.session_logger is not None:
                try:
                    orch.session_mgr.ensure_session(session_id)
                    # 先记录 user 输入
                    orch.session_logger.log_message(
                        session_id=session_id,
                        role="user",
                        content=user_input,
                    )
                    # 按顺序记录所有收集的消息
                    # P1-3 修复：找到最后一条 assistant 消息（无 tool_name），
                    # 将 done 事件的 usage 分配给它（best-effort，多轮中间消息仍为 0）
                    _last_assistant_idx = None
                    for _i, _msg in enumerate(collected_messages):
                        if _msg["role"] == "assistant" and not _msg.get("tool_name"):
                            _last_assistant_idx = _i
                    _stream_token_count = _extract_token_count(stream_done_usage)
                    for _i, msg in enumerate(collected_messages):
                        _msg_kwargs = {
                            "session_id": session_id,
                            "role": msg["role"],
                            "content": msg["content"],
                            "tool_name": msg.get("tool_name"),
                            "tool_call_id": msg.get("tool_call_id"),
                            "is_error": msg.get("is_error", False),
                            "reasoning": msg.get("reasoning"),
                        }
                        # P1-3 修复：最后一条 assistant 消息回填 token_count
                        if _i == _last_assistant_idx:
                            _msg_kwargs["token_count"] = _stream_token_count
                        orch.session_logger.log_message(**_msg_kwargs)
                    # 兜底：如果 collected_messages 为空，或最后一条不是纯文本
                    # assistant 消息（无 tool_name），且 response_text 非空，补记一条
                    # （单轮无 round_start 场景，response_text 是唯一 assistant 文本）
                    need_final_assistant = bool(response_text) and (
                        not collected_messages
                        or collected_messages[-1].get("tool_name")
                        or collected_messages[-1]["role"] != "assistant"
                    )
                    if need_final_assistant:
                        orch.session_logger.log_message(
                            session_id=session_id,
                            role="assistant",
                            content=response_text,
                            reasoning=current_round_reasoning or None,
                            # 批次 2.4: 从 done 事件的 usage 回填 token_count
                            token_count=_extract_token_count(stream_done_usage),
                        )
                except Exception as e:
                    logger.warning("记录会话日志失败: %s", e)

            # 4. 更新历史缓冲（持久化循环内完整 messages，含 tool_use + tool_result）
            # done_messages = enhanced_history + [user_input, ...loop messages...]，
            # 切掉 enhanced_history 部分即为本次新增的消息。
            # 流中断未收到 done（done_messages 为 None）时降级为
            # user_input + response_text，保证至少保留本轮纯文本对话。
            if orch.history_buffer is not None:
                try:
                    if done_messages is not None:
                        new_messages = done_messages[enhanced_history_len:]
                        orch.msg_persistence.persist_new_messages(
                            session_id, new_messages, user_input, response_text
                        )
                    else:
                        # 规范 3 Task 8.2: 中断降级路径不写半截 assistant
                        # 仅持久化 user_input，不持久化 partial response_text
                        # （半截 assistant 会污染下一轮上下文，中断通知已由
                        # _save_interrupt_notice 暂存，下次调用时注入）
                        orch.history_buffer.add_message(
                            session_id, "user", user_input
                        )
                except Exception as e:
                    logger.warning("更新 HistoryBuffer 失败: %s", e)

            # 5. 累加信息到 ConsolidationEngine，达到阈值时触发沉淀
            # Phase 9 Task 6 接入点 F: ConsolidationEngine 累加 filtered_response
            # （防 PII 泄漏到长期记忆向量库）。collected_messages 与
            # session_logger / history_buffer 持久化原始文本（保留上下文完整）。
            # Note: 中断时 response_text 可能为空，兜底用 current_round_text
            _consolidation_response = (
                response_text or current_round_text or "[用户中断了回复]"
            )
            # 对 _consolidation_response 做 PII 脱敏后再累加到沉淀引擎
            if orch.guardrail_engine is not None:
                try:
                    _consolidation_response, _ = (
                        orch.guardrail_engine.filter_output(_consolidation_response)
                    )
                except Exception as e:
                    logger.warning(
                        "finally 块 filter_output 异常，fail-open 使用原值: %s", e
                    )
            if orch.consolidation_engine is not None:
                try:
                    orch.consolidation_engine.add_info(
                        {"role": "user", "content": user_input}
                    )
                    orch.consolidation_engine.add_info(
                        {"role": "assistant", "content": _consolidation_response}
                    )
                    if orch.consolidation_engine.should_consolidate():
                        await orch.msg_persistence.trigger_consolidation(session_id)
                except Exception as e:
                    logger.warning("consolidation 信息累加或触发失败: %s", e)
