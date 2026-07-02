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
"""

import contextvars
import threading
from typing import Optional

current_cancel_event: contextvars.ContextVar[Optional[threading.Event]] = (
    contextvars.ContextVar("cancel_event", default=None)
)
