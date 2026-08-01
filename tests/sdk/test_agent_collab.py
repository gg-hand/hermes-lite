"""SDK 协作适配测试（S4/S5）。

S4 G1: send_collab_response 写入带 collab_id+collab_round+type=response 的协作消息。
S5 G2/G3/G4: on_collab_archived / on_collab_error 回调 + _dispatch_collab_event 分发。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pathlib import Path


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "keys").mkdir(parents=True, exist_ok=True)
    return tmp_path


# =============================================================================
# S4: send_collab_response + transport.post_collab_message
# =============================================================================


@pytest.mark.asyncio
async def test_send_collab_response_writes_collab_round_message(bb_root):
    """S4 G1: send_collab_response 写入带 collab_id+collab_round+type=response 的消息。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)
    transport.post_collab_message = AsyncMock(return_value={"ok": True, "seq": 5})
    agent = TeageAgent(agent_id="sdk_a", transport=transport, bb_root=str(bb_root))

    result = await agent.send_collab_response(
        content="同意方案B", collab_id="collab_xyz", collab_round=3,
        to_agent="worker_001",
    )
    assert result["ok"] is True
    # 验证 transport.post_collab_message 收到正确的协作字段
    posted = transport.post_collab_message.call_args
    msg = posted.args[0] if posted.args else posted.kwargs.get("message")
    assert msg["type"] == "response"
    assert msg["collab_id"] == "collab_xyz"
    assert msg["collab_round"] == 3
    assert msg["from"] == "sdk_a"
    assert msg["to"] == "worker_001"
    assert msg["accept"] is True
    assert "message_id" in msg


@pytest.mark.asyncio
async def test_send_collab_response_accept_false(bb_root):
    """S4 G1: send_collab_response 支持 accept=False（拒绝方案）。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)
    transport.post_collab_message = AsyncMock(return_value={"ok": True, "seq": 6})
    agent = TeageAgent(agent_id="sdk_a", transport=transport, bb_root=str(bb_root))

    await agent.send_collab_response(
        content="拒绝方案B", collab_id="collab_xyz", collab_round=3,
        to_agent="worker_001", accept=False,
    )
    posted = transport.post_collab_message.call_args
    msg = posted.args[0] if posted.args else posted.kwargs.get("message")
    assert msg["accept"] is False


@pytest.mark.asyncio
async def test_send_message_unchanged_no_collab_fields(bb_root):
    """S4 D2: 现有 send_message 不变,仍只发 A2A 点对点(无 collab 字段)。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)
    transport.call_agent = AsyncMock(return_value={"ok": True, "received_by": "b"})
    transport.forward_to_workbench = AsyncMock(return_value={"ok": True})
    agent = TeageAgent(agent_id="sdk_a", transport=transport, bb_root=str(bb_root))

    await agent.send_message("hi", "agent_b")
    # call_agent 收到的 payload 不含 collab_id/collab_round/type
    payload = transport.call_agent.call_args.args[2]
    assert "collab_id" not in payload
    assert "collab_round" not in payload
    assert "type" not in payload


# =============================================================================
# S5: on_collab_archived / on_collab_error 回调 + _dispatch_collab_event
# =============================================================================


@pytest.mark.asyncio
async def test_on_collab_archived_callback_fires(bb_root):
    """S5 G2: 拉取到 collab_archived announce → 触发 on_collab_archived 回调。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)
    archived: list[str] = []
    agent = TeageAgent(
        agent_id="sdk_a", transport=transport, bb_root=str(bb_root),
        on_collab_archived=lambda cid: archived.append(cid),
    )
    msg = {
        "type": "announce", "action": "collab_archived",
        "collab_id": "collab_old", "content": "已归档", "from": "worker_001",
    }
    await agent._dispatch_collab_event(msg)
    assert archived == ["collab_old"]


@pytest.mark.asyncio
async def test_on_collab_error_callback_fires(bb_root):
    """S5 G4: 拉取到 type=error 消息 → 触发 on_collab_error 回调。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)
    errors: list[tuple] = []
    agent = TeageAgent(
        agent_id="sdk_a", transport=transport, bb_root=str(bb_root),
        on_collab_error=lambda cid, rnd, content: errors.append((cid, rnd, content)),
    )
    msg = {
        "type": "error", "collab_id": "collab_x", "collab_round": 2,
        "content": "LLM 重试 3 次失败", "from": "worker_001", "error": True,
    }
    await agent._dispatch_collab_event(msg)
    assert errors == [("collab_x", 2, "LLM 重试 3 次失败")]


@pytest.mark.asyncio
async def test_collab_callbacks_default_noop_no_crash(bb_root):
    """S5 D2: 不传回调时 no-op,不崩溃(现有 on_message 契约不变)。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)
    agent = TeageAgent(agent_id="sdk_a", transport=transport, bb_root=str(bb_root))
    msg = {"type": "announce", "action": "collab_archived", "collab_id": "c1"}
    await agent._dispatch_collab_event(msg)  # 不抛


@pytest.mark.asyncio
async def test_collab_callback_exception_does_not_block(bb_root):
    """S5 D4: 回调抛异常不阻断(仅 warn)。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)

    def bad_cb(cid):
        raise RuntimeError("cb boom")
    agent = TeageAgent(
        agent_id="sdk_a", transport=transport, bb_root=str(bb_root),
        on_collab_archived=bad_cb,
    )
    # 不抛
    await agent._dispatch_collab_event(
        {"type": "announce", "action": "collab_archived", "collab_id": "c1"})


@pytest.mark.asyncio
async def test_dispatch_collab_event_non_collab_message_no_callback(bb_root):
    """S5 G3: 非 collab 事件消息(如普通 relay/request)不触发回调。"""
    from teage_liu.sdk.agent import TeageAgent
    from teage_liu.sdk.transport import A2ATransport

    transport = AsyncMock(spec=A2ATransport)
    archived: list[str] = []
    errors: list[tuple] = []
    agent = TeageAgent(
        agent_id="sdk_a", transport=transport, bb_root=str(bb_root),
        on_collab_archived=lambda cid: archived.append(cid),
        on_collab_error=lambda cid, rnd, content: errors.append((cid, rnd, content)),
    )
    # 普通 relay 消息，不触发任何回调
    await agent._dispatch_collab_event(
        {"type": "relay", "from": "agent_b", "content": "普通消息"})
    await agent._dispatch_collab_event(
        {"type": "request", "from": "director", "content": "广播"})
    assert archived == []
    assert errors == []
