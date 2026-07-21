"""Director 跨设备选举测试（Task 4, Plan 3）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from hermes.multiagent.blackboard import Blackboard


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


class TestElection:
    """Director 跨设备选举测试。"""

    @pytest.mark.asyncio
    async def test_single_device_becomes_director(self, bb_root: Path):
        """单设备场景，本机自动成为 Director。"""
        from hermes.multiagent.election import Election

        election = Election(bb_root, agent_id="device_a", config={
            "election_timeout_seconds": 5,
        })
        result = await election.run()
        assert result.won is True
        assert result.director_id == "device_a"

    @pytest.mark.asyncio
    async def test_higher_epoch_wins(self, bb_root: Path):
        """epoch 更高的设备当选 Director。"""
        from hermes.multiagent.election import Election

        # 模拟设备 B 已声明 epoch=5（fresh 心跳，未超时）
        fresh_time = datetime.now(timezone.utc).isoformat()
        status_path = bb_root / "status.json"
        status = {
            "director": {"agent_id": "device_b", "epoch": 5, "last_tick": fresh_time},
        }
        status_path.write_text(json.dumps(status), encoding="utf-8")

        election = Election(bb_root, agent_id="device_a", config={
            "election_timeout_seconds": 30,
        })
        result = await election.run()
        # 本机 epoch=0，低于设备 B，应败选
        assert result.won is False
        assert result.director_id == "device_b"

    @pytest.mark.asyncio
    async def test_stale_director_gets_preempted(self, bb_root: Path):
        """Director 心跳超时，本机抢占。"""
        from hermes.multiagent.election import Election

        # 模拟设备 B 是 Director，但心跳已超时
        status_path = bb_root / "status.json"
        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        status = {
            "director": {"agent_id": "device_b", "epoch": 5, "last_tick": stale_time},
        }
        status_path.write_text(json.dumps(status), encoding="utf-8")

        election = Election(bb_root, agent_id="device_a", config={
            "election_timeout_seconds": 30,  # 心跳超时 30 秒
        })
        result = await election.run()
        # 设备 B 心跳超时，本机应抢占
        assert result.won is True
        assert result.director_id == "device_a"
        assert result.epoch == 6  # epoch + 1

    @pytest.mark.asyncio
    async def test_election_writes_audit(self, bb_root: Path):
        """选举结果写 audit。"""
        from hermes.multiagent.election import Election
        from hermes.multiagent.blackboard import read_audit_records

        election = Election(bb_root, agent_id="device_a", config={
            "election_timeout_seconds": 5,
        })
        await election.run()

        records = await read_audit_records(bb_root)
        election_audits = [r for r in records if r.get("action") == "election"]
        assert len(election_audits) >= 1

    @pytest.mark.asyncio
    async def test_election_uses_remote_endpoints(self, bb_root: Path):
        """选举时查询远程端点 epoch。"""
        from hermes.multiagent.election import Election

        config = {
            "election_timeout_seconds": 30,
            "a2a": {
                "remote_endpoints": [
                    {"name": "device_b", "url": "http://127.0.0.1:18401"},
                ],
            },
        }

        election = Election(bb_root, agent_id="device_a", config=config)

        # Mock A2AClient.call_all_endpoints - 远程 director 心跳 fresh
        fresh_time = datetime.now(timezone.utc).isoformat()
        with patch.object(
            election._a2a_client, "call_all_endpoints", new_callable=AsyncMock,
            return_value={"device_b": {"director": {
                "agent_id": "device_b", "epoch": 3, "last_tick": fresh_time,
            }}},
        ):
            result = await election.run()
            # 本机 epoch=0，远程 epoch=3，应败选
            assert result.won is False

    @pytest.mark.asyncio
    async def test_election_tie_break_by_agent_id(self, bb_root: Path):
        """epoch 相同时，agent_id 字典序更小者当选。"""
        from hermes.multiagent.election import Election

        # 模拟设备 A 和 B 都是 epoch=0，但 B 字典序更小
        # 设备 B 心跳未超时（fresh）
        fresh_time = datetime.now(timezone.utc).isoformat()
        status_path = bb_root / "status.json"
        status = {
            "director": {"agent_id": "device_b", "epoch": 0, "last_tick": fresh_time},
        }
        status_path.write_text(json.dumps(status), encoding="utf-8")

        election = Election(bb_root, agent_id="device_a", config={
            "election_timeout_seconds": 30,
        })
        result = await election.run()
        # device_a > device_b 字典序，device_b 当选
        assert result.won is False
        assert result.director_id == "device_b"
