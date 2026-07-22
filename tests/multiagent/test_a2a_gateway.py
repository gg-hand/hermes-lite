"""A2A Gateway 服务端测试（Task 1, Plan 3）。"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teage_liu.multiagent.blackboard import Blackboard


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录（含完整骨架）。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def gateway_config() -> dict:
    return {
        "a2a": {
            "enabled": True,
            "listen_host": "127.0.0.1",
            "listen_port": 18400,
            "tls": {"enabled": False},
            "rate_limit_per_second": 100,
            "max_request_size": 1024 * 1024,
        }
    }


def _make_app(bb_root: Path, config: dict) -> FastAPI:
    from teage_liu.multiagent.a2a_gateway import create_a2a_router
    app = FastAPI()
    app.include_router(create_a2a_router(bb_root, config))
    return app


class TestA2AGatewayEndpoints:
    """A2A Gateway 端点测试。"""

    @pytest.mark.asyncio
    async def test_health_endpoint(self, bb_root: Path, gateway_config):
        """GET /a2a/health 返回 200。"""
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.get("/a2a/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "version" in data

    @pytest.mark.asyncio
    async def test_jsonrpc_unknown_method(self, bb_root: Path, gateway_config):
        """未知 JSON-RPC 方法返回 -32601 错误。"""
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "nonexistent_method",
                "params": {},
                "id": 1,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["error"]["code"] == -32601
            assert "method not found" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_jsonrpc_list_agents(self, bb_root: Path, gateway_config):
        """JSON-RPC list_agents 返回 active agents 列表。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator())
        agent_card = {
            "agent_id": "remote_001",
            "agent_version": "1.0.0",
            "protocol_version": "1.0.0",
            "created_at": "2026-07-21T00:00:00Z",
            "last_heartbeat": "2026-07-21T00:00:00Z",
            "heartbeat_interval_seconds": 10,
            "status": "active",
            "role": "worker",
            "endpoint": "http://127.0.0.1:18401",
            "owner": "remote",
            "capabilities": ["file_read"],
            "specialties": [],
            "auth_method": "signed",
            "trust_score": 100,
            "trust_history": [],
            "extensions": {},
            "leave_reason": "",
            "left_at": "",
        }
        await registry.register(agent_card)

        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "list_agents",
                "params": {},
                "id": 2,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "result" in data
            agents = data["result"].get("agents", [])
            assert any(a.get("agent_id") == "remote_001" for a in agents)

    @pytest.mark.asyncio
    async def test_jsonrpc_read_messages(self, bb_root: Path, gateway_config):
        """JSON-RPC read_messages 返回 messages.md 内容。"""
        from teage_liu.multiagent.blackboard import append_message

        await append_message(bb_root, {
            "seq": 1, "from": "remote_001", "to": "*",
            "timestamp": "2026-07-21T00:00:00Z",
            "type": "chat", "content_type": "markdown", "epoch": 0,
        })

        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_messages",
                "params": {"limit": 10},
                "id": 3,
            })
            assert resp.status_code == 200
            data = resp.json()
            messages = data["result"].get("messages", [])
            assert len(messages) >= 1
            assert messages[0]["from"] == "remote_001"

    @pytest.mark.asyncio
    async def test_jsonrpc_append_message_validates_signature(
        self, bb_root: Path, gateway_config
    ):
        """JSON-RPC append_message 校验签名（无签名 → 拒绝 -32001）。"""
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "append_message",
                "params": {
                    "message": {
                        "seq": 1, "from": "remote_001", "to": "*",
                        "timestamp": "2026-07-21T00:00:00Z",
                        "type": "chat", "content_type": "markdown", "epoch": 0,
                    },
                    # 无 signature 字段
                },
                "id": 4,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == -32001  # 签名错误

    @pytest.mark.asyncio
    async def test_jsonrpc_acquire_lock(self, bb_root: Path, gateway_config):
        """JSON-RPC acquire_lock 获取跨设备锁。"""
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "acquire_lock",
                "params": {
                    "lock_name": "messages.md",
                    "agent_id": "remote_001",
                    "fencing_token": 1,
                    "ttl_seconds": 30,
                },
                "id": 5,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["result"]["acquired"] is True


class TestA2AGatewayPathSandbox:
    """A2A Gateway 路径沙箱测试。"""

    @pytest.mark.asyncio
    async def test_path_with_absolute_rejected(self, bb_root: Path, gateway_config):
        """请求中包含绝对路径 → 拒绝。"""
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "/etc/passwd"},
                "id": 6,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == -32002
            assert "absolute" in data["error"]["message"].lower() or \
                   "绝对" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_path_with_traversal_rejected(self, bb_root: Path, gateway_config):
        """请求中包含 .. 路径穿越 → 拒绝。"""
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "../../../etc/passwd"},
                "id": 7,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == -32002
            assert "traversal" in data["error"]["message"].lower() or \
                   "穿越" in data["error"]["message"]


class TestA2AGatewayRateLimit:
    """A2A Gateway 限流测试。"""

    @pytest.mark.asyncio
    async def test_rate_limit_429_on_exceed(self, bb_root: Path):
        """超过限流阈值返回 429。"""
        config = {
            "a2a": {
                "enabled": True,
                "rate_limit_per_second": 2,  # 极低阈值便于测试
            }
        }
        app = _make_app(bb_root, config)
        with TestClient(app) as client:
            # 前两次正常
            for _ in range(2):
                resp = client.get("/a2a/health")
                assert resp.status_code == 200
            # 第三次应被限流
            resp = client.get("/a2a/health")
            assert resp.status_code == 429
