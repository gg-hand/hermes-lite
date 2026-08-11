"""标准 A2A 方法 handlers（params, ctx）签名，供 JsonRpcDispatcher 注册。

ctx 为 A2ARequestContext（见 router.py），携带 request/config/bb_root/task_manager/
engine_adapter/card_builder。返回 wire dict（camelCase）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse

from teage_liu.multiagent.a2a_std.dialect import (
    DIALECT_CANONICAL,
    DIALECT_SDK,
    apply_dialect,
    apply_dialect_event,
    filter_sdk_task,
    sdk_send_message_result,
    sdk_stream_response_for,
    sse_result_frame,
    to_wire_state,
)
from teage_liu.multiagent.a2a_std.engine_adapter import A2AEngineAdapter
from teage_liu.multiagent.a2a_std.exceptions import UnsupportedOperationError
from teage_liu.multiagent.a2a_std.models import (
    MessageSendRequest,
    PushConfigDeleteRequest,
    PushConfigGetRequest,
    PushConfigListRequest,
    PushConfigSetRequest,
    SendMessageStreamingRequest,
    TaskIdParams,
    TaskQueryParams,
)
from teage_liu.multiagent.a2a_std.sse import sse_frame
from teage_liu.multiagent.a2a_std.task_manager import A2ATaskManager

logger = logging.getLogger(__name__)


class A2ARequestContext:
    """标准方法执行上下文。"""

    def __init__(
        self,
        request: Request,
        config: dict,
        bb_root: Any,
        task_manager: A2ATaskManager,
        engine_adapter: A2AEngineAdapter,
        card_builder: Any,
        dialect: str = DIALECT_CANONICAL,
    ) -> None:
        self.request = request
        self.config = config
        self.bb_root = bb_root
        self.task_manager = task_manager
        self.engine_adapter = engine_adapter
        self.card_builder = card_builder
        self.dialect = dialect


# ---------------------------------------------------------------------------
# 消息发送
# ---------------------------------------------------------------------------

def _task_result(task_wire: dict, dialect: str) -> dict:
    """Task 线 dict 按方言输出（SDK 方言：过滤 SDK 字段集）。"""
    wire = apply_dialect(task_wire, dialect)
    if dialect == DIALECT_SDK:
        return filter_sdk_task(wire)
    return wire


async def message_send(params: dict, ctx: A2ARequestContext) -> dict:
    """message/send：创建 Task（始终返回 Task，异步协作模型）。"""
    req = MessageSendRequest.model_validate(params)
    task = await ctx.engine_adapter.handle_message_send(req.message, req.context_id)
    if ctx.dialect == DIALECT_SDK:
        # SDK 方言：SendMessageResponse 线格式 {"task": ...}
        return sdk_send_message_result(apply_dialect(task, DIALECT_SDK))
    return task


async def message_stream(params: dict, ctx: A2ARequestContext) -> Any:
    """message/stream：Accept: text/event-stream 时返回 SSE 流，否则等同 message/send。"""
    req = SendMessageStreamingRequest.model_validate(params)
    accept = ctx.request.headers.get("Accept", "")
    if "text/event-stream" not in accept:
        task = await ctx.engine_adapter.handle_message_send(req.message, req.context_id)
        if ctx.dialect == DIALECT_SDK:
            return sdk_send_message_result(apply_dialect(task, DIALECT_SDK))
        return task

    task = await ctx.engine_adapter.handle_message_send(req.message, req.context_id)
    task_id = task["id"]
    context_id = task.get("contextId")
    req_id = ctx.request.state.req_id
    dialect = ctx.dialect

    async def _event_stream():
        try:
            if dialect == DIALECT_SDK:
                # 首帧：完整 Task（SDK StreamResponse.task 字段）
                yield sse_result_frame(
                    f"{task_id}:task",
                    {"task": filter_sdk_task(apply_dialect(task, DIALECT_SDK))},
                    req_id,
                )
            async for event in ctx.task_manager.get_stream(task_id):
                method = event.get("method", "")
                payload = event.get("params") or {}
                if dialect == DIALECT_SDK:
                    result = sdk_stream_response_for(event, context_id)
                    if result is not None:
                        yield sse_result_frame(event.get("id", task_id), result, req_id)
                elif method:
                    yield sse_frame(
                        event.get("id", task_id), method,
                        apply_dialect_event(payload, dialect), req_id,
                    )
        except Exception as e:  # 异常时补发终态事件，避免调用方永远等待
            logger.error("message/stream 异常 (task=%s): %s", task_id, e)
            if dialect == DIALECT_SDK:
                yield sse_result_frame(f"{task_id}:err", {
                    "statusUpdate": {
                        "taskId": task_id, "contextId": context_id,
                        "status": {"state": to_wire_state("failed", dialect)},
                        "metadata": None,
                    },
                }, req_id)
            else:
                yield sse_frame(
                    f"{task_id}:err", "TaskStatusUpdateEvent",
                    {"id": task_id, "status": {"state": to_wire_state("failed", dialect)},
                     "timestamp": None},
                    req_id,
                )

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 任务查询/取消
# ---------------------------------------------------------------------------

async def tasks_get(params: dict, ctx: A2ARequestContext) -> dict:
    req = TaskIdParams.model_validate(params)
    task = await ctx.task_manager.get_task(req.id, req.context_id)
    return _task_result(task.model_dump(by_alias=True), ctx.dialect)


async def tasks_list(params: dict, ctx: A2ARequestContext) -> list:
    req = TaskQueryParams.model_validate(params)
    tasks = await ctx.task_manager.list_tasks(req.context_id, req.metadata)
    return [_task_result(t.model_dump(by_alias=True), ctx.dialect) for t in tasks]


async def tasks_cancel(params: dict, ctx: A2ARequestContext) -> dict:
    req = TaskIdParams.model_validate(params)
    return _task_result(
        await ctx.engine_adapter.handle_task_cancel(req.id, req.context_id), ctx.dialect,
    )


async def tasks_resubscribe(params: dict, ctx: A2ARequestContext) -> StreamingResponse:
    """tasks/resubscribe：重放 Last-Event-ID 之后的事件，再实时订阅。"""
    req = TaskIdParams.model_validate(params)
    task_id = req.id
    req_id = ctx.request.state.req_id
    last_id = (ctx.request.headers.get("Last-Event-ID") or "").strip() or None

    async def _event_stream():
        try:
            async for event in ctx.task_manager.get_stream(task_id, after_event_id=last_id):
                method = event.get("method", "")
                payload = event.get("params") or {}
                if ctx.dialect == DIALECT_SDK:
                    result = sdk_stream_response_for(event, req.context_id)
                    if result is not None:
                        yield sse_result_frame(event.get("id", task_id), result, req_id)
                elif method:
                    yield sse_frame(
                        event.get("id", task_id), method,
                        apply_dialect_event(payload, ctx.dialect), req_id,
                    )
        except Exception as e:
            logger.error("tasks/resubscribe 异常 (task=%s): %s", task_id, e)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Push 通知配置
# ---------------------------------------------------------------------------

async def push_config_set(params: dict, ctx: A2ARequestContext) -> dict:
    req = PushConfigSetRequest.model_validate(params)
    task = await ctx.task_manager.set_push_config(req.id, req.push_notification_config)
    return _task_result(task.model_dump(by_alias=True), ctx.dialect)


async def push_config_get(params: dict, ctx: A2ARequestContext) -> dict:
    req = PushConfigGetRequest.model_validate(params)
    cfg = await ctx.task_manager.get_push_config(req.id)
    return {"id": req.id, "pushNotificationConfig": cfg}


async def push_config_list(params: dict, ctx: A2ARequestContext) -> list:
    req = PushConfigListRequest.model_validate(params)
    return await ctx.task_manager.list_push_configs(req.context_id)


async def push_config_delete(params: dict, ctx: A2ARequestContext) -> dict:
    req = PushConfigDeleteRequest.model_validate(params)
    task = await ctx.task_manager.delete_push_config(req.id)
    return _task_result(task.model_dump(by_alias=True), ctx.dialect)


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------

async def get_authenticated_extended_card(params: dict, ctx: A2ARequestContext) -> dict:
    """agent/getAuthenticatedExtendedCard：未声明支持时返回 -32004。"""
    card = ctx.card_builder.build()
    if not card.get("supportsAuthenticatedExtendedCard"):
        raise UnsupportedOperationError("Authenticated extended card not supported")
    return card
