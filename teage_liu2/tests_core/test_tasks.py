"""TaskRegistry 专项测试(E4,计划 §4.2)。

核心提供最小后台任务注册:setup 注册,shutdown 统一取消(~30 行);
枝干后台任务经此编排,关闭不泄漏。
"""

from __future__ import annotations

import asyncio

from teage_liu2.core.tasks import TaskRegistry


def test_create_and_cancel_all():
    """验收:注册任务 → cancel_all 统一取消。"""
    async def main():
        tr = TaskRegistry()
        cancelled = []

        async def sleeper(tag):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(tag)
                raise

        t1 = tr.create_task(sleeper("t1"))
        t2 = tr.create_task(sleeper("t2"))
        assert tr.count == 2
        await asyncio.sleep(0)  # 让任务启动进入 sleep(30)(真实场景:运行中取消)

        tr.cancel_all()
        assert tr.count == 0
        # 等待取消传播到任务(任务内 except CancelledError 执行)
        await asyncio.gather(t1, t2, return_exceptions=True)
        assert sorted(cancelled) == ["t1", "t2"]
        assert t1.cancelled() and t2.cancelled()

    asyncio.run(main())


def test_completed_task_auto_removed():
    """验收:已完成任务自动移出注册表。"""
    async def main():
        tr = TaskRegistry()
        tr.create_task(asyncio.sleep(0))
        await asyncio.sleep(0.05)
        assert tr.count == 0

    asyncio.run(main())


def test_cancel_all_idempotent():
    """验收:cancel_all 可多次调用(幂等)。"""
    async def main():
        tr = TaskRegistry()
        tr.create_task(asyncio.sleep(30))
        tr.cancel_all()
        tr.cancel_all()  # 幂等,不崩
        assert tr.count == 0

    asyncio.run(main())
