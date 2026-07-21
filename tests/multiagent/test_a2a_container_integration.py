"""A2A 配置与容器集成测试。"""
import pytest
from pathlib import Path

from hermes.container import CONFIG_TO_COMPONENTS


class TestA2AContainerIntegration:
    """A2A 容器集成测试。"""

    def test_config_to_components_includes_a2a(self):
        """CONFIG_TO_COMPONENTS 包含 a2a 段。"""
        assert "a2a" in CONFIG_TO_COMPONENTS

    def test_a2a_router_registered_when_enabled(self, tmp_path: Path):
        """a2a.enabled=True 时注册路由。"""
        from hermes.app import init_container, register_components, get_container

        config = {
            "a2a": {
                "enabled": True,
                "listen_host": "127.0.0.1",
                "listen_port": 18400,
            },
            "multiagent": {
                "enabled": True,
                "blackboard_dir": str(tmp_path / "bb"),
            },
        }

        init_container(config)
        container = get_container()
        register_components(container)

        # a2a_router 应可获取
        router = container.get("a2a_router")
        assert router is not None

    def test_a2a_not_registered_when_disabled(self, tmp_path: Path):
        """a2a.enabled=False 时不注册路由。"""
        from hermes.app import init_container, register_components, get_container

        config = {"a2a": {"enabled": False}}
        init_container(config)
        container = get_container()
        register_components(container)

        with pytest.raises(KeyError):
            container.get("a2a_router")

    def test_a2a_in_restart_required_keys(self):
        """a2a 路径变更需重启。"""
        from hermes.app import _RESTART_REQUIRED_KEYS
        assert any("a2a" in key for key in _RESTART_REQUIRED_KEYS)
