"""后台任务注册表(E4,计划 §4.2):core 提供的最小后台任务编排。

两类任务(§transport T-4):
- 宿主侧后台任务:枝干 setup 注册,shutdown 统一取消,关闭不泄漏(~30 行)
- 扩展侧登记任务:经 transport ``task_register`` 消息登记(任务归属扩展进程,
  宿主只登记/可观测/协调取消,不承载执行);扩展 teardown 自取消,
  宿主 shutdown 与热重载重建时 cancel_all 兜底
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Coroutine, Dict, List, Optional, Set


@dataclass
class RegisteredTask:
    """扩展侧登记任务(宿主只登记,任务归属扩展进程,T-4)。"""

    task_id: str
    description: str = ""
    owner: str = ""
    status: str = "registered"


class TaskRegistry:
    """后台任务注册表:create_task 注册,shutdown 统一取消(幂等)。"""

    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task] = set()
        #: 扩展侧登记任务表(task_id -> RegisteredTask)
        self._registered: Dict[str, RegisteredTask] = {}

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """注册并启动一个后台任务(完成后自动移出注册表)。"""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------
    # 扩展侧任务登记(§transport T-4:宿主只登记/协调取消,不承载执行)
    # ------------------------------------------------------------------
    def register_task(self, task_id: str, description: str = "", owner: str = "") -> None:
        """登记一个扩展进程内的后台任务(幂等:同 task_id 覆盖描述)。"""
        if not task_id or not isinstance(task_id, str):
            raise ValueError("task_id 必须是非空字符串")
        self._registered[task_id] = RegisteredTask(
            task_id=task_id, description=description, owner=owner
        )

    def cancel_task(self, task_id: str) -> bool:
        """标记取消一个登记任务(协调取消:实际执行终止由扩展进程负责)。"""
        task = self._registered.pop(task_id, None)
        if task is None:
            return False
        task.status = "cancelled"
        return True

    def list_registered(self) -> List[RegisteredTask]:
        """列出全部登记任务(可观测)。"""
        return list(self._registered.values())

    @property
    def registered_count(self) -> int:
        return len(self._registered)

    def cancel_all(self) -> None:
        """取消全部任务(幂等,可多次调用):

        ① 宿主侧后台任务(extensions teardown 前);② 登记任务清空
        (shutdown 与热重载重建时兜底,扩展 teardown 应自取消其任务)。
        """
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._registered.clear()

    @property
    def count(self) -> int:
        return len(self._tasks)
