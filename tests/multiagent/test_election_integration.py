"""Election 接入 WorkerAdapter 的集成测试（Task 1.1）。

覆盖：
- Director 心跳超时触发 Election.run()（本机败选时不进自治）
- Election 胜出时调用 LocalDirectorManager.start() 并返回 "healthy"
- Election.run() 抛异常时回退到自治模式
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from teage_liu.multiagent.blackboard import Blackboard
from teage_liu.multiagent.election import ElectionResult
from teage_liu.multiagent.worker_adapter import WorkerAdapter


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
                "capabilities": ["file_read"],
                "dangerous_tools": [],
            },
            "director": {
                "heartbeat_timeout_seconds": 30,
                "degraded_threshold_seconds": 20,
            },
        }
    }


def _stale_director_md() -> dict:
    """构造一个心跳超时的 director.md 字典（60s 前，超过 30s timeout）。"""
    stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    return {
        "last_director_tick": stale_tick,
        "current_epoch": 1,
        "heartbeat": {"interval_seconds": 10},
    }


class TestElectionIntegration:
    """Election 接入 _check_director_health 的集成测试。"""

    @pytest.mark.asyncio
    async def test_election_triggered_on_director_timeout(
        self, bb_root: Path, worker_config
    ):
        """Director 心跳超时 → 触发 Election.run()，本机不进自治模式。

        场景：本地 director.md 心跳超时；远程端点 epoch=3 当选（本机败选）。
        断言：Election.run() 被调用；_autonomous.enter 未被调用。
        """
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._autonomous.enter = AsyncMock()
        # 选举失败后会 sleep(election_wait_seconds) 再递归重检；置 0 避免真实等待
        worker_config["multiagent"]["director"]["election_wait_seconds"] = 0

        # 第一次 read_director_md 返回 stale（触发选举）；
        # 第二次（递归重检）返回 None → 直接 "offline"，终止递归
        with patch(
            "teage_liu.multiagent.worker_adapter.read_director_md",
            AsyncMock(side_effect=[_stale_director_md(), None]),
        ), patch(
            "teage_liu.multiagent.election.Election"
        ) as MockElection:
            mock_election = MockElection.return_value
            mock_election.run = AsyncMock(
                return_value=ElectionResult(
                    won=False,
                    director_id="remote_agent",
                    epoch=3,
                    reason="lower_epoch",
                )
            )
            result = await adapter._check_director_health()

        # Election.run() 被调用
        mock_election.run.assert_awaited_once()
        # 未进入自治模式
        adapter._autonomous.enter.assert_not_awaited()
        assert adapter._autonomous_mode is False
        # 递归后 director.md 缺失 → "offline"
        assert result == "offline"

    @pytest.mark.asyncio
    async def test_election_winner_starts_director(
        self, bb_root: Path, worker_config
    ):
        """Election 胜出 → 调用 LocalDirectorManager.start() 并返回 "healthy"。

        场景：Election.run() 返回 won=True, epoch=5。
        断言：_director_manager.start() 被调用；函数返回 "healthy"；不进自治。
        """
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._autonomous.enter = AsyncMock()
        # 注入 mock director manager（模拟 LocalDirectorManager 实例）
        adapter._director_manager = AsyncMock()

        with patch(
            "teage_liu.multiagent.worker_adapter.read_director_md",
            AsyncMock(return_value=_stale_director_md()),
        ), patch(
            "teage_liu.multiagent.election.Election"
        ) as MockElection:
            mock_election = MockElection.return_value
            mock_election.run = AsyncMock(
                return_value=ElectionResult(
                    won=True,
                    director_id="worker_001",
                    epoch=5,
                    reason="preempt_stale",
                )
            )
            result = await adapter._check_director_health()

        # director manager.start() 被调用
        adapter._director_manager.start.assert_awaited_once()
        # 返回 healthy
        assert result == "healthy"
        # 不进自治
        adapter._autonomous.enter.assert_not_awaited()
        assert adapter._autonomous_mode is False

    @pytest.mark.asyncio
    async def test_election_fallback_to_autonomous_on_exception(
        self, bb_root: Path, worker_config
    ):
        """Election.run() 抛异常 → 回退到自治模式。

        场景：Election.run() 抛 RuntimeError。
        断言：_autonomous.enter 被调用；进入自治模式。
        """
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._autonomous.enter = AsyncMock()

        with patch(
            "teage_liu.multiagent.worker_adapter.read_director_md",
            AsyncMock(return_value=_stale_director_md()),
        ), patch(
            "teage_liu.multiagent.election.Election"
        ) as MockElection:
            mock_election = MockElection.return_value
            mock_election.run = AsyncMock(side_effect=RuntimeError("election boom"))
            result = await adapter._check_director_health()

        # 回退到自治模式
        adapter._autonomous.enter.assert_awaited_once()
        assert adapter._autonomous_mode is True
        assert adapter._autonomous_epoch == 1
        assert result == "offline"
