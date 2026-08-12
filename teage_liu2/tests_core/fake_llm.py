"""Fake LLM 客户端:脚本化响应,不依赖真实 SDK / 网络。

用法:
    FakeLLMClient([
        {"content": [{"type": "text", "text": "你好"}], "stop_reason": "end_turn"},
        lambda messages: {"content": [...], "stop_reason": "tool_use"},  # callable 按消息动态响应
    ])
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Dict, List, Optional

# 响应脚本:dict 或 callable(messages) -> dict
ScriptItem = Dict[str, Any] | Callable[[List[Dict[str, Any]]], Dict[str, Any]]


class FakeLLMClient:
    """按脚本顺序消费预设响应的 LLM 客户端(鸭子类型,兼容 ChatPipeline)。"""

    def __init__(self, script: Optional[List[ScriptItem]] = None) -> None:
        self._script: List[ScriptItem] = list(script or [])
        self.calls: int = 0
        self.last_messages: Optional[List[Dict[str, Any]]] = None
        self.last_tools: Optional[List[Dict[str, Any]]] = None
        self.last_system: Optional[str] = None
        self.activity_timeout: float = 60.0
        self.stream_total_timeout: float = 300.0

    def _next_response(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        if self._script:
            item = self._script.pop(0)
            if callable(item):
                return item(messages)
            return item
        return {"content": [], "stop_reason": "end_turn"}

    async def chat_main_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[Any] = None,
        activity_timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        self.calls += 1
        self.last_messages = [dict(m) for m in messages]
        self.last_tools = list(tools) if tools else None
        self.last_system = system

        resp = self._next_response(messages)
        content_blocks = resp.get("content", []) or []
        stop_reason = resp.get("stop_reason", "end_turn")

        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    yield {"type": "text", "text": text}
            # reasoning 增量可选模拟
            if block.get("type") == "reasoning_delta":
                text = block.get("text", "")
                if text:
                    yield {"type": "reasoning", "text": text, "signature": None}
        yield {
            "type": "done",
            "stop_reason": stop_reason,
            "content_blocks": content_blocks,
            "usage": resp.get("usage"),
        }

    async def chat_main(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """同步式调用(供非流式路径;M1 主路径为流式)。"""
        text = ""
        stop_reason = "end_turn"
        async for ev in self.chat_main_stream(
            messages, tools=tools, system=system, max_tokens=max_tokens
        ):
            if ev.get("type") == "text":
                text += ev.get("text", "")
            elif ev.get("type") == "done":
                stop_reason = ev.get("stop_reason", "end_turn")
        from ..core.llm import LLMResponse

        return LLMResponse(
            content=[{"type": "text", "text": text}] if text else [],
            stop_reason=stop_reason,
        )

    def close(self) -> None:
        pass
