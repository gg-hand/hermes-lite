"""协作 SSE 通道测试（Task 4）

直接测试 event_stream 生成器逻辑，避免 TestClient.stream 测试无限流卡住。
"""
import asyncio
import json
import pytest
from pathlib import Path


@pytest.fixture
def bb_root(tmp_path):
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


def _get_sse_endpoint():
    """获取 SSE 端点函数"""
    from teage_liu.multiagent.collaboration_routes import create_collab_router

    router = create_collab_router()
    for route in router.routes:
        if getattr(route, "path", "") == "/api/multiagent/collab/sse":
            return route.endpoint
    raise RuntimeError("SSE 端点未找到")


def test_sse_endpoint_exists():
    """SSE 端点存在且可调用"""
    endpoint = _get_sse_endpoint()
    assert callable(endpoint)


def test_sse_event_stream_yields_existing_messages(bb_root, monkeypatch):
    """SSE event_stream 连接时推送 collab_sse_connected 事件（不推送历史消息）。

    历史消息由前端通过 GET /messages 加载，SSE 只负责增量推送，
    避免 HTTP 加载 + SSE 推送导致重复渲染。
    """
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))

    from teage_liu.multiagent.blackboard import append_collab_message

    # 写入两条消息
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "announce", "action": "online", "content": "第一条"
    }))
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B", "type": "relay", "content": "第二条"
    }))

    endpoint = _get_sse_endpoint()
    response = asyncio.run(endpoint())
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"

    events = []

    async def consume_stream():
        async for chunk in response.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for line in text.split("\n"):
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    if len(events) >= 1:
                        return

    asyncio.run(asyncio.wait_for(consume_stream(), timeout=3.0))

    # 连接时只推送 collab_sse_connected 事件（不推送历史消息）
    assert len(events) == 1
    assert events[0]["type"] == "collab_sse_connected"
    assert events[0]["data"]["last_seq"] == 2


def test_sse_event_stream_pushes_new_message(bb_root, monkeypatch):
    """SSE event_stream 推送新增消息（轮询机制）"""
    monkeypatch.setenv("TEAGE_BB_ROOT", str(bb_root))

    from teage_liu.multiagent.blackboard import append_collab_message

    endpoint = _get_sse_endpoint()
    response = asyncio.run(endpoint())

    events = []

    async def consume_and_write():
        async def writer():
            # 等过初始空消息后写入
            await asyncio.sleep(1.5)
            await append_collab_message(bb_root, {
                "from": "agent_A", "type": "announce", "action": "online", "content": "新消息"
            })

        async def consumer():
            async for chunk in response.body_iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                for line in text.split("\n"):
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        events.append(event)
                        if (event.get("type") == "collab_message_append"
                                and event["data"]["message"].get("content") == "新消息"):
                            return

        await asyncio.gather(writer(), consumer())

    asyncio.run(asyncio.wait_for(consume_and_write(), timeout=5.0))

    new_msg_events = [
        e for e in events
        if e.get("data", {}).get("message", {}).get("content") == "新消息"
    ]
    assert len(new_msg_events) == 1
    assert new_msg_events[0]["data"]["message"]["from"] == "agent_A"
