"""A2A Gateway 服务端测试（Task 1, Plan 3）。"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

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
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
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
                   "绝对" in data["error"]["message"] or \
                   "not in readable whitelist" in data["error"]["message"].lower()

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
                   "穿越" in data["error"]["message"] or \
                   "not in readable whitelist" in data["error"]["message"].lower()


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


class TestA2AGatewayReadFileACL:
    """A2A Gateway read_file ACL 测试。"""

    @pytest.mark.asyncio
    async def test_read_file_allows_protocol_md(self, bb_root: Path, gateway_config):
        """允许读取 protocol.md。"""
        (bb_root / "protocol.md").write_text("# Protocol", encoding="utf-8")
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "protocol.md"},
                "id": 1,
            })
            data = resp.json()
            assert "result" in data
            assert data["result"]["exists"] is True

    @pytest.mark.asyncio
    async def test_read_file_allows_director_md(self, bb_root: Path, gateway_config):
        """允许读取 director.md。"""
        (bb_root / "director.md").write_text("---\n---\n", encoding="utf-8")
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "director.md"},
                "id": 1,
            })
            data = resp.json()
            assert data["result"]["exists"] is True

    @pytest.mark.asyncio
    async def test_read_file_rejects_agents_dir(self, bb_root: Path, gateway_config):
        """禁止读取 agents/ 目录（含 agent 元数据）。"""
        (bb_root / "agents").mkdir(exist_ok=True)
        (bb_root / "agents" / "secret.md").write_text("secret", encoding="utf-8")
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "agents/secret.md"},
                "id": 1,
            })
            data = resp.json()
            assert data["error"]["code"] == -32002

    @pytest.mark.asyncio
    async def test_read_file_rejects_audit_dir(self, bb_root: Path, gateway_config):
        """禁止读取 audit/ 目录。"""
        (bb_root / "audit").mkdir(exist_ok=True)
        (bb_root / "audit" / "audit.jsonl").write_text("[]", encoding="utf-8")
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "audit/audit.jsonl"},
                "id": 1,
            })
            data = resp.json()
            assert data["error"]["code"] == -32002

    @pytest.mark.asyncio
    async def test_read_file_rejects_status_json(self, bb_root: Path, gateway_config):
        """禁止读取 status.json（含 fencing_token 等敏感字段）。"""
        (bb_root / "status.json").write_text("{}", encoding="utf-8")
        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "status.json"},
                "id": 1,
            })
            data = resp.json()
            assert data["error"]["code"] == -32002


class TestA2AGatewayHeartbeat:
    """A2A Gateway heartbeat 字段统一测试。"""

    @pytest.mark.asyncio
    async def test_heartbeat_with_status_and_current_task(
        self, bb_root: Path, gateway_config
    ):
        """heartbeat 接受 status 和 current_task 字段。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        # 先注册一个 agent
        registry = AgentRegistry(bb_root, SchemaValidator())
        await registry.register({
            "agent_id": "agent_test_hb",
            "agent_version": "1.0.0",
            "protocol_version": "1.0.0",
            "status": "active",
            "role": "worker",
            "last_heartbeat": "2026-01-01T00:00:00+00:00",
            "heartbeat_interval_seconds": 10,
        })

        app = _make_app(bb_root, gateway_config)
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": "heartbeat",
                "params": {
                    "agent_id": "agent_test_hb",
                    "status": "busy",
                    "current_task": "task-001",
                    "ts": "2026-07-29T10:00:00+00:00",
                },
                "id": 1,
            })
            data = resp.json()
            assert "result" in data
            assert data["result"]["ok"] is True

        # 验证 agent_card 的 status 已更新
        card = await registry.get_agent("agent_test_hb")
        assert card["status"] == "busy"


class TestCollabMessageMethod:
    """主会话协作通道 collab_message JSON-RPC 方法测试。"""

    @pytest.fixture
    def main_gateway_config(self):
        return {
            "multiagent": {"worker": {"agent_id": "teagent-lu"}},
            "a2a": {
                "enabled": True,
                "rate_limit_per_second": 100,
            },
        }

    def _call(self, app, method, params, req_id=1):
        with TestClient(app) as client:
            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0", "method": method, "params": params, "id": req_id,
            })
            return resp.json()

    def test_request_to_self_written_to_main_collab(self, bb_root, main_gateway_config):
        """request 且 to==本机 → 写入 collabs/main_{cid}.md。"""
        app = _make_app(bb_root, main_gateway_config)
        data = self._call(app, "collab_message", {
            "channel": "main_session", "type": "request",
            "from": "teagent-lu", "to": "teagent-lu",
            "content": "帮我总结", "collab_id": "main_abc123",
            "message_id": "main_msg_1", "priority": "high",
            "wait_for_response": True,
        })
        assert "result" in data
        assert data["result"]["ok"] is True
        file = bb_root / "collabs" / "main_abc123.md"
        assert file.exists()
        text = file.read_text(encoding="utf-8")
        assert "channel: main_session" in text
        assert "type: request" in text

    def test_request_to_other_ignored(self, bb_root, main_gateway_config):
        """request 且 to!=本机 → not_target 忽略（不写镜像）。"""
        app = _make_app(bb_root, main_gateway_config)
        data = self._call(app, "collab_message", {
            "channel": "main_session", "type": "request",
            "from": "teagent-lu", "to": "teagent-liu-2",
            "content": "不是给你的", "collab_id": "main_abc456",
            "message_id": "main_msg_2",
        })
        assert data["result"]["ok"] is False
        assert data["result"]["ignored"] == "not_target"
        assert not (bb_root / "collabs" / "main_abc456.md").exists()

    def test_response_unknown_collab_discarded(self, bb_root, main_gateway_config):
        """response 且本地无该 collab → not_participant（防广播污染）。"""
        app = _make_app(bb_root, main_gateway_config)
        data = self._call(app, "collab_message", {
            "channel": "main_session", "type": "response",
            "from": "teagent-liu-2", "to": "teagent-lu",
            "content": "结果", "collab_id": "main_nonexist",
            "message_id": "main_msg_3",
        })
        assert data["result"]["ok"] is False
        assert data["result"]["ignored"] == "not_participant"

    def test_response_known_collab_written(self, bb_root, main_gateway_config):
        """先有 request，response 且 collab 在 index → 写入镜像。"""
        app = _make_app(bb_root, main_gateway_config)
        cid = "main_known1"
        # 先写 request（建 index）
        self._call(app, "collab_message", {
            "channel": "main_session", "type": "request",
            "from": "teagent-lu", "to": "teagent-lu",
            "content": "发起", "collab_id": cid,
            "message_id": "main_msg_4",
        })
        # 再写 response
        data = self._call(app, "collab_message", {
            "channel": "main_session", "type": "response",
            "from": "teagent-liu-2", "to": "teagent-lu",
            "content": "结果内容", "collab_id": cid,
            "message_id": "main_msg_5",
        })
        assert data["result"]["ok"] is True
        text = (bb_root / "collabs" / f"{cid}.md").read_text(encoding="utf-8")
        assert "type: response" in text
        assert "teagent-liu-2" in text

    def test_non_main_namespace_rejected(self, bb_root, main_gateway_config):
        """collab_id 非 main_ 前缀 → invalid_collab_namespace。"""
        app = _make_app(bb_root, main_gateway_config)
        data = self._call(app, "collab_message", {
            "channel": "main_session", "type": "request",
            "from": "teagent-lu", "to": "teagent-lu",
            "content": "非法命名空间", "collab_id": "collab_worker_1",
            "message_id": "main_msg_6",
        })
        assert data["result"]["ok"] is False
        assert data["result"]["ignored"] == "invalid_collab_namespace"
