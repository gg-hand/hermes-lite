"""JSON-RPC 2.0 信封与分发器（标准 A2A 传输层）。

标准错误码：-32700 parse / -32600 invalid request / -32601 method not found
/ -32602 invalid params / -32603 internal；业务错误经 A2AError 映射。
支持批量请求（list）。分发器与路由解耦，可被标准端点复用。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError

from teage_liu.multiagent.a2a_std.exceptions import A2AError

logger = logging.getLogger(__name__)

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

# handler 签名：async (params: dict, ctx: Any) -> Any
Handler = Callable[[dict, Any], Awaitable[Any]]


def make_error(req_id: Any, code: int, message: str, data: Optional[dict] = None) -> dict:
    """构造 JSON-RPC 错误响应体。"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "error": error, "id": req_id}


def make_result(req_id: Any, result: Any) -> dict:
    """构造 JSON-RPC 成功响应体。"""
    return {"jsonrpc": "2.0", "result": result, "id": req_id}


class JsonRpcDispatcher:
    """JSON-RPC 2.0 方法分发器。

    用法：
        d = JsonRpcDispatcher()
        d.register("tasks/get", handler)
        resp = await d.dispatch(payload, ctx)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, method: str, handler: Handler) -> None:
        """注册方法 handler。"""
        self._handlers[method] = handler

    def unregister(self, method: str) -> None:
        """注销方法（扩展关闭时用）。"""
        self._handlers.pop(method, None)

    def has_method(self, method: str) -> bool:
        return method in self._handlers

    @property
    def methods(self) -> set[str]:
        return set(self._handlers)

    async def dispatch(self, payload: Any, ctx: Any = None) -> Any:
        """分发单个或批量请求。"""
        if isinstance(payload, list):
            return [await self._dispatch_one(req, ctx) for req in payload]
        return await self._dispatch_one(payload, ctx)

    async def _dispatch_one(self, req: Any, ctx: Any) -> dict:
        if not isinstance(req, dict):
            return make_error(None, ERR_INVALID_REQUEST, "Invalid Request: not a dict")

        req_id = req.get("id")
        method_name = req.get("method")
        if req.get("jsonrpc") != "2.0" or not isinstance(method_name, str) or not method_name:
            return make_error(req_id, ERR_INVALID_REQUEST, "Invalid Request: missing jsonrpc/method")

        handler = self._handlers.get(method_name)
        if handler is None:
            return make_error(req_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method_name}")

        params = req.get("params") or {}
        if not isinstance(params, dict):
            return make_error(req_id, ERR_INVALID_PARAMS, "Invalid params: must be an object")

        try:
            result = await handler(params, ctx)
            return make_result(req_id, result)
        except A2AError as e:
            return make_error(req_id, e.code, e.message, e.data)
        except ValidationError as e:
            return make_error(req_id, ERR_INVALID_PARAMS, f"Invalid params: {e}")
        except Exception as e:
            logger.exception("JSON-RPC 内部错误: %s", method_name)
            return make_error(req_id, ERR_INTERNAL, f"Internal error: {e}")
