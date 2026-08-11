"""AgentDirector：LLM 驱动的 Director 实现（Task 5）。

Agent 自主判断何时注入 directive。实际实现需要注入 orchestrator 来调用
LLM。当前为简化实现：inject_directive 直接写入 collaboration.md，
handle_arbitration 调用 orchestrator.chat 处理仲裁。
"""
from __future__ import annotations

from pathlib import Path

from teage_liu.multiagent.blackboard import (
    append_collab_message,
    read_collab_messages,
)
from teage_liu.multiagent.director_protocol import DirectorProtocol


class AgentDirector(DirectorProtocol):
    """LLM 驱动的 Director。

    Agent 自主判断何时注入 directive。orchestrator 可选，为 None 时
    handle_arbitration 返回 no_orchestrator 状态。
    """

    def __init__(self, bb_root: Path, orchestrator=None):
        self._bb_root = bb_root
        self._orchestrator = orchestrator

    async def inject_directive(
        self,
        content: str,
        rule_type: str,
        target: str = "*",
        priority: str = "normal",
        deadline: int | None = None,
    ) -> None:
        """注入 directive（issued_by=AgentDirector）。"""
        message: dict = {
            "from": "director",
            "type": "directive",
            "content": content,
            "rule_type": rule_type,
            "target": target,
            "priority": priority,
            "issued_by": "AgentDirector",
        }
        if deadline is not None:
            message["deadline"] = deadline
        await append_collab_message(self._bb_root, message)

    async def handle_arbitration(self, request: dict) -> dict:
        """LLM 处理仲裁请求。"""
        if self._orchestrator is None:
            return {"status": "no_orchestrator", "request": request}

        result = await self._orchestrator.chat(
            f"作为 Director，请处理以下仲裁请求：{request}"
        )
        return {"status": "llm_resolved", "result": result, "request": request}

    async def observe(self) -> dict:
        """观察协作状态。"""
        messages = await read_collab_messages(self._bb_root)
        return {"message_count": len(messages), "messages": messages}
