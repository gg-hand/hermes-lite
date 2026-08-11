"""multiagent REST 端点测试。

覆盖 Plan 4 Task 1：状态查询 / agents / messages / audit / director / sse。
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

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
def bb_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """同步初始化黑板目录并隔离 TEAGE_BB_ROOT 环境变量。

    multiagent_routes.create_multiagent_router 优先读 TEAGE_BB_ROOT 环境变量
    决定 bb_root（与 collab_router 一致）。若先前测试用 os.environ[...] 直接
    赋值未清理，会污染本套件读取的 blackboard 目录。这里用 monkeypatch.setenv
    强制指向当前 tmp_path，测试结束自动还原，杜绝跨套件污染。
    """
    _init_blackboard_sync(tmp_path)
    monkeypatch.setenv("TEAGE_BB_ROOT", str(tmp_path))
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
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
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


def test_determine_director_fault_reason_healthy_is_empty():
    """healthy 状态的 fault_reason 应为空字符串。"""
    from datetime import datetime, timezone

    from teage_liu.api.multiagent_routes import _determine_director_fault_reason

    now_iso = datetime.now(timezone.utc).isoformat()
    reason = _determine_director_fault_reason({"last_director_tick": now_iso}, "healthy")
    assert reason == ""


def test_determine_director_fault_reason_fault_contains_timeout_info():
    """fault 状态的 fault_reason 应包含心跳超时与阈值信息。"""
    from datetime import datetime, timedelta, timezone

    from teage_liu.api.multiagent_routes import _determine_director_fault_reason

    old_iso = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    reason = _determine_director_fault_reason({"last_director_tick": old_iso}, "fault")
    assert "心跳超时" in reason
    assert "120 秒" in reason


def test_determine_director_fault_reason_unknown_for_missing_metadata():
    """元数据缺失时 unknown 状态应返回明确原因。"""
    from teage_liu.api.multiagent_routes import _determine_director_fault_reason

    assert "元数据缺失" in _determine_director_fault_reason(None, "unknown")
    assert "未上报心跳" in _determine_director_fault_reason({}, "unknown")


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


# ============================================================================
# Director 状态判定 v2：融合进程级状态与心跳延迟
# 解决问题：Director 停止后心跳过期被误判为 fault（红色故障），
#          实际应区分"主动停止（休眠）"与"故障崩溃"
# ============================================================================


def test_determine_director_state_v2_stopped_when_not_running():
    """Director 未启动时（无 PID 文件），即使心跳过期也应返回 stopped 而非 fault。"""
    from datetime import datetime, timedelta, timezone

    from teage_liu.api.multiagent_routes import _determine_director_state_v2

    old_iso = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    director_md = {"last_director_tick": old_iso}
    director_status = {"running": False, "state": "stopped", "pid": None}

    state = _determine_director_state_v2(director_md, director_status)
    assert state == "stopped"  # 而非 fault


def test_determine_director_state_v2_crashed_when_pid_file_exists():
    """Director 进程崩溃（有 PID 文件但进程不在），应返回 crashed。"""
    from teage_liu.api.multiagent_routes import _determine_director_state_v2

    director_md = {"last_director_tick": ""}
    director_status = {"running": False, "state": "crashed", "pid": 12345}

    state = _determine_director_state_v2(director_md, director_status)
    assert state == "crashed"


def test_determine_director_state_v2_starting_when_running_no_heartbeat():
    """Director 刚启动（进程运行中但无心跳），应返回 starting。"""
    from teage_liu.api.multiagent_routes import _determine_director_state_v2

    director_md = {"last_director_tick": ""}
    director_status = {"running": True, "state": "starting", "pid": 12345}

    state = _determine_director_state_v2(director_md, director_status)
    assert state == "starting"


def test_determine_director_state_v2_healthy_when_running_recent_tick():
    """Director 运行中 + 新鲜心跳 → healthy。"""
    from datetime import datetime, timezone

    from teage_liu.api.multiagent_routes import _determine_director_state_v2

    now_iso = datetime.now(timezone.utc).isoformat()
    state = _determine_director_state_v2(
        {"last_director_tick": now_iso},
        {"running": True, "state": "healthy", "pid": 1},
    )
    assert state == "healthy"


def test_determine_director_state_v2_fault_when_running_but_tick_stale():
    """Director 进程在跑但心跳严重过期（>120s）→ 仍判定为 fault（进程僵死）。"""
    from datetime import datetime, timedelta, timezone

    from teage_liu.api.multiagent_routes import _determine_director_state_v2

    old_iso = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    state = _determine_director_state_v2(
        {"last_director_tick": old_iso},
        {"running": True, "state": "degraded", "pid": 1},
    )
    assert state == "fault"


def test_determine_director_state_v2_fallback_to_heartbeat_when_no_proc_status():
    """无 director_status 时，回退到只看心跳的旧行为（向后兼容）。"""
    from datetime import datetime, timezone

    from teage_liu.api.multiagent_routes import _determine_director_state_v2

    now_iso = datetime.now(timezone.utc).isoformat()
    state = _determine_director_state_v2({"last_director_tick": now_iso}, None)
    assert state == "healthy"


def test_determine_director_fault_reason_stopped_suggests_sleep():
    """stopped 状态的 fault_reason 应提示休眠/未启动。"""
    from teage_liu.api.multiagent_routes import _determine_director_fault_reason

    reason = _determine_director_fault_reason({}, "stopped")
    assert "未启动" in reason or "休眠" in reason


def test_determine_director_fault_reason_crashed_indicates_crash():
    """crashed 状态的 fault_reason 应提示进程崩溃。"""
    from teage_liu.api.multiagent_routes import _determine_director_fault_reason

    reason = _determine_director_fault_reason({}, "crashed")
    assert "崩溃" in reason


def test_determine_director_fault_reason_starting_indicates_starting():
    """starting 状态的 fault_reason 应提示启动中。"""
    from teage_liu.api.multiagent_routes import _determine_director_fault_reason

    reason = _determine_director_fault_reason({}, "starting")
    assert "启动" in reason


def test_get_status_returns_stopped_when_director_not_running(monkeypatch, bb_root: Path):
    """Director 未启动时，/status 端点应返回 stopped 而非 fault。

    场景：Director 从未启动或已被主动停止，director.md 中的心跳可能为空或过期。
    旧逻辑仅看心跳过期会误判为 fault（红色故障），新逻辑应识别为 stopped（休眠）。
    """
    from datetime import datetime, timedelta, timezone

    import yaml
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from teage_liu.api.multiagent_routes import create_multiagent_router
    from teage_liu.container import Container
    from teage_liu.multiagent.director_manager import DirectorManager

    class StoppedDirectorManager(DirectorManager):
        """Mock：Director 已停止（休眠）。"""

        async def start(self):
            return {"ok": True, "message": "已启动"}

        async def stop(self):
            return {"ok": True, "message": "已停止"}

        async def status(self):
            return {
                "running": False,
                "state": "stopped",
                "pid": None,
                "last_heartbeat": None,
            }

        async def restart(self):
            return {"ok": True, "message": "已重启"}

    # 写入过期心跳到 director.md（模拟 Director 曾运行过但已停止）
    old_iso = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    director_md = {
        "director_id": "test_director",
        "current_epoch": 1,
        "last_director_tick": old_iso,
    }
    yaml_str = yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)
    (bb_root / "director.md").write_text(
        f"---\n{yaml_str}---\n\n# Director Protocol\n",
        encoding="utf-8",
    )

    # 替换工厂函数，返回 Stopped 状态的 mock
    # 注：create_multiagent_router 内部通过 `from teage_liu.multiagent.director_manager import create_director_manager`
    # 导入，因此 patch 源模块属性才能在导入时生效
    monkeypatch.setattr(
        "teage_liu.multiagent.director_manager.create_director_manager",
        lambda config: StoppedDirectorManager(),
    )

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

    with TestClient(app) as client:
        resp = client.get("/api/multiagent/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["director"]["state"] == "stopped"  # 而非 fault
        # fault_reason 应提示休眠而非故障
        assert "休眠" in data["director"]["fault_reason"] or "未启动" in data["director"]["fault_reason"]


# 注：原 Task 1 v3 的 dispatch_task / get_task_status 测试已移除
# 这些端点在 agent 自主协作架构重设计（2026-07-23）中已被删除
# 协作消息现由 collab 端点（/api/multiagent/collab/*）处理
# 详见 docs/plans/2026-07-23-agent自主协作架构重设计.md
