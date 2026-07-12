# tests/test_background_task_registry.py
"""测试 BackgroundTaskRegistry：后台 task 注册/取消/重启。

spec 2026-07-13 阶段 2：新增 BackgroundTaskRegistry 解决 reload/软重启后
后台 task 持有旧组件引用的问题。
"""
from __future__ import annotations
import sys
import os
import asyncio
import pytest

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from background_task_registry import BackgroundTaskRegistry


@pytest.mark.asyncio
class TestBackgroundTaskRegistry:

    async def test_register_and_get(self):
        """注册 task 后可通过名字获取。"""
        registry = BackgroundTaskRegistry()

        async def dummy():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy())
        registry.register("test", task)
        assert registry.get("test") is task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_register_replaces_existing(self):
        """注册同名 task 时取消旧的。"""
        registry = BackgroundTaskRegistry()

        async def dummy():
            await asyncio.sleep(100)

        old_task = asyncio.create_task(dummy())
        registry.register("test", old_task)

        new_task = asyncio.create_task(dummy())
        registry.register("test", new_task)

        # register() 同步调用 old.cancel()，asyncio 取消是惰性的，
        # 需让出一次事件循环使 CancelledError 传播后再断言 cancelled()。
        await asyncio.sleep(0)

        assert registry.get("test") is new_task
        assert old_task.cancelled()
        new_task.cancel()
        try:
            await new_task
        except asyncio.CancelledError:
            pass

    async def test_restart_with_factory(self):
        """restart 用 factory 创建新 task，取消旧的。"""
        registry = BackgroundTaskRegistry()

        call_count = {"n": 0}

        async def factory():
            call_count["n"] += 1

        old_task = asyncio.create_task(asyncio.sleep(100))
        registry.register("test", old_task)

        await registry.restart("test", factory)

        new_task = registry.get("test")
        assert new_task is not old_task
        assert old_task.cancelled()
        await new_task  # 等 factory 完成
        assert call_count["n"] == 1

    async def test_cancel_all(self):
        """cancel_all 取消所有注册的 task。"""
        registry = BackgroundTaskRegistry()

        t1 = asyncio.create_task(asyncio.sleep(100))
        t2 = asyncio.create_task(asyncio.sleep(100))
        registry.register("t1", t1)
        registry.register("t2", t2)

        await registry.cancel_all()

        assert t1.cancelled()
        assert t2.cancelled()
        assert registry.get("t1") is None or registry.get("t1").cancelled()

    async def test_restart_nonexistent(self):
        """restart 不存在的 task 时正常创建新的。"""
        registry = BackgroundTaskRegistry()

        async def factory():
            pass

        await registry.restart("new", factory)
        assert registry.get("new") is not None
