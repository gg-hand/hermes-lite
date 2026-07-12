"""非流式对话处理器。

从 Orchestrator.chat 提取，负责非流式对话流程：
历史获取 → 上下文构建 → React 循环 → 消息持久化 → 记忆沉淀触发。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..llm.reasoning_profiles import ReasoningConfig

logger = logging.getLogger(__name__)

# 延迟导入：与 orchestrator/__init__.py 保持一致的 try/except 降级策略
try:
    from ..agent.context_builder import ContextBuilder
    from ..agent.msg_persistence import MessagePersistence
except ImportError:  # pragma: no cover
    try:
        from agent.context_builder import ContextBuilder  # type: ignore
        from agent.msg_persistence import MessagePersistence  # type: ignore
    except ImportError:  # pragma: no cover
        ContextBuilder = None  # type: ignore
        MessagePersistence = None  # type: ignore

try:
    from ..agent.intent_classifier import (
        IntentClassificationResult,
        classify_intent,
    )
except ImportError:  # pragma: no cover
    try:
        from agent.intent_classifier import (  # type: ignore
            IntentClassificationResult,
            classify_intent,
        )
    except ImportError:  # pragma: no cover
        IntentClassificationResult = None  # type: ignore
        classify_intent = None  # type: ignore


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
    ) -> str:
        """主对话入口（非流式）。

        流程:
            1. 获取 session 历史；
            2. 调用 enhanced_context_builder 构建上下文；
            3. 调用 react_loop.run 执行 React 循环；
            4. 记录用户输入与 assistant 回复到 session_logger；
            5. 更新 history_buffer；
            6. 触发 consolidation。
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
        system_text, enhanced_history, tools_override = await orch.enhanced_context_builder.build(
            session_id, user_input, history
        )
        # 规范 3 Task 8.4: 追加独立 system 消息到 enhanced_history
        if pending_notice_content is not None:
            enhanced_history = list(enhanced_history) + [
                {"role": "system", "content": pending_notice_content}
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
                orch.session_logger.log_message(
                    session_id=session_id,
                    role="assistant",
                    content=response_text,
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
