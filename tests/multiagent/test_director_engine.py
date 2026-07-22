"""Director 引擎单元测试（Task 1）。

覆盖：
- 启动互斥锁 + Epoch 机制 + 硬超时强抢
- 心跳三阶段（healthy / degraded / offline）
- Worker 心跳监督（标记 degraded/offline + 强制释放锁）
- 轮次推进（round_robin 模式）
- SignatureVerifier（ed25519 + VerifyResult 三级）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from teage_liu.multiagent.blackboard import Blackboard, atomic_write
from teage_liu.multiagent.director_engine import DirectorEngine, DirectorHealthState
from teage_liu.multiagent.exceptions import LockAcquisitionError


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def director_config() -> dict:
    return {
        "multiagent": {
            "enabled": True,
            "role": "director",
            "blackboard_dir": "/tmp/bb",
            "director": {
                "enforce_rules": True,
                "conflict_strategy": "llm_arbitration",
                "turn_timeout_seconds": 30,
                "heartbeat_timeout_seconds": 30,
                "fallback_strategy": "priority",
                "grace_period_seconds": 2,
                "recovery_lock_timeout": 30,
                "degraded_threshold_seconds": 20,
            },
        }
    }


class TestDirectorEngineStartup:
    """Director 启动互斥锁 + Epoch 测试。"""

    @pytest.mark.asyncio
    async def test_director_acquire_mutex_lock_on_startup(self, bb_root: Path, director_config):
        """Director 启动时获取 locks/director.lock 独占锁。"""
        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine.start()

        lock_path = bb_root / "locks" / "director.lock"
        assert lock_path.exists()
        assert engine._mutex_lock_acquired is True

        await engine.stop()

    @pytest.mark.asyncio
    async def test_second_director_rejected_when_lock_held(self, bb_root: Path, director_config):
        """第二个 Director 启动被拒绝（锁被持有 + tick 新鲜）。"""
        engine1 = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine1.start()

        engine2 = DirectorEngine(bb_root, director_config, agent_id="director_002")
        with pytest.raises(LockAcquisitionError):
            await engine2.start()

        await engine1.stop()

    @pytest.mark.asyncio
    async def test_director_epoch_increment_on_restart(self, bb_root: Path, director_config):
        """Director 重启递增 current_epoch。"""
        engine1 = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine1.start()
        epoch1 = await engine1._read_current_epoch()
        await engine1.stop()

        engine2 = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine2.start()
        epoch2 = await engine2._read_current_epoch()
        await engine2.stop()

        assert epoch2 == epoch1 + 1

    @pytest.mark.asyncio
    async def test_director_broadcast_started_message(self, bb_root: Path, director_config):
        """Director 启动后广播 type=system 消息。"""
        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine.start()

        messages = await engine._read_messages()
        system_msgs = [m for m in messages if m.get("type") == "system"]
        assert any("director_started" in m.get("content", "") for m in system_msgs)
        assert any(f"epoch={engine._current_epoch}" in m.get("content", "") for m in system_msgs)

        await engine.stop()

    @pytest.mark.asyncio
    async def test_director_hard_timeout_preempt(self, bb_root: Path, director_config):
        """硬超时强抢：fcntl 失败 + tick age > 2×timeout 时强制接管。"""
        # 模拟原 Director 已死（写入过期的 last_director_tick）
        director_md_path = bb_root / "director.md"
        stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        content = (
            "---\n"
            f'director_id: director_old\n'
            f'current_epoch: 1\n'
            f'last_director_tick: "{stale_tick}"\n'
            f'heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n'
            '---\n\n# Director Protocol\n'
        )
        await atomic_write(director_md_path, content)

        # 创建占位锁文件（模拟锁文件存在但持有者已死）
        lock_path = bb_root / "locks" / "director.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("stale_holder")

        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        # 应该走硬超时强抢分支，不抛异常
        await engine.start()
        assert engine._current_epoch >= 2
        await engine.stop()


class TestDirectorHeartbeatMonitor:
    """Director 心跳三阶段渐进测试。"""

    @pytest.mark.asyncio
    async def test_director_health_healthy(self, bb_root: Path, director_config):
        """age < degraded_threshold → healthy。"""
        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine.start()

        # 更新 tick 为当前时间
        await engine._update_director_tick()
        health = await engine._check_self_health()
        assert health.level == "healthy"

        await engine.stop()

    @pytest.mark.asyncio
    async def test_director_health_degraded(self, bb_root: Path, director_config):
        """degraded_threshold ≤ age < timeout → degraded。"""
        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine.start()

        # 模拟 25 秒前的 tick（degraded 区间）
        stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
        await engine._write_director_tick(stale_tick)

        health = await engine._check_self_health()
        assert health.level == "degraded"
        assert health.age > timedelta(seconds=20)

        await engine.stop()

    @pytest.mark.asyncio
    async def test_director_health_offline(self, bb_root: Path, director_config):
        """age ≥ timeout → offline。"""
        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine.start()

        stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        await engine._write_director_tick(stale_tick)

        health = await engine._check_self_health()
        assert health.level == "offline"

        await engine.stop()


class TestDirectorWorkerHeartbeatCheck:
    """Director 监督 Worker 心跳测试。"""

    @pytest.mark.asyncio
    async def test_director_marks_worker_degraded(self, bb_root: Path, director_config):
        """Worker 心跳 age > 2×interval → 标记 degraded。"""
        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine.start()

        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register({
            "agent_id": "worker_001",
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
        })
        # 写入过期心跳（25 秒前，触发 degraded）
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
        await registry.update_heartbeat("worker_001", stale_heartbeat)

        await engine._check_worker_heartbeats()
        agents = await registry.list_active_agents()
        assert any(a["agent_id"] == "worker_001" and a["status"] == "degraded" for a in agents)

        await engine.stop()


class TestDirectorTurnManagement:
    """Director 轮次推进测试。"""

    @pytest.mark.asyncio
    async def test_director_advances_turn_round_robin_order(self, bb_root: Path, director_config):
        """round_robin 模式按 order 列表轮转。"""
        engine = DirectorEngine(bb_root, director_config, agent_id="director_001")
        await engine.start()

        # 设置 director.md 含 turn_policy.order
        from teage_liu.multiagent.blackboard import read_director_md
        director_md = await read_director_md(bb_root)
        director_md["turn_policy"] = {
            "mode": "round_robin",
            "order": ["worker_a", "worker_b", "worker_c"],
        }
        await engine._write_director_md(director_md)

        # 当前轮次 worker_b，推进后应为 worker_c
        await engine._set_current_turn("worker_b")
        await engine._advance_turn()

        status = await engine._read_status()
        assert status["current_turn"]["agent_id"] == "worker_c"

        # 再推进应为 worker_a（回到列表开头）
        await engine._advance_turn()
        status = await engine._read_status()
        assert status["current_turn"]["agent_id"] == "worker_a"

        await engine.stop()


class TestSignatureVerifier:
    """Director 身份签名验证测试。"""

    @pytest.mark.asyncio
    async def test_signature_verify_ok(self, bb_root: Path, director_config, tmp_path):
        """签名验证通过 → VerifyResult.ok。"""
        from teage_liu.multiagent.signature import SignatureVerifier
        from teage_liu.multiagent.exceptions import VerifyResult

        # 生成 Director 密钥对
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        pub_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        verifier = SignatureVerifier(bb_root, public_key_pem=pub_pem)

        # 构造签名 status
        status = {"epoch": 1, "current_turn": {"agent_id": "worker_a"}}
        status_str = json.dumps(status, sort_keys=True)
        signature = private_key.sign(status_str.encode()).hex()
        status["director_signature"] = signature

        result = await verifier.verify_director_write(status, writer_agent_id="director_001")
        assert result.level == "ok"
        assert result.failure_count == 0

    @pytest.mark.asyncio
    async def test_signature_verify_failed_single_degraded(self, bb_root: Path, director_config):
        """签名验证失败（单次）→ degraded。"""
        from teage_liu.multiagent.signature import SignatureVerifier

        verifier = SignatureVerifier(bb_root, public_key_pem="invalid_key")

        status = {"epoch": 1, "director_signature": "invalid_sig"}
        result = await verifier.verify_director_write(status, writer_agent_id="director_001")

        assert result.level == "degraded"
        assert result.failure_count == 1

    @pytest.mark.asyncio
    async def test_signature_verify_failed_3_times_distrust(self, bb_root: Path, director_config):
        """连续 3 次失败 → distrust。"""
        from teage_liu.multiagent.signature import SignatureVerifier

        verifier = SignatureVerifier(bb_root, public_key_pem="invalid_key")

        status = {"epoch": 1, "director_signature": "invalid_sig"}
        for _ in range(2):
            await verifier.verify_director_write(status, writer_agent_id="director_001")

        result = await verifier.verify_director_write(status, writer_agent_id="director_001")
        assert result.level == "distrust"
        assert result.failure_count == 3

    @pytest.mark.asyncio
    async def test_signature_missing_soft_constraint(self, bb_root: Path, director_config):
        """无签名字段 → degraded（软约束）。"""
        from teage_liu.multiagent.signature import SignatureVerifier

        verifier = SignatureVerifier(bb_root, public_key_pem="some_key")
        status = {"epoch": 1}  # 无 director_signature 字段

        result = await verifier.verify_director_write(status, writer_agent_id="director_001")
        assert result.level == "degraded"
        assert "signature_missing" in result.reason

    @pytest.mark.asyncio
    async def test_signature_failure_count_reset_on_success(self, bb_root: Path, director_config, tmp_path):
        """验证通过后重置失败计数。"""
        from teage_liu.multiagent.signature import SignatureVerifier

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        pub_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        verifier = SignatureVerifier(bb_root, public_key_pem=pub_pem)

        # 2 次失败
        bad_status = {"epoch": 1, "director_signature": "bad"}
        await verifier.verify_director_write(bad_status, writer_agent_id="director_001")
        await verifier.verify_director_write(bad_status, writer_agent_id="director_001")

        # 1 次成功
        good_status = {"epoch": 1}
        sig = private_key.sign(json.dumps(good_status, sort_keys=True).encode()).hex()
        good_status["director_signature"] = sig
        result = await verifier.verify_director_write(good_status, writer_agent_id="director_001")

        assert result.level == "ok"
        assert result.failure_count == 0  # 重置

        # 再失败 1 次应从 1 开始（不累计）
        bad_result = await verifier.verify_director_write(bad_status, writer_agent_id="director_001")
        assert bad_result.failure_count == 1
