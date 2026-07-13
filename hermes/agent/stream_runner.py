"""流式 React 循环执行器。

从 ReactLoop.run_stream 提取，负责流式 SSE 事件生成：
LLM 流式响应 → 工具调用 → 中断处理 → done 事件构造。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from hermes.stream_manager import StreamCancelled
from hermes.agent.error_classifier import ErrorClassifier, ErrorClass
from hermes.agent.tool_error import (
    ErrorStage,
    NonStreamHILError,
    PolicyDeniedError,
    StuckDetectedError,
    ToolError,
    UserRejectedError,
    from_exception,
)
if TYPE_CHECKING:
    from ..llm.reasoning_profiles import ReasoningConfig
    from .react_loop import ReactLoop

from .tool_executor import (
    compute_params_hash as _compute_params_hash_fn,
    detect_tool_stuck as _detect_tool_stuck_fn,
    build_stuck_message as _build_stuck_message_fn,
    drop_trailing_orphan_tool_calls as _drop_trailing_orphan_tool_calls_fn,
)
from .stream_handler import (
    block_to_dict as _block_to_dict_fn,
    build_done_event as _build_done_event_fn,
)

logger = logging.getLogger(__name__)


class StreamRunner:
    """流式 React 循环执行器。

    持有 ReactLoop 引用，通过属性访问组件。
    """

    def __init__(self, loop: "ReactLoop") -> None:
        self.loop = loop

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
            - ``{"type": "reasoning", "text": "<增量思考>", "signature": str|None}``：
              推理增量（reasoning 模式开启时），调用方应累加到思考区。
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
        ``reasoning_cfg`` 可选 ReasoningConfig，None 时由 LLMClient 按
        ``is_cron`` 选择默认配置。
        ``is_cron`` 标记是否为 cron 会话，影响 reasoning_cfg 默认选择。
        """
        loop = self.loop

        # 1. 构建 messages = history + [user_input]
        messages: List[Dict[str, Any]] = []
        if history:
            messages.extend(dict(m) for m in history)
        messages.append({"role": "user", "content": user_input})

        # 用户输入计入信息计数器
        loop._info_count += 1

        # Phase 元认知 Task 1: 检测用户对失败的口头反馈（与 run() 对称）
        loop._check_user_failure_feedback(user_input, session_id)

        # 2. 获取工具 schema（Phase 8 Task 5.7: tools_override 优先）
        tools: Optional[List[Dict[str, Any]]] = None
        if tools_override is not None:
            tools = list(tools_override)
        elif loop.tool_registry is not None:
            try:
                tools = loop.tool_registry.get_tools_schema()
            except Exception as e:
                logger.warning("获取工具 schema 失败，降级为纯对话模式: %s", e)
                tools = None

        last_text: str = ""

        # Phase 9 Task 7.3: 单工具重试检测滑动窗口（与 run() 等价语义）
        recent_tool_calls: List[Tuple[str, str, Optional[str]]] = []
        # 卡死检测软警告状态机：首次命中重复 → warn（tool_result 返回警告，
        # 不执行工具，继续循环给 LLM 自我纠正机会）；二次命中 → stop（硬终止）。
        warned_pairs: set = set()
        # reasoning-only 回复全局重试预算（跨轮累计，上限 3 次，Task 12）
        reasoning_only_retry_count: int = 0
        # Phase 元认知 Task 1: Agent 自画像失败信号触发状态（per-run 局部，与 run() 对称）
        consecutive_tool_failures: int = 0
        agent_failure_triggered: bool = False

        for loop_idx in range(loop.max_loops):
            # 🔴 检测点 1：每轮循环开始前检测中断
            if cancel_event and cancel_event.is_set():
                yield _build_done_event_fn(
                    response=last_text,
                    messages=messages,
                    is_complete=False,
                    termination_reason="user_cancel",
                    reasoning_cfg=reasoning_cfg,
                )
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
            # 本轮 reasoning 增量累积（用于前端展示，不进入持久化 messages）
            current_round_reasoning: str = ""
            # 本轮 usage（从 backend done 事件提取，用于 done 事件 enrich）
            current_round_usage: Optional[Dict[str, Any]] = None
            try:
                async for event in loop.llm_client.chat_main_stream(
                    messages=messages,
                    tools=tools,
                    system=system,
                    cancel_event=cancel_event,  # ← 透传 cancel_event
                    session_id=session_id,
                    reasoning_cfg=reasoning_cfg,
                    is_cron=is_cron,
                    stream_manager=stream_manager,
                ):
                    # 🔴 检测点 2：每 token 检测中断（立即停止 LLM 流）
                    if cancel_event and cancel_event.is_set():
                        # 将 partial 内容纳入 messages，确保持久化不丢失
                        # 注：半截 thinking block（无 signature）不放入 messages，
                        # 避免 Anthropic 因 signature 缺失返回 400（SubTask 11.3/11.4）
                        partial_blocks: List[Dict[str, Any]] = []
                        if current_round_text:
                            partial_blocks.append({"type": "text", "text": current_round_text})
                        if partial_blocks:
                            messages.append({"role": "assistant", "content": partial_blocks})
                        yield _build_done_event_fn(
                            response=current_round_text or last_text,
                            messages=messages,
                            is_complete=False,
                            termination_reason="user_cancel",
                            usage=current_round_usage,
                            reasoning_cfg=reasoning_cfg,
                            current_round_text=current_round_text,
                            current_round_reasoning=current_round_reasoning,
                        )
                        return

                    etype = event.get("type")
                    if etype == "text":
                        current_round_text += event.get("text", "")  # ← 累积
                        # 透传文本增量
                        yield event
                    elif etype == "reasoning":
                        # 推理增量累积 + 透传给前端
                        current_round_reasoning += event.get("text", "")
                        yield event
                    elif etype == "done":
                        stop_reason = event.get("stop_reason", "end_turn") or "end_turn"
                        content_blocks = event.get("content_blocks", []) or []
                        # 提取 usage（SubTask 10.4）
                        current_round_usage = event.get("usage")
            except StreamCancelled:
                # 后端 SDK 检测到中断后抛出的异常
                # 注：半截 thinking block（无 signature）不放入 messages（SubTask 11.3/11.4）
                partial_blocks_sc: List[Dict[str, Any]] = []
                if current_round_text:
                    partial_blocks_sc.append({"type": "text", "text": current_round_text})
                if partial_blocks_sc:
                    messages.append({"role": "assistant", "content": partial_blocks_sc})
                yield _build_done_event_fn(
                    response=current_round_text or last_text,
                    messages=messages,
                    is_complete=False,
                    termination_reason="user_cancel",
                    usage=current_round_usage,
                    reasoning_cfg=reasoning_cfg,
                    current_round_text=current_round_text,
                    current_round_reasoning=current_round_reasoning,
                )
                return
            except Exception as e:
                logger.error("LLM 流式调用失败 (loop=%d): %s", loop_idx, e)
                if last_text:
                    yield _build_done_event_fn(
                        response=last_text,
                        messages=messages,
                        is_complete=False,
                        termination_reason="normal",
                        usage=current_round_usage,
                        reasoning_cfg=reasoning_cfg,
                        current_round_text=current_round_text,
                        current_round_reasoning=current_round_reasoning,
                    )
                    return
                raise

            # 解析 content_blocks（SubTask 10.3: 新增 thinking_blocks 提取）
            text_parts: List[str] = []
            tool_use_blocks: List[Dict[str, Any]] = []
            thinking_blocks: List[Dict[str, Any]] = []
            for block in content_blocks:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        text_parts.append(text)
                elif btype == "tool_use":
                    tool_use_blocks.append(block)
                elif btype == "thinking":
                    thinking_blocks.append(block)

            # assistant 响应计入信息计数器
            loop._info_count += 1

            # 将 assistant 完整响应加入 messages（content_blocks 含 thinking block）
            messages.append({"role": "assistant", "content": content_blocks})

            if text_parts:
                last_text = "".join(text_parts)

            # 4. reasoning-only 回复检测（Task 12）
            # 检测条件：无 text 无 tool_use 但有 thinking block
            # 这种情况 LLM 只输出了思考没有回复，需要重试让 LLM 基于思考生成回复
            if not text_parts and not tool_use_blocks and thinking_blocks:
                # 全局重试预算 3 次（跨轮累计）
                if reasoning_only_retry_count < 3:
                    reasoning_only_retry_count += 1
                    logger.info(
                        "检测到 reasoning-only 回复 (loop=%d, retry=%d/3)，"
                        "注入 system message 重试",
                        loop_idx, reasoning_only_retry_count,
                    )
                    # 注入 system message 引导 LLM 基于思考给出回答
                    # 注：不持久化到 history_buffer（SubTask 12.3），仅本轮内存有效
                    messages.append({
                        "role": "user",
                        "content": "[系统提示] 请基于你的思考给出具体回答，不要只输出思考内容。",
                    })
                    # 清理本轮累积，进入下一轮循环
                    current_round_text = ""
                    current_round_reasoning = ""
                    continue
                else:
                    # 达到全局预算，不再重试，yield error
                    logger.warning(
                        "reasoning-only 回复重试达到全局预算 3 次 (loop=%d)，终止",
                        loop_idx,
                    )
                    yield _build_done_event_fn(
                        response=last_text,
                        messages=messages,
                        is_complete=False,
                        termination_reason="normal",
                        usage=current_round_usage,
                        content_blocks=content_blocks,
                        stop_reason=stop_reason,
                        reasoning_cfg=reasoning_cfg,
                    )
                    return

            # 5. 判断是否需要工具调用
            if stop_reason != "tool_use" or not tool_use_blocks:
                # 自然结束（end_turn）→ is_complete=True
                yield _build_done_event_fn(
                    response=last_text,
                    messages=messages,
                    is_complete=True,
                    termination_reason="normal",
                    usage=current_round_usage,
                    content_blocks=content_blocks,
                    stop_reason=stop_reason,
                    reasoning_cfg=reasoning_cfg,
                )
                return

            # 响应包含 tool_use，但未提供 tool_registry：终止循环
            if loop.tool_registry is None:
                logger.warning(
                    "模型请求工具调用但未提供 tool_registry，返回当前文本回复"
                )
                # 清理末尾未执行的 assistant(tool_calls)，避免下轮 400
                messages = _drop_trailing_orphan_tool_calls_fn(messages)
                yield _build_done_event_fn(
                    response=last_text,
                    messages=messages,
                    is_complete=False,
                    termination_reason="normal",
                    usage=current_round_usage,
                    content_blocks=content_blocks,
                    stop_reason=stop_reason,
                    reasoning_cfg=reasoning_cfg,
                )
                return

            # 🔴 检测点 3：工具执行前检测中断
            # 注：已开始的工具会执行完毕（原子性），不半途取消
            if cancel_event and cancel_event.is_set():
                messages = _drop_trailing_orphan_tool_calls_fn(messages)
                yield _build_done_event_fn(
                    response=last_text,
                    messages=messages,
                    is_complete=False,
                    termination_reason="user_cancel",
                    usage=current_round_usage,
                    content_blocks=content_blocks,
                    stop_reason=stop_reason,
                    reasoning_cfg=reasoning_cfg,
                )
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
                params_hash = _compute_params_hash_fn(tool_input)
                is_stuck, stuck_reason = _detect_tool_stuck_fn(
                    tool_name, params_hash, recent_tool_calls
                )
                if is_stuck:
                    pair = (tool_name, params_hash)
                    if pair in warned_pairs:
                        # 二次命中 → 硬终止（LLM 已收到警告仍重复同参数）
                        logger.warning(
                            "检测到工具 %s 重复调用卡死 (loop=%d, reason=%s)，已警告过，终止本轮循环",
                            tool_name,
                            loop_idx,
                            stuck_reason or "count",
                        )
                        # Phase B-4: stash 中断提示到下轮（与 run() 对称）
                        if StuckDetectedError is not None and session_id is not None:
                            stuck_err = StuckDetectedError(
                                tool_name=tool_name,
                                reason=f"重复调用卡死（{stuck_reason or 'count'}）",
                                suggestion="更换参数或换用其他工具",
                            )
                            loop._stash_interrupt_notice(session_id, stuck_err.to_system_block())
                            if loop.metrics is not None:
                                loop.metrics.observe_tool_error_class(tool_name, "stuck_detected")
                        # 清理末尾未执行的 assistant(tool_calls)，避免下轮 400
                        messages = _drop_trailing_orphan_tool_calls_fn(messages)
                        yield _build_done_event_fn(
                            response=_build_stuck_message_fn(tool_name, stuck_reason),
                            messages=messages,
                            is_complete=False,
                            termination_reason="tool_permanent_fail",
                            usage=current_round_usage,
                            content_blocks=content_blocks,
                            stop_reason=stop_reason,
                            reasoning_cfg=reasoning_cfg,
                        )
                        return
                    # 首次命中 → 软警告：把警告作为 tool_result 返回，不执行工具，
                    # 让 LLM 看到反馈后换参数；若仍重复同参数则升级为硬终止。
                    warned_pairs.add(pair)
                    logger.info(
                        "工具 %s 重复调用软警告 (loop=%d, reason=%s)，注入警告跳过执行",
                        tool_name,
                        loop_idx,
                        stuck_reason or "count",
                    )
                    warn_content = (
                        f"[拦截] {tool_name} → 卡死警告\n"
                        f"原因：检测到与最近调用重复（{stuck_reason or 'count'}）\n"
                        f"建议：更换参数、换用其他工具，或询问用户。继续重复同参数将被终止。"
                    )
                    if StuckDetectedError is not None and session_id is not None:
                        warn_err = StuckDetectedError(
                            tool_name=tool_name,
                            reason=f"重复调用软警告（{stuck_reason or 'count'}）",
                            suggestion="更换参数或换用其他工具",
                        )
                        loop._stash_interrupt_notice(session_id, warn_err.to_system_block())
                        warn_content = warn_err.to_system_block()
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": warn_content,
                            "is_error": True,
                        }
                    )
                    recent_tool_calls.append((tool_name, params_hash, "stuck_warned"))
                    if loop.metrics is not None:
                        loop.metrics.observe_tool_error_class(tool_name, "stuck_warned")
                        loop.metrics.observe_tool_retry(tool_name)
                    duration_ms = (time.perf_counter() - t0) * 1000
                    loop._log_audit(
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        result=warn_content,
                        is_error=True,
                        duration_ms=duration_ms,
                    )
                    yield {
                        "type": "tool",
                        "name": tool_name,
                        "input": tool_input,
                        "result": warn_content,
                        "is_error": True,
                        "session_id": session_id,
                    }
                    continue

                # Phase 5: 策略评估
                decision = loop._evaluate_policy(
                    tool_name, tool_input, session_id=session_id
                )

                if decision.action == "deny":
                    # Phase B-2: 拒绝走 pre_execution system 注入路径（与 run() 对称）
                    # 工具未实际执行，详情走 system 注入，tool_result 仅返回占位
                    reason_text = decision.reason or "用户未提供原因"
                    deny_err = PolicyDeniedError(
                        tool_name=tool_name,
                        reason=reason_text,
                        suggestion="停止重试该工具，改为询问用户意图或换一种方式完成任务",
                    )
                    duration_ms = (time.perf_counter() - t0) * 1000
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": deny_err.to_system_block(),
                            "is_error": True,
                        }
                    )
                    loop._log_audit(
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        result=deny_err.to_system_block(),
                        is_error=True,
                        duration_ms=duration_ms,
                        decision=decision,
                    )
                    if loop.metrics is not None:
                        loop.metrics.observe_tool_call(tool_name, False, duration_ms)
                        loop.metrics.observe_tool_error_class(tool_name, "policy_denied")
                        # 注意：不调用 observe_tool_retry，deny 是拦截不是重试（见边界 9.13）
                    yield {
                        "type": "tool",
                        "name": tool_name,
                        "input": tool_input,
                        "result": deny_err.to_system_block(),
                        "is_error": True,
                        "blocked": True,
                        "session_id": session_id,
                        "tool_use_id": tool_use_id,
                    }
                    loop._info_count += 1
                    # 拒绝路径不计入重试检测窗口（工具未实际执行）
                    continue
                elif decision.action == "confirm":
                    # 流式模式：抛出审批请求，等待用户决定
                    if loop.approval_manager is None:
                        # Phase B-2: approval_manager 未初始化走 NonStreamHILError
                        # （流式路径不应进此处，但兜底需对齐 is_error=True 语义）
                        logger.error(
                            "命中 confirm 但 approval_manager 未初始化，降级为拦截: tool=%s",
                            tool_name,
                        )
                        hil_err = NonStreamHILError(
                            tool_name=tool_name,
                            reason="流式路径 approval_manager 未配置",
                            suggestion="检查 approval_manager 注入或改用其他方式",
                        )
                        duration_ms = (time.perf_counter() - t0) * 1000
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": hil_err.to_system_block(),
                                "is_error": True,
                            }
                        )
                        loop._log_audit(
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            result=hil_err.to_system_block(),
                            is_error=True,
                            duration_ms=duration_ms,
                            decision=decision,
                        )
                        if loop.metrics is not None:
                            loop.metrics.observe_tool_call(tool_name, False, duration_ms)
                            loop.metrics.observe_tool_error_class(tool_name, "non_stream_hil")
                        yield {
                            "type": "tool",
                            "name": tool_name,
                            "input": tool_input,
                            "result": hil_err.to_system_block(),
                            "is_error": True,
                            "blocked": True,
                            "session_id": session_id,
                            "tool_use_id": tool_use_id,
                        }
                        loop._info_count += 1
                        continue

                    # 创建审批请求
                    approval_id = loop.approval_manager.create_request(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        reason=decision.reason,
                        risk_level=decision.risk_level,
                        tool_kind=decision.tool_kind,
                    )
                    # 抛出 approval_request 事件给前端
                    yield {
                        "type": "approval_request",
                        "approval_id": approval_id,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "reason": decision.reason,
                        "risk_level": decision.risk_level,
                        "tool_kind": decision.tool_kind,
                    }
                    # 等待用户决定（async generator 可直接 await）
                    decision_str, wait_reason = (
                        await loop.approval_manager.wait_for_decision(approval_id)
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
                            result = loop.tool_registry.execute_tool(
                                tool_name, tool_input
                            )
                        except Exception as e:
                            logger.error("工具执行失败 %s: %s", tool_name, e)
                            result = f"工具执行出错: {e}"
                            is_error = True
                        # Phase 9 Task 6: 工具返回值脱敏（与 allow 分支一致）
                        result = loop.guardrail_engine.sanitize_tool_result(
                            result, tool_name=tool_name
                        )
                        duration_ms = (time.perf_counter() - t0) * 1000
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": result,
                                **({"is_error": True} if is_error else {}),
                            }
                        )
                        loop._log_audit(
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            result=result,
                            is_error=is_error,
                            duration_ms=duration_ms,
                            decision=decision,
                        )
                        if loop.metrics is not None:
                            loop.metrics.observe_tool_call(
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
                        loop._info_count += 1
                    else:
                        # Phase B-2: 用户拒绝或超时走 UserRejectedError（与 run() 语义对齐）
                        # 工具未实际执行，详情走 system 注入
                        reason_text = (
                            wait_reason or decision.reason or "用户未提供原因"
                        )
                        reject_err = UserRejectedError(
                            tool_name=tool_name,
                            reason=reason_text,
                            suggestion="停止重试该工具，改为询问用户意图或换一种方式完成任务",
                        )
                        duration_ms = (time.perf_counter() - t0) * 1000
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": reject_err.to_system_block(),
                                "is_error": True,
                            }
                        )
                        loop._log_audit(
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            result=reject_err.to_system_block(),
                            is_error=True,
                            duration_ms=duration_ms,
                            decision=decision,
                        )
                        if loop.metrics is not None:
                            loop.metrics.observe_tool_call(
                                tool_name, False, duration_ms
                            )
                            loop.metrics.observe_tool_error_class(
                                tool_name, "user_rejected"
                            )
                        yield {
                            "type": "tool",
                            "name": tool_name,
                            "input": tool_input,
                            "result": reject_err.to_system_block(),
                            "is_error": True,
                            "blocked": True,
                            "session_id": session_id,
                            "tool_use_id": tool_use_id,
                        }
                        loop._info_count += 1
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
                error_class: Optional[str] = None
                try:
                    result = loop._execute_tool_with_dispatch(tool_name, tool_input, cancel_event)
                except ToolError as te:
                    # Phase B-2: 统一异常层次，按 stage 分流（与 run() 对称）
                    # pre_execution 错误（如 ParamError）→ tool_result 自包含 [拦截] 详情块
                    # execution 阶段错误 → tool_result 收据
                    is_error = True
                    error_class = te.category
                    if te.stage == ErrorStage.PRE_EXECUTION:
                        result = te.to_system_block()
                    else:
                        result = te.to_receipt()
                    logger.info(
                        "工具 %s 执行失败 [%s]: %s", tool_name, te.category, te.reason
                    )
                except Exception as e:
                    # 兜底：理论上 registry 已归一化，此处防御性处理
                    is_error = True
                    logger.error("工具执行失败 %s: %s", tool_name, e)
                    if from_exception is not None:
                        te = from_exception(tool_name, e)
                        error_class = te.category
                        result = te.to_receipt()
                    else:
                        result = f"工具执行出错: {e}"

                # Phase 9+ 错误分类：调用 ErrorClassifier 分类，记录到滑动窗口
                # （仅当 ToolError 未给出 error_class 时兜底，识别老 handler 字符串错误）
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
                                    "\n\n[系统提示：永久性错误，请勿以相同参数重试。]"
                                )
                            elif ec is ErrorClass.TRANSIENT:
                                result += (
                                    "\n\n[系统提示：临时性错误，可稍后重试。]"
                                )
                    except Exception as exc:
                        logger.debug("ErrorClassifier 分类失败: %s", exc)
                # ErrorClassifier 识别出错误 → 同步 is_error，确保 metrics/audit 准确
                if error_class is not None:
                    is_error = True
                    if loop.metrics is not None:
                        loop.metrics.observe_tool_error_class(tool_name, error_class)
                        # Phase 2 反馈监控：错误分类识别即视为一次重试信号
                        loop.metrics.observe_tool_retry(tool_name)
                # 执行后记录到滑动窗口（含 error_class）
                recent_tool_calls.append((tool_name, params_hash, error_class))

                # Phase 元认知 Task 1: Agent 自画像失败信号触发（与 run() 对称）
                # 连续失败≥2 次 或 PERMANENT 错误类 → 触发 agent 信号入池（仅一次/run）
                # 成功时重置计数器；触发后不再重复入池（signal_pool 内部仍有去重）
                if is_error:
                    consecutive_tool_failures += 1
                    should_trigger = (
                        not agent_failure_triggered
                        and (
                            consecutive_tool_failures >= 2
                            or error_class == "permanent"
                        )
                    )
                    if should_trigger:
                        triggered = loop._maybe_trigger_agent_failure_signal(
                            session_id=session_id,
                            tool_name=tool_name,
                            error_class=error_class,
                            consecutive_failures=consecutive_tool_failures,
                        )
                        if triggered:
                            agent_failure_triggered = True
                else:
                    consecutive_tool_failures = 0

                # Phase 9 Task 6: 工具返回值脱敏（fail-open 软护栏）
                # 对外部工具返回值做注入模式替换 + 边界标记，
                # 可信工具直返原值。GuardrailEngine 内部已 try/except fail-open。
                # 脱敏后的 result 同时用于 tool_results、审计日志与 yield 给前端。
                result = loop.guardrail_engine.sanitize_tool_result(
                    result, tool_name=tool_name
                )

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
                loop._log_audit(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    result=result,
                    is_error=is_error,
                    duration_ms=duration_ms,
                    decision=decision,
                )
                if loop.metrics is not None:
                    loop.metrics.observe_tool_call(tool_name, not is_error, duration_ms)
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
                loop._info_count += 1

            # 检测点 4：工具执行后检查 cancel_event（run_stream 专用）
            if cancel_event and cancel_event.is_set():
                yield _build_done_event_fn(
                    response=last_text,
                    messages=messages,
                    is_complete=False,
                    termination_reason="user_cancel",
                    usage=current_round_usage,
                    content_blocks=content_blocks,
                    stop_reason=stop_reason,
                    reasoning_cfg=reasoning_cfg,
                )
                return

            messages.append({"role": "user", "content": tool_results})

            # === 下一轮 LLM 调用前 ===
            # 继续下一轮循环（LLM 会基于工具结果再次流式输出）

        # 达到 max_loops 仍未完成，触发总结调用
        logger.warning(
            "React 循环达到最大次数 %d，触发总结调用", loop.max_loops
        )
        summary_text = await loop._generate_max_loops_summary(
            messages, last_text, session_id
        )
        # max_loops 耗尽 → is_complete=False
        yield _build_done_event_fn(
            response=summary_text,
            messages=messages,
            is_complete=False,
            termination_reason="max_loops",
            reasoning_cfg=reasoning_cfg,
        )

