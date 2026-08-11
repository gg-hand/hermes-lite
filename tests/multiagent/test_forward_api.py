"""Forward API 签名校验测试。"""
from __future__ import annotations

import pytest
import pytest_asyncio
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from teage_liu.multiagent.collaboration_routes import create_collab_router
from teage_liu.multiagent.blackboard import Blackboard
from teage_liu.multiagent.message_signature import sign_message


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    # create_collab_router() 通过 TEAGE_BB_ROOT 环境变量获取黑板路径
    os.environ["TEAGE_BB_ROOT"] = str(tmp_path)
    return tmp_path


@pytest.fixture
def app(bb_root: Path) -> FastAPI:
    application = FastAPI()
    application.include_router(create_collab_router())
    return application


@pytest.fixture
def key_pair():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture
def registered_agent(bb_root: Path, key_pair):
    """写入公钥到 agents/keys/。"""
    _, public_key = key_pair
    keys_dir = bb_root / "agents" / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (keys_dir / "agent_alice.pem").write_bytes(pem)
    return key_pair


class TestForwardApiSignature:
    """Forward API ed25519 签名校验。"""

    def test_forward_rejects_missing_signature(self, app, registered_agent):
        """无签名的 forward 被拒绝（HTTP 401）。"""
        with TestClient(app) as client:
            resp = client.post("/api/multiagent/collab/forward", json={
                "from_": "agent_alice",
                "to": "*",
                "content": "无签名测试",
                "via": "a2a",
                "message_id": "msg_001",
                "signature": "",
            })
            assert resp.status_code == 401
            assert "signature" in resp.json()["detail"].lower()

    def test_forward_rejects_invalid_signature(self, app, registered_agent):
        """无效签名的 forward 被拒绝。"""
        with TestClient(app) as client:
            resp = client.post("/api/multiagent/collab/forward", json={
                "from_": "agent_alice",
                "to": "*",
                "content": "无效签名测试",
                "via": "a2a",
                "message_id": "msg_002",
                "signature": "deadbeef",
            })
            assert resp.status_code == 401

    def test_forward_rejects_unregistered_agent(self, app, bb_root: Path):
        """未注册公钥的 agent 签名被拒绝。"""
        private_key = Ed25519PrivateKey.generate()
        message = {
            "from": "agent_unknown",
            "to": "*",
            "content": "未注册 agent",
            "via": "a2a",
            "message_id": "msg_003",
        }
        signature = sign_message(message, private_key)

        with TestClient(app) as client:
            resp = client.post("/api/multiagent/collab/forward", json={
                **message,
                "from_": message["from"],
                "signature": signature,
            })
            assert resp.status_code == 401

    def test_forward_accepts_valid_signature(self, app, registered_agent):
        """有效签名的 forward 写入成功。"""
        private_key, _ = registered_agent
        message = {
            "from": "agent_alice",
            "to": "*",
            "content": "有效签名测试",
            "via": "a2a",
            "message_id": "msg_004",
        }
        signature = sign_message(message, private_key)

        with TestClient(app) as client:
            resp = client.post("/api/multiagent/collab/forward", json={
                **message,
                "from_": message["from"],
                "signature": signature,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert isinstance(data["seq"], int)

    def test_forward_deduplicates_by_message_id(self, app, registered_agent):
        """相同 message_id 的消息去重。"""
        private_key, _ = registered_agent
        message = {
            "from": "agent_alice",
            "to": "*",
            "content": "去重测试",
            "via": "a2a",
            "message_id": "msg_dedup_001",
        }
        signature = sign_message(message, private_key)

        with TestClient(app) as client:
            # 第一次写入
            resp1 = client.post("/api/multiagent/collab/forward", json={
                **message, "from_": message["from"], "signature": signature,
            })
            assert resp1.status_code == 200
            assert resp1.json()["deduplicated"] is False

            # 第二次重复写入
            resp2 = client.post("/api/multiagent/collab/forward", json={
                **message, "from_": message["from"], "signature": signature,
            })
            assert resp2.status_code == 200
            assert resp2.json()["deduplicated"] is True
