"""A2A 线协议双方言支持。

背景：a2a-protocol.org 规范 JSON schema（v0.2.5 及早期）与官方 a2a-sdk
1.1.2（v1.0 protobuf 方言）在方法名/枚举上有分歧：
- 方法名：规范 `message/send` vs SDK `SendMessage`（PascalCase）
- 枚举：规范 `working` / `user` vs SDK `TASK_STATE_WORKING` / `ROLE_USER`

策略：**请求侧嗅探方言，响应按请求方言输出**——两类客户端都能互通：
- SDK 客户端（PascalCase 方法 / UPPER_SNAKE 枚举）→ SDK 方言响应（其 protobuf 可解析）
- 规范风格客户端（message/send / 小写枚举）→ 规范方言响应

内部 Task 记录始终存规范小写值，方言转换仅发生在线边界（handlers/sse 帧）。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

# SDK 1.1.2 PascalCase 方法名 → 规范方法名
SDK_METHOD_ALIASES: dict[str, str] = {
    "SendMessage": "message/send",
    "SendStreamingMessage": "message/stream",
    "GetTask": "tasks/get",
    "ListTasks": "tasks/list",
    "CancelTask": "tasks/cancel",
    "SubscribeToTask": "tasks/resubscribe",
    "CreateTaskPushNotificationConfig": "tasks/pushNotificationConfig/set",
    "GetTaskPushNotificationConfig": "tasks/pushNotificationConfig/get",
    "ListTaskPushNotificationConfigs": "tasks/pushNotificationConfig/list",
    "DeleteTaskPushNotificationConfig": "tasks/pushNotificationConfig/delete",
    "GetExtendedAgentCard": "agent/getAuthenticatedExtendedCard",
}

DIALECT_SDK = "sdk"
DIALECT_CANONICAL = "canonical"


def detect_dialect(method: str, params: Optional[dict]) -> str:
    """嗅探请求方言：SDK 方法名或 UPPER_SNAKE 枚举 → sdk，否则 canonical。"""
    if method in SDK_METHOD_ALIASES:
        return DIALECT_SDK
    if params:
        blob = json.dumps(params, ensure_ascii=False)
        if "ROLE_USER" in blob or "ROLE_AGENT" in blob or "TASK_STATE_" in blob:
            return DIALECT_SDK
    return DIALECT_CANONICAL


def normalize_method(method: str) -> str:
    """SDK PascalCase 方法名 → 规范方法名（未命中原样返回）。"""
    return SDK_METHOD_ALIASES.get(method, method)


def to_wire_state(state: str, dialect: str) -> str:
    """内部规范小写状态 → 线值。"""
    if dialect == DIALECT_SDK:
        return "TASK_STATE_" + state.upper()
    return state


def to_wire_role(role: str, dialect: str) -> str:
    """内部规范小写角色 → 线值。"""
    if dialect == DIALECT_SDK:
        return "ROLE_" + role.upper()
    return role


def apply_dialect(task_wire: dict, dialect: str) -> dict:
    """Task 线 dict 按方言重写 status.state 与 history[].role（浅拷贝）。"""
    if dialect == DIALECT_CANONICAL:
        return task_wire
    out = copy.deepcopy(task_wire)
    status = out.get("status")
    if isinstance(status, dict) and isinstance(status.get("state"), str):
        status["state"] = to_wire_state(status["state"], dialect)
    for msg in out.get("history") or []:
        if isinstance(msg, dict) and isinstance(msg.get("role"), str):
            msg["role"] = to_wire_role(msg["role"], dialect)
    return out


def apply_dialect_event(event_params: dict, dialect: str) -> dict:
    """SSE 事件 params 按方言重写状态/角色。"""
    if dialect == DIALECT_CANONICAL:
        return event_params
    out = copy.deepcopy(event_params)
    status = out.get("status")
    if isinstance(status, dict) and isinstance(status.get("state"), str):
        status["state"] = to_wire_state(status["state"], dialect)
    return out


# ---------------------------------------------------------------------------
# SDK 1.1.2 方言：严格 ParseDict，响应只能含 SDK protobuf 已知字段
# ---------------------------------------------------------------------------

_SDK_TASK_KEYS = {"id", "contextId", "status", "artifacts", "history", "metadata"}
_SDK_STATUS_KEYS = {"state", "message", "timestamp"}
_SDK_MESSAGE_KEYS = {"messageId", "contextId", "taskId", "role", "parts", "metadata"}
_SDK_PART_KEYS = {"text", "raw", "url", "data", "metadata", "filename", "mediaType"}
_SDK_ARTIFACT_KEYS = {"artifactId", "name", "description", "parts", "metadata"}


def _pick(d: dict, keys: set[str]) -> dict:
    return {k: v for k, v in d.items() if k in keys}


def _sdk_part(part: dict) -> dict:
    """过滤 Part 为 SDK 字段集（去掉 kind 判别字段）。"""
    out = _pick(part, _SDK_PART_KEYS)
    if "bytes" in part and "raw" not in out:
        out["raw"] = part["bytes"]
    return out


def _sdk_message(msg: dict) -> dict:
    out = _pick(msg, _SDK_MESSAGE_KEYS)
    if isinstance(out.get("parts"), list):
        out["parts"] = [_sdk_part(p) for p in out["parts"]]
    return out


def _sdk_artifact(art: dict) -> dict:
    out = _pick(art, _SDK_ARTIFACT_KEYS)
    if isinstance(out.get("parts"), list):
        out["parts"] = [_sdk_part(p) for p in out["parts"]]
    return out


def filter_sdk_task(task_wire: dict) -> dict:
    """Task 线 dict 过滤为 SDK protobuf 已知字段（严格 ParseDict 兼容）。"""
    out = _pick(task_wire, _SDK_TASK_KEYS)
    status = out.get("status")
    if isinstance(status, dict):
        out["status"] = _pick(status, _SDK_STATUS_KEYS)
    if isinstance(out.get("artifacts"), list):
        out["artifacts"] = [_sdk_artifact(a) for a in out["artifacts"]]
    if isinstance(out.get("history"), list):
        out["history"] = [_sdk_message(m) for m in out["history"]]
    return out


def sdk_send_message_result(task_wire: dict) -> dict:
    """SDK 方言 message/send 响应：SendMessageResponse 线格式 {"task": ...}。"""
    return {"task": filter_sdk_task(task_wire)}


def sdk_stream_response_for(event: dict, context_id: str) -> Optional[dict]:
    """SDK 方言 SSE 帧的 result（StreamResponse 线格式）。

    我方事件 → SDK：
    - TaskStatusUpdateEvent → {"statusUpdate": {taskId, contextId, status, metadata}}
    - TaskArtifactUpdateEvent → 每个 artifact 一帧 {"artifactUpdate": {...lastChunk}}
    """
    method = event.get("method")
    params = event.get("params") or {}
    task_id = params.get("id")
    if method == "TaskArtifactUpdateEvent":
        artifacts = params.get("artifacts") or []
        if not artifacts:
            return None
        last = artifacts[-1]
        return {
            "artifactUpdate": {
                "taskId": task_id,
                "contextId": context_id,
                "artifact": _sdk_artifact(last),
                "append": True,
                "lastChunk": True,
                "metadata": None,
            }
        }
    status = params.get("status") or {}
    if isinstance(status.get("state"), str):
        status = {**status, "state": to_wire_state(status["state"], DIALECT_SDK)}
    return {
        "statusUpdate": {
            "taskId": task_id,
            "contextId": context_id,
            "status": _pick(status, _SDK_STATUS_KEYS),
            "metadata": None,
        }
    }


def sse_result_frame(event_id: str, result: dict, req_id: Any = None) -> str:
    """SDK 方言 SSE 帧：data 行是 JSON-RPC 响应（result = StreamResponse）。"""
    payload = {"jsonrpc": "2.0", "result": result, "id": req_id}
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {event_id}\ndata: {data}\n\n"
