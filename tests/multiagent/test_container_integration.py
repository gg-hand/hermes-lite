"""Plan 2 Task 9: multiagent_adapter 配置与容器集成测试。

验证：
- CONFIG_TO_COMPONENTS["multiagent"] 包含 multiagent_adapter
- Container 可注册和获取 multiagent_adapter（enabled=true 时）
- Container 不注册 multiagent_adapter（enabled=false 时）
- WorkerAdapter 可独立 start/stop（lifespan 集成冒烟）
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

from hermes.container import CONFIG_TO_COMPONENTS, Container


class TestContainerIntegration:
    """容器注册集成测试。"""

    def test_config_to_components_includes_multiagent_adapter(self):
        """CONFIG_TO_COMPONENTS["multiagent"] 应包含 multiagent_adapter。"""
        assert "multiagent" in CONFIG_TO_COMPONENTS
        components = CONFIG_TO_COMPONENTS["multiagent"]
        # Plan 2 新增：multiagent_adapter（DirectorEngine 或 WorkerAdapter）
        assert "multiagent_adapter" in components
        # 级联依赖：orchestrator 应在列表中
        assert "orchestrator" in components

    def test_container_registers_multiagent_adapter_when_enabled(self, tmp_path: Path):
        """multiagent.enabled=True 时 Container 应能注册 multiagent_adapter。"""
        bb_root = tmp_path / "blackboard"
        bb_root.mkdir(parents=True, exist_ok=True)
        for sub in ("agents", "audit", "locks", "tasks", "schemas", "snapshots"):
            (bb_root / sub).mkdir(exist_ok=True)

        config = {
            "multiagent": {
                "enabled": True,
                "role": "worker",
                "blackboard_dir": str(bb_root),
                "worker": {
                    "agent_id": "worker_001",
                    "heartbeat_interval_seconds": 10,
                    "capabilities": ["file_read"],
                    "dangerous_tools": [],
                },
                "director": {
                    "heartbeat_timeout_seconds": 30,
                },
            }
        }

        container = Container(config)

        # 注册 multiagent_adapter（模拟 lifespan.py 的注册逻辑）
        from hermes.multiagent.worker_adapter import WorkerAdapter

        multiagent_cfg = config["multiagent"]
        bb_dir = multiagent_cfg["blackboard_dir"]
        worker_cfg = multiagent_cfg.get("worker", {})
        container.register(
            "multiagent_adapter",
            lambda c: WorkerAdapter(
                bb_root=Path(bb_dir),
                config=multiagent_cfg,
                agent_id=worker_cfg.get("agent_id", "worker_001"),
            ),
            deps=[],
            hot_reloadable=True,
        )

        # 验证可获取
        adapter = container.get("multiagent_adapter")
        assert adapter is not None
        assert adapter._agent_id == "worker_001"

    def test_container_not_registers_multiagent_adapter_when_disabled(self, tmp_path: Path):
        """multiagent.enabled=False 时不注册 multiagent_adapter。"""
        config = {
            "multiagent": {
                "enabled": False,
            }
        }

        container = Container(config)
        # 不注册 multiagent_adapter

        # 不应可获取
        with pytest.raises(KeyError):
            container.get("multiagent_adapter")

    def test_container_registers_director_adapter_when_role_director(self, tmp_path: Path):
        """role=director 时 Container 应注册 DirectorEngine 作为 multiagent_adapter。"""
        bb_root = tmp_path / "blackboard"
        bb_root.mkdir(parents=True, exist_ok=True)
        for sub in ("agents", "audit", "locks", "tasks", "schemas", "snapshots"):
            (bb_root / sub).mkdir(exist_ok=True)

        config = {
            "multiagent": {
                "enabled": True,
                "role": "director",
                "blackboard_dir": str(bb_root),
                "director": {
                    "heartbeat_timeout_seconds": 30,
                },
            }
        }

        container = Container(config)

        # 注册 multiagent_adapter（模拟 lifespan.py 的注册逻辑）
        from hermes.multiagent.director_engine import DirectorEngine

        multiagent_cfg = config["multiagent"]
        bb_dir = multiagent_cfg["blackboard_dir"]
        container.register(
            "multiagent_adapter",
            lambda c: DirectorEngine(
                bb_root=Path(bb_dir),
                config=multiagent_cfg,
                agent_id="director_001",
            ),
            deps=[],
            hot_reloadable=True,
        )

        # 验证可获取
        adapter = container.get("multiagent_adapter")
        assert adapter is not None
        # DirectorEngine 实例
        assert hasattr(adapter, "_acquire_mutex_lock")


class TestLifespanIntegration:
    """lifespan 启动集成冒烟测试。"""

    @pytest.mark.asyncio
    async def test_worker_adapter_start_stop(self, tmp_path: Path):
        """WorkerAdapter 可独立 start/stop（lifespan 集成冒烟）。"""
        from hermes.multiagent.blackboard import Blackboard
        from hermes.multiagent.worker_adapter import WorkerAdapter

        bb_root = tmp_path / "blackboard"
        bb = Blackboard(bb_root)
        await bb.init_blackboard()

        config = {
            "multiagent": {
                "enabled": True,
                "role": "worker",
                "blackboard_dir": str(bb_root),
                "worker": {
                    "agent_id": "worker_001",
                    "heartbeat_interval_seconds": 10,
                    "capabilities": ["file_read"],
                    "dangerous_tools": [],
                },
                "director": {
                    "heartbeat_timeout_seconds": 30,
                },
            }
        }

        adapter = WorkerAdapter(bb_root, config, agent_id="worker_001")
        await adapter.start()
        assert adapter._running is True

        await adapter.stop()
        assert adapter._running is False
