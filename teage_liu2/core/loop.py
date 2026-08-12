"""循环形态:在 step(单次 LLM 调用)之上驱动 ReAct 循环(可插拔扩展)。

设计(对齐设计文档 §7):
- 循环是"主干形态"而非 Branch:需要再次调用 LLM 的能力,且与事件流交织
- 循环**不引用任何工具枝干**,只通过 HookChain 触发 on_tool_call;
  工具枝干不存在(全部 NotImplemented)→ 友好终止(no_tool_executor)
- 无工具时自动单轮 = 裸形态行为(LLM 不 tool_use 即一次结束)
- 每轮 = 事件流中的一段(step_start/step_end);tool_use 与 tool_result
  事件穿插其中,最后以 done 收尾
- 坑知识保留:孤立 assistant(tool_calls) 清理(无 tool_result 应答的
  assistant 消息在终止路径上被剥除,避免下轮 LLM 400)

M1 裁剪:卡死检测(滑动窗口 md5 参数哈希)/ 策略评估 / 审批 / 审计 /
指标 —— 由 M2 tools 枝干与后续枝干带回。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from .hooks import BranchContext, HookChain, no_executor
from .llm import LLMClient
from .step import StepExecutor
from .types import (
    EV_DONE,
    EV_STEP_END,
    EV_TOOL_RESULT,
    EV_TOOL_USE,
    TERMINATION_MAX_LOOPS,
    TERMINATION_NO_TOOL_EXECUTOR,
    TERMINATION_NORMAL,
    Event,
    Message,
    split_content_blocks,
    tool_result_block,
)

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
    """循环形态:step + 工具派发 + 事件流。"""

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

    async def run_stream(
        self,
        ctx: BranchContext,
        messages: List[Message],
        system: Optional[str] = None,
        cancel_event: Optional[Any] = None,
    ) -> AsyncIterator[Event]:
        """驱动循环,逐个 yield 事件,以 done 或 error 收尾。"""
        last_text = ""

        for step_idx in range(self.max_loops):
            if cancel_event is not None and cancel_event.is_set():
                yield {
                    "type": EV_DONE,
                    "session_id": ctx.session_id,
                    "response": last_text,
                    "messages": messages,
                    "is_complete": False,
                    "termination_reason": "user_cancel",
                }
                return

            # 一轮:单次 LLM 调用
            content_blocks: List[Dict[str, Any]] = []
            stop_reason = "end_turn"
            usage: Optional[Dict[str, Any]] = None
            step_completed = False
            async for ev in self.step.execute(
                messages,
                tools=ctx.tools or None,
                system=system,
                cancel_event=cancel_event,
                session_id=ctx.session_id,
            ):
                if ev.get("type") == EV_STEP_END:
                    content_blocks = ev.get("content_blocks", []) or []
                    stop_reason = ev.get("stop_reason", "end_turn") or "end_turn"
                    usage = ev.get("usage")
                    step_completed = True
                else:
                    # 透传 step_start / text_delta / reasoning_delta / error
                    yield ev
                    if ev.get("type") == "error":
                        return

            if not step_completed:
                # error 已 yield,循环终止
                return

            text_parts, tool_use_blocks, _ = split_content_blocks(content_blocks)
            if text_parts:
                last_text = "".join(text_parts)

            messages = list(messages) + [{"role": "assistant", "content": content_blocks}]

            # 自然结束(end_turn)→ 完成
            if stop_reason != "tool_use" or not tool_use_blocks:
                yield {
                    "type": EV_DONE,
                    "session_id": ctx.session_id,
                    "response": last_text,
                    "messages": messages,
                    "is_complete": True,
                    "termination_reason": TERMINATION_NORMAL,
                    "usage": usage,
                    "content_blocks": content_blocks,
                    "stop_reason": stop_reason,
                }
                return

            # tool_use:派发给枝干执行
            tool_results: List[Dict[str, Any]] = []
            for tb in tool_use_blocks:
                tool_name = tb.get("name", "")
                tool_input = tb.get("input", {}) or {}
                tool_use_id = tb.get("id", "")

                yield {
                    "type": EV_TOOL_USE,
                    "session_id": ctx.session_id,
                    "name": tool_name,
                    "input": tool_input,
                }

                result = await self.hooks.dispatch_tool_call(ctx, tool_name, tool_input)
                if no_executor(result):
                    # 无枝干可执行:友好终止(保留已产出的文本)
                    logger.warning(
                        "模型请求工具 %s 但无枝干执行,友好终止", tool_name
                    )
                    messages = drop_trailing_orphan_tool_calls(messages)
                    yield {
                        "type": EV_DONE,
                        "session_id": ctx.session_id,
                        "response": last_text,
                        "messages": messages,
                        "is_complete": False,
                        "termination_reason": TERMINATION_NO_TOOL_EXECUTOR,
                        "usage": usage,
                        "content_blocks": content_blocks,
                        "stop_reason": stop_reason,
                    }
                    return

                is_error = isinstance(result, Exception)
                result_text = str(result) if not is_error else f"工具执行出错: {result}"
                tool_results.append(
                    tool_result_block(tool_use_id, result_text, is_error=is_error)
                )
                yield {
                    "type": EV_TOOL_RESULT,
                    "session_id": ctx.session_id,
                    "name": tool_name,
                    "result": result_text,
                    "is_error": is_error,
                }

            # 回喂工具结果,进入下一轮
            messages = list(messages) + [{"role": "user", "content": tool_results}]

        # max_loops 耗尽
        logger.warning("React 循环达到最大次数 %d", self.max_loops)
        messages = drop_trailing_orphan_tool_calls(messages)
        yield {
            "type": EV_DONE,
            "session_id": ctx.session_id,
            "response": last_text,
            "messages": messages,
            "is_complete": False,
            "termination_reason": TERMINATION_MAX_LOOPS,
        }
