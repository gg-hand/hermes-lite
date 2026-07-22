"""Shared ContextVar for propagating cancel_event into synchronous tool handlers.

Usage::

    from ._cancel_context import current_cancel_event

    # Set before tool dispatch:
    token = current_cancel_event.set(cancel_event)
    try:
        handler(...)
    finally:
        current_cancel_event.reset(token)

    # Read inside tool handler:
    cancel_event = current_cancel_event.get()
    if cancel_event and cancel_event.is_set():
        return "[已被用户中断]"

ContextVar 仅在**同一线程**内有效。当前所有工具均在 ReactLoop 主线程
同步执行，因此工作正确。

规范 9.2 Task 0b: 新增 ``current_session_id`` ContextVar，用于向工具 handler
（如 profile_update）透传当前会话 ID，实现 per-session 频次限制。
"""

import contextvars
import threading
from typing import Optional

current_cancel_event: contextvars.ContextVar[Optional[threading.Event]] = (
    contextvars.ContextVar("cancel_event", default=None)
)

# 规范 9.2 Task 0b: 当前会话 ID ContextVar
# 在 _execute_tool_with_dispatch 入口 set，出口 reset，
# profile_update handler 通过此 ContextVar 获取 session_id 实现 per-session 频次限制。
current_session_id: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("session_id", default=None)
)
