"""标准 A2A v1.0 异常层级（对应 A2A 标准库错误码）。

与旧 A2A Gateway 的 -32001..-32005 语义（签名/路径/锁/限流/director）**不同**：
这里是标准 A2A 的官方错误码：
- -32001 TaskNotFoundError
- -32002 TaskNotCancelableError
- -32003 PushNotificationNotSupportedError
- -32004 UnsupportedOperationError
- -32005 ContentTypeNotSupportedError
- -32006 MessageExceptionError
"""
from __future__ import annotations

from typing import Any, Optional


class A2AError(Exception):
    """A2A 标准错误基类。"""

    def __init__(self, message: str, code: int = -32603, data: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


class TaskNotFoundError(A2AError):
    """任务不存在。"""

    def __init__(self, message: str = "Task not found") -> None:
        super().__init__(message, code=-32001)


class TaskNotCancelableError(A2AError):
    """任务不可取消（已是终态）。"""

    def __init__(self, message: str = "Task is not cancelable") -> None:
        super().__init__(message, code=-32002)


class PushNotificationNotSupportedError(A2AError):
    """未声明 pushNotifications 能力。"""

    def __init__(self, message: str = "Push notifications not supported") -> None:
        super().__init__(message, code=-32003)


class UnsupportedOperationError(A2AError):
    """不支持的操作（如 A2A-Version 不匹配、不支持扩展卡片）。"""

    def __init__(self, message: str = "Unsupported operation") -> None:
        super().__init__(message, code=-32004)


class ContentTypeNotSupportedError(A2AError):
    """不支持的 Content-Type。"""

    def __init__(self, message: str = "Content type not supported") -> None:
        super().__init__(message, code=-32005)


class MessageExceptionError(A2AError):
    """消息异常（携带 Message 到 data.message）。"""

    def __init__(self, message: str = "Message exception", data: Optional[dict] = None) -> None:
        super().__init__(message, code=-32006, data=data)


def to_jsonrpc_error(exc: A2AError, req_id: Any = None) -> dict:
    """将 A2AError 转为 JSON-RPC 错误响应体。"""
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        error["data"] = exc.data
    return {"jsonrpc": "2.0", "error": error, "id": req_id}
