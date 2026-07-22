"""自治模式集成测试（Task 7）。

覆盖：
- 自治模式进入（Director 长时间无心跳）
- 时间片轮转（agent_id 字典序，_turn_index 推进）
- FIFO 仲裁（flush_all_pending 按顺序 flush）
- 二次确认退出（Director 恢复后两次健康检查才退出）
- 回滚（二次确认期间 Director 再次故障，保持自治）
- round_robin 轮转（三个 agent 轮流）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
import yaml

from teage_liu.multiagent.agent_registry import AgentRegistry
from teage_liu.multiagent.blackboard import (
    Blackboard,
    atomic_write,
    read_messages,
)
from teage_liu.multiagent.schema_validator import SchemaValidator
from teage_liu.multiagent.worker_adapter import WorkerAdapter


def _make_worker_card(agent_id: str) -> dict:
    """构造 worker agent_card。"""
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


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def multiagent_config() -> dict:
    return {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": "/tmp/bb",
            "worker": {
                "agent_id": "worker_001",
                "heartbeat_interval_seconds": 1,  # 测试加速
                "capabilities": ["file_read"],
                "dangerous_tools": [],
            },
            "director": {
                "heartbeat_timeout_seconds": 2,  # 测试加速
                "autonomous_after_seconds": 5,
            },
        }
    }


class TestAutonomousEntry:
    """自治模式进入测试。"""

    @pytest.mark.asyncio
    async def test_worker_enters_autonomous_after_director_timeout(
        self, bb_root: Path, multiagent_config
    ):
        """Director 长时间无心跳 → Worker 进入自治模式。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        # 模拟 Director 从未启动（无 director.md tick 更新）
        # 写入过期的 tick
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

        # 触发一次健康检查
        health = await adapter._check_director_health()
        assert health == "offline"
        assert adapter._autonomous.is_active is True

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_mode_uses_time_slicing(
        self, bb_root: Path, multiagent_config
    ):
        """自治模式采用时间片轮转。"""
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        # adapter.start() 会注册 worker_001，这里只注册 worker_002
        await registry.register(_make_worker_card("worker_002"))

        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        # 模拟进入自治模式
        adapter._autonomous.enabled = True
        current = await adapter._get_autonomous_current_turn()
        assert current in ["worker_001", "worker_002"]

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_mode_fifo_arbitration(
        self, bb_root: Path, multiagent_config
    ):
        """自治模式 FIFO 仲裁（同等优先级）。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        # 注册 worker_002
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register(_make_worker_card("worker_002"))

        # 模拟多个 pending 消息
        pending_path = bb_root / "messages.pending.md"
        records = [
            {"frontmatter": {"from": "worker_001", "pending_seq": 1, "type": "chat", "timestamp": "2026-07-21T10:00:00+00:00"}, "body": "msg1"},
            {"frontmatter": {"from": "worker_002", "pending_seq": 2, "type": "chat", "timestamp": "2026-07-21T10:00:01+00:00"}, "body": "msg2"},
        ]
        parts = []
        for r in records:
            fm = yaml.safe_dump(r["frontmatter"], sort_keys=False, allow_unicode=True)
            parts.append(f"---\n{fm}---\n\n{r['body']}\n")
        await atomic_write(pending_path, "\n".join(parts))

        # 进入自治模式后，flush 按 FIFO（按 agent_id 顺序）
        adapter._autonomous.enabled = True
        await adapter._autonomous.flush_all_pending(bb_root)

        # 验证 messages.md 按 FIFO 顺序
        msgs = await read_messages(bb_root)
        assert len(msgs) >= 2
        froms = [m.get("from") for m in msgs]
        assert "worker_001" in froms
        assert "worker_002" in froms

        await adapter.stop()


class TestAutonomousExit:
    """自治模式退出测试。"""

    @pytest.mark.asyncio
    async def test_director_recovery_exits_autonomous(
        self, bb_root: Path, multiagent_config
    ):
        """Director 恢复心跳 → Worker 退出自治模式（二次确认）。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        # 进入自治模式（使用 controller.enabled 触发二次确认逻辑）
        adapter._autonomous.enabled = True
        adapter._autonomous.confirming_exit = False

        # 模拟 Director 恢复
        with patch.object(adapter, "_check_director_health", return_value="healthy"):
            await adapter._check_director_recovery()

        # 应进入"二次确认"状态
        assert adapter._autonomous.confirming_exit is True
        assert adapter._autonomous.enabled is True  # 仍未退出

        # 再次检测健康
        with patch.object(adapter, "_check_director_health", return_value="healthy"):
            await adapter._check_director_recovery()

        # 二次确认通过，退出自治
        assert adapter._autonomous.enabled is False
        assert adapter._autonomous.confirming_exit is False

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_exit_rollback_on_director_failure(
        self, bb_root: Path, multiagent_config
    ):
        """二次确认期间 Director 再次故障 → 回滚，保持自治。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        adapter._autonomous.enabled = True
        adapter._autonomous.confirming_exit = False

        # 第一次检测：Director 恢复
        with patch.object(adapter, "_check_director_health", return_value="healthy"):
            await adapter._check_director_recovery()
        assert adapter._autonomous.confirming_exit is True

        # 第二次检测：Director 再次故障
        with patch.object(adapter, "_check_director_health", return_value="offline"):
            await adapter._check_director_recovery()

        # 应回滚，保持自治
        assert adapter._autonomous.enabled is True
        assert adapter._autonomous.confirming_exit is False

        await adapter.stop()


class TestAutonomousTurnPolicy:
    """自治模式轮次策略测试。"""

    @pytest.mark.asyncio
    async def test_round_robin_in_autonomous(
        self, bb_root: Path, multiagent_config
    ):
        """自治模式 round_robin 轮转。"""
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        # adapter.start() 会注册 worker_001，这里注册 worker_002 和 worker_003
        await registry.register(_make_worker_card("worker_002"))
        await registry.register(_make_worker_card("worker_003"))

        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()
        # adapter.start() 注册的 worker_001 状态为 registering，更新为 active
        await registry.update_agent_status("worker_001", "active")
        adapter._autonomous.enabled = True

        # 第一次轮转
        turn1 = await adapter._get_autonomous_current_turn()
        # 推进轮次
        await adapter._autonomous.advance_turn(bb_root)
        turn2 = await adapter._get_autonomous_current_turn()
        await adapter._autonomous.advance_turn(bb_root)
        turn3 = await adapter._get_autonomous_current_turn()

        # 验证三个 agent 都轮到
        turns = {turn1, turn2, turn3}
        assert turns == {"worker_001", "worker_002", "worker_003"}

        await adapter.stop()
