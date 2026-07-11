"""DI 容器测试:注册/获取/热重载/级联/循环检测/原子性回滚。"""
import sys, os, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from container import Container, ContainerConfigError, ConfigReloadError, ComponentRef


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
        c = Container({"llm": {"model": "v1"}})
        c.register("llm_client", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        c.register("orchestrator", lambda c: FakeOrchestrator(c.get("llm_client")),
                   deps=["llm_client"], hot_reloadable=False)
        old_orch = c.get("orchestrator")
        old_llm = c.get("llm_client")

        c.reload({"llm"}, {"llm": {"model": "v2"}})

        new_orch = c.get("orchestrator")
        new_llm = c.get("llm_client")
        assert new_llm is not old_llm
        assert new_orch is not old_orch
        assert new_llm.config == {"model": "v2"}

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
        c.register("llm_client", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        original = c.get("llm_client")

        # 替换 factory 为会失败的版本
        c._factories["llm_client"] = factory
        with pytest.raises(ConfigReloadError):
            c.reload({"llm"}, {"llm": {"model": "v2"}})

        # 旧实例不变
        assert c.get("llm_client") is original
        assert c.config["llm"] == {"model": "v1"}

    def test_delayed_close(self):
        c = Container({"llm": {"model": "v1"}, "server": {"hot_reload_grace_period": 0}})
        c.register("llm_client", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        old = c.get("llm_client")

        c.reload({"llm"}, {"llm": {"model": "v2"}, "server": {"hot_reload_grace_period": 0}})
        # grace period=0,等待短暂时间后旧实例应被关闭
        time.sleep(0.5)
        assert old.closed is True


class TestComponentRef:
    def test_ref_forwards_to_latest(self):
        c = Container({"llm": {"model": "v1"}})
        c.register("llm_client", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        ref = ComponentRef(c, "llm_client")
        assert ref.config == {"model": "v1"}

        c.reload({"llm"}, {"llm": {"model": "v2"}})
        assert ref.config == {"model": "v2"}
