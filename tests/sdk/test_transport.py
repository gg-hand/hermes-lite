"""SDK 传输层测试（A2A Client + Server + Forward 封装）。"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from teage_liu.sdk.transport import A2ATransport
from teage_liu.sdk.exceptions import AuthenticationError, TransportError


@pytest.fixture
def key_pair():
    """生成 ed25519 密钥对。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.generate()


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "keys").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def transport(key_pair, bb_root):
    """A2ATransport 实例（mock A2AClient + A2AServer + Forward）。"""
    private_key = key_pair
    transport = A2ATransport(
        agent_id="agent_alice",
        private_key=private_key,
        bb_root=bb_root,
        forward_endpoint="http://workbench:18400",
    )
    # mock A2A Client（出站调用其他 agent）
    transport._a2a_client = MagicMock()
    transport._a2a_client.call_method = AsyncMock()
    transport._a2a_client.close = AsyncMock()
    # mock Forward HTTP client（归档到工作台）
    transport._forward_client = MagicMock()
    transport._forward_client.post = AsyncMock()
    return transport


@pytest.mark.asyncio
async def test_call_agent_method_success(transport):
    """调用其他 agent 的 A2A Server 方法成功。"""
    transport._a2a_client.call_method.return_value = {"ok": True, "received_by": "agent_bob"}
    result = await transport.call_agent("agent_bob", "send_message", {
        "from": "agent_alice", "to": "agent_bob", "content": "hi",
    })
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_call_agent_delegates_signing_to_a2a_client(transport):
    """transport 层不签名，签名由 A2AClient.call_method 单点负责。

    架构变更：原 transport._sign_params 已移除，避免与 A2AClient._sign_params
    双重签名。签名正确性由 test_a2a_client.py::test_sign_request_with_ed25519 覆盖。
    """
    transport._a2a_client.call_method.return_value = {"ok": True}
    await transport.call_agent("agent_bob", "send_message", {
        "from": "agent_alice", "to": "agent_bob", "content": "hi",
    })
    call_args = transport._a2a_client.call_method.call_args
    params = call_args.args[2]
    # transport 不附加 signature，签名延迟到 a2a_client 内部
    assert "signature" not in params


@pytest.mark.asyncio
async def test_forward_to_workbench(transport):
    """forward_to_workbench 归档消息副本到 collaboration.md。"""
    transport._forward_client.post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"ok": True, "seq": 42, "deduplicated": False}),
    )
    result = await transport.forward_to_workbench(
        original_from="agent_alice",
        original_to="agent_bob",
        content="A2A 消息内容",
        message_id="msg_001",
    )
    assert result["ok"] is True
    assert result["seq"] == 42
    # 验证 POST 调用包含签名
    post_args = transport._forward_client.post.call_args
    payload = post_args.kwargs["json"]
    assert "signature" in payload


@pytest.mark.asyncio
async def test_call_agent_signature_error_mapped(transport):
    """签名错误映射为 AuthenticationError。"""
    from teage_liu.multiagent.a2a_client import A2AClientError
    transport._a2a_client.call_method.side_effect = A2AClientError(
        "Signature verification failed", code=-32001
    )
    with pytest.raises(AuthenticationError):
        await transport.call_agent("agent_bob", "send_message", {
            "from": "agent_alice", "to": "agent_bob", "content": "hi",
        })


@pytest.mark.asyncio
async def test_call_agent_transport_error_mapped(transport):
    """网络错误映射为 TransportError。"""
    from teage_liu.multiagent.a2a_client import A2AClientError
    transport._a2a_client.call_method.side_effect = A2AClientError("Connection refused")
    with pytest.raises(TransportError):
        await transport.call_agent("agent_bob", "ping", {})


@pytest.mark.asyncio
async def test_forward_rejected_by_workbench(transport):
    """工作台拒绝 forward（签名无效）返回 AuthenticationError。"""
    transport._forward_client.post.return_value = MagicMock(
        status_code=401,
        json=MagicMock(return_value={"detail": "Signature verification failed"}),
    )
    with pytest.raises(AuthenticationError):
        await transport.forward_to_workbench(
            original_from="agent_alice",
            original_to="agent_bob",
            content="test",
            message_id="msg_002",
        )
