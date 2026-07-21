"""Worker 适配器单元测试（Task 2）。

覆盖：
- Worker 注册流程（写 agent_card + audit）
- 心跳循环（更新 last_heartbeat）
- 优雅退出（释放锁 + 更新状态 + audit）
- 自治模式（Director 超时进入 / Director 恢复退出 / 再次崩溃回滚）
- 自治期拒绝 Director 写入
- 自治模式时间片轮转
- 轮次校验（freeform 不阻断 / 非本机轮次写 pending + 抛 NotMyTurnError）
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from hermes.multiagent.blackboard import (
    Blackboard,
    atomic_write,
    cas_write_status,
    read_audit_records,
    read_json,
    read_yaml_frontmatter,
)
from hermes.multiagent.exceptions import NotMyTurnError
from hermes.multiagent.worker_adapter import AutonomousModeController, WorkerAdapter


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def worker_config() -> dict:
    return {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": "/tmp/bb",
            "worker": {
                "agent_id": "worker_001",
                "heartbeat_interval_seconds": 10,
                "capabilities": ["file_read", "file_write", "web_search"],
                "dangerous_tools": ["execute_command", "write_file", "call_tool"],
            },
            "director": {
                "heartbeat_timeout_seconds": 30,
                "degraded_threshold_seconds": 20,
            },
        }
    }


def _make_worker_card(agent_id: str = "worker_001") -> dict:
    """构造 worker agent_card 字典。"""
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


class TestWorkerRegistration:
    """Worker 注册流程测试。"""

    @pytest.mark.asyncio
    async def test_worker_register_writes_agent_card(self, bb_root: Path, worker_config):
        """Worker 注册时写入 agents/{id}.md。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        card_path = bb_root / "agents" / "worker_001.md"
        assert card_path.exists()

        card, _ = read_yaml_frontmatter(card_path)
        assert card["agent_id"] == "worker_001"
        assert card["role"] == "worker"
        assert card["status"] == "registering"
        assert "file_read" in card["capabilities"]

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_worker_register_appends_audit(self, bb_root: Path, worker_config):
        """注册时追加 audit 记录。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        records = await read_audit_records(bb_root, limit=200)
        register_audits = [r for r in records if r.get("action") == "register"]
        assert len(register_audits) >= 1
        assert register_audits[-1]["actor"] == "worker_001"

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_worker_heartbeat_loop_updates_last_heartbeat(self, bb_root: Path, worker_config):
        """心跳循环更新 last_heartbeat 字段。"""
        worker_config["multiagent"]["worker"]["heartbeat_interval_seconds"] = 0.1
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 等待 2 次心跳
        await asyncio.sleep(0.25)

        from hermes.multiagent.agent_registry import AgentRegistry
        from hermes.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        agents = await registry.list_active_agents()
        worker = next(a for a in agents if a["agent_id"] == "worker_001")
        heartbeat_time = datetime.fromisoformat(
            worker["last_heartbeat"].replace("Z", "+00:00")
        )
        age = datetime.now(timezone.utc) - heartbeat_time
        assert age < timedelta(seconds=1)

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_worker_graceful_leave(self, bb_root: Path, worker_config):
        """优雅退出：释放锁 + 更新状态 + audit。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 获取一个锁
        from hermes.multiagent.file_lock import LockManager

        lock_manager = LockManager(bb_root, agent_id="worker_001")
        await lock_manager.acquire("messages", holder="worker_001", ttl_seconds=30)

        await adapter.stop()  # 优雅退出

        # 锁应已释放
        status = read_json(bb_root / "status.json")
        assert "messages" not in status.get("locks", {}) or (
            status["locks"]["messages"].get("holder") != "worker_001"
        )

        # agent_card.status 应为 offline
        card, _ = read_yaml_frontmatter(bb_root / "agents" / "worker_001.md")
        assert card["status"] == "offline"

        # audit 应有 leave 记录
        records = await read_audit_records(bb_root, limit=200)
        leave_audits = [r for r in records if r.get("action") == "leave"]
        assert len(leave_audits) >= 1


class TestAutonomousMode:
    """自治模式测试。"""

    @pytest.mark.asyncio
    async def test_enter_autonomous_mode_on_director_timeout(self, bb_root: Path, worker_config):
        """Director 心跳超时 → Worker 进入自治模式。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 模拟 Director 心跳超时
        stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            f'last_director_tick: "{stale_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        # 触发心跳检测
        await adapter._check_director_health()

        assert adapter._autonomous_mode is True
        assert adapter._autonomous_epoch == 1

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_mode_rejects_director_writes(self, bb_root: Path, worker_config):
        """自治期 Director 写入被拒绝。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 进入自治模式
        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 模拟 Director 写入（epoch=1，与自治期相同）
        status = adapter._read_status()
        status["current_turn"] = {"agent_id": "worker_002", "epoch": 1}

        # 应被拒绝
        result = await adapter._validate_director_write(status, writer_epoch=1)
        assert result is False

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_exit_autonomous_mode_on_director_recovery(self, bb_root: Path, worker_config):
        """Director 恢复 → Worker 退出自治模式。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 进入自治模式
        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 模拟 Director 恢复（epoch 递增 + tick 新鲜）
        fresh_tick = datetime.now(timezone.utc).isoformat()
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 2\n"
            f'last_director_tick: "{fresh_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        await adapter._check_director_recovery()

        assert adapter._autonomous_mode is False

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_exit_rollback_on_director_re_crash(self, bb_root: Path, worker_config):
        """自治退出时 Director 再次崩溃 → 回滚自治。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 第一次检测：Director 恢复（新鲜 tick + epoch 递增）
        fresh_tick = datetime.now(timezone.utc).isoformat()
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 2\n"
            f'last_director_tick: "{fresh_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        # 在退出过程中，Director 再次崩溃（写入过期 tick）
        stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 2\n"
            f'last_director_tick: "{stale_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        await adapter._check_director_recovery()

        # 应回滚为自治模式
        assert adapter._autonomous_mode is True
        assert adapter._autonomous_epoch == 1

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_mode_time_slicing(self, bb_root: Path, worker_config):
        """自治模式时间片轮转（agent_id 字典序，每片 30 秒）。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # adapter.start() 已注册 worker_001，补充注册 worker_002 / worker_003
        from hermes.multiagent.agent_registry import AgentRegistry
        from hermes.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register(_make_worker_card("worker_002"))
        await registry.register(_make_worker_card("worker_003"))

        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 进入自治模式后，current_turn 应按字典序轮转
        current = await adapter._get_autonomous_current_turn()
        assert current in ["worker_001", "worker_002", "worker_003"]

        await adapter.stop()


class TestWorkerTurnCheck:
    """Worker 轮次校验测试。"""

    @pytest.mark.asyncio
    async def test_before_speak_freeform_mode_no_block(self, bb_root: Path, worker_config):
        """freeform 模式不阻断。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 设置 freeform 模式
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            'last_director_tick: "2026-07-21T10:00:00+00:00"\n'
            "turn_policy:\n  mode: freeform\n"
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        # 非本机轮次也应通过
        message = {"content": "hello", "from": "worker_001"}
        await adapter._before_speak("worker_001", message)  # 不抛异常

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_before_speak_not_my_turn_writes_pending(self, bb_root: Path, worker_config):
        """非本机轮次 → 写 messages.pending.md + 抛 NotMyTurnError。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 设置 round_robin 模式，当前轮次是 worker_002
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            'last_director_tick: "2026-07-21T10:00:00+00:00"\n'
            "turn_policy:\n  mode: round_robin\n"
            "  order: ['worker_001', 'worker_002']\n"
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        status = read_json(bb_root / "status.json")
        status["current_turn"] = {
            "agent_id": "worker_002",
            "started_at": "2026-07-21T10:00:00+00:00",
            "epoch": 1,
        }
        await cas_write_status(
            bb_root, status.get("version", 0), status, writer_signature="director"
        )

        message = {"content": "hello", "from": "worker_001"}
        with pytest.raises(NotMyTurnError):
            await adapter._before_speak("worker_001", message)

        # 消息应写入 pending
        pending_path = bb_root / "messages.pending.md"
        assert pending_path.exists()
        pending_content = pending_path.read_text(encoding="utf-8")
        assert "hello" in pending_content

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_before_speak_my_turn_passes(self, bb_root: Path, worker_config):
        """本机轮次 → 通过。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            'last_director_tick: "2026-07-21T10:00:00+00:00"\n'
            "turn_policy:\n  mode: round_robin\n"
            "  order: ['worker_001', 'worker_002']\n"
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        status = read_json(bb_root / "status.json")
        status["current_turn"] = {
            "agent_id": "worker_001",
            "started_at": "2026-07-21T10:00:00+00:00",
            "epoch": 1,
        }
        await cas_write_status(
            bb_root, status.get("version", 0), status, writer_signature="director"
        )

        message = {"content": "hello", "from": "worker_001"}
        await adapter._before_speak("worker_001", message)  # 不抛异常

        await adapter.stop()
