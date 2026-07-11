"""热重载测试：LLM/安全/存储三种场景（Task 5）。

验证 Container.reload() 的原子性重建、级联重建、ComponentRef 代理转发。
同时验证 detect_changed_sections 的顶层段比较逻辑。
"""
from __future__ import annotations

import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if os.path.join(_PROJECT_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

import pytest  # noqa: E402
from container import (  # noqa: E402
    Container,
    ComponentRef,
    ConfigReloadError,
    detect_changed_sections,
)


# ---------------------------------------------------------------------------
# detect_changed_sections
# ---------------------------------------------------------------------------

class TestDetectChangedSections:
    def test_no_change(self):
        old = {"llm": {"model": "gpt-4"}, "server": {"port": 8080}}
        new = {"llm": {"model": "gpt-4"}, "server": {"port": 8080}}
        assert detect_changed_sections(old, new) == set()

    def test_llm_change(self):
        old = {"llm": {"model": "gpt-4"}}
        new = {"llm": {"model": "claude-3"}}
        assert detect_changed_sections(old, new) == {"llm"}

    def test_multiple_sections_change(self):
        old = {"llm": {"model": "v1"}, "security": {"enabled": True}}
        new = {"llm": {"model": "v2"}, "security": {"enabled": False}}
        assert detect_changed_sections(old, new) == {"llm", "security"}

    def test_new_section_added(self):
        old = {"llm": {}}
        new = {"llm": {}, "memory": {"threshold": 7}}
        assert detect_changed_sections(old, new) == {"memory"}


# ---------------------------------------------------------------------------
# LLM 热重载
# ---------------------------------------------------------------------------

class TestLLMHotReload:
    def test_llm_reload_rebuilds_client_and_orchestrator(self):
        c = Container({"llm": {"model": "v1"}, "server": {"hot_reload_grace_period": 0}})

        class FakeLLM:
            def __init__(self, config):
                self.model = config["model"]
                self.closed = False

            def close(self):
                self.closed = True

        class FakeOrch:
            def __init__(self, llm):
                self.llm = llm

            def close(self):
                pass

        c.register("llm_client", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        c.register("orchestrator", lambda c: FakeOrch(c.get("llm_client")),
                   deps=["llm_client"], hot_reloadable=False)

        old_llm = c.get("llm_client")
        old_orch = c.get("orchestrator")

        reloaded = c.reload({"llm"}, {"llm": {"model": "v2"},
                                      "server": {"hot_reload_grace_period": 0}})

        assert "llm_client" in reloaded
        assert "orchestrator" in reloaded
        assert c.get("llm_client").model == "v2"
        assert c.get("orchestrator") is not old_orch
        time.sleep(0.5)
        assert old_llm.closed is True


# ---------------------------------------------------------------------------
# 安全策略热重载
# ---------------------------------------------------------------------------

class TestSecurityHotReload:
    def test_security_reload_rebuilds_policy_engine(self):
        c = Container({"security": {"enabled": True},
                       "server": {"hot_reload_grace_period": 0}})

        class FakePolicy:
            def __init__(self, config):
                self.enabled = config["enabled"]

            def close(self):
                pass

        c.register("policy_engine", lambda c: FakePolicy(c.config["security"]),
                   deps=[], hot_reloadable=True)
        c.register("orchestrator", lambda c: object(),
                   deps=["policy_engine"], hot_reloadable=False)

        reloaded = c.reload({"security"}, {"security": {"enabled": False},
                                           "server": {"hot_reload_grace_period": 0}})
        assert "policy_engine" in reloaded
        assert c.get("policy_engine").enabled is False


# ---------------------------------------------------------------------------
# ComponentRef 在热重载中转发到最新实例
# ---------------------------------------------------------------------------

class TestComponentRefInReload:
    def test_component_ref_gets_latest(self):
        c = Container({"llm": {"model": "v1"},
                       "server": {"hot_reload_grace_period": 0}})

        class FakeLLM:
            def __init__(self, config):
                self.model = config["model"]

            def close(self):
                pass

        class FakeCron:
            def __init__(self, orch_ref):
                self._ref = orch_ref

        c.register("llm_client", lambda c: FakeLLM(c.config["llm"]),
                   deps=[], hot_reloadable=True)
        c.register("orchestrator", lambda c: FakeLLM(c.config["llm"]),
                   deps=["llm_client"], hot_reloadable=False)
        c.register("cron_scheduler",
                   lambda c: FakeCron(ComponentRef(c, "orchestrator")),
                   deps=[], hot_reloadable=False)

        cron = c.get("cron_scheduler")
        c.reload({"llm"}, {"llm": {"model": "v2"},
                           "server": {"hot_reload_grace_period": 0}})
        # ComponentRef 应转发到新 orchestrator
        assert cron._ref.model == "v2"


# ---------------------------------------------------------------------------
# 敏感字段分离
# ---------------------------------------------------------------------------

class TestSensitiveFieldSeparation:
    """验证 write_config_with_sensitive_separation 将敏感字段写入 .env。"""

    def test_sensitive_field_moved_to_env(self, tmp_path):
        from config import write_config_with_sensitive_separation
        config_path = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        new_config = {
            "llm": {"main_api_key": "sk-secret-key", "main_model": "gpt-4"},
            "server": {"port": 8000},
        }
        write_config_with_sensitive_separation(
            new_config, str(config_path), str(env_path)
        )
        # config.yaml 中应含 ${LLM_MAIN_API_KEY} 占位符
        yaml_content = config_path.read_text(encoding="utf-8")
        assert "${LLM_MAIN_API_KEY}" in yaml_content
        assert "sk-secret-key" not in yaml_content
        # .env 中应含实际值
        env_content = env_path.read_text(encoding="utf-8")
        assert "LLM_MAIN_API_KEY=sk-secret-key" in env_content

    def test_already_placeholder_not_re_written(self, tmp_path):
        from config import write_config_with_sensitive_separation
        config_path = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        new_config = {
            "llm": {"main_api_key": "${LLM_MAIN_API_KEY}", "main_model": "gpt-4"},
        }
        write_config_with_sensitive_separation(
            new_config, str(config_path), str(env_path)
        )
        yaml_content = config_path.read_text(encoding="utf-8")
        assert "${LLM_MAIN_API_KEY}" in yaml_content
        # .env 不应被写入（已经是占位符）
        assert not env_path.exists() or "LLM_MAIN_API_KEY=" not in env_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lifespan 接入容器（Task 4）
# ---------------------------------------------------------------------------

class TestLifespanContainerInjection:
    """验证 inject_lifespan_instances 将 lifespan 创建的实例注入容器。"""

    def test_inject_lifespan_instances_exists(self):
        """inject_lifespan_instances 函数应存在于 app 模块。"""
        from app import inject_lifespan_instances
        assert callable(inject_lifespan_instances)

    def test_inject_all_instances_bypasses_factories(self):
        """注入后 container.get() 应返回注入的实例而非工厂创建的。"""
        from app import init_container, register_components, inject_lifespan_instances, get_container
        config = {"storage": {}, "monitoring": {}, "security": {},
                  "tasks": {}, "skills": {}, "files": {}, "llm": {},
                  "memory": {}, "guardrails": {}, "cron": {},
                  "history": {}, "tools": {}, "server": {},
                  "_config_path": "config.yaml"}
        init_container(config)
        container = get_container()
        register_components(container)

        # 模拟 lifespan 创建的实例
        fake_orch = object()
        fake_session_logger = object()
        fake_metrics = object()
        inject_lifespan_instances(
            container,
            orchestrator=fake_orch,
            session_logger=fake_session_logger,
            metrics_collector=fake_metrics,
            metrics_store=None,
            audit_logger=None,
            approval_manager=None,
            task_manager=None,
            stream_manager=None,
            skill_loader=None,
            mcp_manager=None,
            upload_manager=None,
            etl_engine=None,
            cron_scheduler=None,
            proposal_store=None,
            health_checker=None,
        )

        assert container.get("orchestrator") is fake_orch
        assert container.get("session_logger") is fake_session_logger
        assert container.get("metrics_collector") is fake_metrics


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
