﻿"""DI 容器测试:注册/获取/热重载/级联/循环检测/原子性回滚。"""
import sys, os, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes"))

import pytest
from hermes.container import Container, ContainerConfigError, ConfigReloadError, ComponentRef


class FakeLLM:
    def __init__(self, config):
        self.config = config
        self.closed = False
    def close(self):
        self.closed = True

class FakeOrchestrator:
    def __init__(self, llm_client):
        self.llm_client = llm_client
        self.closed = False
    def close(self):
        self.closed = True


class TestContainerBasics:
    def test_register_and_get(self):
        c = Container({"llm": {"model": "gpt-4"}})
        c.register("llm_client", lambda c: FakeLLM(c.config["llm"]), deps=[], hot_reloadable=True)
        client = c.get("llm_client")
        assert client.config == {"model": "gpt-4"}

    def test_singleton(self):
        c = Container({})
        c.register("llm_client", lambda c: FakeLLM({}), deps=[], hot_reloadable=True)
        assert c.get("llm_client") is c.get("llm_client")

    def test_dependency_injection(self):
        c = Container({})
        c.register("llm_client", lambda c: FakeLLM({}), deps=[], hot_reloadable=True)
        c.register("orchestrator", lambda c: FakeOrchestrator(c.get("llm_client")),
                   deps=["llm_client"], hot_reloadable=False)
        orch = c.get("orchestrator")
        assert orch.llm_client is c.get("llm_client")


class TestValidation:
    def test_cycle_detection(self):
        c = Container({})
        c.register("a", lambda c: None, deps=["b"])
        c.register("b", lambda c: None, deps=["a"])
        with pytest.raises(ContainerConfigError, match="循环依赖"):
            c.validate()

    def test_missing_dep(self):
        c = Container({})
        c.register("a", lambda c: None, deps=["nonexistent"])
        with pytest.raises(ContainerConfigError, match="未注册"):
            c.validate()


class TestHotReload:
    def test_reload_rebuilds_component(self):
        """llm 配置变更时，orchestrator（方案B整体注册）被重建。"""
        c = Container({"llm": {"model": "v1"}})
        c.register("orchestrator", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        old_orch = c.get("orchestrator")

        c.reload({"llm"}, {"llm": {"model": "v2"}})

        new_orch = c.get("orchestrator")
        assert new_orch is not old_orch
        assert new_orch.config == {"model": "v2"}

    def test_reload_atomic_rollback(self):
        class FailingLLM:
            def __init__(self, config): self.config = config
            def close(self): pass

        fail_count = [0]
        def factory(c):
            if fail_count[0] == 0:
                fail_count[0] += 1
                raise RuntimeError("创建失败")
            return FailingLLM(c.config["llm"])

        c = Container({"llm": {"model": "v1"}})
        c.register("orchestrator", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        original = c.get("orchestrator")

        # 替换 factory 为会失败的版本
        c._factories["orchestrator"] = factory
        with pytest.raises(ConfigReloadError):
            c.reload({"llm"}, {"llm": {"model": "v2"}})

        # 旧实例不变
        assert c.get("orchestrator") is original
        assert c.config["llm"] == {"model": "v1"}

    def test_delayed_close(self):
        c = Container({"llm": {"model": "v1"}, "server": {"hot_reload_grace_period": 0}})
        c.register("orchestrator", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        old = c.get("orchestrator")

        c.reload({"llm"}, {"llm": {"model": "v2"}, "server": {"hot_reload_grace_period": 0}})
        # grace period=0,等待短暂时间后旧实例应被关闭
        time.sleep(0.5)
        assert old.closed is True


class TestComponentRef:
    def test_ref_forwards_to_latest(self):
        c = Container({"llm": {"model": "v1"}})
        c.register("orchestrator", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        ref = ComponentRef(c, "orchestrator")
        assert ref.config == {"model": "v1"}

        c.reload({"llm"}, {"llm": {"model": "v2"}})
        assert ref.config == {"model": "v2"}


class TestConfigMapping:
    """验证 CONFIG_TO_COMPONENTS 映射覆盖所有配置段。"""

    def test_llm_maps_to_orchestrator(self):
        """llm 配置变更应触发 orchestrator 重建。"""
        from hermes.container import CONFIG_TO_COMPONENTS
        assert "orchestrator" in CONFIG_TO_COMPONENTS.get("llm", [])

    def test_monitoring_section_exists(self):
        """monitoring 配置段应映射到 metrics 组件。"""
        from hermes.container import CONFIG_TO_COMPONENTS
        assert "metrics_collector" in CONFIG_TO_COMPONENTS.get("monitoring", [])
        assert "metrics_store" in CONFIG_TO_COMPONENTS.get("monitoring", [])
        assert "audit_logger" in CONFIG_TO_COMPONENTS.get("monitoring", [])

    def test_all_config_sections_covered(self):
        """所有配置段都应在 CONFIG_TO_COMPONENTS 中有映射。"""
        from hermes.container import CONFIG_TO_COMPONENTS
        expected = {"llm", "security", "storage", "memory", "monitoring",
                    "tasks", "skills", "files", "guardrails", "cron",
                    "history", "tools", "server"}
        missing = expected - set(CONFIG_TO_COMPONENTS.keys())
        assert not missing, f"缺少配置段映射: {missing}"

    def test_llm_does_not_map_to_llm_client(self):
        """方案B：llm 不再映射到独立的 llm_client，而是 orchestrator 整体。"""
        from hermes.container import CONFIG_TO_COMPONENTS
        assert "llm_client" not in CONFIG_TO_COMPONENTS.get("llm", [])


class TestRegisterComponents:
    """验证 register_components 注册所有外部组件。"""

    def test_register_components_exists(self):
        """register_components 函数应存在于 app 模块。"""
        from hermes.app import register_components
        assert callable(register_components)

    def test_register_all_external_components(self):
        """register_components 应注册15个组件工厂。"""
        from hermes.app import register_components
        c = Container({"storage": {}, "monitoring": {}, "security": {},
                       "tasks": {}, "skills": {}, "files": {}, "llm": {},
                       "memory": {}, "guardrails": {}, "cron": {},
                       "history": {}, "tools": {}, "server": {},
                       "_config_path": "config.yaml"})
        register_components(c)
        expected = {"session_logger", "metrics_collector", "metrics_store",
                    "audit_logger", "approval_manager", "task_manager",
                    "orchestrator", "stream_manager", "skill_loader",
                    "mcp_manager", "upload_manager", "etl_engine",
                    "cron_scheduler", "proposal_store", "health_checker"}
        registered = set(c._factories.keys())
        missing = expected - registered
        assert not missing, f"缺少组件注册: {missing}"

    def test_container_validate_no_cycles(self):
        """注册后容器应通过循环依赖检测。"""
        from hermes.app import register_components
        c = Container({"storage": {}, "monitoring": {}, "security": {},
                       "tasks": {}, "skills": {}, "files": {}, "llm": {},
                       "memory": {}, "guardrails": {}, "cron": {},
                       "history": {}, "tools": {}, "server": {},
                       "_config_path": "config.yaml"})
        register_components(c)
        c.validate()  # 不抛异常即通过


class TestSetInstance:
    """验证 set_instance 注入已创建的实例，绕过工厂。"""

    def test_set_instance_returns_injected(self):
        """set_instance 后 get 应返回注入的实例而非工厂创建的。"""
        c = Container({})
        c.register("orchestrator", lambda c: FakeLLM({"factory": True}),
                   deps=[], hot_reloadable=False)
        injected = FakeLLM({"injected": True})
        c.set_instance("orchestrator", injected)
        assert c.get("orchestrator") is injected

    def test_set_instance_bypasses_factory(self):
        """set_instance 后工厂不应被调用。"""
        factory_called = [False]

        def factory(c):
            factory_called[0] = True
            return FakeLLM({})

        c = Container({})
        c.register("orchestrator", factory, deps=[], hot_reloadable=False)
        injected = FakeLLM({"real": True})
        c.set_instance("orchestrator", injected)
        result = c.get("orchestrator")
        assert result is injected
        assert factory_called[0] is False

    def test_set_instance_unregistered_raises(self):
        """set_instance 未注册的组件应抛 KeyError。"""
        c = Container({})
        with pytest.raises(KeyError, match="未注册"):
            c.set_instance("nonexistent", object())

    def test_set_instance_then_reload_rebuilds(self):
        """set_instance 注入后，reload 仍应通过工厂重建。"""
        c = Container({"llm": {"model": "v1"}, "server": {"hot_reload_grace_period": 0}})
        c.register("orchestrator", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        injected = FakeLLM({"injected": True})
        c.set_instance("orchestrator", injected)
        assert c.get("orchestrator") is injected

        c.reload({"llm"}, {"llm": {"model": "v2"},
                            "server": {"hot_reload_grace_period": 0}})
        rebuilt = c.get("orchestrator")
        assert rebuilt is not injected
        assert rebuilt.config == {"model": "v2"}
