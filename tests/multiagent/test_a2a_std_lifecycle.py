"""标准 A2A 完整生命周期测试（含 TaskDriver 映射 + SSE 流式）。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teage_liu.multiagent.a2a_std.engine_adapter import A2AEngineAdapter
from teage_liu.multiagent.a2a_std.models import Message, TextPart
from teage_liu.multiagent.a2a_std.router import create_a2a_std_router
from teage_liu.multiagent.a2a_std.task_manager import A2ATaskManager
from teage_liu.multiagent.a2a_std.task_store import TaskEventHub, TaskStore
from teage_liu.multiagent.blackboard import append_collab_message, read_collab_messages


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _config(bb_root: Path) -> dict:
    return {
        "a2a": {
            "enabled": True,
            "standard": {
                "enabled": True,
                "name": "teagent-lu",
                "capabilities": {"streaming": True, "push_notifications": False},
                "task_store_path": str(bb_root / "tasks" / "a2a" / "tasks.json"),
            },
        },
        "multiagent": {
            "role": "worker",
            "worker": {"agent_id": "teagent-lu"},
            "blackboard_dir": str(bb_root),
        },
        "security": {"api_key": ""},
        "server": {"port": 8000},
    }


def _std_message(text: str, meta: dict | None = None) -> Message:
    return Message(
        role="user",
        message_id=f"m_{abs(hash(text)) & 0xffff}",
        parts=[TextPart(text=text)],
        metadata=meta or {},
    )


class TestLifecycle:
    """submitted → working → (input-required) → completed，含 artifacts。"""

    def test_full_lifecycle_completed(self, tmp_path):
        bb_root = tmp_path / "bb"
        bb_root.mkdir(exist_ok=True)

        async def scenario():
            store = TaskStore(_config(bb_root)["a2a"]["standard"]["task_store_path"])
            hub = TaskEventHub()
            manager = A2ATaskManager(store, hub)
            adapter = A2AEngineAdapter(bb_root, _config(bb_root), manager)

            # 1. message/send → submitted → working
            task_wire = await adapter.handle_message_send(_std_message("查资料"), None)
            task_id = task_wire["id"]
            context_id = task_wire["contextId"]
            assert task_wire["status"]["state"] == "working"

            # 2. 引擎侧（worker）写入 response → driver 推进 artifacts + completed
            await append_collab_message(bb_root, {
                "from": "teagent-lu", "to": "*", "type": "response",
                "content": "查到资料了", "message_id": "resp_1",
            }, collab_id=context_id)
            await append_collab_message(bb_root, {
                "from": "director", "to": "*", "type": "consensus",
                "content": "结束", "message_id": "end_1",
            }, collab_id=context_id)

            # 3. driver 扫描
            await adapter.tick_once()

            task = await manager.get_task(task_id)
            assert task.status.state.value == "completed"
            assert len(task.artifacts) >= 1
            assert task.artifacts[0].name.startswith("response_")
            assert task.artifacts[0].parts[0].text == "查到资料了"
            return task

        task = _run(scenario())
        assert task.status.state.value == "completed"

    def test_failed_on_error_message(self, tmp_path):
        bb_root = tmp_path / "bb"
        bb_root.mkdir(exist_ok=True)

        async def scenario():
            store = TaskStore(_config(bb_root)["a2a"]["standard"]["task_store_path"])
            hub = TaskEventHub()
            manager = A2ATaskManager(store, hub)
            adapter = A2AEngineAdapter(bb_root, _config(bb_root), manager)
            task_wire = await adapter.handle_message_send(_std_message("干活"), "c_err")
            await append_collab_message(bb_root, {
                "from": "teagent-lu", "to": "*", "type": "error",
                "content": "失败了", "message_id": "err_1",
            }, collab_id="c_err")
            await adapter.tick_once()
            return await manager.get_task(task_wire["id"])

        task = _run(scenario())
        assert task.status.state.value == "failed"

    def test_input_required_then_completed(self, tmp_path):
        bb_root = tmp_path / "bb"
        bb_root.mkdir(exist_ok=True)

        async def scenario():
            store = TaskStore(_config(bb_root)["a2a"]["standard"]["task_store_path"])
            hub = TaskEventHub()
            manager = A2ATaskManager(store, hub)
            adapter = A2AEngineAdapter(bb_root, _config(bb_root), manager)
            task_wire = await adapter.handle_message_send(_std_message("审批"), "c_ir")
            cid = task_wire["contextId"]

            # worker 需要更多输入
            await append_collab_message(bb_root, {
                "from": "teagent-lu", "to": "*", "type": "status",
                "status": "input_required", "content": "需要更多信息",
                "message_id": "ir_1",
            }, collab_id=cid)
            await adapter.tick_once()
            task = await manager.get_task(task_wire["id"])
            assert task.status.state == "input-required"

            # 输入补充后完成
            await append_collab_message(bb_root, {
                "from": "teagent-lu", "to": "*", "type": "response",
                "content": "完成", "message_id": "resp_2",
            }, collab_id=cid)
            await append_collab_message(bb_root, {
                "from": "director", "to": "*", "type": "end",
                "content": "end", "message_id": "end_2",
            }, collab_id=cid)
            await adapter.tick_once()
            return await manager.get_task(task_wire["id"])

        task = _run(scenario())
        assert task.status.state.value == "completed"

    def test_terminal_immutable(self, tmp_path):
        bb_root = tmp_path / "bb"
        bb_root.mkdir(exist_ok=True)

        async def scenario():
            store = TaskStore(_config(bb_root)["a2a"]["standard"]["task_store_path"])
            hub = TaskEventHub()
            manager = A2ATaskManager(store, hub)
            adapter = A2AEngineAdapter(bb_root, _config(bb_root), manager)
            task_wire = await adapter.handle_message_send(_std_message("x"), "c_t")
            task_id = task_wire["id"]
            await manager.cancel_task(task_id)
            # 终态不可再变
            try:
                await manager.update_state(task_id, "working")  # type: ignore[arg-type]
                raised = False
            except Exception:
                raised = True
            return raised

        assert _run(scenario()) is True


class TestStreaming:
    """SSE 流式测试（真实 uvicorn HTTP 服务）。

    注：本环境 starlette 1.3.1 + httpx 0.28 的 ASGITransport 对长驻
    StreamingResponse 交付挂起（ASGI 2.0 collapsing task group 路径），
    故 SSE 端到端测试起真实 uvicorn（线程内），与生产路径一致。
    """

    def _serve(self, tmp_path):
        import socket
        import threading
        import time

        import uvicorn

        bb_root = tmp_path / "bb"
        bb_root.mkdir(exist_ok=True)
        config = _config(bb_root)
        app = FastAPI()
        router = create_a2a_std_router(bb_root, config)
        app.include_router(router)

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning",
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "uvicorn 未启动"
        return server, thread, port, router

    @staticmethod
    def _shutdown(server, thread):
        server.should_exit = True
        thread.join(timeout=10)

    def test_live_sse_stream_with_real_http(self, tmp_path):
        """真实 HTTP 上的 message/stream：帧结构正确、终态事件必发、流关闭。"""
        import httpx

        server, thread, port, router = self._serve(tmp_path)
        adapter = router.a2a_std_engine_adapter
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                payload = {
                    "jsonrpc": "2.0", "method": "message/stream",
                    "params": {"message": _std_message("流式任务").model_dump(by_alias=True)},
                    "id": 7,
                }
                with client.stream("POST", "/a2a/std/jsonrpc", json=payload,
                                   headers={"Accept": "text/event-stream"}) as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    lines = resp.iter_lines()
                    # 首帧：id: <taskId:seq> / event: / data:
                    first = next(lines)
                    assert first.startswith("id: t_")
                    event_id = first.split("id: ", 1)[1]
                    assert ":" in event_id
                    task_id = event_id.split(":", 1)[0]
                    assert next(lines).startswith("event: TaskStatusUpdateEvent")

                    # 流打开期间并发推进引擎（同进程直连，跨线程仅文件/轮询，无事件循环依赖）
                    async def finish():
                        tasks = await router.a2a_std_task_manager.list_tasks()
                        target = next(t for t in tasks if t.id == task_id)
                        await append_collab_message(
                            adapter._bb_root,
                            {"from": "teagent-lu", "to": "*", "type": "response",
                             "content": "完成", "message_id": "fin_1"},
                            collab_id=target.context_id,
                        )
                        await append_collab_message(
                            adapter._bb_root,
                            {"from": "director", "to": "*", "type": "end",
                             "content": "end", "message_id": "fin_2"},
                            collab_id=target.context_id,
                        )
                        await adapter.tick_once()
                    _run(finish())

                    # 等待终态事件 + 流关闭（驱动循环轮询 ≤5s 兜底）
                    rest = list(lines)
                    body = "\n".join(rest)
                    assert "TaskStatusUpdateEvent" in body
                    assert '"state":"completed"' in body or '"state": "completed"' in body
                    assert '"jsonrpc":"2.0"' in body or '"jsonrpc": "2.0"' in body
        finally:
            self._shutdown(server, thread)

    def test_resubscribe_with_last_event_id(self, tmp_path):
        """tasks/resubscribe：Last-Event-ID 之后的事件才重放。"""
        import httpx

        server, thread, port, router = self._serve(tmp_path)
        adapter = router.a2a_std_engine_adapter
        try:
            async def prepare():
                wire = await adapter.handle_message_send(_std_message("重放任务"), "c_rs")
                await append_collab_message(
                    adapter._bb_root,
                    {"from": "director", "to": "*", "type": "end",
                     "content": "end", "message_id": "rs_end"},
                    collab_id="c_rs",
                )
                await adapter.tick_once()
                return wire["id"]
            task_id = _run(prepare())

            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                with client.stream(
                    "POST", "/a2a/std/jsonrpc",
                    json={"jsonrpc": "2.0", "method": "tasks/resubscribe",
                          "params": {"id": task_id}, "id": 8},
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    full = "".join(resp.iter_text())
                with client.stream(
                    "POST", "/a2a/std/jsonrpc",
                    json={"jsonrpc": "2.0", "method": "tasks/resubscribe",
                          "params": {"id": task_id}, "id": 9},
                    headers={"Accept": "text/event-stream",
                             "Last-Event-ID": f"{task_id}:1"},
                ) as resp:
                    partial = "".join(resp.iter_text())

            assert f"id: {task_id}:1" in full
            assert f"id: {task_id}:1" not in partial  # 断点后的首个事件被跳过
            assert '"state":"completed"' in partial or '"state": "completed"' in partial
        finally:
            self._shutdown(server, thread)

    def test_message_stream_without_sse_accept_returns_task(self, tmp_path):
        server, thread, port, router = self._serve(tmp_path)
        try:
            import httpx
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                resp = client.post("/a2a/std/jsonrpc", json={
                    "jsonrpc": "2.0", "method": "message/stream",
                    "params": {"message": _std_message("x").model_dump(by_alias=True)},
                    "id": 9,
                })
            body = resp.json()
            assert "result" in body
            assert body["result"]["id"].startswith("t_")
        finally:
            self._shutdown(server, thread)


class TestLiveStreamDelivery:
    """get_stream 实时投递（不经过 HTTP，覆盖 hub→generator 路径）。"""

    def test_live_events_delivered_until_terminal(self, tmp_path):
        bb_root = tmp_path / "bb"
        bb_root.mkdir(exist_ok=True)

        async def scenario():
            store = TaskStore(_config(bb_root)["a2a"]["standard"]["task_store_path"])
            hub = TaskEventHub()
            manager = A2ATaskManager(store, hub)
            adapter = A2AEngineAdapter(bb_root, _config(bb_root), manager)
            wire = await adapter.handle_message_send(_std_message("live"), "c_live")
            tid = wire["id"]

            events = []
            stream_task = asyncio.create_task(_collect(manager, tid, events))

            # 流打开后推进到终态
            await asyncio.sleep(0.05)
            await append_collab_message(
                adapter._bb_root,
                {"from": "director", "to": "*", "type": "end",
                 "content": "end", "message_id": "live_end"},
                collab_id="c_live",
            )
            await adapter.tick_once()
            await asyncio.wait_for(stream_task, timeout=5.0)
            return events

        async def _collect(manager, tid, events):
            async for evt in manager.get_stream(tid):
                events.append(evt)

        events = _run(scenario())
        methods = [e["method"] for e in events]
        assert "TaskStatusUpdateEvent" in methods
        # 终态事件必发且流关闭（循环正常结束）
        last = events[-1]
        assert last["params"]["status"]["state"] == "completed"

    def test_resubscribe_replays_after_disconnect(self, tmp_path):
        """断线后 resubscribe：Last-Event-ID 之后的事件被重放。"""
        bb_root = tmp_path / "bb"
        bb_root.mkdir(exist_ok=True)

        async def scenario():
            store = TaskStore(_config(bb_root)["a2a"]["standard"]["task_store_path"])
            hub = TaskEventHub()
            manager = A2ATaskManager(store, hub)
            adapter = A2AEngineAdapter(bb_root, _config(bb_root), manager)
            wire = await adapter.handle_message_send(_std_message("rs"), "c_rs2")
            tid = wire["id"]
            await append_collab_message(
                adapter._bb_root,
                {"from": "director", "to": "*", "type": "end",
                 "content": "end", "message_id": "rs2_end"},
                collab_id="c_rs2",
            )
            await adapter.tick_once()

            # 全量重放
            all_events = [e async for e in manager.get_stream(tid)]
            # 断点重放（跳过 t_xxx:1）
            after = [e async for e in manager.get_stream(tid, after_event_id=f"{tid}:1")]
            return all_events, after

        all_events, after = _run(scenario())
        assert len(after) < len(all_events)
        assert all(e["id"] != f"{all_events[0]['id']}" for e in after)  # 首个事件被跳过
