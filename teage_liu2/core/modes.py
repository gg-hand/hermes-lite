"""形态(E6,计划 §4.2):统一接口 run_stream(ctx, messages, system) → 事件流。

pipeline 按 mode 字典选择(bare/loop);新形态 = 新类(实现 run_stream)+
一行注册。统一事件源契约:必以 done 或 error 收尾。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from .types import (
    EV_DONE,
    EV_STEP_END,
    TERMINATION_NORMAL,
    Event,
    Message,
)

# 形态常量
MODE_BARE = "bare"     # 单次 LLM 调用,无循环
MODE_LOOP = "loop"     # React 循环(默认)


class BareMode:
    """裸形态:单次 LLM 调用,主干在 step_end 后补发 done。"""

    def __init__(self, step_executor: Any) -> None:
        self.step = step_executor
        #: 最终快照(§5 会话态 extra 写回依据):单步形态无推进,恒为输入快照
        self.final_snapshot: Any = None

    async def run_stream(
        self,
        snapshot: Any,
        messages: List[Message],
        system: Optional[str] = None,
        cancel_event: Optional[Any] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Event]:
        """单轮执行:透传 step 事件,step_end 后补发 done 收尾。"""
        self.final_snapshot = snapshot
        async for ev in self.step.execute(
            messages,
            tools=None,
            system=system,
            cancel_event=cancel_event,
            session_id=session_id,
        ):
            if ev.get("type") == EV_STEP_END:
                stop_reason = ev.get("stop_reason", "end_turn")
                yield {
                    "type": EV_DONE,
                    "session_id": session_id,
                    "response": "",
                    "messages": messages,
                    "is_complete": stop_reason == "end_turn",
                    "termination_reason": TERMINATION_NORMAL,
                    "usage": ev.get("usage"),
                    "content_blocks": ev.get("content_blocks", []),
                    "stop_reason": stop_reason,
                }
                return
            yield ev
