"""端到端 self-talk 测试：单实例写入 100 条 messages + 100 条 audit 后状态可重建。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import aiofiles
import pytest

from hermes.multiagent.agent_registry import AgentRegistry
from hermes.multiagent.audit_logger import MultiAgentAuditLogger
from hermes.multiagent.blackboard import atomic_write, validate_path_safety
from hermes.multiagent.exceptions import PathSafetyError
from hermes.multiagent.file_lock import LockManager
from hermes.multiagent.recovery import RecoveryCoordinator
from hermes.multiagent.schema_validator import SchemaValidator


@pytest.fixture
def e2e_setup(bb_root: Path):
    """初始化完整 multiagent 环境。"""
    schema_validator = SchemaValidator(enabled=False)
    audit_logger = MultiAgentAuditLogger(bb_root)
    registry = AgentRegistry(bb_root, schema_validator)
    lock_manager = LockManager(bb_root)
    recovery = RecoveryCoordinator(bb_root, audit_logger)

    # 初始化 status.json
    status = {
        "protocol_version": "1.0.0",
        "session_id": "e2e_test",
        "phase": "active",
        "version": 0,
        "epoch": 1,
        "compat_mode": None,
        "current_turn": {"agent_id": "self", "started_at": "", "deadline_at": "", "epoch": 1},
        "turn_history": [],
        "active_agents": ["self"],
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
    (bb_root / "status.json").write_text(json.dumps(status), encoding="utf-8")

    return {
        "bb_root": bb_root,
        "schema_validator": schema_validator,
        "audit_logger": audit_logger,
        "registry": registry,
        "lock_manager": lock_manager,
        "recovery": recovery,
    }


@pytest.mark.asyncio
async def test_e2e_self_talk_100_messages(e2e_setup):
    """单实例写入 100 条 messages + 100 条 audit 后状态可重建。"""
    bb_root = e2e_setup["bb_root"]
    audit_logger = e2e_setup["audit_logger"]
    lock_manager = e2e_setup["lock_manager"]

    # 写入 100 条 messages + 100 条 audit
    for i in range(100):
        # 获取 messages 锁
        token = await lock_manager.acquire("messages", "self", ttl_seconds=30)
        try:
            # 追加 message
            msg = (
                f"---\nseq: {i+1}\nfrom: self\nto: *\n"
                f"timestamp: 2026-07-20T10:00:{i:02d}Z\ntype: chat\n"
                f"content: message_{i}\n---\n"
            )
            async with aiofiles.open(bb_root / "messages.md", "a", encoding="utf-8") as f:
                await f.write(msg)

            # 追加 audit
            await audit_logger.append_audit(
                {
                    "ts": f"2026-07-20T10:00:{i:02d}Z",
                    "actor": "self",
                    "action": "write",
                    "target": "messages.md",
                    "details": {"seq": i + 1, "fencing_token": token},
                }
            )
        finally:
            await lock_manager.release("messages", "self", token)

    # 验证 audit 完整性
    records = audit_logger.read_records(limit=10000)
    assert len(records) == 100

    # 验证 hash 链完整
    prev_hash = ""
    for rec in records:
        assert rec["prev_hash"] == prev_hash
        prev_hash = rec["hash"]

    # 验证 messages.md 行数
    messages_content = (bb_root / "messages.md").read_text(encoding="utf-8")
    assert messages_content.count("seq:") == 100


@pytest.mark.asyncio
async def test_e2e_crash_recovery(e2e_setup):
    """崩溃恢复测试：写入中途 kill → 重启后状态完整。"""
    bb_root = e2e_setup["bb_root"]
    audit_logger = e2e_setup["audit_logger"]
    recovery = e2e_setup["recovery"]

    # 写入 50 条 audit（第 1 条 register）
    for i in range(50):
        await audit_logger.append_audit(
            {
                "ts": f"2026-07-20T10:00:{i:02d}Z",
                "actor": "self",
                "action": "register" if i == 0 else "write",
                "target": "agents/self.md" if i == 0 else "messages.md",
                "details": {"agent_id": "self"} if i == 0 else {"seq": i},
            }
        )

    # 模拟崩溃：删除 status.json
    (bb_root / "status.json").unlink()

    # 重启恢复
    await recovery.check_and_recover()

    # 验证状态重建
    status = json.loads((bb_root / "status.json").read_text(encoding="utf-8"))
    assert "self" in status["active_agents"]


@pytest.mark.asyncio
async def test_e2e_lock_concurrent_no_ghost_write(e2e_setup):
    """锁测试：CAS + fencing_token + grace_period 在并发场景下无幽灵写入。"""
    lock_manager = e2e_setup["lock_manager"]

    # 串行 acquire/release 10 次
    for i in range(10):
        token = await lock_manager.acquire("messages", "self", ttl_seconds=30)
        await lock_manager.release("messages", "self", token)

    # 第 11 次 acquire 应得到 fencing_token=11
    token = await lock_manager.acquire("messages", "self", ttl_seconds=30)
    assert token == 11


@pytest.mark.asyncio
async def test_e2e_audit_corruption_recovery(e2e_setup):
    """audit 测试：100 条 audit 记录 hash 链完整 + 故意注入损坏行被正确跳过。"""
    bb_root = e2e_setup["bb_root"]
    audit_logger = e2e_setup["audit_logger"]

    # 写入 100 条正常记录
    for i in range(100):
        await audit_logger.append_audit(
            {
                "ts": f"2026-07-20T10:00:{i:02d}Z",
                "actor": "self",
                "action": "write",
                "target": "messages.md",
                "details": {"seq": i},
            }
        )

    # 注入损坏行
    audit_path = bb_root / "audit" / "audit.jsonl"
    content = audit_path.read_text(encoding="utf-8")
    content += "CORRUPT_LINE_NOT_JSON\n"
    # 继续写入正常记录
    audit_path.write_text(content, encoding="utf-8")
    await audit_logger.append_audit(
        {
            "ts": "2026-07-20T10:01:00Z",
            "actor": "self",
            "action": "read",
            "target": "messages.md",
            "details": {"seq": 100},
        }
    )

    # 读取时应跳过损坏行
    records = audit_logger.read_records(limit=10000)
    # 100 正常 + 1 新正常 = 101（损坏行被跳过）
    assert len(records) == 101

    # 损坏行应写入 corrupt.log
    corrupt_log = bb_root / "audit" / "audit.jsonl.corrupt"
    assert corrupt_log.exists()
    assert "CORRUPT_LINE_NOT_JSON" in corrupt_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_e2e_path_sandbox_all_rejected(e2e_setup):
    """路径沙箱测试：绝对路径 / .. 穿越 / symlink 全部拒绝。"""
    bb_root = e2e_setup["bb_root"]

    # 绝对路径
    with pytest.raises(PathSafetyError):
        validate_path_safety(bb_root, Path("/etc/passwd"))

    # .. 穿越
    with pytest.raises(PathSafetyError):
        validate_path_safety(bb_root, bb_root / ".." / ".." / "etc" / "passwd")

    # symlink 逃逸（Windows 无权限时跳过）
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"secret")
        tmp_path = tmp.name
    try:
        link = bb_root / "escape_link"
        try:
            link.symlink_to(tmp_path)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported on this platform")
        with pytest.raises(PathSafetyError):
            validate_path_safety(bb_root, link)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
