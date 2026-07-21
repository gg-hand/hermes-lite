"""远程 agent 适配器测试（Task 3, Plan 3）。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from hermes.multiagent.blackboard import Blackboard


@pytest_asyncio.fixture
async def local_bb(tmp_path: Path) -> Path:
    bb_root = tmp_path / "local"
    bb = Blackboard(bb_root)
    await bb.init_blackboard()
    return bb_root


@pytest.fixture
def remote_config() -> dict:
    return {
        "a2a": {
            "remote_endpoints": [
                {"name": "device_b", "url": "http://127.0.0.1:18401"},
            ],
            "timeout_seconds": 5,
            "retry_count": 1,
        },
        "multiagent": {
            "role": "remote_worker",
            "agent_id": "remote_worker_001",
            "heartbeat_interval_seconds": 1,
            "capabilities": ["file_read"],
        },
    }


class TestRemoteAgentAdapter:
    """远程 agent 适配器测试。"""

    @pytest.mark.asyncio
    async def test_adapter_initialization(self, local_bb: Path, remote_config):
        """适配器正确初始化。"""
        from hermes.multiagent.remote_agent_adapter import RemoteAgentAdapter
        adapter = RemoteAgentAdapter(local_bb, remote_config, agent_id="remote_worker_001")
        assert adapter._agent_id == "remote_worker_001"
        assert adapter._a2a_client is not None

    @pytest.mark.asyncio
    async def test_register_to_remote(self, local_bb: Path, remote_config):
        """注册到远程 blackboard。"""
        from hermes.multiagent.remote_agent_adapter import RemoteAgentAdapter

        adapter = RemoteAgentAdapter(local_bb, remote_config, agent_id="remote_worker_001")

        with patch.object(
            adapter._a2a_client, "call_method", new_callable=AsyncMock, return_value={"registered": True}
        ):
            result = await adapter.register_to_remote("device_b")
            assert result["registered"] is True

    @pytest.mark.asyncio
    async def test_heartbeat_loop_calls_remote(self, local_bb: Path, remote_config):
        """心跳调用远程 heartbeat 方法。"""
        from hermes.multiagent.remote_agent_adapter import RemoteAgentAdapter

        adapter = RemoteAgentAdapter(local_bb, remote_config, agent_id="remote_worker_001")

        mock_call = AsyncMock(return_value={"ok": True})
        with patch.object(adapter._a2a_client, "call_method", new=mock_call):
            await adapter._send_heartbeat("device_b")
            mock_call.assert_called_once()
            args = mock_call.call_args
            assert args.args[1] == "heartbeat"

    @pytest.mark.asyncio
    async def test_read_remote_messages(self, local_bb: Path, remote_config):
        """读取远程消息。"""
        from hermes.multiagent.remote_agent_adapter import RemoteAgentAdapter

        adapter = RemoteAgentAdapter(local_bb, remote_config, agent_id="remote_worker_001")

        with patch.object(
            adapter._a2a_client, "call_method", new_callable=AsyncMock,
            return_value={"messages": [{"seq": 1, "from": "remote_002"}]},
        ):
            messages = await adapter.read_remote_messages("device_b", limit=10)
            assert len(messages) == 1
            assert messages[0]["from"] == "remote_002"

    @pytest.mark.asyncio
    async def test_append_remote_message_with_signature(self, local_bb: Path, remote_config):
        """向远程 blackboard 追加消息（带签名）。"""
        from hermes.multiagent.remote_agent_adapter import RemoteAgentAdapter
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        adapter = RemoteAgentAdapter(
            local_bb, remote_config, agent_id="remote_worker_001", private_key=private_key,
        )

        captured = {}

        async def mock_call(endpoint, method, params):
            captured["method"] = method
            captured["params"] = params
            return {"ok": True, "seq": 1}

        with patch.object(adapter._a2a_client, "call_method", new=mock_call):
            await adapter.append_remote_message("device_b", {
                "seq": 1, "from": "remote_worker_001", "to": "*",
                "type": "chat", "content_type": "markdown",
                "timestamp": "2026-07-21T00:00:00Z", "epoch": 0,
            })

        assert captured["method"] == "append_message"
        assert "signature" in captured["params"]

    @pytest.mark.asyncio
    async def test_acquire_remote_lock(self, local_bb: Path, remote_config):
        """获取远程锁。"""
        from hermes.multiagent.remote_agent_adapter import RemoteAgentAdapter

        adapter = RemoteAgentAdapter(local_bb, remote_config, agent_id="remote_worker_001")

        with patch.object(
            adapter._a2a_client, "call_method", new_callable=AsyncMock,
            return_value={"acquired": True, "fencing_token": 1},
        ):
            result = await adapter.acquire_remote_lock(
                "device_b", "messages.md", fencing_token=1, ttl_seconds=30
            )
            assert result["acquired"] is True

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self, local_bb: Path, remote_config):
        """适配器 start/stop 生命周期。"""
        from hermes.multiagent.remote_agent_adapter import RemoteAgentAdapter

        adapter = RemoteAgentAdapter(local_bb, remote_config, agent_id="remote_worker_001")

        with patch.object(adapter._a2a_client, "call_method", new_callable=AsyncMock, return_value={"ok": True}):
            await adapter.start()
            assert adapter._running is True

            await asyncio.sleep(0.05)

            await adapter.stop()
            assert adapter._running is False
            assert adapter._a2a_client._closed is True
