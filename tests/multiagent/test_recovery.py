"""recovery.py 测试：崩溃恢复 + audit 重放。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from teage_liu.multiagent.audit_logger import MultiAgentAuditLogger
from teage_liu.multiagent.recovery import RecoveryCoordinator


@pytest.fixture
def audit_logger(bb_root: Path) -> MultiAgentAuditLogger:
    return MultiAgentAuditLogger(bb_root)


@pytest.fixture
def recovery(bb_root: Path, audit_logger: MultiAgentAuditLogger) -> RecoveryCoordinator:
    return RecoveryCoordinator(bb_root, audit_logger)


@pytest.mark.asyncio
async def test_rebuild_state_from_audit_empty(bb_root: Path, recovery: RecoveryCoordinator):
    """空 audit 应重建出初始 status.json。"""
    status = await recovery.rebuild_state_from_audit()
    assert status["version"] >= 0
    assert status["active_agents"] == []
    assert status["locks"] == {}


@pytest.mark.asyncio
async def test_rebuild_state_from_audit_with_records(bb_root: Path, recovery: RecoveryCoordinator, audit_logger: MultiAgentAuditLogger):
    """有 audit 记录应重建出对应状态。"""
    # 写入 audit 记录
    await audit_logger.append_audit({
        "ts": "t1", "actor": "agent_a", "action": "register",
        "target": "agents/agent_a.md", "details": {"agent_id": "agent_a"},
    })
    await audit_logger.append_audit({
        "ts": "t2", "actor": "agent_b", "action": "register",
        "target": "agents/agent_b.md", "details": {"agent_id": "agent_b"},
    })

    status = await recovery.rebuild_state_from_audit()
    assert "agent_a" in status["active_agents"]
    assert "agent_b" in status["active_agents"]


@pytest.mark.asyncio
async def test_recovery_period_fence(bb_root: Path, recovery: RecoveryCoordinator):
    """恢复期 fence：旧 epoch 写入应被拒绝。"""
    # 模拟恢复期 director_status=recovering
    status = {
        "protocol_version": "1.0.0", "session_id": "test", "phase": "active",
        "version": 1, "epoch": 2,  # 新 epoch
        "director_status": "recovering",
        "locks": {}, "active_agents": [],
    }
    (bb_root / "status.json").write_text(json.dumps(status), encoding="utf-8")

    # 旧 epoch 写入应被拒绝
    with pytest.raises(Exception, match="fence|epoch|recovering"):
        await recovery.check_write_allowed(lock_name="messages", epoch=1)


@pytest.mark.asyncio
async def test_check_and_recover_no_recovery_needed(bb_root: Path, recovery: RecoveryCoordinator):
    """status.json 正常时不需要恢复。"""
    status = {
        "protocol_version": "1.0.0", "session_id": "test", "phase": "active",
        "version": 1, "epoch": 1, "director_status": "active",
        "locks": {}, "active_agents": [],
    }
    (bb_root / "status.json").write_text(json.dumps(status), encoding="utf-8")

    await recovery.check_and_recover()
    # status.json 应保持不变
    result = json.loads((bb_root / "status.json").read_text(encoding="utf-8"))
    assert result["director_status"] == "active"


@pytest.mark.asyncio
async def test_check_and_recover_rebuilds_missing_status(bb_root: Path, recovery: RecoveryCoordinator, audit_logger: MultiAgentAuditLogger):
    """status.json 缺失时应从 audit 重建。"""
    # 删除 status.json
    (bb_root / "status.json").unlink(missing_ok=True)

    # 写入 audit 记录
    await audit_logger.append_audit({
        "ts": "t1", "actor": "agent_a", "action": "register",
        "target": "agents/agent_a.md", "details": {"agent_id": "agent_a"},
    })

    await recovery.check_and_recover()
    # status.json 应被重建
    assert (bb_root / "status.json").exists()
    result = json.loads((bb_root / "status.json").read_text(encoding="utf-8"))
    assert "agent_a" in result["active_agents"]


# ---------------- Task 4: _initial_status 默认值统一 ----------------


def test_initial_status_director_status_offline():
    """_initial_status 的 director_status 应为 offline（Director 未启动）。"""
    from teage_liu.multiagent.recovery import _initial_status
    status = _initial_status()
    assert status["director_status"] == "offline", \
        f"director_status 应为 offline，实际: {status['director_status']}"
    assert status["director_signature"] == "", "director_signature 应为空"


def test_initial_status_consistent_with_blackboard_init():
    """_initial_status 关键字段与 Blackboard.init_blackboard 保持一致。"""
    from teage_liu.multiagent.recovery import _initial_status
    status = _initial_status()
    # 与 blackboard.py 的 init_blackboard 初始 status 保持一致
    assert status["director_status"] == "offline"
    assert status["phase"] == "init", f"phase 应为 init，实际: {status['phase']}"
    assert status["recovery_stage"] == "idle", \
        f"recovery_stage 应为 idle，实际: {status['recovery_stage']}"
    assert status["epoch"] == 0, f"epoch 应为 0，实际: {status['epoch']}"
    assert status["recovery_progress"] == {}, "recovery_progress 应为空 dict"
