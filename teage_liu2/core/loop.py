"""循环形态(阶段 2 协议化):在 step(单次 LLM 调用)之上驱动 ReAct 循环。

对齐设计文档 §4.4(增量收口)/§8(工具路径四连)/§5(状态流转):
- 每轮 step 前:inject_round 全收集 → 增量收口(merge + 语义校验)→ 送 LLM
- step 后:append assistant 到快照(revision+1)→ after_step(action 立即应用)
- 工具路径四连(§8):pre_tool_call 链(整体前移到 tool_use 事件之前)→
  tool_use 事件(携带 original_input + effective_input)→ on_tool_call 执行 →
  tool_result 事件(modified 标记)→ 回喂 → 下一轮前过增量收口
- 终止原因:normal / max_loops / no_tool_executor / tool_rejected / user_cancel
- 坑知识保留:孤立 assistant(tool_calls) 清理(防下轮 LLM 400)
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from .assembler import incremental_finalize
from .errors import (
    TERMINATION_INTERCEPTED,
    TERMINATION_MAX_LOOPS,
    TERMINATION_NORMAL,
    TERMINATION_NO_TOOL_EXECUTOR,
    TERMINATION_USER_CANCEL,
)
from .hooks import HookChain, no_executor
from .llm import LLMClient
from .step import StepExecutor
from .types import (
    EV_DONE,
    EV_ERROR,
    EV_STEP_END,
    EV_TOOL_RESULT,
    EV_TOOL_USE,
    Event,
    Message,
    Snapshot,
    StepSummary,
    split_content_blocks,
    tool_result_block,
)
from .types import extract_text

logger = logging.getLogger(__name__)


def drop_trailing_orphan_tool_calls(messages: List[Message]) -> List[Message]:
    """清理消息末尾无 tool_result 应答的 assistant(tool_calls)。

    终止路径(no_tool_executor / max_loops)会留下未应答的 tool_use 块,
    若不清理,下轮 LLM 调用时 Anthropic/OpenAI 会 400。
    """
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "assistant":
        return messages
    content = last.get("content")
    if not isinstance(content, list):
        return messages
    if not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
        return messages
    # 末尾 assistant 含 tool_use 且其 id 无对应 tool_result → 整条剥除
    return messages[:-1]


class ReactLoop:
    """循环形态:step + 工具派发 + 事件流(阶段 2:Snapshot+Action 流转)。"""

    def __init__(
        self,
        llm_client: LLMClient,
        hooks: HookChain,
        max_loops: int = 50,
        activity_timeout: Optional[float] = None,
        stream_total_timeout: Optional[float] = None,
    ) -> None:
        self.step = StepExecutor(
            llm_client,
            activity_timeout=activity_timeout,
            stream_total_timeout=stream_total_timeout,
        )
        self.hooks = hooks
        self.max_loops = max_loops
        #: 最终快照(§5 会话态 extra 写回依据):每轮推进后更新,结束时为对话终态
        self.final_snapshot: Optional[Snapshot] = None

    async def run_stream(
        self,
        snapshot: Snapshot,
        messages: List[Message],
        system: Optional[str] = None,
        cancel_event: Optional[Any] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Event]:
        """驱动循环,逐个 yield 事件,以 done 或 error 收尾(E6 统一接口)。

        :param snapshot: 当前快照(对话级状态,不可变;本方法内部推进产生新快照)
        :param messages: 收口四连后的有效消息序列(送 LLM 用,含对话级注入)
        """
        cur = snapshot
        self.final_snapshot = cur
        msgs = list(messages)
        last_text = ""
        # 本轮产出初始化在 for 之前:首轮前取消分支也引用这些值,
        # 避免 UnboundLocalError(P1-1 回归锚定)
        content_blocks: List[Dict[str, Any]] = []
        stop_reason = "end_turn"
        usage: Optional[Dict[str, Any]] = None

        for step_idx in range(self.max_loops):
            cur = cur.with_round(step_idx + 1)  # E3:轮次号每轮递增(启动 0)
            self.final_snapshot = cur

            if cancel_event is not None and cancel_event.is_set():
                yield self._done(
                    cur, last_text, msgs, TERMINATION_USER_CANCEL,
                    usage, content_blocks, stop_reason,
                )
                return

            # 轮次间注入(§4.2 全收集合并)→ 增量收口(§4.4 不变量)
            round_injections = await self.hooks.inject_round(cur)
            round_messages, problem = incremental_finalize(msgs, round_injections)
            if problem:
                yield {
                    "type": EV_ERROR,
                    "session_id": cur.session_id,
                    "message": f"增量收口语义校验失败: {problem}",
                }
                return

            # 一轮:单次 LLM 调用
            step_completed = False
            step_started = time.monotonic()
            async for ev in self.step.execute(
                round_messages,
                tools=cur.tools or None,
                system=system,
                cancel_event=cancel_event,
                session_id=cur.session_id,
            ):
                if ev.get("type") == EV_STEP_END:
                    content_blocks = ev.get("content_blocks", []) or []
                    stop_reason = ev.get("stop_reason", "end_turn") or "end_turn"
                    usage = ev.get("usage")
                    step_completed = True
                # 全部事件透传(含 step_end:事件流契约,前端/观测枝干/落盘依赖)
                yield ev
                if ev.get("type") == EV_ERROR:
                    return

            if not step_completed:
                # error 已 yield,循环终止
                return

            duration = time.monotonic() - step_started
            text_parts, tool_use_blocks, _ = split_content_blocks(content_blocks)
            if text_parts:
                last_text = "".join(text_parts)

            # step 后:append assistant 到快照(revision+1,结构共享)
            cur = cur.with_messages(
                list(cur.messages) + [{"role": "assistant", "content": content_blocks}]
            )
            self.final_snapshot = cur
            summary = StepSummary(
                round_=cur.round,
                text=last_text,
                content_blocks=content_blocks,
                tool_uses=tool_use_blocks,
                usage=usage,
                duration=duration,
            )
            # after_step(注册序,action 立即应用;append 影响下一轮送 LLM 前的增量收口)
            cur = await self.hooks.after_step_all(cur, summary)
            self.final_snapshot = cur
            msgs = list(cur.messages)

            # 自然结束(end_turn)→ 完成
            if stop_reason != "tool_use" or not tool_use_blocks:
                yield self._done(
                    cur, last_text, msgs, TERMINATION_NORMAL,
                    usage, content_blocks, stop_reason,
                    is_complete=True,
                )
                return

            # tool_use:工具路径四连(§8)
            tool_results: List[Dict[str, Any]] = []
            for tb in tool_use_blocks:
                tool_name = tb.get("name", "")
                tool_input = tb.get("input", {}) or {}
                tool_use_id = tb.get("id", "")

                # ① pre_tool_call 链(reject 短路 / modify 叠加)
                decision, effective_input = await self.hooks.pre_tool_call_all(
                    cur, tool_name, tool_input
                )
                # ② tool_use 事件(携带 original_input + effective_input)
                tool_use_event: Dict[str, Any] = {
                    "type": EV_TOOL_USE,
                    "session_id": cur.session_id,
                    "name": tool_name,
                    "input": tool_input,
                }
                if effective_input != tool_input:
                    tool_use_event["original_input"] = tool_input
                    tool_use_event["effective_input"] = effective_input
                yield tool_use_event

                if decision.is_reject:
                    # 有执行者但被策略拒绝 → tool_rejected(§8)
                    logger.warning(
                        "工具 %s 被策略拒绝(pre_tool_call),tool_rejected 回喂",
                        tool_name,
                    )
                    yield {
                        "type": EV_TOOL_RESULT,
                        "session_id": cur.session_id,
                        "name": tool_name,
                        "tool_use_id": tool_use_id,
                        "result": f"工具被策略拒绝: {decision.input or ''}",
                        "is_error": True,
                        "termination_reason": "tool_rejected",
                    }
                    tool_results.append(
                        tool_result_block(
                            tool_use_id,
                            f"工具被策略拒绝: {decision.input or ''}",
                            is_error=True,
                        )
                    )
                    continue

                # ③ on_tool_call 执行
                result = await self.hooks.dispatch_tool_call(
                    cur, tool_name, effective_input
                )
                exec_duration = time.monotonic() - step_started

                if no_executor(result):
                    # 无枝干可执行:友好终止(保留已产出的文本)
                    logger.warning(
                        "模型请求工具 %s 但无枝干执行,友好终止", tool_name
                    )
                    cur = cur.with_messages(drop_trailing_orphan_tool_calls(cur.messages))
                    self.final_snapshot = cur
                    yield self._done(
                        cur, last_text, list(cur.messages), TERMINATION_NO_TOOL_EXECUTOR,
                        usage, content_blocks, stop_reason,
                    )
                    return

                # post_tool_call(工具执行后,action 立即应用)
                cur = await self.hooks.post_tool_call_all(
                    cur, tool_name, effective_input, result, exec_duration
                )
                self.final_snapshot = cur

                # ④ tool_result 事件(modified 标记)
                is_error = isinstance(result, Exception)
                result_text = (
                    str(result) if not is_error else f"工具执行出错: {result}"
                )
                tool_result_event: Dict[str, Any] = {
                    "type": EV_TOOL_RESULT,
                    "session_id": cur.session_id,
                    "name": tool_name,
                    "tool_use_id": tool_use_id,  # 配对落盘依据(D1 消息级落盘)
                    "result": result_text,
                    "is_error": is_error,
                }
                if effective_input != tool_input:
                    tool_result_event["modified"] = True
                yield tool_result_event

                tool_results.append(
                    tool_result_block(tool_use_id, result_text, is_error=is_error)
                )

            # 回喂工具结果,进入下一轮(下一轮前过增量收口)
            cur = cur.with_messages(
                list(cur.messages)
                + [{"role": "user", "content": tool_results}]
            )
            self.final_snapshot = cur
            msgs = list(cur.messages)

            # 轮中 SetStop(after_step/post_tool_call 置 stop)→ 拦截终止
            if cur.stop:
                logger.info("轮中 SetStop,对话被拦截(%s)", cur.stop_reason)
                yield self._done(
                    cur, last_text, msgs, TERMINATION_INTERCEPTED,
                    usage, content_blocks, stop_reason,
                )
                return

        # max_loops 耗尽
        logger.warning("React 循环达到最大次数 %d", self.max_loops)
        cur = cur.with_messages(drop_trailing_orphan_tool_calls(cur.messages))
        self.final_snapshot = cur
        yield self._done(
            cur, last_text, list(cur.messages), TERMINATION_MAX_LOOPS,
            usage, content_blocks, stop_reason,
        )

    @staticmethod
    def _done(
        snapshot: Snapshot,
        response: str,
        messages: List[Message],
        termination_reason: str,
        usage: Optional[Dict[str, Any]],
        content_blocks: List[Dict[str, Any]],
        stop_reason: str,
        is_complete: bool = False,
    ) -> Event:
        """done 事件统一 9 键(§3.1:done 9 键齐整)。"""
        return {
            "type": EV_DONE,
            "session_id": snapshot.session_id,
            "response": response,
            "messages": messages,
            "is_complete": is_complete,
            "termination_reason": termination_reason,
            "usage": usage,
            "content_blocks": content_blocks,
            "stop_reason": stop_reason,
        }
