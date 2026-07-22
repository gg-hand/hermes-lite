"""信任分管理：初始 100，单次裁定最大扣分 5，阈值触发状态降级。

信任分阈值：
- degraded_threshold（默认 60）：低于此值标记 agent degraded
- rejected_threshold（默认 30）：低于此值标记 agent rejected
- force_offline_threshold（默认 10）：低于此值强制下线

单次裁定最大扣分 max_single_delta（默认 5），防止 LLM 仲裁器误判导致大幅扣分。

全链路异步：所有 IO 通过 atomic_write（async）+ await append_audit（async）。
read_yaml_frontmatter 为 sync 函数（仅读 frontmatter，无 IO 阻塞），可直接调用。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from teage_liu.multiagent.blackboard import (
    append_audit,
    atomic_write,
    read_yaml_frontmatter,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """当前 UTC 时间 ISO 格式。"""
    return datetime.now(timezone.utc).isoformat()


class TrustScoreManager:
    """信任分管理器。

    维护每个 agent 的 trust_score（0-100），并提供阈值触发的状态降级：
    - score < degraded_threshold → status=degraded
    - score < rejected_threshold → status=rejected
    - score < force_offline_threshold → status=offline
    """

    def __init__(self, bb_root: Path, config: dict | None = None):
        """初始化 TrustScoreManager。

        Args:
            bb_root: 黑板根目录
            config: 信任分配置（含 initial_score / degraded_threshold /
                    rejected_threshold / force_offline_threshold / max_single_delta）
        """
        self._bb_root = bb_root
        config = config or {}
        self._initial_score = config.get("initial_score", 100)
        self._degraded_threshold = config.get("degraded_threshold", 60)
        self._rejected_threshold = config.get("rejected_threshold", 30)
        self._force_offline_threshold = config.get("force_offline_threshold", 10)
        self._max_single_delta = config.get("max_single_delta", 5)

    async def get_score(self, agent_id: str) -> int:
        """获取 agent 信任分。

        若 agent_card 不存在或无 trust_score 字段，返回 initial_score。
        """
        card = await self._read_agent_card(agent_id)
        return card.get("trust_score", self._initial_score)

    async def apply_delta(
        self, agent_id: str, delta: int, reason: str
    ) -> int:
        """调整信任分。

        流程：
        1. 限制单次调整幅度到 [-max_single_delta, +max_single_delta]
        2. 计算新分数并钳制到 [0, 100]
        3. 更新 agent_card 的 trust_score 字段
        4. 根据阈值更新 status 字段（active / degraded / rejected / offline）
        5. 写 audit（action=arbitrate，含 trust_delta / new_score / new_status）

        Args:
            agent_id: agent ID
            delta: 调整值（正数增加，负数减少）
            reason: 调整原因（写入 audit.details.reason）

        Returns:
            调整后的信任分
        """
        # 限制单次调整幅度
        clamped_delta = max(
            -self._max_single_delta,
            min(self._max_single_delta, delta),
        )

        card = await self._read_agent_card(agent_id)
        current_score = card.get("trust_score", self._initial_score)
        # 下界 0（不可低于 0），上界不限（正向激励可超过 100）
        new_score = max(0, current_score + clamped_delta)

        # 更新 agent_card 的 trust_score 和 status
        card["trust_score"] = new_score
        current_status = card.get("status", "active")
        new_status = self._determine_status(new_score, current_status)
        if new_status != current_status:
            card["status"] = new_status
        await self._write_agent_card(agent_id, card)

        # 写 audit
        await append_audit(self._bb_root, {
            "ts": _now_iso(),
            "actor": "director",
            "action": "arbitrate",
            "target": f"agents/{agent_id}.md",
            "op_id": str(uuid.uuid4()),
            "epoch": card.get("epoch", 0),
            "details": {
                "reason": reason,
                "trust_delta": clamped_delta,
                "original_delta": delta,
                "old_score": current_score,
                "new_score": new_score,
                "new_status": new_status,
            },
            "prev_hash": "",
            "hash": "",
            "signature": "",
        })

        logger.info(
            "信任分调整: agent=%s, delta=%d (clamped from %d), %d → %d, reason=%s, status=%s",
            agent_id,
            clamped_delta,
            delta,
            current_score,
            new_score,
            reason,
            new_status,
        )
        return new_score

    def _determine_status(self, score: int, current_status: str) -> str:
        """根据信任分确定 agent 状态。

        优先级：force_offline > rejected > degraded > active
        若分数恢复到 degraded_threshold 以上，且当前是 degraded/rejected，恢复为 active。
        """
        if score < self._force_offline_threshold:
            return "offline"
        if score < self._rejected_threshold:
            return "rejected"
        if score < self._degraded_threshold:
            return "degraded"
        # 恢复到 active（仅当前是 degraded/rejected 时）
        if current_status in ("degraded", "rejected") and score >= self._degraded_threshold:
            return "active"
        return current_status

    async def _read_agent_card(self, agent_id: str) -> dict:
        """读取 agent_card frontmatter。

        read_yaml_frontmatter 是 sync 函数（仅读 frontmatter，不涉及大块 IO 阻塞），
        返回 (frontmatter_dict, body_str)。若文件不存在返回 {}。
        """
        card_path = self._bb_root / "agents" / f"{agent_id}.md"
        if not card_path.exists():
            return {}
        frontmatter, _ = read_yaml_frontmatter(card_path)
        return frontmatter or {}

    async def _write_agent_card(self, agent_id: str, card: dict) -> None:
        """写入 agent_card（atomic_write 异步）。"""
        card_path = self._bb_root / "agents" / f"{agent_id}.md"
        frontmatter = yaml.safe_dump(card, sort_keys=False, allow_unicode=True)
        content = f"---\n{frontmatter}---\n\n# Agent Card\n"
        await atomic_write(card_path, content)
