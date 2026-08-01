"""Agent 注册 + 心跳。

Phase 1 范围：基础注册（不含 Director 仲裁）。
- register：写入 agents/{id}.md（YAML frontmatter + body）
- unregister：标记 status=offline + leave_reason + left_at
- update_heartbeat：更新 last_heartbeat
- list_active_agents：扫描 agents/ 目录，过滤 status != offline
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from teage_liu.multiagent.blackboard import atomic_write, read_yaml_frontmatter
from teage_liu.multiagent.file_lock import FileLock
from teage_liu.multiagent.schema_validator import SchemaValidator


HEARTBEAT_TIMEOUT_SECONDS = 90

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_local_agent(frontmatter: dict) -> bool:
    """判断 agent 是否为本机进程内的 agent（基于 pid + host）。

    兜底用途：本机 agent 心跳可能未及时更新但仍在线，应跳过心跳校验。
    字段缺失或无法比较时返回 False（无法判定为本机）。
    """
    pid = frontmatter.get("pid")
    host = frontmatter.get("host")
    if pid is None or host is None:
        return False
    try:
        return int(pid) == os.getpid() and str(host) == socket.gethostname()
    except (TypeError, ValueError, OSError):
        return False


def _is_heartbeat_stale(frontmatter: dict) -> bool:
    """判断心跳是否过期（超过 HEARTBEAT_TIMEOUT_SECONDS 秒）。

    last_heartbeat 可为 ISO 字符串（默认 _now_iso 写入）或数值时间戳
    （update_heartbeat 接受外部 timestamp 参数）。
    缺失或无法解析时返回 False（不过滤，兜底保留，避免误删）。
    """
    last_heartbeat = frontmatter.get("last_heartbeat")
    if not last_heartbeat:
        return False

    if isinstance(last_heartbeat, (int, float)):
        try:
            hb_time = datetime.fromtimestamp(float(last_heartbeat), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return False
    else:
        hb_str = str(last_heartbeat)
        # 兼容 ISO8601 'Z' 后缀（fromisoformat 3.11+ 才支持）
        if hb_str.endswith("Z"):
            hb_str = hb_str[:-1] + "+00:00"
        try:
            hb_time = datetime.fromisoformat(hb_str)
        except ValueError:
            return False
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - hb_time).total_seconds()
    return age > HEARTBEAT_TIMEOUT_SECONDS


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

    async def update_heartbeat(self, agent_id: str, timestamp: str | None = None) -> None:
        """更新 agent 心跳。

        Args:
            agent_id: agent 标识
            timestamp: 可选自定义时间戳（用于测试模拟过期心跳）；默认当前 UTC 时间

        任务1.2：跨进程 FileLock 保护读-改-写临界区，防止多 Worker / Director
        并发写 agent_card.md 导致 frontmatter 字段丢失。

        自愈修复：同时将 status 设为 "active"。任何正在发送心跳的 agent 按定义
        即为活跃。修复重启竞态：旧进程的 unregister 可能在新进程 register 之后
        执行，将 status 覆写为 "offline"，而心跳只更新 last_heartbeat 不修正
        status，导致 list_active_agents 永久排除该 agent。
        """
        agent_file = self._agents_dir / f"{agent_id}.md"
        if not agent_file.exists():
            return

        async with FileLock(agent_file):
            frontmatter, body = read_yaml_frontmatter(agent_file)
            frontmatter["last_heartbeat"] = timestamp or _now_iso()
            frontmatter["status"] = "active"

            await atomic_write(agent_file, self._dump_frontmatter(frontmatter, body))

    async def update_agent_status(self, agent_id: str, status: str) -> None:
        """更新 agent 状态（active / degraded / offline）。

        Args:
            agent_id: agent 标识
            status: 新状态值

        任务1.2：跨进程 FileLock 保护读-改-写临界区（同 update_heartbeat）。
        """
        agent_file = self._agents_dir / f"{agent_id}.md"
        if not agent_file.exists():
            return

        async with FileLock(agent_file):
            frontmatter, body = read_yaml_frontmatter(agent_file)
            frontmatter["status"] = status

            await atomic_write(agent_file, self._dump_frontmatter(frontmatter, body))

    async def update_fields(self, agent_id: str, **fields) -> None:
        """统一原子更新 agent_card 任意 frontmatter 字段(Phase3 N-1)。

        所有写入者(Director/Registry/Worker)走同一把跨进程 FileLock,
        读-改-写临界区串行化,根除并发覆盖。
        """
        agent_file = self._agents_dir / f"{agent_id}.md"
        if not agent_file.exists():
            return
        async with FileLock(agent_file):
            frontmatter, body = read_yaml_frontmatter(agent_file)
            for k, v in fields.items():
                frontmatter[k] = v
            await atomic_write(agent_file, self._dump_frontmatter(frontmatter, body))

    async def list_active_agents(self) -> list[dict]:
        """列出所有非 offline 的 agent。

        心跳新鲜度校验：last_heartbeat 超过 HEARTBEAT_TIMEOUT_SECONDS 秒视为
        离线，不返回给调用方。本机进程内的 agent（pid + host 匹配）即使心跳
        过期也保留（兜底：本机 agent 心跳可能未及时更新但仍在线）。
        last_heartbeat 缺失或无法解析的 agent 保留，避免误删。
        """
        actives = []
        for agent_file in self._agents_dir.glob("*.md"):
            try:
                frontmatter, _ = read_yaml_frontmatter(agent_file)
            except (PermissionError, FileNotFoundError, OSError) as e:
                # Windows 并发写（atomic_write 的 os.replace）可能导致瞬时读取失败，
                # 跳过该 agent 本次轮询，下次心跳检查再重试。
                logger.debug("读取 agent_card %s 失败（并发写？）：%s", agent_file.name, e)
                continue
            if frontmatter.get("status") == "offline":
                continue
            if not _is_local_agent(frontmatter) and _is_heartbeat_stale(frontmatter):
                logger.debug("agent %s 心跳过期，已过滤", frontmatter.get("agent_id"))
                continue
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
