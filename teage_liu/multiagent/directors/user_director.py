"""UserDirector：用户手动干预的 Director 实现（Task 5）。

用户通过工作台 UI 注入 directive。handle_arbitration 返回 pending_user
状态，由前端处理实际仲裁。
"""
from __future__ import annotations

from pathlib import Path

from teage_liu.multiagent.blackboard import (
    append_collab_message,
    read_collab_messages,
)
from teage_liu.multiagent.director_protocol import DirectorProtocol


class UserDirector(DirectorProtocol):
    """用户手动坐镇的 Director。

    用户通过工作台 UI 注入 directive。仲裁请求返回 pending_user，
    等待用户通过前端响应。
    """

    def __init__(self, bb_root: Path):
        self._bb_root = bb_root

    async def inject_directive(
        self,
        content: str,
        rule_type: str,
        target: str = "*",
        priority: str = "normal",
        deadline: int | None = None,
    ) -> None:
        """注入 directive 到 collaboration.md（issued_by=UserDirector）。"""
        message: dict = {
            "from": "director",
            "type": "directive",
            "content": content,
            "rule_type": rule_type,
            "target": target,
            "priority": priority,
            "issued_by": "UserDirector",
        }
        if deadline is not None:
            message["deadline"] = deadline
        await append_collab_message(self._bb_root, message)

    async def handle_arbitration(self, request: dict) -> dict:
        """用户处理仲裁请求（通过工作台 UI）。"""
        return {"status": "pending_user", "request": request}

    async def observe(self) -> dict:
        """观察协作状态。"""
        messages = await read_collab_messages(self._bb_root)
        return {"message_count": len(messages), "messages": messages}
