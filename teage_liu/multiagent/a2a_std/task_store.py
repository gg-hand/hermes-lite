"""A2A Task 持久化存储 + 事件集线器。

- TaskStore：单 JSON 文件（{blackboard_dir}/tasks/a2a/tasks.json），
  复用 FileLock + atomic_write，**重启后任务存活**。
- TaskEventHub：每任务 asyncio.Queue 订阅（空闲 TTL 淘汰）+ 内存环形缓冲，
  供 tasks/resubscribe 重放错过的流式事件。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from teage_liu.multiagent.blackboard import atomic_write
from teage_liu.multiagent.file_lock import FileLock

logger = logging.getLogger(__name__)

# 事件环形缓冲上限（resubscribe 重放窗口）
_RING_MAX = 200
# 订阅空闲淘汰时间（秒）
_SUB_IDLE_TTL = 300.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class A2ATaskRecord:
    """Task 持久化记录（JSON 兼容）。"""

    task_id: str
    context_id: Optional[str]
    state: str  # TaskState value
    created_at: str
    updated_at: str
    history: list = field(default_factory=list)  # list[dict]（Message wire 形式）
    artifacts: list = field(default_factory=list)  # list[dict]（Artifact wire 形式）
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "context_id": self.context_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": self.history,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "A2ATaskRecord":
        return cls(
            task_id=d["task_id"],
            context_id=d.get("context_id"),
            state=d.get("state", "submitted"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            history=d.get("history", []),
            artifacts=d.get("artifacts", []),
            metadata=d.get("metadata", {}),
        )


class TaskStore:
    """Task 持久化存储（单 JSON 文件，FileLock 保护读-改-写）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._cache: Optional[dict[str, A2ATaskRecord]] = None

    async def _ensure_file(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            await atomic_write(self._path, "{}")

    async def _load(self) -> dict[str, A2ATaskRecord]:
        if self._cache is not None:
            return self._cache
        await self._ensure_file()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("A2A TaskStore 读取失败，重置为空: %s", e)
            raw = {}
        self._cache = {
            tid: A2ATaskRecord.from_dict(d)
            for tid, d in raw.items() if isinstance(d, dict)
        }
        return self._cache

    async def _persist(self) -> None:
        """持锁原子写入（读-改-写一体，调用方需已持有锁）。"""
        payload = {tid: rec.to_dict() for tid, rec in self._cache.items()}
        await atomic_write(self._path, json.dumps(payload, ensure_ascii=False, indent=2))

    async def upsert(self, record: A2ATaskRecord) -> None:
        async with FileLock(self._path):
            cache = await self._load()
            cache[record.task_id] = record
            await self._persist()

    async def get(self, task_id: str) -> Optional[A2ATaskRecord]:
        async with FileLock(self._path):
            cache = await self._load()
            rec = cache.get(task_id)
            if rec is not None:
                rec = A2ATaskRecord.from_dict(rec.to_dict())  # 防御性拷贝
            return rec

    async def list(self, context_id: Optional[str] = None) -> list[A2ATaskRecord]:
        async with FileLock(self._path):
            cache = await self._load()
            records = list(cache.values())
        if context_id is not None:
            records = [r for r in records if r.context_id == context_id]
        return records

    async def delete(self, task_id: str) -> bool:
        async with FileLock(self._path):
            cache = await self._load()
            removed = cache.pop(task_id, None) is not None
            if removed:
                await self._persist()
            return removed

    def reset_cache(self) -> None:
        """测试辅助：清空内存缓存强制重读。"""
        self._cache = None


class TaskEventHub:
    """Task 事件集线器：实时订阅 + 环形缓冲重放。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._ring: dict[str, list[dict]] = {}
        self._last_seen: dict[str, dict] = {}  # task_id -> {event_id -> ts} 用于 Last-Event-ID

    async def subscribe(self, task_id: str) -> asyncio.Queue:
        """订阅任务事件流，返回队列（消费方负责调用 unsubscribe）。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        subs = self._subs.get(task_id)
        if subs and queue in subs:
            subs.remove(queue)
        if not subs:
            self._subs.pop(task_id, None)

    async def publish(self, task_id: str, event: dict) -> None:
        """发布事件：投递所有订阅 + 写入环形缓冲。"""
        self._ring.setdefault(task_id, []).append(event)
        if len(self._ring[task_id]) > _RING_MAX:
            del self._ring[task_id][: len(self._ring[task_id]) - _RING_MAX]
        # 记录事件 id（供 Last-Event-ID 定位）
        evt_id = str(event.get("id", ""))
        if evt_id:
            self._last_seen[task_id] = {"event_id": evt_id, "ts": _now_iso()}
        for q in list(self._subs.get(task_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 消费方过慢：丢弃最旧一条
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def replay(self, task_id: str, after_event_id: Optional[str] = None) -> list[dict]:
        """重放环形缓冲中的事件（resubscribe 用）。"""
        events = list(self._ring.get(task_id, []))
        if not after_event_id:
            return events
        for i, evt in enumerate(events):
            if str(evt.get("id", "")) == after_event_id:
                return events[i + 1:]
        return events

    def clear(self, task_id: str) -> None:
        self._ring.pop(task_id, None)
        self._last_seen.pop(task_id, None)
        self._subs.pop(task_id, None)
