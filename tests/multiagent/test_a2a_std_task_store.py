"""A2A TaskStore / TaskEventHub 测试。"""
from __future__ import annotations

import asyncio

import pytest

from teage_liu.multiagent.a2a_std.task_store import (
    A2ATaskRecord,
    TaskEventHub,
    TaskStore,
)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestTaskStore:
    """持久化存储。"""

    def test_upsert_get_roundtrip(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        rec = A2ATaskRecord(
            task_id="t_abc", context_id="c1", state="submitted",
            created_at="t1", updated_at="t2",
        )
        _run(store.upsert(rec))
        got = _run(store.get("t_abc"))
        assert got is not None
        assert got.task_id == "t_abc"
        assert got.context_id == "c1"
        assert got.state == "submitted"

    def test_persistence_across_restart(self, tmp_path):
        """重启存活：重新实例化 TaskStore 后任务仍在。"""
        path = tmp_path / "tasks.json"
        _run(TaskStore(path).upsert(A2ATaskRecord(
            task_id="t_1", context_id="c1", state="working",
            created_at="t", updated_at="t", history=[{"role": "user"}],
            artifacts=[{"name": "a1"}], metadata={"k": "v"},
        )))
        store2 = TaskStore(path)
        got = _run(store2.get("t_1"))
        assert got.state == "working"
        assert got.history == [{"role": "user"}]
        assert got.artifacts == [{"name": "a1"}]
        assert got.metadata == {"k": "v"}

    def test_list_filter_by_context(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        _run(store.upsert(A2ATaskRecord(task_id="t_a", context_id="c1", state="submitted", created_at="t", updated_at="t")))
        _run(store.upsert(A2ATaskRecord(task_id="t_b", context_id="c2", state="submitted", created_at="t", updated_at="t")))
        assert [r.task_id for r in _run(store.list("c1"))] == ["t_a"]
        assert len(_run(store.list())) == 2

    def test_delete(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        _run(store.upsert(A2ATaskRecord(task_id="t_x", context_id=None, state="submitted", created_at="t", updated_at="t")))
        assert _run(store.delete("t_x")) is True
        assert _run(store.get("t_x")) is None
        assert _run(store.delete("t_x")) is False

    def test_concurrent_upserts(self, tmp_path):
        """并发 upsert 无丢失（FileLock 保护）。"""
        store = TaskStore(tmp_path / "tasks.json")

        async def scenario():
            await asyncio.gather(*[
                store.upsert(A2ATaskRecord(
                    task_id=f"t_{i}", context_id=None, state="submitted",
                    created_at="t", updated_at="t",
                ))
                for i in range(20)
            ])
            return len(await store.list())

        assert _run(scenario()) == 20


class TestTaskEventHub:
    """事件集线器。"""

    def test_publish_subscribe(self):
        hub = TaskEventHub()

        async def scenario():
            q = await hub.subscribe("t_1")
            await hub.publish("t_1", {"method": "TaskStatusUpdateEvent", "params": {}, "id": "t_1"})
            event = await asyncio.wait_for(q.get(), timeout=1.0)
            hub.unsubscribe("t_1", q)
            return event

        assert _run(scenario())["method"] == "TaskStatusUpdateEvent"

    def test_replay_after_event_id(self):
        hub = TaskEventHub()

        async def scenario():
            for i in range(5):
                await hub.publish("t_1", {"id": f"t_1:{i}", "params": {"seq": i}, "method": "e"})
            events = hub.replay("t_1", after_event_id="t_1:2")
            return [e["params"]["seq"] for e in events]

        assert _run(scenario()) == [3, 4]

    def test_replay_ring(self):
        hub = TaskEventHub()

        async def scenario():
            await hub.publish("t_1", {"id": "t_1", "params": {"n": 1}, "method": "e"})
            return [e["params"]["n"] for e in hub.replay("t_1")]

        assert _run(scenario()) == [1]

    def test_clear(self):
        hub = TaskEventHub()

        async def scenario():
            await hub.publish("t_1", {"id": "t_1", "params": {}, "method": "e"})
            hub.clear("t_1")
            return hub.replay("t_1")

        assert _run(scenario()) == []
