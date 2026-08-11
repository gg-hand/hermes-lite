"""Director LLM 上下文注入机制（搭便车模式）。

设计原则：
- Director 写 directive 到 collaboration.md（可审计记录）
- 系统轮询发现新 directive 后入队"待注入队列"
- Agent 因任何原因调用 LLM 时，drain 队列拼到 system prompt
- 注入后清空队列，不重复注入
- 这是软约束，agent 自主决定遵循程度

与轮询 directive 的区别：
- 轮询模式：Worker 主动轮询 → 触发独立 LLM 调用（高成本）
- 注入模式：directive 入队 → 搭便车拼到已有 LLM 调用（零额外成本）
"""
from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DirectorInjector:
    """Director 上下文注入器。

    维护待注入的 directive 队列，agent 调用 LLM 时 drain 拼到 system prompt。
    """

    def __init__(
        self,
        bb_root: Path,
        agent_id: str,
        decentralized: bool = True,
    ) -> None:
        """初始化注入器。

        Args:
            bb_root: 黑板根目录
            agent_id: 本 agent 的 ID（用于过滤 target）
            decentralized: 是否启用 worker 协作去 director 中心化模式。
                True：注入内容改为「协作背景指导」，明确告知 worker 不请示 Director；
                False：保留旧逻辑（Director 协调引导）。
                受 worker_collab_decentralized 配置项控制，便于回滚。
        """
        self._bb_root = bb_root
        self._agent_id = agent_id
        self._decentralized = decentralized
        self._pending: deque[dict] = deque()
        self._last_seen_seq: int = 0

    async def enqueue_directive(self, directive: dict) -> None:
        """入队一条 directive。

        Args:
            directive: directive 消息 dict（含 from/type/content/rule_type/target/seq）
        """
        self._pending.append(directive)
        logger.debug(
            "Director directive 入队 (seq=%s): %s",
            directive.get("seq"), directive.get("content", "")[:80],
        )

    def drain_pending_directives(self) -> str:
        """drain 队列并返回格式化的 LLM 上下文片段。

        Returns:
            格式化的 directive 上下文片段，空队列返回空字符串。
            注入后清空队列。
        """
        if not self._pending:
            return ""

        blocks: list[str] = []
        while self._pending:
            d = self._pending.popleft()
            content = d.get("content", "")
            rule_type = d.get("rule_type", "guidance")
            ts = d.get("timestamp", "")
            issued_by = d.get("issued_by", "AgentDirector")

            if self._decentralized:
                # 改动点 1：worker 协作去 director 中心化模式
                # 注入内容从「Director 引导 / 协调和引导协作」改为
                # 「协作背景指导 / Director 仅提供任务背景，不审批方案，
                #  请直接与其他 worker 协商，不要回复或请示 Director」
                block = (
                    f"[协作背景指导]\n"
                    f"说明：Director 仅提供任务背景与指导，不审批方案。"
                    f"请直接与其他在线 worker 协商推进，不要回复或请示 Director。\n"
                    f"背景内容：{content}\n"
                    f"指导类型：{rule_type}\n"
                    f"来源：{issued_by} | 时间：{ts}"
                )
            else:
                # 旧逻辑：保留以支持回滚（worker_collab_decentralized=False）
                block = (
                    f"[Director 引导]\n"
                    f"角色提示：当前协作中存在 Director 角色，其职责是协调和引导协作。\n"
                    f"当前引导：{content}\n"
                    f"引导类型：{rule_type}\n"
                    f"来源：{issued_by} | 时间：{ts}"
                )
            blocks.append(block)

        logger.info("注入 %d 条 Director directive 到 LLM 上下文", len(blocks))
        return "\n\n".join(blocks)

    async def poll_and_enqueue_new_directives(self) -> int:
        """轮询 collaboration.md 发现新 directive 并入队。

        Returns:
            新入队的 directive 数量
        """
        from teage_liu.multiagent.blackboard import read_collab_messages

        messages = await read_collab_messages(self._bb_root)
        count = 0

        for m in messages:
            if m.get("type") != "directive":
                continue
            seq = m.get("seq", 0)
            if not isinstance(seq, int) or seq <= self._last_seen_seq:
                continue

            # 先更新 _last_seen_seq（不论 target 是否匹配，避免非本 agent
            # 的 directive 每轮被重复扫描导致 O(N²) 开销）
            if seq > self._last_seen_seq:
                self._last_seen_seq = seq

            # target 过滤：只入队 target=* 或 target=本 agent
            target = m.get("target", "*")
            if target != "*" and target != self._agent_id:
                continue

            await self.enqueue_directive(m)
            count += 1

        if count > 0:
            logger.info(
                "Agent %s 发现 %d 条新 Director directive",
                self._agent_id, count,
            )
        return count

    def _pending_count(self) -> int:
        """返回待注入队列长度（测试用）。"""
        return len(self._pending)
