"""SDK TeageAgent 核心类测试。"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from teage_liu.sdk.agent import TeageAgent
from teage_liu.sdk.transport import A2ATransport


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "keys").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def mock_transport():
    transport = MagicMock(spec=A2ATransport)
    transport.call_agent = AsyncMock()
    transport.forward_to_workbench = AsyncMock()
    transport.close = AsyncMock()
    return transport


@pytest.fixture
def agent(mock_transport, bb_root):
    return TeageAgent(
        agent_id="agent_test",
        capabilities=["research", "analysis"],
        private_key=None,
        bb_root=bb_root,
        forward_endpoint="http://workbench:18400",
        transport=mock_transport,
    )


@pytest.mark.asyncio
async def test_send_message_to_agent(agent, mock_transport):
    """send_message 通过 A2A 直接发给目标 agent + forward 归档。"""
    mock_transport.call_agent.return_value = {"ok": True, "received_by": "agent_bob"}
    mock_transport.forward_to_workbench.return_value = {"ok": True, "seq": 1}

    result = await agent.send_message("hello", to_agent="agent_bob")
    assert result["ok"] is True

    # 验证 A2A 点对点调用
    call_args = mock_transport.call_agent.call_args
    assert call_args.args[0] == "agent_bob"
    assert call_args.args[1] == "send_message"
    params = call_args.args[2]
    assert params["from"] == "agent_test"
    assert params["to"] == "agent_bob"
    assert params["content"] == "hello"

    # 验证 forward 归档
    forward_args = mock_transport.forward_to_workbench.call_args
    assert forward_args.kwargs["original_from"] == "agent_test"
    assert forward_args.kwargs["original_to"] == "agent_bob"


@pytest.mark.asyncio
async def test_forward_a2a_message(agent, mock_transport):
    """forward_a2a_message 归档 A2A 消息副本到工作台。"""
    mock_transport.forward_to_workbench.return_value = {"ok": True, "seq": 42}
    result = await agent.forward_a2a_message(
        original_from="agent_alice",
        original_to="agent_test",
        content="A2A 消息",
        message_id="msg_001",
    )
    assert result["seq"] == 42
    mock_transport.forward_to_workbench.assert_called_once()


@pytest.mark.asyncio
async def test_query_other_agent_capabilities(agent, mock_transport):
    """query_agent 调用其他 agent 的 query_capabilities。"""
    mock_transport.call_agent.return_value = {
        "agent_id": "agent_bob",
        "capabilities": ["analysis"],
    }
    result = await agent.query_agent("agent_bob")
    assert "capabilities" in result
    mock_transport.call_agent.assert_called_with(
        "agent_bob", "query_capabilities", {}
    )


@pytest.mark.asyncio
async def test_get_directive_context(agent, bb_root):
    """get_directive_context 返回待注入的 Director directive。"""
    from teage_liu.multiagent.blackboard import append_collab_message

    # 写入 directive
    await append_collab_message(bb_root, {
        "from": "director", "to": "*",
        "type": "directive",
        "content": "请按顺序执行",
        "rule_type": "ordering",
        "target": "*",
    })

    # poll + drain
    await agent._director_injector.poll_and_enqueue_new_directives()
    context = await agent.get_directive_context()
    assert "请按顺序执行" in context
    # 再次 drain 应为空（已清空）
    assert await agent.get_directive_context() == ""


@pytest.mark.asyncio
async def test_start_stop_lifecycle(agent, mock_transport):
    """start/stop 完整生命周期。"""
    await agent.start()
    assert agent._running is True
    assert agent._heartbeat_task is not None

    await agent.stop()
    assert agent._running is False


@pytest.mark.asyncio
async def test_on_message_received_callback(agent):
    """A2A Server 收到消息后触发回调。"""
    received = []
    agent.on_message = lambda msg: received.append(msg)

    # 模拟 A2A Server 收到消息
    await agent._a2a_server.on_message_callback({
        "from": "agent_bob",
        "to": "agent_test",
        "content": "hi from bob",
    })
    assert len(received) == 1
    assert received[0]["content"] == "hi from bob"
