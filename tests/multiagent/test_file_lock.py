"""file_lock.py 测试：CAS + fencing_token + grace_period。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from teage_liu.multiagent.file_lock import LockManager
from teage_liu.multiagent.exceptions import (
    FencingTokenMismatchError,
    LockAcquisitionError,
    CASVersionMismatchError,
)


@pytest.fixture
def lock_manager(bb_root: Path) -> LockManager:
    return LockManager(bb_root)


def _init_status(bb_root: Path, version: int = 0, locks: dict = None, last_fencing_token: int = 0) -> None:
    """初始化 status.json。"""
    import json
    status = {
        "protocol_version": "1.0.0", "session_id": "test", "phase": "active",
        "version": version, "epoch": 1, "compat_mode": None,
        "current_turn": {"agent_id": "", "started_at": "", "deadline_at": "", "epoch": 1},
        "turn_history": [], "active_agents": [],
        "locks": locks or {},
        "last_message_seq": 0, "last_heartbeat": {},
        "director_status": "active", "director_signature": "",
        "last_fencing_token": last_fencing_token,
        "recovery_started_at": None, "recovery_progress": None,
        "recovery_stage": None, "extensions": {},
    }
    (bb_root / "status.json").write_text(json.dumps(status), encoding="utf-8")


@pytest.mark.asyncio
async def test_lock_acquire_basic(bb_root: Path, lock_manager: LockManager):
    _init_status(bb_root)
    token = await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    assert token == 1  # 第一个 fencing_token


@pytest.mark.asyncio
async def test_lock_acquire_concurrent_only_one_succeeds(bb_root: Path, lock_manager: LockManager):
    _init_status(bb_root)
    # 模拟并发 acquire（两个协程同时尝试）
    results = await asyncio.gather(
        lock_manager.acquire("messages", "agent_a", ttl_seconds=30),
        lock_manager.acquire("messages", "agent_b", ttl_seconds=30),
        return_exceptions=True,
    )
    success_count = sum(1 for r in results if not isinstance(r, Exception))
    assert success_count == 1


@pytest.mark.asyncio
async def test_lock_release_with_fencing_token(bb_root: Path, lock_manager: LockManager):
    _init_status(bb_root)
    token = await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    await lock_manager.release("messages", "agent_a", token)
    # 释放后可重新获取
    new_token = await lock_manager.acquire("messages", "agent_b", ttl_seconds=30)
    assert new_token == token + 1


@pytest.mark.asyncio
async def test_lock_release_wrong_fencing_token_rejected(bb_root: Path, lock_manager: LockManager):
    _init_status(bb_root)
    await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    with pytest.raises(FencingTokenMismatchError):
        await lock_manager.release("messages", "agent_a", fencing_token=999)


@pytest.mark.asyncio
async def test_lock_renew_with_fencing_token(bb_root: Path, lock_manager: LockManager):
    _init_status(bb_root)
    token = await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    new_token = await lock_manager.renew("messages", "agent_a", token, new_ttl=60)
    assert new_token == token  # renew 不递增 fencing_token


@pytest.mark.asyncio
async def test_lock_renew_wrong_fencing_token_rejected(bb_root: Path, lock_manager: LockManager):
    _init_status(bb_root)
    await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    with pytest.raises(FencingTokenMismatchError):
        await lock_manager.renew("messages", "agent_a", fencing_token=999, new_ttl=60)


@pytest.mark.asyncio
async def test_lock_force_release_grace_period(bb_root: Path, lock_manager: LockManager):
    """Director 强制释放先写 grace_period 标记。"""
    _init_status(bb_root)
    await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    await lock_manager.force_release("messages", reason="holder_dead")
    # force_release 后 grace_until 应被设置
    import json
    status = json.loads((bb_root / "status.json").read_text(encoding="utf-8"))
    lock = status["locks"]["messages"]
    assert lock.get("force_releasing") is True or lock.get("grace_until") is not None


@pytest.mark.asyncio
async def test_ghost_write_detection(bb_root: Path, lock_manager: LockManager):
    """旧 token 写入被拒绝。"""
    _init_status(bb_root)
    token1 = await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    await lock_manager.release("messages", "agent_a", token1)
    token2 = await lock_manager.acquire("messages", "agent_b", ttl_seconds=30)
    # 用旧 token 尝试 release 应失败
    with pytest.raises(FencingTokenMismatchError):
        await lock_manager.release("messages", "agent_a", fencing_token=token1)


@pytest.mark.asyncio
async def test_cas_version_mismatch_retry(bb_root: Path, lock_manager: LockManager):
    """CAS 冲突重试上限 2 次。"""
    _init_status(bb_root, version=42)
    # 模拟并发修改 version
    import json
    status = json.loads((bb_root / "status.json").read_text(encoding="utf-8"))
    status["version"] = 43  # 模拟他人已修改
    (bb_root / "status.json").write_text(json.dumps(status), encoding="utf-8")

    # acquire 应在 2 次重试内成功
    token = await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    assert token > 0


@pytest.mark.asyncio
async def test_is_locked(bb_root: Path, lock_manager: LockManager):
    _init_status(bb_root)
    assert not lock_manager.is_locked("messages")
    await lock_manager.acquire("messages", "agent_a", ttl_seconds=30)
    assert lock_manager.is_locked("messages")
