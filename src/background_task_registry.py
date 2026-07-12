# src/background_task_registry.py
"""后台 asyncio task 注册表，支持按名取消和重启。

用于解决 reload/软重启后台 task 持有旧组件引用的问题。
spec 2026-07-13 阶段 2：新增。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class BackgroundTaskRegistry:
    """后台 asyncio task 注册表，支持按名取消和重启。"""

    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}

    def register(self, name: str, task: asyncio.Task) -> None:
        """注册后台 task。若同名 task 已存在，先取消旧的。"""
        old = self._tasks.get(name)
        if old is not None and not old.done():
            old.cancel()
        self._tasks[name] = task

    def get(self, name: str) -> Optional[asyncio.Task]:
        return self._tasks.get(name)

    async def restart(self, name: str, factory: Callable) -> None:
        """重启指定 task：取消旧的，用 factory 创建新的。

        factory 是无参 callable，返回 coroutine。
        """
        old = self._tasks.get(name)
        if old is not None and not old.done():
            old.cancel()
            try:
                await asyncio.wait_for(old, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        new_task = asyncio.create_task(factory())
        self._tasks[name] = new_task
        logger.info("后台 task '%s' 已重启", name)

    async def cancel_all(self) -> None:
        """取消所有后台 task（用于关闭）。"""
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        self._tasks.clear()
