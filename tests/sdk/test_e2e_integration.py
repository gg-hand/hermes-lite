"""SDK 端到端集成测试。

完整流程（修正后架构）：
1. 启动工作台（A2A Gateway + Forward API，测试用 FastAPI TestClient）
2. SDK Agent 注册到工作台
3. Agent 间 A2A 点对点通信（send_message → 对方 A2A Server）
4. 消息副本通过 Forward API 归档到 collaboration.md
5. 验证 collaboration.md 中有归档的消息
6. Director directive 注入 LLM 上下文
"""
from __future__ import annotations

import asyncio
import os
import pytest
import pytest_asyncio
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teage_liu.multiagent.collaboration_routes import create_collab_router
from teage_liu.multiagent.a2a_gateway import create_a2a_router
from teage_liu.multiagent.blackboard import Blackboard, read_collab_messages, append_collab_message
from teage_liu.multiagent.message_signature import sign_message
from teage_liu.sdk.agent import TeageAgent
from teage_liu.sdk.transport import A2ATransport


@pytest_asyncio.fixture
async def e2e_bb_root(tmp_path: Path) -> Path:
    """初始化完整黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    # create_collab_router() 通过 TEAGE_BB_ROOT 环境变量获取黑板路径
    os.environ["TEAGE_BB_ROOT"] = str(tmp_path)
    return tmp_path


@pytest.fixture
def e2e_app(e2e_bb_root: Path) -> FastAPI:
    """创建包含 A2A Gateway + Forward API 的 FastAPI app。"""
    config = {
        "a2a": {
            "enabled": True,
            "rate_limit_per_second": 100,
        }
    }
    app = FastAPI()
    app.include_router(create_a2a_router(e2e_bb_root, config))
    app.include_router(create_collab_router())
    return app


@pytest.fixture
def agent_key_pair():
    """生成 agent 密钥对并写入黑板。"""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.mark.asyncio
async def test_e2e_a2a_communication_and_forward(e2e_bb_root: Path, e2e_app: FastAPI, agent_key_pair):
    """端到端：注册 → A2A 发消息 → Forward 归档 → 验证 collaboration.md。"""
    private_key, public_key = agent_key_pair

    # 写入公钥到黑板
    keys_dir = e2e_bb_root / "agents" / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (keys_dir / "e2e_agent.pem").write_bytes(pem)

    with TestClient(e2e_app) as client:
        # 创建 mock transport，将 A2A 调用和 Forward 调用转发到 TestClient
        transport = A2ATransport(
            agent_id="e2e_agent",
            private_key=private_key,
            bb_root=e2e_bb_root,
            forward_endpoint="http://test",
        )

        # 替换 transport 的 A2A Client call_method（转发到 TestClient）
        async def mock_call_method(endpoint_name, method, params):
            # 自动附加签名（写操作）
            if method in ("send_message",) and "signature" not in params:
                payload = {k: v for k, v in params.items() if k != "signature"}
                params = {**params, "signature": sign_message(payload, private_key)}

            resp = client.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": 1,
            })
            data = resp.json()
            if "error" in data:
                raise Exception(data["error"]["message"])
            return data["result"]

        transport._a2a_client.call_method = mock_call_method

        # 替换 transport 的 Forward HTTP client（转发到 TestClient）
        class MockForwardClient:
            async def post(self, url, json=None):
                resp = client.post("/api/multiagent/collab/forward", json=json)
                return resp

        transport._forward_client = MockForwardClient()

        # 创建 agent
        agent = TeageAgent(
            agent_id="e2e_agent",
            capabilities=["test"],
            private_key=private_key,
            bb_root=str(e2e_bb_root),
            forward_endpoint="http://test",
            transport=transport,
        )

        # 1. 注册到工作台
        reg_result = await agent.register()
        assert reg_result["registered"] is True

        # 2. Forward 消息到工作台（模拟 A2A 通信后的归档）
        forward_result = await agent.forward_a2a_message(
            original_from="e2e_agent",
            original_to="agent_bob",
            content="hello via a2a",
            message_id="msg_e2e_001",
        )
        assert forward_result["ok"] is True
        assert "seq" in forward_result

        # 3. 验证消息已归档到 collaboration.md
        messages = await read_collab_messages(e2e_bb_root)
        relay_messages = [m for m in messages if m.get("via") == "a2a"]
        assert len(relay_messages) >= 1
        assert any(m.get("content") == "hello via a2a" for m in relay_messages)


@pytest.mark.asyncio
async def test_e2e_director_injection(e2e_bb_root: Path, agent_key_pair):
    """端到端：Director 写 directive → Agent get_directive_context 获取注入内容。"""
    private_key, public_key = agent_key_pair

    # 写入公钥
    keys_dir = e2e_bb_root / "agents" / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (keys_dir / "e2e_agent.pem").write_bytes(pem)

    # Director 写 directive 到 collaboration.md
    await append_collab_message(e2e_bb_root, {
        "from": "director",
        "to": "*",
        "type": "directive",
        "content": "请按顺序执行任务",
        "rule_type": "ordering",
        "target": "*",
    })

    # 创建 agent（不需要实际网络连接）
    transport = A2ATransport(
        agent_id="e2e_agent",
        private_key=private_key,
        bb_root=e2e_bb_root,
        forward_endpoint="",
    )

    agent = TeageAgent(
        agent_id="e2e_agent",
        capabilities=["test"],
        private_key=private_key,
        bb_root=str(e2e_bb_root),
        transport=transport,
    )

    # 获取 Director 上下文
    context = await agent.get_directive_context()
    assert "请按顺序执行任务" in context
    assert "Director" in context

    # 再次 drain 应为空（已清空队列）
    context2 = await agent.get_directive_context()
    assert context2 == ""
