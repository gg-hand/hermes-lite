"""JSON-RPC 2.0 信封与分发器测试（jsonrpc.py）。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from teage_liu.multiagent.a2a_std.exceptions import (
    TaskNotFoundError,
    to_jsonrpc_error,
)
from teage_liu.multiagent.a2a_std.jsonrpc import JsonRpcDispatcher, make_error


class TestDispatcher:
    """分发器基础行为。"""

    @pytest.fixture
    def dispatcher(self):
        d = JsonRpcDispatcher()

        async def ping(params, ctx):
            return {"pong": True, "echo": params.get("x")}

        d.register("ping", ping)
        return d

    def test_success_result(self, dispatcher):
        resp = dispatcher.dispatch_sync({"jsonrpc": "2.0", "method": "ping", "params": {"x": 1}, "id": 1})
        assert resp["result"] == {"pong": True, "echo": 1}
        assert resp["id"] == 1

    def test_missing_jsonrpc_version(self, dispatcher):
        resp = dispatcher.dispatch_sync({"method": "ping", "id": 1})
        assert resp["error"]["code"] == -32600

    def test_unknown_method(self, dispatcher):
        resp = dispatcher.dispatch_sync({"jsonrpc": "2.0", "method": "nope", "id": 1})
        assert resp["error"]["code"] == -32601

    def test_non_dict_params(self, dispatcher):
        resp = dispatcher.dispatch_sync({"jsonrpc": "2.0", "method": "ping", "params": [1, 2], "id": 1})
        assert resp["error"]["code"] == -32602

    def test_non_dict_request(self, dispatcher):
        resp = dispatcher.dispatch_sync("not-a-request")
        assert resp["error"]["code"] == -32600

    def test_batch(self, dispatcher):
        resp = dispatcher.dispatch_sync([
            {"jsonrpc": "2.0", "method": "ping", "params": {}, "id": 1},
            {"jsonrpc": "2.0", "method": "nope", "id": 2},
        ])
        assert len(resp) == 2
        assert "result" in resp[0]
        assert resp[1]["error"]["code"] == -32601

    def test_a2a_error_mapped(self):
        d = JsonRpcDispatcher()

        async def get(params, ctx):
            raise TaskNotFoundError("no such task")

        d.register("tasks/get", get)
        resp = d.dispatch_sync({"jsonrpc": "2.0", "method": "tasks/get", "params": {}, "id": 5})
        assert resp["error"]["code"] == -32001
        assert resp["id"] == 5

    def test_unexpected_exception_maps_internal(self):
        d = JsonRpcDispatcher()

        async def boom(params, ctx):
            raise ValueError("boom")

        d.register("boom", boom)
        resp = d.dispatch_sync({"jsonrpc": "2.0", "method": "boom", "params": {}, "id": 1})
        assert resp["error"]["code"] == -32603

    def test_unregister(self, dispatcher):
        dispatcher.unregister("ping")
        assert not dispatcher.has_method("ping")


class TestErrorHelpers:
    """错误构造辅助。"""

    def test_make_error(self):
        err = make_error(3, -32001, "Task not found")
        assert err == {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Task not found"}, "id": 3}

    def test_to_jsonrpc_error_with_data(self):
        exc = TaskNotFoundError()
        err = to_jsonrpc_error(exc, req_id=7)
        assert err["error"]["code"] == -32001
        assert err["id"] == 7

    def test_pydantic_validation_error_maps_invalid_params(self):
        class Req(BaseModel):
            id: int

        d = JsonRpcDispatcher()

        async def handler(params, ctx):
            Req.model_validate(params)  # 抛 ValidationError
            return {}

        d.register("tasks/get", handler)
        resp = d.dispatch_sync({"jsonrpc": "2.0", "method": "tasks/get", "params": {"id": "not-int"}, "id": 1})
        assert resp["error"]["code"] == -32602


# 同步测试辅助：在 sync 测试中跑 async dispatcher
import asyncio  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# 给 JsonRpcDispatcher 附加同步分发辅助（仅测试用）
def _dispatch_sync(self, payload, ctx=None):
    return _run(self.dispatch(payload, ctx))


JsonRpcDispatcher.dispatch_sync = _dispatch_sync  # type: ignore[attr-defined]
