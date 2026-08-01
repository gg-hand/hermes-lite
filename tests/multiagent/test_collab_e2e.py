"""端到端协作流程测试（Task 11）。

验证完整链路：agent 自主协作 + Director 按需插入 + 工作台透明观察。

注意：
- 普通协作 request（from != "director"）进入 _normal_queue，由 A2A 主泵/空闲超时清空
- Director 广播 request（from == "director"）走紧急插队，立即触发 LLM 并写 response
- ordering 类 directive 入 _normal_queue（不是 _directive_queue）
- intervention 类 directive 走紧急插队
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from teage_liu.multiagent.blackboard import (
    append_collab_message,
    read_collab_messages,
)
from teage_liu.multiagent.worker_adapter import WorkerAdapter
from teage_liu.multiagent.directors.script_director import ScriptDirector


@pytest.fixture
def bb_root(tmp_path):
    """临时黑板根目录"""
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


@pytest.fixture
def client(tmp_path, monkeypatch):
    """最小化 FastAPI 客户端（仅挂载 collab_router，避免完整 lifespan 依赖）"""
    bb_root = tmp_path / "blackboard"
    bb_root.mkdir()
    (bb_root / "collabs").mkdir()
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))

    from teage_liu.multiagent.collaboration_routes import create_collab_router

    app = FastAPI()
    app.include_router(create_collab_router())
    return TestClient(app)


def _make_worker(bb_root, agent_id="agent_A", capabilities=None):
    """构造带 mock orchestrator 的 WorkerAdapter"""
    orchestrator = AsyncMock()
    orchestrator.chat.return_value = "我可以做情感分析"
    config = {
        "multiagent": {
            "worker": {"capabilities": capabilities or ["sentiment_analysis"]},
            "director": {},
            "collab": {"poll_interval_seconds": 2, "idle_timeout_seconds": 60},
        }
    }
    return WorkerAdapter(
        bb_root=bb_root,
        config=config,
        agent_id=agent_id,
        orchestrator=orchestrator,
    )


# ========== E2E 1: Agent 自主协作完整流程（用户广播 → 紧急 LLM → response）==========


def test_agent_self_collab_e2e(bb_root):
    """Agent 自主协作完整流程：用户广播 → Agent 紧急响应 → 写 response"""
    # 1. Agent A 上线声明
    worker_a = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker_a._write_announce("online"))

    # 2. Director 广播协作请求（from=director 走紧急插队路径）
    asyncio.run(append_collab_message(bb_root, {
        "from": "director",
        "type": "request",
        "content": "需要情感分析",
        "capabilities_needed": ["sentiment_analysis"],
    }))

    # 3. Agent A 轮询并处理（紧急路径触发 LLM + 写 response）
    asyncio.run(worker_a._poll_collab_once())

    # 4. 验证：Agent A 写了 response
    messages = asyncio.run(read_collab_messages(bb_root))
    response_msgs = [m for m in messages if m["type"] == "response"]
    assert len(response_msgs) == 1
    assert response_msgs[0]["from"] == "agent_A"
    assert response_msgs[0]["accept"] is True

    # 5. 验证 LLM 被调用
    worker_a._orchestrator.chat.assert_called_once()


# ========== E2E 2: Director 按需插入完整流程 ==========


def test_director_ordering_directive_e2e(bb_root):
    """Director 注入 ordering directive → 入普通队列 → 搭便车注入 LLM 上下文"""
    # 1. Director 注入 ordering directive
    director = ScriptDirector(bb_root)
    asyncio.run(director.inject_directive(
        content="按顺序执行",
        rule_type="ordering",
    ))

    # 2. Agent 收到 directive 入普通队列
    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._poll_collab_once())

    # 3. 验证 directive 在普通队列中（不是 _directive_queue）
    assert len(worker._normal_queue) == 1
    assert worker._normal_queue[0]["content"] == "按顺序执行"
    worker._orchestrator.chat.assert_not_called()  # 普通 directive 不立即触发

    # 4. Director 广播触发紧急 LLM 调用，顺便清空普通队列（搭便车）
    asyncio.run(append_collab_message(bb_root, {
        "from": "director",
        "type": "request",
        "content": "开始协作",
    }))
    asyncio.run(worker._poll_collab_once())

    # 5. 验证 LLM 调用时 directive 已注入 extra_system_prompt
    worker._orchestrator.chat.assert_called_once()
    call_args = worker._orchestrator.chat.call_args
    system_prompt = call_args.kwargs.get("extra_system_prompt", "")
    assert "按顺序执行" in system_prompt
    assert "协作背景指导" in system_prompt

    # 6. 队列已清空
    assert len(worker._normal_queue) == 0


def test_director_intervention_directive_e2e(bb_root):
    """Director intervention 类 directive → 立即触发 LLM（紧急插队）"""
    director = ScriptDirector(bb_root)
    asyncio.run(director.inject_directive(
        content="检测到死锁，立即停止",
        rule_type="intervention",
        priority="critical",
    ))

    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._poll_collab_once())

    # 紧急插队立即触发 LLM
    worker._orchestrator.chat.assert_called_once()
    call_args = worker._orchestrator.chat.call_args
    system_prompt = call_args.kwargs.get("extra_system_prompt", "")
    assert "应急干预" in system_prompt
    assert "检测到死锁" in system_prompt


# ========== E2E 3: A2A 转发去重完整流程 ==========


def test_a2a_forward_dedup_e2e(bb_root):
    """A2A 转发去重：两个 agent 转发同一条消息只保留一条（Phase1 E-4：迁移到 append_collab_message 直调）"""
    from teage_liu.multiagent.blackboard import append_collab_message
    # A 转发
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A",
        "to": "agent_B",
        "type": "relay",
        "content": "A2A 消息",
        "via": "a2a",
        "forwarded_by": "agent_A",
        "message_id": "msg_001",
    }))

    # B 转发（相同 message_id）
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A",
        "to": "agent_B",
        "type": "relay",
        "content": "A2A 消息",
        "via": "a2a",
        "forwarded_by": "agent_B",
        "message_id": "msg_001",
    }))

    # 验证：只有一条消息
    messages = asyncio.run(read_collab_messages(bb_root))
    relay_msgs = [
        m for m in messages
        if m["type"] == "relay" and m.get("via") == "a2a"
    ]
    assert len(relay_msgs) == 1
    assert relay_msgs[0]["message_id"] == "msg_001"


# ========== E2E 4: 工作台通过 API 观察协作 ==========


def test_workbench_observe_via_api(client):
    """工作台通过 API 观察协作：写入多种消息 → API 获取验证

    注：/forward 接口强制 ed25519 签名校验（外部 agent 归档入口），
    工作台观察测试改用 /append 写入 relay 测试数据（管理接口，无签名要求）。
    Phase1 E-4：Worker 内部归档改走 append_collab_message 直接写黑板，同样无签名。
    """
    # 写入 announce
    client.post("/api/multiagent/collab/announce", json={
        "agent_id": "agent_A",
        "action": "online",
        "capabilities": ["sentiment_analysis"],
    })
    # 写入用户广播（request）
    client.post("/api/multiagent/collab/broadcast", json={
        "content": "需要协作",
    })
    # 写入 A2A 转发（relay）——通过 /append 管理接口写入测试归档数据
    client.post("/api/multiagent/collab/append", json={
        "message": {
            "from": "agent_A",
            "to": "agent_B",
            "type": "relay",
            "content": "A2A 消息",
            "via": "a2a",
            "message_id": "msg_001",
        },
    })

    # 通过 API 获取所有消息
    resp = client.get("/api/multiagent/collab/messages")
    data = resp.json()
    assert data["total"] == 3

    # 验证消息类型
    types = [m["type"] for m in data["messages"]]
    assert "announce" in types
    assert "request" in types
    assert "relay" in types


def test_workbench_agent_list_via_api(client):
    """工作台通过 API 获取 agent 列表"""
    client.post("/api/multiagent/collab/announce", json={
        "agent_id": "agent_A",
        "action": "online",
        "capabilities": ["sentiment_analysis"],
        "agent_name": "情感分析助手",
    })
    client.post("/api/multiagent/collab/announce", json={
        "agent_id": "agent_B",
        "action": "online",
        "capabilities": ["text_summary"],
        "agent_name": "总结助手",
    })

    resp = client.get("/api/multiagent/collab/agents")
    assert resp.status_code == 200
    agent_ids = [a["agent_id"] for a in resp.json()["agents"]]
    assert "agent_A" in agent_ids
    assert "agent_B" in agent_ids


# ========== E2E 5: collab_id 隔离 ==========


def test_collab_id_isolation_e2e(bb_root):
    """不同 collab_id 的消息互不干扰"""
    # 全局消息
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "announce", "action": "online", "content": "global",
    }))
    # collab_001 消息
    asyncio.run(append_collab_message(
        bb_root,
        {"from": "agent_A", "type": "request", "content": "collab1 msg"},
        collab_id="collab_001",
    ))
    # collab_002 消息
    asyncio.run(append_collab_message(
        bb_root,
        {"from": "agent_B", "type": "request", "content": "collab2 msg"},
        collab_id="collab_002",
    ))

    # 验证隔离
    global_msgs = asyncio.run(read_collab_messages(bb_root))
    collab1_msgs = asyncio.run(read_collab_messages(bb_root, collab_id="collab_001"))
    collab2_msgs = asyncio.run(read_collab_messages(bb_root, collab_id="collab_002"))

    assert len(global_msgs) == 1
    assert len(collab1_msgs) == 1
    assert len(collab2_msgs) == 1
    assert collab1_msgs[0]["content"] == "collab1 msg"
    assert collab2_msgs[0]["content"] == "collab2 msg"
