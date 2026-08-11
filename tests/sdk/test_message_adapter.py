"""SDK 消息适配层测试（多通道统一 + 去重）。"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from teage_liu.sdk.message_adapter import MessageAdapter


@pytest.fixture
def adapter():
    """MessageAdapter 实例（mock httpx）。"""
    return MessageAdapter(
        workbench_endpoint="http://workbench:18400",
        collab_id=None,
    )


@pytest.mark.asyncio
async def test_enqueue_inbound_and_drain(adapter):
    """enqueue_inbound 推入 A2A 消息，poll_new_messages 排出。"""
    await adapter.enqueue_inbound({
        "from": "agent_bob", "to": "agent_alice",
        "type": "relay", "content": "hi", "message_id": "msg_001",
    })
    messages = await adapter.poll_new_messages()
    assert len(messages) == 1
    assert messages[0]["content"] == "hi"
    # 再次 poll 队列为空
    messages = await adapter.poll_new_messages()
    assert len(messages) == 0


@pytest.mark.asyncio
async def test_poll_broadcast_messages_incremental(adapter):
    """轮询 collaboration.md 广播消息（增量，仅返回 seq > last_seen_seq）。"""
    broadcast_messages = [
        {"seq": 1, "from": "director", "type": "request", "content": "task1"},
        {"seq": 2, "from": "director", "type": "directive", "content": "rule"},
    ]
    # mock REST API 响应
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": broadcast_messages}

    with patch.object(adapter._http_client, "get", new=AsyncMock(return_value=mock_response)):
        # 首次轮询，last_seen_seq=0，返回所有消息
        msgs = await adapter.poll_new_messages()
        assert len(msgs) == 2
        assert adapter._last_seen_seq == 2

        # 第二次轮询，无新消息
        msgs = await adapter.poll_new_messages()
        assert len(msgs) == 0


@pytest.mark.asyncio
async def test_deduplicate_by_message_id(adapter):
    """按 message_id 去重（A2A + 广播同一消息只返回一次）。"""
    messages = [
        {"seq": 1, "message_id": "msg_001", "content": "a"},
        {"seq": 2, "message_id": "msg_002", "content": "b"},
        {"seq": 3, "message_id": "msg_001", "content": "a"},  # 重复
    ]
    deduped = adapter.deduplicate(messages)
    assert len(deduped) == 2
    assert deduped[0]["seq"] == 1
    assert deduped[1]["seq"] == 2


@pytest.mark.asyncio
async def test_deduplicate_without_message_id_passes_through(adapter):
    """无 message_id 的消息不去重。"""
    messages = [
        {"seq": 1, "content": "a"},
        {"seq": 2, "content": "b"},
    ]
    deduped = adapter.deduplicate(messages)
    assert len(deduped) == 2


@pytest.mark.asyncio
async def test_mixed_sources_deduplication(adapter):
    """A2A 入站 + 广播轮询混合去重。"""
    # A2A 入站消息
    await adapter.enqueue_inbound({
        "from": "agent_bob", "to": "agent_alice",
        "type": "relay", "content": "via a2a", "message_id": "msg_001",
    })
    # 广播轮询返回同 message_id 的转发副本
    broadcast_messages = [
        {"seq": 5, "from": "agent_bob", "to": "agent_alice",
         "type": "relay", "content": "via a2a", "message_id": "msg_001", "via": "a2a"},
    ]
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": broadcast_messages}
    with patch.object(adapter._http_client, "get", new=AsyncMock(return_value=mock_response)):
        msgs = await adapter.poll_new_messages()
        # 去重后只返回 1 条
        assert len(msgs) == 1
