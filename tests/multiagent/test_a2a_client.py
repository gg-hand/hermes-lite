"""A2A 客户端测试（httpx 异步, Task 2, Plan 3）。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx


@pytest.fixture
def client_config() -> dict:
    return {
        "a2a": {
            "remote_endpoints": [
                {"name": "device_b", "url": "http://127.0.0.1:18401"},
                {"name": "device_c", "url": "http://127.0.0.1:18402"},
            ],
            "timeout_seconds": 10,
            "retry_count": 2,
        }
    }


class TestA2AClient:
    """A2A 客户端测试。"""

    @pytest.mark.asyncio
    async def test_client_initialization(self, client_config):
        """客户端正确初始化。"""
        from teage_liu.multiagent.a2a_client import A2AClient
        client = A2AClient(client_config)
        assert len(client._endpoints) == 2
        assert client._endpoints[0]["name"] == "device_b"

    @pytest.mark.asyncio
    async def test_call_method_returns_result(self, client_config):
        """call_method 返回 JSON-RPC result。"""
        from teage_liu.multiagent.a2a_client import A2AClient

        mock_response = httpx.Response(
            200,
            json={"jsonrpc": "2.0", "result": {"agents": []}, "id": 1},
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            client = A2AClient(client_config)
            result = await client.call_method("device_b", "list_agents", {})
            assert result == {"agents": []}

    @pytest.mark.asyncio
    async def test_call_method_returns_error(self, client_config):
        """call_method 返回 JSON-RPC error。"""
        from teage_liu.multiagent.a2a_client import A2AClient, A2AClientError

        mock_response = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": "Method not found"},
                "id": 1,
            },
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            client = A2AClient(client_config)
            with pytest.raises(A2AClientError, match="Method not found"):
                await client.call_method("device_b", "nonexistent", {})

    @pytest.mark.asyncio
    async def test_call_method_with_retry(self, client_config):
        """网络错误时自动重试。"""
        from teage_liu.multiagent.a2a_client import A2AClient

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": "ok", "id": 1})

        with patch("httpx.AsyncClient.post", new=mock_post):
            client = A2AClient(client_config)
            result = await client.call_method("device_b", "test_method", {})
            assert result == "ok"
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_call_method_retry_exhausted(self, client_config):
        """重试耗尽后抛出 A2AClientError。"""
        from teage_liu.multiagent.a2a_client import A2AClient, A2AClientError

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
                   side_effect=httpx.ConnectError("Connection refused")):
            client = A2AClient(client_config)
            with pytest.raises(A2AClientError, match="Connection refused"):
                await client.call_method("device_b", "test_method", {})

    @pytest.mark.asyncio
    async def test_call_all_endpoints(self, client_config):
        """call_all_endpoints 并行调用所有端点。"""
        from teage_liu.multiagent.a2a_client import A2AClient

        mock_response = httpx.Response(
            200, json={"jsonrpc": "2.0", "result": "ok", "id": 1}
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            client = A2AClient(client_config)
            results = await client.call_all_endpoints("health", {})
            assert "device_b" in results
            assert "device_c" in results
            assert results["device_b"] == "ok"

    @pytest.mark.asyncio
    async def test_sign_request_with_ed25519(self, client_config):
        """请求自动签名（ed25519）。"""
        from teage_liu.multiagent.a2a_client import A2AClient
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        client = A2AClient(client_config, signer_id="remote_001", private_key=private_key)

        mock_response = httpx.Response(
            200, json={"jsonrpc": "2.0", "result": {"ok": True}, "id": 1}
        )

        captured_request = {}

        async def capture_post(*args, **kwargs):
            captured_request["json"] = kwargs.get("json")
            return mock_response

        with patch("httpx.AsyncClient.post", new=capture_post):
            await client.call_method(
                "device_b", "append_message", {"message": {"from": "remote_001"}}
            )

        # 验证签名字段存在
        assert "signature" in captured_request["json"]["params"]

    @pytest.mark.asyncio
    async def test_client_context_manager(self, client_config):
        """客户端可作为 async context manager 使用。"""
        from teage_liu.multiagent.a2a_client import A2AClient

        async with A2AClient(client_config) as client:
            assert client._http_client is not None
        # 退出后应关闭
        assert client._closed is True
