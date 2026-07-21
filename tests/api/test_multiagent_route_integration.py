"""multiagent 路由集成测试（Plan 4 Task 5）。

验证 hermes/app.py 的 register_components 正确注册 multiagent_router 到容器，
以及通过容器获取的路由可挂载到 FastAPI app 并提供端点。

RED 阶段：register_components 未注册 multiagent_router，
         container.get("multiagent_router") 返回 None，断言失败。

参考：Plan 4 doc §Task 5 GREEN 节关于 register_components 修改的描述。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def _reset_container():
    """每个测试前后清理全局容器，避免污染其他测试。"""
    from hermes.app import close_container

    close_container()
    yield
    close_container()


def _init_blackboard_skeleton(bb_root: Path) -> None:
    """初始化黑板目录骨架（与 test_multiagent_routes.py 同款）。"""
    bb_root.mkdir(parents=True, exist_ok=True)
    for sub in ("agents", "audit", "locks", "tasks", "schemas", "snapshots"):
        (bb_root / sub).mkdir(exist_ok=True)
    # 初始 status.json
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


class TestRouteIntegration:
    """路由集成测试：register_components 应注册 multiagent_router。"""

    def test_multiagent_router_registered_when_enabled(
        self, tmp_path: Path, _reset_container
    ):
        """multiagent.enabled=True 时 register_components 注册 multiagent_router。

        RED 失败原因：register_components 未注册 multiagent_router，
                      container.get("multiagent_router") 返回 None。
        """
        from hermes.app import init_container, register_components, get_container

        bb_dir = tmp_path / "bb"
        _init_blackboard_skeleton(bb_dir)

        config = {
            "multiagent": {
                "enabled": True,
                "role": "worker",
                "blackboard_dir": str(bb_dir),
            },
        }
        init_container(config)
        container = get_container()
        register_components(container)

        # 验证容器中注册了 multiagent_router
        router = container.get("multiagent_router")
        assert router is not None, "multiagent_router 未注册到容器"
        # 验证是 APIRouter 实例
        assert isinstance(router, APIRouter), (
            f"multiagent_router 应为 APIRouter 实例，实际: {type(router).__name__}"
        )

    def test_multiagent_router_not_registered_when_disabled(
        self, tmp_path: Path, _reset_container
    ):
        """multiagent.enabled=False 时 register_components 不注册 multiagent_router。"""
        from hermes.app import init_container, register_components, get_container

        config = {"multiagent": {"enabled": False}}
        init_container(config)
        container = get_container()
        register_components(container)

        # 验证容器中未注册 multiagent_router（container.get 抛 KeyError）
        with pytest.raises(KeyError, match="multiagent_router"):
            container.get("multiagent_router")

    def test_status_endpoint_returns_200_via_container_router(
        self, tmp_path: Path, _reset_container
    ):
        """enabled=True 时通过容器注册的路由 GET /api/multiagent/status 返回 200。

        RED 失败原因：container.get("multiagent_router") 返回 None，
                      app.include_router(None) 抛 TypeError。
        """
        from hermes.app import init_container, register_components, get_container

        bb_dir = tmp_path / "bb"
        _init_blackboard_skeleton(bb_dir)

        config = {
            "multiagent": {
                "enabled": True,
                "role": "worker",
                "blackboard_dir": str(bb_dir),
            },
        }
        init_container(config)
        container = get_container()
        register_components(container)

        app = FastAPI()
        router = container.get("multiagent_router")
        assert router is not None, "multiagent_router 未注册，无法挂载"
        app.include_router(router)

        with TestClient(app) as client:
            resp = client.get("/api/multiagent/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is True
            assert data["role"] == "worker"
            assert "director" in data
            assert "agents" in data

    def test_router_is_hot_reloadable(self, tmp_path: Path, _reset_container):
        """multiagent_router 注册时标记为 hot_reloadable=True（支持配置热更新）。"""
        from hermes.app import init_container, register_components, get_container

        bb_dir = tmp_path / "bb"
        _init_blackboard_skeleton(bb_dir)

        config = {
            "multiagent": {
                "enabled": True,
                "role": "worker",
                "blackboard_dir": str(bb_dir),
            },
        }
        init_container(config)
        container = get_container()
        register_components(container)

        # 验证注册时标记为 hot_reloadable（容器内部维护 _hot_reloadable 字典）
        assert "multiagent_router" in container._factories, (
            "multiagent_router 未在容器工厂表中注册"
        )
        hot_reloadable = container._hot_reloadable.get("multiagent_router", False)
        assert hot_reloadable is True, "multiagent_router 应标记为 hot_reloadable=True"

    def test_router_multiple_endpoints_present(
        self, tmp_path: Path, _reset_container
    ):
        """容器注册的 multiagent_router 包含全部 6 个端点。"""
        from hermes.app import init_container, register_components, get_container

        bb_dir = tmp_path / "bb"
        _init_blackboard_skeleton(bb_dir)

        config = {
            "multiagent": {
                "enabled": True,
                "role": "worker",
                "blackboard_dir": str(bb_dir),
            },
        }
        init_container(config)
        container = get_container()
        register_components(container)

        router = container.get("multiagent_router")
        assert router is not None

        # 收集所有注册的路径
        paths = {route.path for route in router.routes}
        expected_paths = {
            "/api/multiagent/status",
            "/api/multiagent/agents",
            "/api/multiagent/messages",
            "/api/multiagent/audit",
            "/api/multiagent/director",
            "/api/multiagent/sse",
        }
        missing = expected_paths - paths
        assert not missing, f"缺少端点: {missing}，实际路径: {paths}"
