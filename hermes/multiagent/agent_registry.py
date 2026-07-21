"""Agent 注册 + 心跳。

Phase 1 范围：基础注册（不含 Director 仲裁）。
- register：写入 agents/{id}.md（YAML frontmatter + body）
- unregister：标记 status=offline + leave_reason + left_at
- update_heartbeat：更新 last_heartbeat
- list_active_agents：扫描 agents/ 目录，过滤 status != offline
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from hermes.logging_setup import logger
from hermes.multiagent.blackboard import atomic_write, read_yaml_frontmatter
from hermes.multiagent.schema_validator import SchemaValidator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentAlreadyRegisteredError(Exception):
    """Agent 已注册。"""


class AgentRegistry:
    """Agent 注册表（基于 agents/{id}.md 文件）。"""

    def __init__(self, bb_root: Path, schema_validator: SchemaValidator) -> None:
        self._bb_root = bb_root
        self._agents_dir = bb_root / "agents"
        self._schema_validator = schema_validator
        self._agents_dir.mkdir(parents=True, exist_ok=True)

    async def register(self, agent_card: dict) -> None:
        """注册 agent。写入 agents/{id}.md。"""
        agent_id = agent_card["agent_id"]

        # 检查是否已注册
        agent_file = self._agents_dir / f"{agent_id}.md"
        if agent_file.exists():
            raise AgentAlreadyRegisteredError(f"agent '{agent_id}' already_registered")

        # schema 校验
        self._schema_validator.validate_agent_card(agent_card)

        # 写入 agent_card.md
        body = f"# {agent_id}\n\nAgent registration.\n"
        content = self._dump_frontmatter(agent_card, body)
        await atomic_write(agent_file, content)

    async def unregister(self, agent_id: str, leave_reason: str = "") -> None:
        """注销 agent。标记 status=offline。"""
        agent_file = self._agents_dir / f"{agent_id}.md"
        if not agent_file.exists():
            return

        # 读取现有 frontmatter
        frontmatter, body = read_yaml_frontmatter(agent_file)
        frontmatter["status"] = "offline"
        frontmatter["leave_reason"] = leave_reason
        frontmatter["left_at"] = _now_iso()

        await atomic_write(agent_file, self._dump_frontmatter(frontmatter, body))

    async def update_heartbeat(self, agent_id: str) -> None:
        """更新 agent 心跳。"""
        agent_file = self._agents_dir / f"{agent_id}.md"
        if not agent_file.exists():
            return

        frontmatter, body = read_yaml_frontmatter(agent_file)
        frontmatter["last_heartbeat"] = _now_iso()

        await atomic_write(agent_file, self._dump_frontmatter(frontmatter, body))

    async def list_active_agents(self) -> list[dict]:
        """列出所有非 offline 的 agent。"""
        actives = []
        for agent_file in self._agents_dir.glob("*.md"):
            frontmatter, _ = read_yaml_frontmatter(agent_file)
            if frontmatter.get("status") != "offline":
                actives.append(frontmatter)
        return actives

    async def get_agent(self, agent_id: str) -> Optional[dict]:
        """获取单个 agent。"""
        agent_file = self._agents_dir / f"{agent_id}.md"
        if not agent_file.exists():
            return None
        frontmatter, _ = read_yaml_frontmatter(agent_file)
        return frontmatter

    def _dump_frontmatter(self, frontmatter: dict, body: str) -> str:
        """序列化 frontmatter + body。"""
        yaml_str = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        return f"---\n{yaml_str}---\n{body}"
