"""StdA2AClient 测试（mock httpx）。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ConnectError

from teage_liu.multiagent.a2a_std.client import StdA2AClient, StdA2AClientError
from teage_liu.multiagent.a2a_std.models import Message, TextPart


def _config() -> dict:
    return {
        "a2a": {
            "remote_endpoints": [
                {"name": "peer-a", "url": "http://peer-a.example.com"},
            ],
            "timeout_seconds": 5,
            "retry_count": 1,
        }
    }


def _message(text: str = "hi") -> Message:
    return Message(role="user", message_id="m_1", parts=[TextPart(text=text)])


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def _card_patch():
    """mock GET 卡片返回（card.url 指向 peer 端点）。"""
    return patch.object(
        StdA2AClient, "_client",
        create=True,
        new_callable=lambda: _FakeHttpClient(),
    )


class _FakeHttpClient:
    """可注入的 httpx.AsyncClient 替身。"""

    def __init__(self):
        self.get = AsyncMock(return_value=_FakeResponse(
            {"protocolVersion": "1.0", "name": "peer",
             "url": "http://peer-a.example.com/a2a/std/jsonrpc"}
        ))
        self.post = AsyncMock()
        self.stream = MagicMock()

    async def aclose(self):
        pass


def _make_client() -> tuple[StdA2AClient, _FakeHttpClient]:
    client = StdA2AClient(_config())
    fake = _FakeHttpClient()
    client._client = fake  # type: ignore[assignment]
    return client, fake


class TestDiscovery:
    """Agent Card 发现。"""

    def test_fetch_agent_card_and_cache(self):
        client, fake = _make_client()
        got = _run(client.fetch_agent_card("peer-a"))
        _run(client.fetch_agent_card("peer-a"))  # 二次走缓存
        assert got["name"] == "peer"
        assert fake.get.await_count == 1

    def test_jsonrpc_url_from_card(self):
        client, fake = _make_client()
        url = _run(client.get_jsonrpc_url("peer-a"))
        assert url == "http://peer-a.example.com/a2a/std/jsonrpc"

    def test_jsonrpc_url_fallback_on_card_failure(self):
        client, fake = _make_client()
        fake.get = AsyncMock(side_effect=ConnectError("net"))
        url = _run(client.get_jsonrpc_url("peer-a"))
        assert url == "http://peer-a.example.com/a2a/std/jsonrpc"


class TestMethods:
    """标准方法调用。"""

    def test_message_send(self):
        client, fake = _make_client()
        fake.post.return_value = _FakeResponse(
            {"jsonrpc": "2.0", "result": {"id": "t_1", "status": {"state": "working"}}, "id": 1}
        )
        task = _run(client.message_send("peer-a", _message("hello")))
        assert task["id"] == "t_1"
        payload = fake.post.call_args.kwargs["json"]
        assert payload["method"] == "message/send"
        assert payload["params"]["message"]["parts"][0]["text"] == "hello"
        assert fake.post.call_args.kwargs["headers"]["A2A-Version"] == "1.0"

    def test_tasks_get_with_context(self):
        client, fake = _make_client()
        fake.post.return_value = _FakeResponse(
            {"jsonrpc": "2.0", "result": {"id": "t_1"}, "id": 1}
        )
        task = _run(client.tasks_get("peer-a", "t_1", context_id="c1"))
        assert task["id"] == "t_1"
        payload = fake.post.call_args.kwargs["json"]
        assert payload["method"] == "tasks/get"
        assert payload["params"] == {"id": "t_1", "contextId": "c1"}

    def test_tasks_cancel(self):
        client, fake = _make_client()
        fake.post.return_value = _FakeResponse(
            {"jsonrpc": "2.0", "result": {"id": "t_1", "status": {"state": "canceled"}}, "id": 1}
        )
        task = _run(client.tasks_cancel("peer-a", "t_1"))
        assert task["status"]["state"] == "canceled"

    def test_jsonrpc_error_raises_with_code(self):
        client, fake = _make_client()
        fake.post.return_value = _FakeResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Task not found"}, "id": 1}
        )
        with pytest.raises(StdA2AClientError) as exc:
            _run(client.tasks_get("peer-a", "t_nope"))
        assert exc.value.code == -32001

    def test_retry_on_connect_error(self):
        client, fake = _make_client()
        fake.post.side_effect = [
            ConnectError("boom"),
            _FakeResponse({"jsonrpc": "2.0", "result": {"id": "t_1"}, "id": 1}),
        ]
        task = _run(client.tasks_get("peer-a", "t_1"))
        assert task["id"] == "t_1"
        assert fake.post.await_count == 2


class TestStreaming:
    """SSE 事件流。"""

    def test_message_stream_parses_frames(self):
        client, fake = _make_client()
        frames = (
            "id: t_1:1\nevent: TaskStatusUpdateEvent\n"
            'data: {"jsonrpc":"2.0","id":1,"method":"TaskStatusUpdateEvent",'
            '"params":{"id":"t_1","status":{"state":"working"}}}\n\n'
            "id: t_1:2\nevent: TaskStatusUpdateEvent\n"
            'data: {"jsonrpc":"2.0","id":1,"method":"TaskStatusUpdateEvent",'
            '"params":{"id":"t_1","status":{"state":"completed"}}}\n\n'
        )

        class _FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def aiter_lines(self):
                async def gen():
                    for ln in frames.split("\n"):
                        yield ln
                return gen()

        fake.stream.return_value = _FakeStream()

        async def _collect():
            return [e async for e in client.message_stream("peer-a", _message())]

        events = _run(_collect())
        assert len(events) == 2
        assert events[0]["method"] == "TaskStatusUpdateEvent"
        assert events[0]["params"]["status"]["state"] == "working"
        assert events[1]["params"]["status"]["state"] == "completed"

    def test_wait_for_terminal_polls(self):
        client, fake = _make_client()
        states = iter([{"state": "working"}, {"state": "working"}, {"state": "completed"}])

        async def fake_get(endpoint, task_id, context_id=None):
            return {"id": task_id, "status": next(states)}

        client.tasks_get = fake_get  # type: ignore[assignment]
        task = _run(client.wait_for_terminal("peer-a", "t_1", timeout=10))
        assert task["status"]["state"] == "completed"

    def test_wait_for_terminal_timeout(self):
        client, fake = _make_client()

        async def fake_get(endpoint, task_id, context_id=None):
            return {"id": task_id, "status": {"state": "working"}}

        client.tasks_get = fake_get  # type: ignore[assignment]
        with pytest.raises(StdA2AClientError):
            _run(client.wait_for_terminal("peer-a", "t_1", timeout=0.5))
