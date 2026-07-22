"""信任分管理单元测试（Task 5）。

覆盖：
- 初始信任分 100
- apply_delta 正向/负向调整
- max_single_delta 限制（默认 5）
- 信任分范围 [0, 100] 钳制
- degraded_threshold（60）触发状态降级
- rejected_threshold（30）触发状态降级
- force_offline_threshold（10）强制下线
- audit 记录写入（action=arbitrate）
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from teage_liu.multiagent.agent_registry import AgentRegistry
from teage_liu.multiagent.blackboard import Blackboard, read_audit_records
from teage_liu.multiagent.schema_validator import SchemaValidator
from teage_liu.multiagent.trust_score import TrustScoreManager


def _make_worker_card(agent_id: str, trust_score: int = 100) -> dict:
    """构造 worker agent_card（用于注册）。"""
    return {
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "created_at": "2026-07-20T09:55:00Z",
        "last_heartbeat": "2026-07-20T10:00:00Z",
        "heartbeat_interval_seconds": 10,
        "status": "active",
        "role": "worker",
        "endpoint": "http://localhost:8000",
        "owner": "user_a",
        "capabilities": ["file_read"],
        "specialties": [],
        "auth_method": "local",
        "trust_score": trust_score,
        "trust_history": [],
        "extensions": {},
        "leave_reason": "",
        "left_at": "",
    }


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def trust_config() -> dict:
    return {
        "initial_score": 100,
        "degraded_threshold": 60,
        "rejected_threshold": 30,
        "force_offline_threshold": 10,
        "max_single_delta": 5,
    }


@pytest_asyncio.fixture
async def registry(bb_root: Path) -> AgentRegistry:
    """AgentRegistry 实例（schema 校验关闭）。"""
    return AgentRegistry(bb_root, SchemaValidator(enabled=False))


class TestTrustScoreManager:
    """TrustScoreManager 测试。"""

    @pytest.mark.asyncio
    async def test_initial_score_100(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """新 agent 初始信任分 100。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        score = await manager.get_score("worker_001")
        assert score == 100

    @pytest.mark.asyncio
    async def test_apply_delta_positive(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """正向调整（信任分增加）。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        new_score = await manager.apply_delta("worker_001", delta=3, reason="good_behavior")
        assert new_score == 103

    @pytest.mark.asyncio
    async def test_apply_delta_negative(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """负向调整（信任分减少）。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        new_score = await manager.apply_delta("worker_001", delta=-2, reason="minor_violation")
        assert new_score == 98

    @pytest.mark.asyncio
    async def test_apply_delta_max_single_delta_limit(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """单次裁定最大扣分限制（max_single_delta=5）。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        # 尝试扣 10 分，应被限制为 5 分
        new_score = await manager.apply_delta("worker_001", delta=-10, reason="severe_violation")
        assert new_score == 95  # 100 - 5

    @pytest.mark.asyncio
    async def test_apply_delta_clamp_to_0_100(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """信任分范围 [0, 100]。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        # 连续扣到 0
        for _ in range(20):  # 100 - 20*5 = 0
            new_score = await manager.apply_delta("worker_001", delta=-5, reason="violation")
        assert new_score == 0

        # 再扣应保持 0
        new_score = await manager.apply_delta("worker_001", delta=-5, reason="violation")
        assert new_score == 0

    @pytest.mark.asyncio
    async def test_degraded_threshold_triggers_status_change(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """信任分低于 degraded_threshold → 标记 agent degraded。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        # 扣到 60 以下
        for _ in range(9):  # 100 - 9*5 = 55
            await manager.apply_delta("worker_001", delta=-5, reason="violation")

        agents = await registry.list_active_agents()
        worker = next(a for a in agents if a["agent_id"] == "worker_001")
        assert worker["status"] == "degraded"
        assert worker["trust_score"] == 55

    @pytest.mark.asyncio
    async def test_rejected_threshold_triggers_status_change(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """信任分低于 rejected_threshold → 标记 agent rejected。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        # 扣到 30 以下
        for _ in range(15):  # 100 - 15*5 = 25
            await manager.apply_delta("worker_001", delta=-5, reason="violation")

        agents = await registry.list_active_agents()
        worker = next(a for a in agents if a["agent_id"] == "worker_001")
        assert worker["status"] == "rejected"
        assert worker["trust_score"] == 25

    @pytest.mark.asyncio
    async def test_force_offline_threshold_triggers_offline(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """信任分低于 force_offline_threshold → 强制下线。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        # 扣到 10 以下
        for _ in range(19):  # 100 - 19*5 = 5
            await manager.apply_delta("worker_001", delta=-5, reason="violation")

        # list_active_agents 过滤 status=offline，因此需要直接读 agent_card
        agent = await registry.get_agent("worker_001")
        assert agent["status"] == "offline"
        assert agent["trust_score"] == 5

    @pytest.mark.asyncio
    async def test_apply_delta_writes_audit(self, bb_root: Path, trust_config, registry: AgentRegistry):
        """信任分调整时写 audit。"""
        await registry.register(_make_worker_card("worker_001"))

        manager = TrustScoreManager(bb_root, trust_config)
        await manager.apply_delta("worker_001", delta=-3, reason="minor_violation")

        records = await read_audit_records(bb_root)
        trust_audits = [
            r for r in records
            if r.get("action") == "arbitrate"
            and r.get("details", {}).get("reason") == "minor_violation"
        ]
        assert len(trust_audits) >= 1
        assert trust_audits[-1]["details"]["trust_delta"] == -3
        assert trust_audits[-1]["details"]["new_score"] == 97
