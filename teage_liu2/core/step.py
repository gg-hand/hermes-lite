"""单步执行器:一次 LLM 调用 → 事件流(裸形态)。

- yield: step_start → (text_delta / reasoning_delta 透传) → step_end
- step_end 携带 content_blocks / stop_reason / usage,供 loop 判断是否继续
- LLM 异常(ActivityTimeout / StreamCancelled / 其他)→ yield error 事件后返回
  (不抛);错误语义由事件类型表达
- stream_total_timeout 兜底:整体超时(与 per-token 超时互补)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from .errors import (
    LLM_API_ERROR,
    LLM_CANCELED,
    LLM_STREAM_FAILED,
    LLM_TIMEOUT,
)
from .llm import ActivityTimeout, LLMClient, StreamCancelled
from .types import (
    EV_ERROR,
    EV_REASONING_DELTA,
    EV_STEP_END,
    EV_STEP_START,
    EV_TEXT_DELTA,
    Event,
    Message,
)

logger = logging.getLogger(__name__)


class StepExecutor:
    """单步执行器:持有 LLMClient,一次调用产出一条事件流。"""

    def __init__(
        self,
        llm_client: LLMClient,
        activity_timeout: Optional[float] = None,
        stream_total_timeout: Optional[float] = None,
    ) -> None:
        self.llm_client = llm_client
        # None → 使用 LLMClient 从 config 读取的默认值
        self.activity_timeout = activity_timeout
        self.stream_total_timeout = stream_total_timeout

    async def execute(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        cancel_event: Optional[Any] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Event]:
        """执行一次 LLM 调用,逐个 yield 事件。

        正常结束以 step_end 收尾;异常以 error 收尾(不再 yield step_end,
        调用方据此判定本轮失败)。
        """
        yield {
            "type": EV_STEP_START,
            "session_id": session_id,
            "step": 0,
        }

        activity_timeout = self.activity_timeout
        if activity_timeout is None:
            activity_timeout = getattr(self.llm_client, "activity_timeout", 60.0)
        stream_total_timeout = self.stream_total_timeout
        if stream_total_timeout is None:
            stream_total_timeout = getattr(self.llm_client, "stream_total_timeout", 300.0)

        content_blocks: List[Dict[str, Any]] = []
        stop_reason: str = "end_turn"
        usage: Optional[Dict[str, Any]] = None

        try:
            # 流式总超时兜底(asyncio.timeout, Python 3.11+)
            async with asyncio.timeout(stream_total_timeout):
                async for ev in self.llm_client.chat_main_stream(
                    messages=messages,
                    tools=tools,
                    system=system,
                    cancel_event=cancel_event,
                    activity_timeout=activity_timeout,
                ):
                    etype = ev.get("type")
                    if etype == "text":
                        yield {"type": EV_TEXT_DELTA, "session_id": session_id, "text": ev.get("text", "")}
                    elif etype == "reasoning":
                        yield {
                            "type": EV_REASONING_DELTA,
                            "session_id": session_id,
                            "text": ev.get("text", ""),
                            "signature": ev.get("signature"),
                        }
                    elif etype == "done":
                        stop_reason = ev.get("stop_reason", "end_turn") or "end_turn"
                        content_blocks = ev.get("content_blocks", []) or []
                        usage = ev.get("usage")
        except ActivityTimeout:
            logger.warning("%s: LLM 响应超时(无输出 %s)", LLM_TIMEOUT, activity_timeout)
            yield {
                "type": EV_ERROR,
                "session_id": session_id,
                "message": f"LLM 响应超时({activity_timeout}s 无输出)",
                "code": LLM_TIMEOUT,
            }
            return
        except StreamCancelled:
            logger.info("%s: LLM 流被用户取消", LLM_CANCELED)
            yield {
                "type": EV_ERROR,
                "session_id": session_id,
                "message": "对话已取消",
                "code": LLM_CANCELED,
            }
            return
        except asyncio.TimeoutError:
            logger.error(
                "%s: LLM 流式调用总超时(>%.1fs)", LLM_STREAM_FAILED, stream_total_timeout
            )
            yield {
                "type": EV_ERROR,
                "session_id": session_id,
                "message": f"LLM 响应超时(> {stream_total_timeout}s)",
                "code": LLM_STREAM_FAILED,
            }
            return
        except Exception as e:
            logger.error("%s: LLM 调用失败: %s", LLM_API_ERROR, e)
            yield {
                "type": EV_ERROR,
                "session_id": session_id,
                "message": f"LLM 调用失败: {e}",
                "code": LLM_API_ERROR,
            }
            return

        yield {
            "type": EV_STEP_END,
            "session_id": session_id,
            "content_blocks": content_blocks,
            "stop_reason": stop_reason,
            "usage": usage,
        }
