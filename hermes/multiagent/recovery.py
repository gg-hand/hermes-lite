"""崩溃恢复 + audit 重放。

设计原则：
- RecoveryCoordinator 启动时检查 status.json 是否完整
- 缺失或损坏时从 audit.jsonl 重放重建
- 恢复期 fence：director_status=recovering 时拒绝旧 epoch 写入
- 快照恢复：检查 snapshots/，存在则解压重放（P1-13，Phase 1 简化版不实现）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes.logging_setup import logger
from hermes.multiagent.audit_logger import MultiAgentAuditLogger
from hermes.multiagent.blackboard import atomic_write, read_json
from hermes.multiagent.exceptions import PathSafetyError


class RecoveryFenceError(Exception):
    """恢复期 fence 错误。"""


def _initial_status() -> dict:
    """生成初始 status.json。"""
    return {
        "protocol_version": "1.0.0",
        "session_id": "default",
        "phase": "initializing",
        "version": 0,
        "epoch": 1,
        "compat_mode": None,
        "current_turn": {"agent_id": "", "started_at": "", "deadline_at": "", "epoch": 1},
        "turn_history": [],
        "active_agents": [],
        "locks": {},
        "last_message_seq": 0,
        "last_heartbeat": {},
        "director_status": "active",
        "director_signature": "",
        "last_fencing_token": 0,
        "recovery_started_at": None,
        "recovery_progress": None,
        "recovery_stage": None,
        "extensions": {},
    }


class RecoveryCoordinator:
    """崩溃恢复协调器。"""

    def __init__(self, bb_root: Path, audit_logger: MultiAgentAuditLogger) -> None:
        self._bb_root = bb_root
        self._audit_logger = audit_logger
        self._status_path = bb_root / "status.json"

    async def rebuild_state_from_audit(self) -> dict:
        """从 audit.jsonl 重放重建 status.json。"""
        status = _initial_status()
        active_agents: set[str] = set()
        locks: dict = {}

        records = self._audit_logger.read_records(limit=10000)
        for rec in records:
            action = rec.get("action")
            details = rec.get("details", {})
            target = rec.get("target", "")

            if action == "register" and "agent_id" in details:
                active_agents.add(details["agent_id"])
            elif action == "unregister" and "agent_id" in details:
                active_agents.discard(details["agent_id"])
            elif action == "lock_acquire" and "lock_name" in details:
                locks[details["lock_name"]] = {
                    "holder": details.get("holder", ""),
                    "fencing_token": details.get("fencing_token", 0),
                }
            elif action == "lock_release" and "lock_name" in details:
                locks.pop(details["lock_name"], None)

        status["active_agents"] = sorted(active_agents)
        status["locks"] = locks
        status["version"] = len(records)
        status["phase"] = "active"

        # 写入重建后的 status.json
        await atomic_write(self._status_path, json.dumps(status, ensure_ascii=False))
        return status

    async def check_and_recover(self) -> None:
        """启动时检查并恢复。"""
        if not self._status_path.exists():
            logger.info("status.json missing, rebuilding from audit")
            await self.rebuild_state_from_audit()
            return

        try:
            status = read_json(self._status_path)
            # 基本完整性检查
            if "version" not in status or "active_agents" not in status:
                logger.warning("status.json corrupt, rebuilding from audit")
                await self.rebuild_state_from_audit()
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"status.json parse failed: {e}, rebuilding from audit")
            await self.rebuild_state_from_audit()

    async def check_write_allowed(self, lock_name: str, epoch: int) -> None:
        """检查写入是否允许（恢复期 fence）。

        Args:
            lock_name: 锁名（如 messages）
            epoch: 写入者的 epoch

        Raises:
            RecoveryFenceError: 恢复期 fence 拒绝
        """
        if not self._status_path.exists():
            return

        status = read_json(self._status_path)
        director_status = status.get("director_status")

        if director_status == "recovering":
            # 恢复期 fence：messages 锁拒绝旧 epoch 写入
            if lock_name == "messages" and epoch < status.get("epoch", 0):
                raise RecoveryFenceError(
                    f"fence period: lock '{lock_name}' blocked for old epoch {epoch} "
                    f"(current epoch={status.get('epoch')})"
                )
