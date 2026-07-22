"""同步 React 循环执行器。

从 ReactLoop.run 提取，负责非流式 React 循环执行：
LLM 调用 → 工具执行 → 卡死检测 → 终止判定。
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
)

logger = logging.getLogger(__name__)


class SyncRunner:
    """同步 React 循环执行器。

    持有 ReactLoop 引用，通过属性访问组件。
    """

    def __init__(self, loop: "ReactLoop") -> None:
        self.loop = loop

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
        """执行 React 循环。

        参数:
            user_input: 当前用户输入文本。
            history: 历史消息列表 [{role, content}]，可为 None。
                     history 中的消息不参与信息计数器累加。
            system: 系统提示词，若为 None 则不发送 system 字段。
            session_id: 可选会话 ID，由 orchestrator 透传,
                     非流式模式仅作占位（不 yield 事件），保留以便日志关联。
            tools_override: Phase 8 Task 5.7。工具 schema 覆盖列表。为 ``None``
                     时使用 ``self.tool_registry.get_tools_schema()``（默认
                     用户会话路径）。非 ``None`` 时直接使用此列表，用于 cron
                     调度会话按 ``active_tools_snapshot`` 过滤后的请求级隔离。
            cancel_event: Phase 9+ 可选取消事件（threading.Event），由
                     Orchestrator/StreamManager 透传。被 set 时在循环的三个
                     检测点终止执行，返回 is_complete=False。
            reasoning_cfg: 可选 ReasoningConfig，None 时由 LLMClient 按
                     ``is_cron`` 选择默认配置。
            is_cron: 标记是否为 cron 会话，影响 reasoning_cfg 默认选择。

        返回:
            (final_response, messages_used, is_complete, termination_reason):
                final_response: 最终回复文本（若全程无文本则返回空串）。
                messages_used: 整个循环中使用过的完整消息列表（含历史与新增）。
                is_complete: 本轮是否自然完成。Phase 9 Task 7.4：
                    - ``True``：模型返回 end_turn 自然结束；
                    - ``False``：达到 max_loops、卡死终止或被取消，由 orchestrator
                      检查 TodoList 决定是否自动续接。
                termination_reason: 终止原因，取值：
                    - ``"normal"``：模型 end_turn 或 LLM 失败降级
                    - ``"user_cancel"``：cancel_event 被设置
                    - ``"tool_permanent_fail"``：工具卡死检测命中
                    - ``"max_loops"``：达到最大循环次数
        """
        loop = self.loop

        # 批次 2.4: 重置 last_usage，避免跨调用污染
        loop.last_usage = None

        # 1. 构建 messages = history + [user_input]
        messages: List[Dict[str, Any]] = []
        if history:
            # 浅拷贝避免外部修改影响传入历史
            messages.extend(dict(m) for m in history)
        messages.append({"role": "user", "content": user_input})

        # 用户输入计入信息计数器
        loop._info_count += 1

        # Phase 元认知 Task 1: 检测用户对失败的口头反馈（"又错了"/"上次说过"等），
        # 触发 Agent 自画像信号入池。非阻塞，触发后继续主流程。
        loop._check_user_failure_feedback(user_input, session_id)

        # 2. 获取工具 schema（tool_registry 为 None 时纯对话模式）
        # Phase 8 Task 5.7: tools_override 优先（cron 路径请求级过滤）
        tools: Optional[List[Dict[str, Any]]] = None
        if tools_override is not None:
            # 浅拷贝避免外部修改污染调用方持有的列表
            tools = list(tools_override)
        elif loop.tool_registry is not None:
            try:
                tools = loop.tool_registry.get_tools_schema()
            except Exception as e:
                logger.warning("获取工具 schema 失败，降级为纯对话模式: %s", e)
                tools = None

        last_text: str = ""

        # Phase 9 Task 7.3 + Phase 9+ 错误分类: 单工具重试检测滑动窗口
        # 维护最近 5 次工具调用记录 (tool_name, params_hash, error_class)，
        # error_class 为 ErrorClass.value（"anti_crawler"/"permanent"等）或 None。
        # 窗口在本次 run() 内维护，不跨 run 调用持久化。
        recent_tool_calls: List[Tuple[str, str, Optional[str]]] = []
        # 卡死检测软警告状态机：首次命中重复 → warn（tool_result 返回警告，
        # 不执行工具，继续循环给 LLM 自我纠正机会）；二次命中 → stop（硬终止）。
        # (tool_name, params_hash) 对的集合，per-run 局部状态。
        warned_pairs: set = set()
        # Phase 元认知 Task 1: Agent 自画像失败信号触发状态（per-run 局部）
        # consecutive_tool_failures: 连续工具失败次数，is_error 时递增，成功时重置
        # agent_failure_triggered: 本轮是否已触发过 Agent 失败信号入池，
        #   避免同一 run 内重复入池（多次失败仅入池一次，由 signal_pool 内部去重）
        consecutive_tool_failures: int = 0
        agent_failure_triggered: bool = False

        for loop_idx in range(loop.max_loops):
            # 检测点 1：每轮开始前检查 cancel_event
            if cancel_event and cancel_event.is_set():
                return last_text, messages, False, "user_cancel"

            # 3. 调用主对话 LLM
            try:
                response = await loop.llm_client.chat_main(
                    messages=messages,
                    tools=tools,
                    system=system,
                    reasoning_cfg=reasoning_cfg,
                    is_cron=is_cron,
                )
            except Exception as e:
                logger.error("LLM 调用失败 (loop=%d): %s", loop_idx, e)
                # 若已有文本回复，降级返回；否则向上抛出
                if last_text:
                    return last_text, messages, False, "normal"
                raise

            # 批次 2.4: 累积 LLM usage 到 loop.last_usage，供 chat_handler 回填 token_count
            # 多轮工具调用时 input/output tokens 求和，反映整个 react 循环的总消耗
            _resp_usage = getattr(response, "usage", None)
            if _resp_usage and isinstance(_resp_usage, dict):
                if loop.last_usage is None:
                    loop.last_usage = {"input_tokens": 0, "output_tokens": 0}
                loop.last_usage["input_tokens"] += _resp_usage.get("input_tokens", 0)
                loop.last_usage["output_tokens"] += _resp_usage.get("output_tokens", 0)

            # 解析响应
            content_blocks = getattr(response, "content", []) or []
            stop_reason = getattr(response, "stop_reason", None)

            # 提取文本与工具调用块，并将原始 block 转为 dict 便于回传
            text_parts: List[str] = []
            tool_use_blocks: List[Dict[str, Any]] = []
            assistant_content: List[Dict[str, Any]] = []

            for block in content_blocks:
                block_dict = _block_to_dict_fn(block)
                assistant_content.append(block_dict)
                btype = block_dict.get("type")
                if btype == "text":
                    text = block_dict.get("text", "")
                    if text:
                        text_parts.append(text)
                elif btype == "tool_use":
                    tool_use_blocks.append(block_dict)

            # assistant 响应计入信息计数器
            loop._info_count += 1

            # 将 assistant 完整响应（含 text 与 tool_use 块）加入 messages
            messages.append({"role": "assistant", "content": assistant_content})

            if text_parts:
                last_text = "".join(text_parts)

            # 4. 判断是否需要工具调用
            # stop_reason != "tool_use" 或无 tool_use 块时，视为最终回复
            if stop_reason != "tool_use" or not tool_use_blocks:
                # 自然结束（end_turn）→ is_complete=True
                return last_text, messages, True, "normal"

            # 响应包含 tool_use，但未提供 tool_registry：终止循环
            if loop.tool_registry is None:
                logger.warning(
                    "模型请求工具调用但未提供 tool_registry，返回当前文本回复"
                )
                # 清理末尾未执行的 assistant(tool_calls)，避免下轮 400
                messages = _drop_trailing_orphan_tool_calls_fn(messages)
                return last_text, messages, False, "normal"

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
                        # Phase B-4: stash 中断提示到下轮（当轮循环终止，无注入时机）
                        # 通过 orchestrator._pending_interrupt_notices 在下一轮 chat() 注入
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
                        return (
                            _build_stuck_message_fn(tool_name, stuck_reason),
                            messages,
                            False,
                            "tool_permanent_fail",
                        )
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
                    loop._info_count += 1
                    continue

                # Phase 5: 策略评估
                decision = loop._evaluate_policy(
                    tool_name, tool_input, session_id=session_id
                )

                if decision.action == "deny":
                    # Phase B: 拒绝走 pre_execution system 注入路径
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
                    # 审计与指标上报（拒绝视为错误，is_error=True）
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
                    loop._info_count += 1
                    # 拒绝路径不计入重试检测窗口（工具未实际执行）
                    continue
                elif decision.action == "confirm":
                    # 非流式模式不支持 HIL，自动拒绝（走 pre_execution system 注入）
                    logger.warning(
                        "非流式 run() 命中 confirm 但不支持 HIL，自动拒绝: tool=%s",
                        tool_name,
                    )
                    hil_err = NonStreamHILError(
                        tool_name=tool_name,
                        reason="非流式模式不支持 HIL 审批",
                        suggestion="改用 /chat/stream 端点以支持审批",
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
                        # 注意：不调用 observe_tool_retry，拦截不是重试（见边界 9.13）
                    loop._info_count += 1
                    continue

                # action == "allow"：正常执行
                # 检测点 2：工具执行前检查 cancel_event
                if cancel_event and cancel_event.is_set():
                    messages = _drop_trailing_orphan_tool_calls_fn(messages)
                    return last_text, messages, False, "user_cancel"

                # Phase 8 Task 5.7: 通过 _execute_tool_with_dispatch 派发到
                # cron_tool_registry（子进程）或 tool_registry（全局）。
                error_class: Optional[str] = None
                try:
                    result = loop._execute_tool_with_dispatch(tool_name, tool_input, cancel_event)
                except ToolError as te:
                    # 统一异常层次：registry 抛 ToolError 子类，按 stage 分流
                    is_error = True
                    error_class = te.category
                    if te.stage == ErrorStage.PRE_EXECUTION:
                        # pre_execution 错误（如 ParamError）→ tool_result 自包含 [拦截] 详情块
                        result = te.to_system_block()
                    else:
                        # execution 阶段错误 → tool_result 收据
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
                            # 添加策略提示
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

                # Phase 元认知 Task 1: Agent 自画像失败信号触发
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
                result = loop.guardrail_engine.sanitize_tool_result(
                    result, tool_name=tool_name
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result,
                        **({"is_error": True} if is_error else {}),
                    }
                )
                duration_ms = (time.perf_counter() - t0) * 1000
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
                # 每条 tool_result 计入信息计数器
                loop._info_count += 1

            # 检测点 3：工具执行后、下一轮 LLM 调用前检查 cancel_event
            if cancel_event and cancel_event.is_set():
                return last_text, messages, False, "user_cancel"

            messages.append({"role": "user", "content": tool_results})

            # === 下一轮 LLM 调用前 ===
            # 继续下一轮循环

        # 达到 max_loops 仍未完成，触发总结调用
        logger.warning(
            "React 循环达到最大次数 %d，触发总结调用", loop.max_loops
        )
        summary_text = await loop._generate_max_loops_summary(
            messages, last_text, session_id
        )
        # max_loops 耗尽 → is_complete=False（orchestrator 检查 TodoList 决定续接）
        return summary_text, messages, False, "max_loops"

