"""A2A Task 生命周期管理器。

标准生命周期：submitted → working；中断（非终态）：input-required / auth-required；
终态：completed / failed / canceled / rejected（终态不可重启）。
任何状态变更路径必须发终态事件，否则调用方永远等待。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import AsyncIterator, Callable, Optional

from teage_liu.multiagent.a2a_std.exceptions import (
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from teage_liu.multiagent.a2a_std.models import (
    Artifact,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from teage_liu.multiagent.a2a_std.task_store import A2ATaskRecord, TaskEventHub, TaskStore, _now_iso

logger = logging.getLogger(__name__)

# 默认 task id 生成器：服务端生成，t_ 前缀与内部 op_id / collab_id 命名空间分离
def _default_id_gen() -> str:
    return f"t_{uuid.uuid4().hex[:12]}"


class A2ATaskManager:
    """标准 A2A Task 生命周期管理（引擎无关，纯 task 存储/事件层）。"""

    def __init__(
        self,
        store: TaskStore,
        event_hub: TaskEventHub,
        id_gen: Callable[[], str] = _default_id_gen,
        push_notifications_enabled: bool = False,
    ) -> None:
        self._store = store
        self._hub = event_hub
        self._id_gen = id_gen
        self._push_enabled = push_notifications_enabled
        # 每任务事件序号：SSE 事件唯一 id（taskId:seq），Last-Event-ID 精确断点
        self._event_seq: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 任务生命周期
    # ------------------------------------------------------------------

    async def create_task(
        self, message: Message, context_id: Optional[str] = None
    ) -> Task:
        """创建任务（submitted）并发布状态事件。"""
        task_id = self._id_gen()
        now = _now_iso()
        record = A2ATaskRecord(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.SUBMITTED.value,
            created_at=now,
            updated_at=now,
            history=[message.model_dump(by_alias=True)],
        )
        await self._store.upsert(record)
        task = self._record_to_task(record)
        await self._publish_status(task, initial=True)
        logger.info("A2A Task 创建: %s (context=%s)", task_id, context_id)
        return task

    async def get_task(
        self, task_id: str, context_id: Optional[str] = None
    ) -> Task:
        """获取任务（校验 context 归属）。"""
        record = await self._store.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        if context_id is not None and record.context_id != context_id:
            raise TaskNotFoundError(f"Task not found in context: {task_id}")
        return self._record_to_task(record)

    async def list_tasks(
        self, context_id: Optional[str] = None, metadata_filter: Optional[dict] = None
    ) -> list[Task]:
        """列出任务（可选 contextId / metadata 过滤）。"""
        records = await self._store.list(context_id)
        tasks = [self._record_to_task(r) for r in records]
        if metadata_filter:
            tasks = [
                t for t in tasks
                if t.metadata and all(t.metadata.get(k) == v for k, v in metadata_filter.items())
            ]
        return tasks

    async def cancel_task(
        self, task_id: str, context_id: Optional[str] = None
    ) -> Task:
        """取消任务（仅非终态可取消）。"""
        task = await self.get_task(task_id, context_id)
        if task.status.state.is_terminal:
            raise TaskNotCancelableError(f"Task already in terminal state: {task.status.state.value}")
        return await self.update_state(task_id, TaskState.CANCELED)

    async def update_state(
        self,
        task_id: str,
        new_state: TaskState,
        *,
        message: Optional[Message] = None,
        artifacts: Optional[list[Artifact]] = None,
        metadata: Optional[dict] = None,
    ) -> Task:
        """推进任务状态——唯一写点：持久化 + 发布事件 + 状态转移守卫。"""
        record = await self._store.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        cur_state = TaskState(record.state)
        if cur_state.is_terminal and new_state != cur_state:
            raise TaskNotCancelableError(
                f"Terminal task cannot transition: {cur_state.value} -> {new_state.value}"
            )

        record.state = new_state.value
        record.updated_at = _now_iso()
        if message is not None:
            record.history.append(message.model_dump(by_alias=True))
        if artifacts is not None:
            record.artifacts = [a.model_dump(by_alias=True) for a in artifacts]
        if metadata is not None:
            record.metadata = {**(record.metadata or {}), **metadata}

        await self._store.upsert(record)
        task = self._record_to_task(record)

        await self._publish_status(task)
        if artifacts is not None:
            await self._publish_artifacts(task)
        return task

    # ------------------------------------------------------------------
    # 流式事件（message/stream、tasks/resubscribe）
    # ------------------------------------------------------------------

    async def get_stream(
        self,
        task_id: str,
        history_length: Optional[int] = None,
        after_event_id: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """事件流：先重放环形缓冲（可断点），再实时订阅，直至终态事件后自然结束。

        产出事件 dict（{method, params, id} 线格式）。
        """
        # 先校验任务存在
        task = await self.get_task(task_id)
        terminal_seen = False

        # 重放历史事件（若有；resubscribe 按 Last-Event-ID 断点）
        for event in self._hub.replay(task_id, after_event_id):
            yield event
            if event.get("method") == "TaskStatusUpdateEvent" and self._is_terminal_event(event):
                terminal_seen = True
                break

        if terminal_seen:
            return

        queue = await self._hub.subscribe(task_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # 空闲检查：任务是否已终态（如重启后状态在文件中已是终态）
                    current = await self.get_task(task_id)
                    if current.status.state.is_terminal:
                        # 补发一次终态事件，保证流必以终态事件收尾
                        terminal_event = self._make_status_event(current)
                        yield terminal_event
                        return
                    continue
                yield event
                if event.get("method") == "TaskStatusUpdateEvent" and self._is_terminal_event(event):
                    return
        finally:
            self._hub.unsubscribe(task_id, queue)

    # ------------------------------------------------------------------
    # Push 通知配置（未启用时返回 -32003）
    # ------------------------------------------------------------------

    async def set_push_config(
        self, task_id: str, config: TaskPushNotificationConfig
    ) -> Task:
        self._require_push_enabled()
        task = await self.get_task(task_id)
        metadata = {**(task.metadata or {}), "push_notification": config.model_dump(by_alias=True)}
        return await self.update_state(task_id, task.status.state, metadata=metadata)

    async def get_push_config(self, task_id: str) -> Optional[dict]:
        self._require_push_enabled()
        task = await self.get_task(task_id)
        return (task.metadata or {}).get("push_notification")

    async def list_push_configs(self, context_id: Optional[str] = None) -> list[dict]:
        self._require_push_enabled()
        tasks = await self.list_tasks(context_id)
        return [
            t.metadata["push_notification"]
            for t in tasks if t.metadata and "push_notification" in t.metadata
        ]

    async def delete_push_config(self, task_id: str) -> Task:
        self._require_push_enabled()
        task = await self.get_task(task_id)
        metadata = dict(task.metadata or {})
        metadata.pop("push_notification", None)
        return await self.update_state(task_id, task.status.state, metadata=metadata)

    def _require_push_enabled(self) -> None:
        if not self._push_enabled:
            raise PushNotificationNotSupportedError(
                "pushNotifications capability not declared; webhook push is disabled"
            )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _record_to_task(self, record: A2ATaskRecord) -> Task:
        return Task(
            id=record.task_id,
            context_id=record.context_id,
            status=TaskStatus(
                state=TaskState(record.state),
                timestamp=record.updated_at,
            ),
            artifacts=[Artifact.model_validate(a) for a in record.artifacts],
            history=[Message.model_validate(m) for m in record.history],
            metadata=dict(record.metadata or {}),
        )

    def _make_status_event(self, task: Task) -> dict:
        return TaskStatusUpdateEvent(
            id=task.id,
            status=task.status,
            artifacts=task.artifacts,
        ).model_dump(by_alias=True)

    def _make_artifacts_event(self, task: Task) -> dict:
        return TaskArtifactUpdateEvent(
            id=task.id, artifacts=task.artifacts
        ).model_dump(by_alias=True)

    def _next_event_id(self, task_id: str) -> str:
        """生成唯一事件 id（taskId:seq，供 SSE id:/Last-Event-ID）。"""
        seq = self._event_seq.get(task_id, 0) + 1
        self._event_seq[task_id] = seq
        return f"{task_id}:{seq}"

    async def _publish_status(self, task: Task, initial: bool = False) -> None:
        # 线格式：method/params/id（id 为唯一事件 id），SSE 帧直接取用
        event = {
            "method": "TaskStatusUpdateEvent",
            "params": self._make_status_event(task),
            "id": self._next_event_id(task.id),
        }
        await self._hub.publish(task.id, event)

    async def _publish_artifacts(self, task: Task) -> None:
        event = {
            "method": "TaskArtifactUpdateEvent",
            "params": self._make_artifacts_event(task),
            "id": self._next_event_id(task.id),
        }
        await self._hub.publish(task.id, event)

    @staticmethod
    def _is_terminal_event(event: dict) -> bool:
        params = event.get("params") or {}
        status = params.get("status") or {}
        try:
            return TaskState.parse(status.get("state", "")).is_terminal
        except ValueError:
            return False
