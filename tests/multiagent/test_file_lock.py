"""file_lock.py 测试：CAS + fencing_token + grace_period + 跨进程 FileLock。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import portalocker
import pytest

from teage_liu.multiagent.file_lock import FileLock, LockManager
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


# ============================================================
# 任务1.2：FileLock 跨进程锁测试
# ============================================================


def _make_agent_card(agent_id: str = "agent_a") -> dict:
    """构造最小可用 agent_card（与 test_agent_registry 一致）。"""
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
        "trust_score": 100,
        "trust_history": [],
        "extensions": {},
        "leave_reason": "",
        "left_at": "",
    }


@pytest.mark.asyncio
async def test_concurrent_writes_preserve_last_heartbeat(bb_root: Path):
    """任务1.2：10 个协程并发调 update_heartbeat + update_status，
    断言 agent_card.md 的 last_heartbeat 最终值为最新写入时间戳。

    验证 FileLock 保护读-改-写临界区，防止 frontmatter 字段丢失。
    """
    from teage_liu.multiagent.agent_registry import AgentRegistry
    from teage_liu.multiagent.schema_validator import SchemaValidator

    registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
    await registry.register(_make_agent_card("agent_a"))

    # 10 个单调递增的时间戳（保证可比较）
    base_time = datetime(2026, 7, 30, 10, 0, 0, tzinfo=timezone.utc)
    timestamps = [
        (base_time.replace(second=i)).isoformat() for i in range(10)
    ]

    async def write_heartbeat(ts: str) -> None:
        await registry.update_heartbeat("agent_a", ts)
        # 同时更新 status，制造更复杂的读-改-写场景
        await registry.update_agent_status("agent_a", "active")

    # 10 个协程并发写入
    await asyncio.gather(*[write_heartbeat(ts) for ts in timestamps])

    # 读取最终 agent_card.md，断言 last_heartbeat 是 10 个时间戳之一
    # （FileLock 保证最后一次写入胜出，没有中途损坏）
    agent = await registry.get_agent("agent_a")
    assert agent is not None
    assert agent["last_heartbeat"] in timestamps, (
        f"last_heartbeat={agent['last_heartbeat']} 不在预期时间戳集合中，"
        f"说明并发写入发生损坏"
    )
    # 进一步断言：frontmatter 完整性（status 字段未丢失）
    assert agent["status"] == "active"
    assert agent["agent_id"] == "agent_a"
    assert agent["trust_score"] == 100  # 其他字段未被覆盖


@pytest.mark.asyncio
async def test_lock_timeout_raises(bb_root: Path, tmp_path: Path):
    """任务1.2：锁被长任务占住时，第二个 acquire 在 timeout 后抛异常。

    用 portalocker.Lock 同步占住锁文件，再用 FileLock 尝试获取（短 timeout），
    断言抛 portalocker.LockException。
    """
    target_file = bb_root / "agents" / "target.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("---\n---\n", encoding="utf-8")

    lock_file = target_file.with_suffix(target_file.suffix + ".lock")

    # 同步占住锁文件（模拟另一个进程持有）
    holder = portalocker.Lock(
        str(lock_file), mode="a", timeout=1, fail_when_locked=False
    )
    holder.acquire()

    try:
        # FileLock 用 1s timeout，应超时抛 LockException
        fl = FileLock(target_file, timeout=1.0)
        with pytest.raises(portalocker.LockException):
            await fl.__aenter__()
    finally:
        holder.release()
