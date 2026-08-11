"""SSE 帧构造（标准 A2A 流式传输）。

每帧三行：
- `id: <taskId>`          供 tasks/resubscribe 的 Last-Event-ID 断点续传
- `event: <方法名>`        TaskStatusUpdateEvent / TaskArtifactUpdateEvent
- `data: <JSON-RPC 帧>`    {"jsonrpc":"2.0","id":<req_id>,"method":<方法名>,"params":{...}}

终态事件发出后服务端关闭流（v1.0 完成判定 = task 状态 + 流关闭）。
"""
from __future__ import annotations

import json
from typing import Any, Optional


def sse_frame(
    event_id: str,
    event_method: str,
    params: dict,
    req_id: Any = None,
) -> str:
    """构造单条 SSE 帧（含尾随空行）。

    Args:
        event_id: 唯一事件 id（taskId:seq），客户端回传 Last-Event-ID 断点续传。
        event_method: 事件方法名（TaskStatusUpdateEvent / TaskArtifactUpdateEvent）。
    """
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": event_method,
        "params": params,
    }
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {event_id}\nevent: {event_method}\ndata: {data}\n\n"


def sse_close_frame(req_id: Any = None) -> str:
    """流结束标记帧（可选，兼容客户端按事件结束）。"""
    payload = {"jsonrpc": "2.0", "id": req_id, "method": "streamEnded", "params": {}}
    return f"event: streamEnded\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def parse_last_event_id(header_value: Optional[str]) -> Optional[str]:
    """解析 Last-Event-ID 请求头（断点续传用）。"""
    if not header_value:
        return None
    return header_value.strip() or None
