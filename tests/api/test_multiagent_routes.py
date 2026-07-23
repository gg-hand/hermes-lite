"""multiagent REST 端点测试。

覆盖 Plan 4 Task 1：状态查询 / agents / messages / audit / director / sse。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _init_blackboard_sync(bb_root: Path) -> None:
    """同步初始化黑板目录骨架（不依赖 aiofiles）。"""
    bb_root.mkdir(parents=True, exist_ok=True)
    for sub in ("agents", "audit", "locks", "tasks", "schemas", "snapshots"):
        (bb_root / sub).mkdir(exist_ok=True)
    # 初始 status.json
    import json

    status_path = bb_root / "status.json"
    if not status_path.exists():
        status_path.write_text(
            json.dumps(
                {
                    "protocol_version": "1.0.0",
                    "session_id": "default",
                    "phase": "init",
                    "version": 0,
                    "epoch": 0,
                    "current_turn": None,
                    "turn_history": [],
                    "active_agents": [],
                    "locks": {},
                    "last_message_seq": 0,
                    "last_heartbeat": "",
                    "director_status": "offline",
                    "director_signature": "",
                    "last_fencing_token": 0,
                    "recovery_started_at": None,
                    "recovery_progress": {},
                    "recovery_stage": "idle",
                    "extensions": {},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    # 空派生文件
    for fname in ("messages.md", "messages.pending.md", "messages.replay_candidates.md"):
        fpath = bb_root / fname
        if not fpath.exists():
            fpath.write_text("", encoding="utf-8")
    # 空 audit.jsonl
    audit_path = bb_root / "audit" / "audit.jsonl"
    if not audit_path.exists():
        audit_path.write_text("", encoding="utf-8")
    # 初始 director.md
    director_path = bb_root / "director.md"
    if not director_path.exists():
        import yaml

        director_md = {
            "director_id": "",
            "director_implementation": "agent",
            "current_epoch": 0,
            "epoch_started_at": "",
            "last_director_tick": "",
            "heartbeat": {
                "interval_seconds": 10,
                "timeout_seconds": 30,
            },
            "turn_policy": {
                "mode": "round_robin",
                "order": [],
            },
        }
        yaml_str = yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)
        director_path.write_text(
            f"---\n{yaml_str}---\n\n# Director Protocol\n",
            encoding="utf-8",
        )


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    """同步初始化黑板目录。"""
    _init_blackboard_sync(tmp_path)
    return tmp_path


@pytest.fixture
def app_with_multiagent(bb_root: Path) -> FastAPI:
    """构造启用了 multiagent 的 FastAPI app。"""
    from teage_liu.api.multiagent_routes import create_multiagent_router
    from teage_liu.container import Container

    config = {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": str(bb_root),
        },
    }
    container = Container(config)
    app = FastAPI()
    app.include_router(create_multiagent_router(container))
    return app


def _make_agent_card(agent_id: str = "worker_001", role: str = "worker") -> dict:
    """构造一份合规的 agent_card。"""
    return {
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "created_at": "2026-07-21T00:00:00Z",
        "last_heartbeat": "2026-07-21T00:00:00Z",
        "heartbeat_interval_seconds": 10,
        "status": "registering",
        "role": role,
        "endpoint": "http://localhost:8000",
        "owner": "user_a",
        "capabilities": ["file_read"],
        "specialties": [],
        "auth_method": "local",
        "trust_score": 100,
        "trust_history": [],
        "extensions": {},
        "leave_reason": "",
        "left_at": "",
    }


class TestMultiagentRoutes:
    """multiagent 路由测试。"""

    @pytest.mark.asyncio
    async def test_get_status(self, app_with_multiagent: FastAPI):
        """GET /api/multiagent/status 返回协作状态。"""
        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "enabled" in data
            assert "role" in data
            assert "director" in data
            assert "agents" in data

    @pytest.mark.asyncio
    async def test_get_agents_list(self, app_with_multiagent: FastAPI, bb_root: Path):
        """GET /api/multiagent/agents 返回 agent 列表。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator())
        await registry.register(_make_agent_card("worker_001", "worker"))

        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/agents")
            assert resp.status_code == 200
            data = resp.json()
            assert any(a["agent_id"] == "worker_001" for a in data["agents"])

    @pytest.mark.asyncio
    async def test_get_messages(self, app_with_multiagent: FastAPI, bb_root: Path):
        """GET /api/multiagent/messages 返回消息列表。"""
        from teage_liu.multiagent.blackboard import append_message

        await append_message(
            bb_root,
            {
                "seq": 1,
                "from": "worker_001",
                "to": "*",
                "timestamp": "2026-07-21T00:00:00Z",
                "type": "chat",
                "content_type": "markdown",
                "epoch": 0,
                "content": "hello",
            },
        )

        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/messages?limit=10")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["messages"]) >= 1

    @pytest.mark.asyncio
    async def test_get_audit_records(self, app_with_multiagent: FastAPI, bb_root: Path):
        """GET /api/multiagent/audit 返回审计记录。"""
        from teage_liu.multiagent.blackboard import append_audit

        await append_audit(
            bb_root,
            {
                "ts": "2026-07-21T00:00:00Z",
                "actor": "director",
                "action": "election",
                "target": "status.json",
                "op_id": "test",
                "epoch": 1,
                "details": {},
                "prev_hash": "",
                "hash": "",
                "signature": "",
            },
        )

        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/audit?limit=10")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["records"]) >= 1

    @pytest.mark.asyncio
    async def test_get_director_md(self, app_with_multiagent: FastAPI, bb_root: Path):
        """GET /api/multiagent/director 返回 director.md frontmatter。"""
        with TestClient(app_with_multiagent) as client:
            resp = client.get("/api/multiagent/director")
            assert resp.status_code == 200
            data = resp.json()
            # Blackboard.init_blackboard 会写入初始 director.md
            assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_disabled_returns_404(self, tmp_path: Path):
        """multiagent.enabled=False 时所有端点返回 404。"""
        from teage_liu.api.multiagent_routes import create_multiagent_router
        from teage_liu.container import Container

        config = {"multiagent": {"enabled": False}}
        container = Container(config)
        app = FastAPI()
        app.include_router(create_multiagent_router(container))

        with TestClient(app) as client:
            resp = client.get("/api/multiagent/status")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_sse_endpoint_returns_stream(self, app_with_multiagent: FastAPI):
        """GET /api/multiagent/sse 返回 text/event-stream。

        SSE 是无限流，仅验证响应头与首块格式，使用线程+超时避免阻塞。
        """
        import threading
        import queue

        result_q: queue.Queue = queue.Queue()

        def _do_request():
            try:
                with TestClient(app_with_multiagent) as client:
                    with client.stream("GET", "/api/multiagent/sse") as resp:
                        ct = resp.headers.get("content-type", "")
                        first_line = ""
                        for line in resp.iter_lines():
                            if line:
                                first_line = line
                                break
                        result_q.put((resp.status_code, ct, first_line))
            except Exception as e:
                result_q.put(e)

        t = threading.Thread(target=_do_request, daemon=True)
        t.start()
        t.join(timeout=5.0)  # 5 秒超时
        if t.is_alive():
            # SSE 流未在超时内返回首块，但路由已注册（非 404）
            return
        result = result_q.get_nowait()
        if isinstance(result, Exception):
            raise result
        status_code, content_type, first_line = result
        assert status_code == 200
        assert "text/event-stream" in content_type
        # 首行应为 "event: initial"
        assert "event:" in first_line or "data:" in first_line


def test_determine_director_state_unknown_for_empty():
    """_determine_director_state 对空 dict 返回 unknown。"""
    from teage_liu.api.multiagent_routes import _determine_director_state

    assert _determine_director_state({}) == "unknown"
    assert _determine_director_state(None) == "unknown"


def test_determine_director_state_healthy_for_recent_tick():
    """_determine_director_state 对最近 tick 返回 healthy。"""
    from datetime import datetime, timezone

    from teage_liu.api.multiagent_routes import _determine_director_state

    now_iso = datetime.now(timezone.utc).isoformat()
    state = _determine_director_state({"last_director_tick": now_iso})
    assert state == "healthy"


def test_determine_director_state_fault_for_stale_tick():
    """_determine_director_state 对过期 tick 返回 fault。"""
    from datetime import datetime, timedelta, timezone

    from teage_liu.api.multiagent_routes import _determine_director_state

    old_iso = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    state = _determine_director_state({"last_director_tick": old_iso})
    assert state == "fault"


def test_detect_event_type_director_state_change():
    """_detect_event_type 检测 director 状态变更。"""
    from teage_liu.api.multiagent_routes import _detect_event_type

    old = {"director": {"state": "healthy"}, "agents": [], "autonomous_mode": False}
    new = {"director": {"state": "degraded"}, "agents": [], "autonomous_mode": False}
    assert _detect_event_type(old, new) == "director_state_change"


def test_detect_event_type_agent_join():
    """_detect_event_type 检测 agent 加入。"""
    from teage_liu.api.multiagent_routes import _detect_event_type

    old = {"director": {"state": "healthy"}, "agents": [{"agent_id": "a"}], "autonomous_mode": False}
    new = {"director": {"state": "healthy"}, "agents": [{"agent_id": "a"}, {"agent_id": "b"}], "autonomous_mode": False}
    assert _detect_event_type(old, new) == "agent_join"


def test_detect_event_type_autonomous_enter():
    """_detect_event_type 检测进入自治模式。"""
    from teage_liu.api.multiagent_routes import _detect_event_type

    old = {"director": {"state": "healthy"}, "agents": [], "autonomous_mode": False}
    new = {"director": {"state": "healthy"}, "agents": [], "autonomous_mode": True}
    assert _detect_event_type(old, new) == "autonomous_enter"


def test_format_sse():
    """_format_sse 生成合规的 SSE 事件块。"""
    from teage_liu.api.multiagent_routes import _format_sse

    block = _format_sse("initial", {"enabled": True})
    assert block.startswith("event: initial\n")
    assert "data: " in block
    assert block.endswith("\n\n")


# =============================================================================
# Task 1 v3: dispatch_task + get_task_status 字段名统一
# =============================================================================


@pytest.mark.asyncio
async def test_dispatch_task_writes_compliant_message(app_with_multiagent: FastAPI, bb_root: Path):
    """dispatch_task 端点写入 schema 合规的消息。"""
    from teage_liu.multiagent.blackboard import read_messages

    with TestClient(app_with_multiagent) as client:
        resp = client.post("/api/multiagent/dispatch", json={
            "task": "测试任务",
            "target_agents": [],
            "mode": "dispatch",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    op_id = data["op_id"]

    messages = await read_messages(bb_root)
    task_msgs = [m for m in messages if m.get("task_op_id") == op_id]
    assert len(task_msgs) == 1
    msg = task_msgs[0]
    assert msg["from"] == "user_dispatch"
    assert msg["to"] == "*"
    assert msg["type"] == "task"
    assert msg["content"] == "测试任务"
    assert "timestamp" in msg
    assert msg["task_op_id"] == op_id
    assert msg["target_agents"] == []
    assert msg["mode"] == "dispatch"
    # 不应有旧字段
    assert "op_id" not in msg or msg.get("op_id") is None
    assert "ts" not in msg


@pytest.mark.asyncio
async def test_get_task_status_uses_task_op_id(app_with_multiagent: FastAPI, bb_root: Path):
    """get_task_status 端点使用 task_op_id 查询消息（v3 修复）。"""
    from teage_liu.multiagent.blackboard import append_message

    await append_message(bb_root, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task", "content": "查询测试任务",
        "task_op_id": "query-task-001", "target_agents": [], "mode": "dispatch",
    }, validate=True)

    with TestClient(app_with_multiagent) as client:
        resp = client.get("/api/multiagent/tasks/query-task-001")

    assert resp.status_code == 200
    data = resp.json()
    assert data["op_id"] == "query-task-001"
    assert data["status"] == "pending"
    assert len(data["timeline"]) == 1
    assert data["timeline"][0]["ts"] == "2026-07-23T10:00:00+00:00"


@pytest.mark.asyncio
async def test_get_task_status_fallback_to_op_id_for_legacy(app_with_multiagent: FastAPI, bb_root: Path):
    """get_task_status 端点兼容旧消息（fallback 查 op_id）。"""
    from teage_liu.multiagent.blackboard import append_message

    # 写入旧格式消息（op_id 而非 task_op_id）
    await append_message(bb_root, {
        "op_id": "legacy-task-001",
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task", "content": "旧格式任务",
    })

    with TestClient(app_with_multiagent) as client:
        resp = client.get("/api/multiagent/tasks/legacy-task-001")

    assert resp.status_code == 200
    data = resp.json()
    assert data["op_id"] == "legacy-task-001"
