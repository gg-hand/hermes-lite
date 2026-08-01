"""Worker 协作轮询 + A2A 转发 + 双队列 + 紧急插队测试（Task 6，D3 修复签名）。"""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock

from teage_liu.multiagent.worker_adapter import WorkerAdapter


@pytest.fixture
def bb_root(tmp_path):
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


@pytest.fixture
def worker(bb_root):
    """D3 修复：使用现有 WorkerAdapter 签名 (bb_root, config, agent_id, orchestrator)。"""
    orchestrator = AsyncMock()
    config = {
        "multiagent": {
            "worker": {
                "capabilities": ["sentiment_analysis"],
                "heartbeat_interval_seconds": 10,
            },
            "director": {},
            "collab": {
                "poll_interval_seconds": 2,
                "idle_timeout_seconds": 60,
            },
        }
    }
    return WorkerAdapter(
        bb_root=bb_root,
        config=config,
        agent_id="agent_A",
        orchestrator=orchestrator,
    )


def test_worker_announce_on_start(bb_root):
    """Worker 启动时写 announce"""
    orchestrator = AsyncMock()
    config = {
        "multiagent": {
            "worker": {"capabilities": ["sentiment_analysis"]},
            "director": {},
            "collab": {},
        }
    }
    w = WorkerAdapter(
        bb_root=bb_root, config=config,
        agent_id="agent_A", orchestrator=orchestrator,
    )
    asyncio.run(w._write_announce("online"))

    from teage_liu.multiagent.blackboard import read_collab_messages
    messages = asyncio.run(read_collab_messages(bb_root))
    assert len(messages) == 1
    assert messages[0]["type"] == "announce"
    assert messages[0]["action"] == "online"
    assert messages[0]["from"] == "agent_A"


def test_worker_polls_request(bb_root, worker):
    """Worker 轮询到 request 消息（普通队列，搭 A2A 主泵便车）"""
    from teage_liu.multiagent.blackboard import append_collab_message

    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B",
        "type": "request",
        "content": "需要情感分析",
        "capabilities_needed": ["sentiment_analysis"],
    }))

    asyncio.run(worker._poll_collab_once())

    assert len(worker._normal_queue) == 1
    worker._orchestrator.chat.assert_not_called()


def test_director_broadcast_triggers_immediate_llm(bb_root, worker):
    """Director 广播 request 立即触发 LLM 调用（紧急插队）"""
    from teage_liu.multiagent.blackboard import append_collab_message

    asyncio.run(append_collab_message(bb_root, {
        "from": "director",
        "type": "request",
        "content": "帮我读直播间弹幕",
    }))

    asyncio.run(worker._poll_collab_once())

    worker._orchestrator.chat.assert_called_once()


def test_intervention_directive_triggers_immediate_llm(bb_root, worker):
    """intervention 类 directive 立即触发 LLM 调用（紧急插队）"""
    from teage_liu.multiagent.blackboard import append_collab_message

    asyncio.run(append_collab_message(bb_root, {
        "from": "director",
        "type": "directive",
        "content": "检测到死锁，立即停止",
        "rule_type": "intervention",
        "target": "*",
        "priority": "high",
        "issued_by": "ScriptDirector",
    }))

    asyncio.run(worker._poll_collab_once())

    worker._orchestrator.chat.assert_called_once()
    assert len(worker._normal_queue) == 0


def test_ordering_directive_enters_normal_queue(bb_root, worker):
    """ordering 类 directive 进入普通队列（搭便车）"""
    from teage_liu.multiagent.blackboard import append_collab_message

    asyncio.run(append_collab_message(bb_root, {
        "from": "director",
        "type": "directive",
        "content": "按顺序执行",
        "rule_type": "ordering",
        "target": "*",
        "priority": "high",
        "issued_by": "AgentDirector",
    }))

    asyncio.run(worker._poll_collab_once())

    assert len(worker._normal_queue) == 1
    worker._orchestrator.chat.assert_not_called()


def test_inject_normal_queue_to_llm_context(bb_root, worker):
    """A2A 主泵触发时，把普通队列消息注入 LLM 上下文"""
    worker._normal_queue.append({
        "from": "director",
        "type": "directive",
        "content": "按顺序执行",
        "rule_type": "ordering",
        "priority": "high",
        "issued_by": "AgentDirector",
    })

    system_prompt = "你是助手。"
    injected = worker._inject_normal_queue_to_context(system_prompt)

    assert "按顺序执行" in injected
    assert "Director 引导" in injected
    assert "高优先级" in injected
    assert "AgentDirector" in injected
    assert len(worker._normal_queue) == 0


def test_forward_a2a_message(bb_root, worker):
    """Worker 转发 A2A 消息到 collaboration.md（Phase1 E-4：迁移到 append_collab_message 直调）"""
    from teage_liu.multiagent.blackboard import append_collab_message
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A",
        "to": "agent_B",
        "type": "relay",
        "content": "A2A 消息内容",
        "via": "a2a",
        "forwarded_by": worker._agent_id,
        "message_id": "msg_001",
    }))

    from teage_liu.multiagent.blackboard import read_collab_messages
    messages = asyncio.run(read_collab_messages(bb_root))
    assert len(messages) == 1
    assert messages[0]["type"] == "relay"
    assert messages[0]["via"] == "a2a"
    assert messages[0]["message_id"] == "msg_001"


def test_send_relay(bb_root, worker):
    """Worker 发送 relay 消息（Phase1 E-4：迁移到 append_collab_message 直调）"""
    from teage_liu.multiagent.blackboard import append_collab_message
    asyncio.run(append_collab_message(bb_root, {
        "from": worker._agent_id,
        "to": "agent_B",
        "type": "relay",
        "content": "我读完了第1条",
    }))

    from teage_liu.multiagent.blackboard import read_collab_messages
    messages = asyncio.run(read_collab_messages(bb_root))
    assert messages[0]["type"] == "relay"
    assert messages[0]["from"] == "agent_A"
    assert messages[0]["to"] == "agent_B"


def test_a2a_call_pumps_normal_queue(bb_root, worker):
    """A2A 调用作为主泵，触发时清空普通队列"""
    worker._normal_queue.append({
        "from": "director",
        "type": "directive",
        "content": "按顺序执行",
        "rule_type": "ordering",
    })

    asyncio.run(worker._call_llm_with_pump(
        prompt="处理 A2A 消息",
        system_prompt="你是助手。"
    ))

    worker._orchestrator.chat.assert_called_once()
    assert len(worker._normal_queue) == 0


def test_idle_timeout_triggers_llm(bb_root, worker):
    """空闲超时保护：60s 无 A2A 且队列非空 → 触发 LLM"""
    worker._idle_timeout_seconds = 0.1

    worker._normal_queue.append({
        "from": "agent_B",
        "type": "request",
        "content": "需要协作",
    })

    worker._last_a2a_time = time.time() - 1

    asyncio.run(worker._check_idle_timeout())

    worker._orchestrator.chat.assert_called_once()
    assert len(worker._normal_queue) == 0
