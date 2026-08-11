"""标准 A2A 端点测试（Agent Card + JSON-RPC 方法 + 鉴权/头/批量）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teage_liu.multiagent.a2a_std.router import create_a2a_std_router


def _make_app(bb_root: Path, config: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(create_a2a_std_router(bb_root, config))
    return app


def _base_config(**overrides) -> dict:
    """最小配置（a2a.standard 全默认）。"""
    config = {
        "a2a": {
            "enabled": True,
            "standard": {
                "enabled": True,
                "name": "teagent-lu",
                "description": "test agent",
                "capabilities": {"streaming": True, "push_notifications": False},
            },
        },
        "multiagent": {
            "role": "worker",
            "worker": {"agent_id": "teagent-lu"},
            "blackboard_dir": "data/blackboard",
        },
        "security": {"api_key": ""},
        "server": {"port": 8000},
    }
    # 应用 overrides
    def _deep_merge(base: dict, ov: dict) -> dict:
        for k, v in ov.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    return _deep_merge(config, overrides)


def _send(client: TestClient, method: str, params: dict, headers: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    resp = client.post("/a2a/std/jsonrpc", json=payload, headers=headers)
    assert resp.status_code == 200
    return resp.json()


def _message(text: str = "你好", meta: dict | None = None) -> dict:
    return {
        "role": "user",
        "messageId": "m_" + text[:8],
        "parts": [{"kind": "text", "text": text}],
        "metadata": meta or {},
    }


class TestAgentCard:
    """Agent Card 发布。"""

    def test_card_served(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            resp = client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200
        card = resp.json()
        assert card["protocolVersion"] == "1.0"
        assert card["preferredTransport"] == "JSONRPC"
        assert card["name"] == "teagent-lu"
        assert card["url"].endswith("/a2a/std/jsonrpc")
        assert card["capabilities"]["streaming"] is True
        assert card["capabilities"]["pushNotifications"] is False
        assert card["defaultInputModes"] == ["text", "text/plain"]
        assert "extensions" in card

    def test_card_no_cache(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            resp = client.get("/.well-known/agent-card.json")
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_card_bearer_scheme_when_api_key(self, tmp_path):
        """securitySchemes 线格式为 map（官方 SDK v1.0 实现）。"""
        app = _make_app(tmp_path, _base_config(security={"api_key": "sekret"}))
        with TestClient(app) as client:
            card = client.get("/.well-known/agent-card.json").json()
        assert card["securitySchemes"] == {"bearer": {"scheme": "bearer"}}

    def test_card_no_scheme_without_api_key(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            card = client.get("/.well-known/agent-card.json").json()
        assert "securitySchemes" not in card

    def test_card_endpoint_url_override(self, tmp_path):
        app = _make_app(
            tmp_path,
            _base_config(a2a={"standard": {"endpoint_url": "https://a.example.com/a2a/std/jsonrpc"}}),
        )
        with TestClient(app) as client:
            card = client.get("/.well-known/agent-card.json").json()
        assert card["url"] == "https://a.example.com/a2a/std/jsonrpc"


class TestJsonRpcMethods:
    """标准方法。"""

    def test_message_send_returns_task(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "message/send", {
                "message": _message("帮我查资料", {"to": "*", "from": "teagent-lu"}),
            })
        task = data["result"]
        assert task["id"].startswith("t_")
        assert task["status"]["state"] == "working"
        assert task["contextId"].startswith("a2a_")
        assert "history" in task

    def test_message_send_with_context(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "message/send", {
                "message": _message("任务1"),
                "contextId": "collab_abc",
            })
        task = data["result"]
        assert task["contextId"] == "collab_abc"

    def test_message_send_invalid_params(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "message/send", {"message": {"role": "bad"}})
        assert data["error"]["code"] == -32602

    def test_tasks_get(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            task = _send(client, "message/send", {"message": _message("hi")})["result"]
            got = _send(client, "tasks/get", {"id": task["id"]})["result"]
        assert got["id"] == task["id"]
        assert got["status"]["state"] == "working"

    def test_tasks_get_not_found(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "tasks/get", {"id": "t_nope"})
        assert data["error"]["code"] == -32001

    def test_tasks_list(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            _send(client, "message/send", {"message": _message("a")})
            data = _send(client, "tasks/list", {})
        assert len(data["result"]) >= 1

    def test_tasks_cancel(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            task = _send(client, "message/send", {"message": _message("hi")})["result"]
            cancelled = _send(client, "tasks/cancel", {"id": task["id"]})["result"]
        assert cancelled["status"]["state"] == "canceled"

    def test_cancel_terminal_rejected(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            task = _send(client, "message/send", {"message": _message("hi")})["result"]
            _send(client, "tasks/cancel", {"id": task["id"]})
            data = _send(client, "tasks/cancel", {"id": task["id"]})
        assert data["error"]["code"] == -32002

    def test_push_config_not_supported(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            task = _send(client, "message/send", {"message": _message("hi")})["result"]
            data = _send(client, "tasks/pushNotificationConfig/set", {
                "id": task["id"],
                "pushNotificationConfig": {"url": "http://hook.example.com"},
            })
        assert data["error"]["code"] == -32003

    def test_get_authenticated_extended_card_unsupported(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "agent/getAuthenticatedExtendedCard", {})
        assert data["error"]["code"] == -32004

    def test_unknown_method(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "no/such/method", {})
        assert data["error"]["code"] == -32601

    def test_batch(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        payload = [
            {"jsonrpc": "2.0", "method": "tasks/list", "params": {}, "id": 1},
            {"jsonrpc": "2.0", "method": "nope", "params": {}, "id": 2},
        ]
        with TestClient(app) as client:
            resp = client.post("/a2a/std/jsonrpc", json=payload)
        body = resp.json()
        assert len(body) == 2
        assert "result" in body[0]
        assert body[1]["error"]["code"] == -32601

    def test_response_headers(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            resp = client.post("/a2a/std/jsonrpc", json={
                "jsonrpc": "2.0", "method": "tasks/list", "params": {}, "id": 1,
            })
        assert resp.headers.get("A2A-Version") == "1.0"
        assert "A2A-Extensions" in resp.headers

    def test_unsupported_a2a_version(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            resp = client.post("/a2a/std/jsonrpc", json={
                "jsonrpc": "2.0", "method": "tasks/list", "params": {}, "id": 1,
            }, headers={"A2A-Version": "0.2"})
        body = resp.json()
        assert body["error"]["code"] == -32004

    def test_parse_error(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            resp = client.post("/a2a/std/jsonrpc", content="{bad json", headers={"Content-Type": "application/json"})
        body = resp.json()
        assert body["error"]["code"] == -32700


class TestExtensionGating:
    """A2A-Extensions 头门控（Phase E 端到端）。"""

    def test_extension_requires_header(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "list_agents", {})
        assert data["error"]["code"] == -32601

    def test_extension_allowed_with_header(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "list_agents", {}, headers={"A2A-Extensions": "list_agents"})
        assert "result" in data
        assert "agents" in data["result"]

    def test_extensions_disabled(self, tmp_path):
        app = _make_app(tmp_path, _base_config(a2a={"standard": {"extensions_enabled": False}}))
        with TestClient(app) as client:
            data = _send(client, "list_agents", {}, headers={"A2A-Extensions": "list_agents"})
        assert data["error"]["code"] == -32601

    def test_heartbeat_extension(self, tmp_path):
        app = _make_app(tmp_path, _base_config())
        with TestClient(app) as client:
            data = _send(client, "heartbeat", {"agent_id": "teagent-lu"},
                         headers={"A2A-Extensions": "heartbeat"})
        assert data["result"]["ok"] is True
