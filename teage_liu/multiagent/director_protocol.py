"""Director 接口协议（Task 5）。

Director 是"按需插入者"，默认静默观察，只在需要时插入 directive。
三种实现：AgentDirector（LLM驱动）/ UserDirector（用户手动）/ ScriptDirector（脚本规则）。

设计文档 §6 Director 接口化。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class DirectorProtocol(ABC):
    """Director 协议：定义干预 agent 协作的标准操作。

    Director 不是固定角色，而是可插拔的接口协议。所有实现必须提供：
    - inject_directive: 主动注入协作引导（双通道：collaboration.md + LLM 上下文）
    - handle_arbitration: 处理 agent 的仲裁请求（被动响应）
    - observe: 观察协作状态（用于异常检测）
    """

    @abstractmethod
    async def inject_directive(
        self,
        content: str,
        rule_type: str,
        target: str = "*",
        priority: str = "normal",
        deadline: int | None = None,
    ) -> None:
        """主动注入协作引导（双通道）。

        1. 写入 collaboration.md（可审计记录）
        2. 注入目标 agent 的 LLM 对话上下文（agent 自然感知，由 WorkerAdapter 适配层处理）

        Args:
            content: 引导内容（自然语言描述的协作约束）
            rule_type: ordering（顺序约束）/ constraint（规则约束）/ intervention（应急干预）
            target: 目标 agent_id 或 "*"（全部）
            priority: 优先级（high/normal/low），字段级强化用
            deadline: 期望生效时限（秒），字段级强化用
        """

    @abstractmethod
    async def handle_arbitration(self, request: dict) -> dict:
        """处理 agent 的仲裁请求（被动响应）。

        当 agent 间出现分歧时，可向 Director 发起仲裁请求。

        Returns:
            包含 status 字段的字典，如 {"status": "pending_user", "request": ...}
        """

    @abstractmethod
    async def observe(self) -> dict:
        """观察协作状态（用于异常检测）。

        Returns:
            包含 message_count 和 messages 字段的字典
        """
