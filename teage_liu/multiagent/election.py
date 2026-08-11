"""Director 跨设备选举：基于 epoch + 字典序仲裁。

选举规则：
1. 收集所有候选 Director 的 epoch（本地 + 远程端点）
2. 选择 epoch 最高的候选者
3. 若 epoch 相同，选择 agent_id 字典序更小者
4. 若当前 Director 心跳超时，本机可抢占（epoch + 1）
5. 选举结果写 audit.md

选举触发时机：
- 启动时（首次或重启）
- Director 心跳超时检测到时
- 手动触发（管理员 API）
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from teage_liu.multiagent.a2a_client import A2AClient
from teage_liu.multiagent.blackboard import append_audit, atomic_write, read_json

logger = logging.getLogger(__name__)


@dataclass
class ElectionResult:
    """选举结果。"""
    won: bool
    director_id: str
    epoch: int
    reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Election:
    """Director 跨设备选举。"""

    def __init__(self, bb_root: Path, agent_id: str, config: dict):
        self._bb_root = bb_root
        self._agent_id = agent_id
        self._config = config
        self._election_timeout = config.get("election_timeout_seconds", 30)
        # 仅在配置了 a2a 段时初始化 A2AClient
        self._a2a_client: A2AClient | None = None
        if config.get("a2a"):
            self._a2a_client = A2AClient(config)

    async def run(self) -> ElectionResult:
        """执行选举。

        Returns:
            ElectionResult：won=True 表示本机当选。
        """
        # 1. 读取本地 status.json
        local_status = await self._read_local_status()
        local_director = (local_status or {}).get("director", {}) if local_status else {}

        # 2. 查询远程端点
        remote_directors = await self._query_remote_directors()

        # 3. 收集所有候选
        candidates: list[dict] = []
        if local_director:
            candidates.append(local_director)
        for ep_name, remote_status in remote_directors.items():
            if isinstance(remote_status, dict) and remote_status.get("director"):
                d = dict(remote_status["director"])
                d["source"] = ep_name
                candidates.append(d)

        # 4. 如果没有候选，本机自动当选
        if not candidates:
            return await self._win_election(0, "no_candidates")

        # 5. 找出最高 epoch
        max_epoch = max(c.get("epoch", 0) for c in candidates)
        highest_epoch_candidates = [
            c for c in candidates if c.get("epoch", 0) == max_epoch
        ]

        # 6. 检查当前 Director 心跳是否超时
        current_director = next(
            (c for c in highest_epoch_candidates if c.get("agent_id")),
            None,
        )
        if current_director:
            is_stale = self._is_director_stale(current_director)
            if is_stale and len(highest_epoch_candidates) == 1:
                # 当前 Director 心跳超时，本机抢占
                return await self._win_election(max_epoch + 1, "preempt_stale")

            # 字典序仲裁（仅当当前 director 不 stale 或多个候选时）
            winner_id = min(c.get("agent_id", "") for c in highest_epoch_candidates)
            if winner_id == self._agent_id:
                return await self._win_election(max_epoch, "tie_break_win")
            return ElectionResult(
                won=False,
                director_id=winner_id,
                epoch=max_epoch,
                reason="tie_break_loss" if len(highest_epoch_candidates) > 1 else "lower_epoch",
            )

        return await self._win_election(max_epoch, "no_active_director")

    async def _win_election(self, epoch: int, reason: str) -> ElectionResult:
        """本机赢得选举，写入 status.json + audit。"""
        # 写 status.json
        status = await self._read_local_status() or {}
        now = _now_iso()
        status["director"] = {
            "agent_id": self._agent_id,
            "epoch": epoch,
            "last_tick": now,
            "elected_at": now,
        }
        status_path = self._bb_root / "status.json"
        await atomic_write(status_path, json.dumps(status, indent=2, ensure_ascii=False))

        # 写 audit
        await append_audit(self._bb_root, {
            "ts": now,
            "actor": self._agent_id,
            "action": "election",
            "target": "status.json",
            "op_id": str(uuid.uuid4()),
            "epoch": epoch,
            "details": {"reason": reason, "won": True},
            "prev_hash": "", "hash": "", "signature": "",
        })

        logger.info("选举获胜: agent=%s, epoch=%d, reason=%s", self._agent_id, epoch, reason)
        return ElectionResult(won=True, director_id=self._agent_id, epoch=epoch, reason=reason)

    async def _read_local_status(self) -> dict | None:
        """读取本地 status.json。"""
        status_path = self._bb_root / "status.json"
        if not status_path.exists():
            return None
        try:
            return read_json(status_path)
        except Exception:
            return None

    async def _query_remote_directors(self) -> dict[str, Any]:
        """查询远程端点的 director 状态。"""
        if not self._a2a_client:
            return {}
        try:
            return await self._a2a_client.call_all_endpoints("read_director_md", {})
        except Exception as e:
            logger.warning("查询远程 director 失败: %s", e)
            return {}

    def _is_director_stale(self, director: dict) -> bool:
        """检查 Director 心跳是否超时。"""
        last_tick = director.get("last_tick")
        if not last_tick:
            return True
        try:
            tick_dt = datetime.fromisoformat(last_tick.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - tick_dt) > timedelta(seconds=self._election_timeout)
        except (ValueError, TypeError):
            return True
