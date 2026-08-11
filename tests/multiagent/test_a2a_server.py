"""A2A Server（agent 侧）测试。"""
from __future__ import annotations

import pytest
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from teage_liu.multiagent.a2a_server import A2AServer
from teage_liu.multiagent.message_signature import sign_message


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "keys").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def key_pair():
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


@pytest.fixture
def registered_agent(bb_root: Path, key_pair):
    private_key, public_key = key_pair
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (bb_root / "agents" / "keys" / "agent_alice.pem").write_bytes(pem)
    return private_key


@pytest.fixture
def server(bb_root: Path):
    """A2A Server 实例。"""
    return A2AServer(
        agent_id="agent_bob",
        bb_root=bb_root,
    )


@pytest.fixture
def app(server: A2AServer) -> FastAPI:
    application = FastAPI()
    application.include_router(server.create_router())
    return application


class TestA2AServer:
    """A2A Server 核心功能测试。"""

    def test_register_custom_handler(self, server: A2AServer):
        """注册自定义 JSON-RPC 方法。"""
        async def handle_greet(params: dict) -> dict:
            return {"greeting": f"Hello {params.get('name', 'stranger')}"}

        server.register_handler("greet", handle_greet)
        assert "greet" in server._handlers

    def test_call_custom_handler(self, server: A2AServer, app: FastAPI):
        """调用自定义方法返回结果。"""
        async def handle_greet(params: dict) -> dict:
            return {"greeting": f"Hello {params.get('name', 'stranger')}"}

        server.register_handler("greet", handle_greet)

        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "greet",
                "params": {"name": "alice"},
                "id": 1,
            })
            data = resp.json()
            assert data["result"]["greeting"] == "Hello alice"

    def test_unknown_method_returns_error(self, app: FastAPI):
        """未知方法返回 -32601。"""
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "nonexistent",
                "params": {},
                "id": 1,
            })
            data = resp.json()
            assert data["error"]["code"] == -32601

    def test_builtin_send_message_rejects_missing_signature(
        self, app: FastAPI
    ):
        """内置 send_message 方法拒绝无签名消息。"""
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "send_message",
                "params": {
                    "from": "agent_alice",
                    "to": "agent_bob",
                    "content": "hi",
                    "signature": "",
                },
                "id": 1,
            })
            data = resp.json()
            assert data["error"]["code"] == -32001

    def test_builtin_send_message_accepts_valid_signature(
        self, app: FastAPI, registered_agent
    ):
        """内置 send_message 方法接受有效签名。"""
        private_key = registered_agent
        message = {
            "from": "agent_alice",
            "to": "agent_bob",
            "content": "hello from alice",
        }
        signature = sign_message(message, private_key)

        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "send_message",
                "params": {**message, "signature": signature},
                "id": 1,
            })
            data = resp.json()
            assert "result" in data
            assert data["result"]["ok"] is True

    def test_builtin_query_capabilities(self, app: FastAPI):
        """内置 query_capabilities 方法返回 agent 能力。"""
        # server fixture 的 agent_bob 需设置 capabilities
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "query_capabilities",
                "params": {},
                "id": 1,
            })
            data = resp.json()
            assert "result" in data
            assert "capabilities" in data["result"]

    def test_message_callback_invoked(self, server: A2AServer, app: FastAPI, registered_agent):
        """收到消息后触发注册的 callback。"""
        received = []

        async def on_message(message: dict) -> None:
            received.append(message)

        server.on_message_callback = on_message

        private_key = registered_agent
        message = {
            "from": "agent_alice",
            "to": "agent_bob",
            "content": "callback test",
        }
        signature = sign_message(message, private_key)

        with TestClient(app) as client:
            client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "send_message",
                "params": {**message, "signature": signature},
                "id": 1,
            })

        assert len(received) == 1
        assert received[0]["content"] == "callback test"
