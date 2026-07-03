"""流式请求中断管理器。

为每个活跃的 SSE 流维护一个 threading.Event，支持：
- 即时中断（immediate）：直接 event.set()
- 优雅中断（graceful）：暂存用户消息，等待断点到达后再 event.set()

threading.Event 可在 async 与 sync 代码中通用检测，
解决调用链中 async（server→orchestrator→react_loop）与
sync（react_loop→client→SDK）混合的中断信号传播问题。
"""

from __future__ import annotations

import logging
import threading
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class StreamCancelled(Exception):
    """用户中断了流式 LLM 调用时抛出的异常。

    被 @retry_on_failure 装饰器捕获时不会重试（_is_retryable 中检查），
    区别于网络错误/限流等可重试异常。
    """
    pass


class StreamManager:
    """管理 per-session 的取消信号（threading.Event）。

    线程安全：内部使用 threading.Lock 保护 dict 并发访问。
    cancel() 幂等：多次调用同一个 session 的 cancel 无副作用。

    使用方式：
        cancel_event = stream_manager.register(session_id)
        try:
            # 流式生成，各层检测 cancel_event.is_set()
            ...
        finally:
            stream_manager.unregister(session_id)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        # graceful 模式下暂存的新消息：session_id -> new_message
        self._graceful_pending: dict[str, str] = {}
        # per-session 的主动取消回调（async callable，指向 await stream.close()）
        # 与 _events 平行，受同一个 _lock 保护。
        # 用途：/chat/cancel immediate 模式主动断开 LLM HTTP 连接，
        # 不必等下一个 token 到达才检测 cancel_event。
        self._cancel_callbacks: dict[str, Optional[Awaitable]] = {}

    # ── 基础操作 ──

    def register(self, session_id: str) -> threading.Event:
        """注册一个新会话的取消事件。

        若 session 已存在（用户发新消息覆盖旧流），自动取消旧 event
        以防止旧生成器和新生成器并发执行。
        """
        event = threading.Event()
        with self._lock:
            existing = self._events.get(session_id)
            if existing is not None:
                existing.set()
                # 清理旧 graceful 暂存消息
                self._graceful_pending.pop(session_id, None)
                # 清理旧流的 cancel_callback（防止旧流 callback 误刷新流）
                self._cancel_callbacks.pop(session_id, None)
                logger.info(
                    "检测到活跃会话 %s，已自动取消旧流", session_id
                )
            self._events[session_id] = event
        return event

    def cancel(self, session_id: str) -> bool:
        """触发会话的即时中断。

        幂等：event.set() 多次调用无副作用。
        返回 True 表示 session 存在且成功设置，False 表示 session 不存在。
        """
        with self._lock:
            event = self._events.get(session_id)
        if event is None:
            logger.debug("StreamManager cancel: session %s 不存在（可能已结束）", session_id)
            return False
        event.set()
        logger.info("StreamManager cancel: %s", session_id)
        return True

    def unregister(self, session_id: str) -> None:
        """移除会话（无所有权检查，向后兼容）。

        安全地多次调用（幂等）。新代码应优先使用 :meth:`unregister_event`。
        """
        with self._lock:
            self._events.pop(session_id, None)
            self._graceful_pending.pop(session_id, None)
            self._cancel_callbacks.pop(session_id, None)
        logger.debug("StreamManager unregister: %s", session_id)

    def unregister_event(
        self, session_id: str, cancel_event: threading.Event
    ) -> None:
        """仅当存储的 event 与调用者传入的相同时才移除。

        防止旧生成器的 finally 块误删新生成器的 event。新代码必须使用此方法。

        参数:
            session_id: 会话 ID。
            cancel_event: 调用者持有的 event 对象。
        """
        with self._lock:
            stored = self._events.get(session_id)
            if stored is cancel_event:
                self._events.pop(session_id, None)
                self._graceful_pending.pop(session_id, None)
                # 同步清理 cancel_callback 槽位（防止旧流 callback 误刷新流）
                self._cancel_callbacks.pop(session_id, None)
                logger.debug("StreamManager unregister_event: %s (owned)", session_id)
            else:
                logger.debug(
                    "StreamManager unregister_event: %s (skipped, ownership mismatch)",
                    session_id,
                )

    def is_cancelled(self, session_id: str) -> bool:
        """检查会话是否已被取消。线程安全。"""
        with self._lock:
            event = self._events.get(session_id)
        if event is None:
            return False
        return event.is_set()

    # ── Graceful 模式 ──

    def register_graceful(self, session_id: str, new_message: str) -> None:
        """标记为优雅中断模式，暂存用户新消息。

        注意：此时不设置 cancel_event。调用方（async_event_generator）
        应检测 is_graceful_pending()，等 BreakpointDetector 判定到达断点
        后再调用 cancel()。
        """
        with self._lock:
            self._graceful_pending[session_id] = new_message
        logger.info("StreamManager graceful pending: %s (msg_len=%d)",
                     session_id, len(new_message))

    # ── 两段式取消 ──

    def force_cancel(self, session_id: str):
        """两段式取消。仅在 graceful 已 pending 时触发强杀。

        返回:
            ``(status, message)`` 二元组：
            - status: ``"force_killed"``（强杀）或 ``"cancelling"``（普通取消）
            - message: graceful 模式下暂存的用户新消息（没有时返回 None）
        """
        with self._lock:
            was_graceful = session_id in self._graceful_pending
            msg = self._graceful_pending.pop(session_id, None)
            if was_graceful:
                event = self._events.get(session_id)
                if event:
                    event.set()
                return ("force_killed", msg)
            # 无 graceful pending → 当作即时取消
            event = self._events.get(session_id)
            if event:
                event.set()
            return ("cancelling", None)

    def is_graceful_pending(self, session_id: str) -> bool:
        """检查是否有待处理的优雅中断。"""
        with self._lock:
            return session_id in self._graceful_pending

    def pop_graceful_message(self, session_id: str) -> Optional[str]:
        """取出并清除暂存的优雅中断消息。

        在触发 cancel() 后调用此方法获取用户的新消息。
        """
        with self._lock:
            return self._graceful_pending.pop(session_id, None)

    # ── 主动流取消（cancel_callback）──
    #
    # 与 cancel_event（threading.Event）平行的中断机制：
    # - cancel_event 用于同步工具 handler 中断（_cancel_context ContextVar 机制不变）
    # - cancel_callback 用于 /chat/cancel immediate 模式主动断开 LLM HTTP 连接，
    #   不必等下一个 token 到达才检测 cancel_event
    # backend 启动流时注册 stream.close（async callable），流结束时清理。

    async def set_cancel_callback(
        self, session_id: str, callback: Optional[Callable]
    ) -> None:
        """注册或清理 per-session 的主动取消回调。

        backend 启动 LLM 流时注册 ``stream.close``（async callable，调用返回
        coroutine），流结束后传 ``None`` 清理。

        线程安全：字典读写受 ``_lock`` 保护。
        若 session_id 已有旧 callback，直接覆盖（旧流可能已结束）。

        参数:
            session_id: 会话 ID。
            callback: async callable（调用返回 coroutine），或 None 表示清理。
        """
        with self._lock:
            if callback is None:
                self._cancel_callbacks.pop(session_id, None)
            else:
                # 覆盖旧 callback（旧流可能已结束）
                self._cancel_callbacks[session_id] = callback
        logger.debug(
            "StreamManager set_cancel_callback: %s (%s)",
            session_id,
            "cleared" if callback is None else "set",
        )

    async def trigger_cancel(self, session_id: str) -> bool:
        """触发 per-session 的主动取消回调。

        从 ``_cancel_callbacks`` 取出 callback，在锁外 ``await callback()``
        立即关闭 LLM HTTP 连接（不等下一个 token）。

        线程安全：取 callback 时用 ``_lock`` 保护，``await`` 在锁外执行
        （避免持锁 await 死锁）。await 后清理槽位。

        参数:
            session_id: 会话 ID。

        返回:
            True 表示 callback 已注册并被调用；
            False 表示未注册（流未启动或已结束），调用方应降级为
            仅 ``cancel_event.set()`` 的兜底路径。
        """
        with self._lock:
            callback = self._cancel_callbacks.get(session_id)
        if callback is None:
            logger.debug(
                "StreamManager trigger_cancel: %s 无 callback（未注册或已清理）",
                session_id,
            )
            return False
        # 在锁外 await，避免持锁 await 死锁
        try:
            await callback()
        finally:
            with self._lock:
                self._cancel_callbacks[session_id] = None
        logger.info(
            "StreamManager trigger_cancel: %s (callback 已调用)", session_id
        )
        return True
