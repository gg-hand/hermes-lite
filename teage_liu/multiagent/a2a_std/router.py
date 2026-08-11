"""标准 A2A v1.0 FastAPI 路由。

端点：
- GET  /.well-known/agent-card.json   Agent Card 发布（RFC 8615）
- POST /a2a/std/jsonrpc              标准 JSON-RPC 2.0 端点

响应头：A2A-Version: 1.0、A2A-Extensions: <声明列表>
请求头校验：A2A-Version（不支持 → -32004）、A2A-Extensions（扩展门控）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from teage_liu.multiagent.a2a_std.agent_card import AgentCardBuilder
from teage_liu.multiagent.a2a_std.dialect import detect_dialect, normalize_method
from teage_liu.multiagent.a2a_std.engine_adapter import A2AEngineAdapter
from teage_liu.multiagent.a2a_std.exceptions import UnsupportedOperationError
from teage_liu.multiagent.a2a_std.handlers import (
    A2ARequestContext,
    get_authenticated_extended_card,
    message_send,
    message_stream,
    push_config_delete,
    push_config_get,
    push_config_list,
    push_config_set,
    tasks_cancel,
    tasks_get,
    tasks_list,
    tasks_resubscribe,
)
from teage_liu.multiagent.a2a_std.jsonrpc import JsonRpcDispatcher, make_error, make_result
from teage_liu.multiagent.a2a_std.task_manager import A2ATaskManager
from teage_liu.multiagent.a2a_std.task_store import TaskEventHub, TaskStore

logger = logging.getLogger(__name__)

_SUPPORTED_VERSIONS = {"1.0"}


def create_a2a_std_router(
    bb_root: Path, config: dict, container: Any = None
) -> APIRouter:
    """创建标准 A2A 路由器。

    Args:
        bb_root: blackboard 根目录。
        config: 完整应用配置（a2a.standard 段驱动）。
        container: 预留 DI 容器（后续集成 StdA2AClient 等）。
    """
    a2a_cfg = config.get("a2a", {}) or {}
    std_cfg = a2a_cfg.get("standard", {}) or {}

    # ---- 任务层 ----
    store_path = std_cfg.get("task_store_path") or (
        Path(bb_root) / "tasks" / "a2a" / "tasks.json"
    )
    store = TaskStore(Path(store_path))
    hub = TaskEventHub()
    push_enabled = bool((std_cfg.get("capabilities") or {}).get("push_notifications", False))
    task_manager = A2ATaskManager(
        store, hub, push_notifications_enabled=push_enabled
    )
    card_builder = AgentCardBuilder(config, bb_root)
    engine_adapter = A2AEngineAdapter(bb_root, config, task_manager)

    # ---- 分发器：标准方法 ----
    dispatcher = JsonRpcDispatcher()
    dispatcher.register("message/send", message_send)
    dispatcher.register("message/stream", message_stream)
    dispatcher.register("tasks/get", tasks_get)
    dispatcher.register("tasks/list", tasks_list)
    dispatcher.register("tasks/cancel", tasks_cancel)
    dispatcher.register("tasks/resubscribe", tasks_resubscribe)
    dispatcher.register("tasks/pushNotificationConfig/set", push_config_set)
    dispatcher.register("tasks/pushNotificationConfig/get", push_config_get)
    dispatcher.register("tasks/pushNotificationConfig/list", push_config_list)
    dispatcher.register("tasks/pushNotificationConfig/delete", push_config_delete)
    dispatcher.register("agent/getAuthenticatedExtendedCard", get_authenticated_extended_card)

    # ---- 扩展（Phase E：A2A-Extensions 头门控） ----
    from teage_liu.multiagent.a2a_std.extensions import (
        EXTENSION_METHODS,
        build_extension_registry,
    )

    extensions_enabled = std_cfg.get("extensions_enabled", True)
    # 未显式配置 extensions 列表时默认启用全部扩展方法
    declared_extensions: list[str] = list(
        std_cfg.get("extensions") or (EXTENSION_METHODS if extensions_enabled else [])
    )
    extension_registry: dict[str, Any] = {}
    if extensions_enabled and declared_extensions:
        extension_registry = build_extension_registry(bb_root, config)
        # 仅保留配置声明的扩展
        extension_registry = {
            k: v for k, v in extension_registry.items() if k in declared_extensions
        }
        logger.info("A2A 扩展注册: %s", sorted(extension_registry))

    router = APIRouter(tags=["a2a-standard"])

    # ------------------------------------------------------------------
    # Agent Card
    # ------------------------------------------------------------------
    @router.get("/.well-known/agent-card.json")
    async def agent_card(request: Request) -> JSONResponse:
        return card_builder.card_route()

    # ------------------------------------------------------------------
    # JSON-RPC 端点
    # ------------------------------------------------------------------
    @router.post("/a2a/std/jsonrpc")
    async def jsonrpc(request: Request) -> Any:
        # 1. A2A-Version 校验
        version = request.headers.get("A2A-Version")
        if version and version not in _SUPPORTED_VERSIONS:
            return JSONResponse(
                status_code=200,
                content=make_error(
                    None, -32004, f"Unsupported A2A-Version: {version}"
                ),
            )

        # 2. 解析 body
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=200, content=make_error(None, -32700, "Parse error")
            )
        except Exception:
            return JSONResponse(
                status_code=200, content=make_error(None, -32700, "Parse error")
            )

        # 3. 构造上下文（req_id 供 SSE 帧回填）
        ctx = A2ARequestContext(
            request=request,
            config=config,
            bb_root=bb_root,
            task_manager=task_manager,
            engine_adapter=engine_adapter,
            card_builder=card_builder,
        )

        # 4. 分发（单条 + 批量 + 扩展门控）
        response = await _dispatch_with_extensions(
            payload, ctx, dispatcher, extension_registry, declared_extensions
        )

        if isinstance(response, StreamingResponse):
            return response

        return JSONResponse(
            status_code=200,
            content=response,
            headers={
                "A2A-Version": "1.0",
                "A2A-Extensions": ",".join(declared_extensions),
            },
        )

    # 挂载运行时引用（lifespan 启停 TaskDriver 用）
    router.a2a_std_engine_adapter = engine_adapter  # type: ignore[attr-defined]
    router.a2a_std_task_manager = task_manager  # type: ignore[attr-defined]

    return router


async def _dispatch_with_extensions(
    payload: Any,
    ctx: A2ARequestContext,
    dispatcher: JsonRpcDispatcher,
    extension_registry: dict[str, Any],
    declared_extensions: list[str],
) -> Any:
    """分发：message/stream SSE 特判 + 扩展头门控 + 标准分发。"""
    if isinstance(payload, list):
        return [
            await _dispatch_one_with_extensions(
                req, ctx, dispatcher, extension_registry, declared_extensions
            )
            for req in payload
        ]
    return await _dispatch_one_with_extensions(
        payload, ctx, dispatcher, extension_registry, declared_extensions
    )


async def _dispatch_one_with_extensions(
    req: Any,
    ctx: A2ARequestContext,
    dispatcher: JsonRpcDispatcher,
    extension_registry: dict[str, Any],
    declared_extensions: list[str],
) -> Any:
    if not isinstance(req, dict):
        return make_error(None, -32600, "Invalid Request: not a dict")
    req_id = req.get("id")
    raw_method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    # 双方言：SDK PascalCase 方法名归一为规范名；按请求嗅探响应方言
    ctx.dialect = detect_dialect(raw_method or "", params)
    method_name = normalize_method(raw_method or "")
    if method_name != raw_method:
        req = {**req, "method": method_name}

    # 扩展方法：必须在 A2A-Extensions 请求头中声明
    if method_name in extension_registry:
        header = ctx.request.headers.get("A2A-Extensions", "")
        declared = {e.strip() for e in header.split(",") if e.strip()}
        if method_name not in declared:
            return make_error(req_id, -32601, f"Method not found: {method_name}")
        try:
            result = await extension_registry[method_name](params)
            return make_result(req_id, result)
        except Exception as e:
            logger.exception("A2A 扩展方法异常: %s", method_name)
            return make_error(req_id, -32603, f"Internal error: {e}")

    # SSE 流式方法特判（返回 StreamingResponse 时不能走 JSON-RPC 包装）
    if method_name in ("message/stream", "tasks/resubscribe"):
        ctx.request.state.req_id = req_id
        handler = message_stream if method_name == "message/stream" else tasks_resubscribe
        try:
            result = await handler(params, ctx)
        except ValidationError as e:
            return make_error(req_id, -32602, f"Invalid params: {e}")
        if isinstance(result, StreamingResponse):
            return result
        return make_result(req_id, result)

    # 标准方法
    ctx.request.state.req_id = req_id
    return await dispatcher.dispatch(req, ctx)
