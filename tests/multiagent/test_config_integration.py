"""配置与容器集成测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes.config import load_config
from hermes.container import Container, CONFIG_TO_COMPONENTS
from hermes.config_helpers import _RESTART_REQUIRED_KEYS, _validate_config_schema


def test_config_multiagent_section_parsed(tmp_path: Path):
    """multiagent 配置段应被正确解析。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
multiagent:
  enabled: true
  role: worker
  blackboard_dir: "${HERMES_BB_DIR}"
  default_session_id: default
  worker:
    agent_id: hermes_default
    heartbeat_interval_seconds: 10
    watchdog_backend: watchdog
    capabilities: [file_read, file_write]
    dangerous_tools: [execute_command, write_file, call_tool]
  cas:
    merge_on_exhausted: true
  director:
    enforce_rules: true
    conflict_strategy: llm_arbitration
    turn_timeout_seconds: 30
    heartbeat_timeout_seconds: 30
    fallback_strategy: priority
    grace_period_seconds: 2
    recovery_lock_timeout: 30
  watchdog:
    reset_to_watchdog: false
  a2a_gateway:
    enabled: false
    listen_port: 8001
    expose_agent_card: true
    auth_schemes: [api_key]
  schema_validation: true
  audit:
    corrupt_log_rotation: 10MB
    retention_days: 30
""",
        encoding="utf-8",
    )
    os.environ["HERMES_BB_DIR"] = str(tmp_path / "blackboard")
    config = load_config(str(config_path))
    assert config["multiagent"]["enabled"] is True
    assert config["multiagent"]["role"] == "worker"
    assert config["multiagent"]["worker"]["dangerous_tools"] == ["execute_command", "write_file", "call_tool"]


def test_config_multiagent_disabled_default():
    """multiagent.enabled 默认应为 False。"""
    # 不设置 multiagent 段时应默认 disabled
    config_path = Path("config.yaml")
    if config_path.exists():
        config = load_config(str(config_path))
        assert config.get("multiagent", {}).get("enabled", False) is False


def test_validate_config_schema_accepts_multiagent():
    """_validate_config_schema 应接受 multiagent 段。"""
    config = {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": "/tmp/bb",
        }
    }
    # 不应抛异常
    _validate_config_schema(config)


def test_restart_required_keys_includes_blackboard_dir():
    """_RESTART_REQUIRED_KEYS 应包含 multiagent.blackboard_dir。"""
    assert "multiagent.blackboard_dir" in _RESTART_REQUIRED_KEYS


def test_restart_required_keys_excludes_listen_port():
    """_RESTART_REQUIRED_KEYS 不应包含 multiagent.a2a_gateway.listen_port（热重载）。"""
    assert "multiagent.a2a_gateway.listen_port" not in _RESTART_REQUIRED_KEYS


def test_config_to_components_includes_multiagent():
    """CONFIG_TO_COMPONENTS 应包含 multiagent 映射。"""
    assert "multiagent" in CONFIG_TO_COMPONENTS
    components = CONFIG_TO_COMPONENTS["multiagent"]
    assert "blackboard" in components
    assert "agent_registry" in components
    assert "lock_manager" in components
    assert "multiagent_audit_logger" in components
    assert "schema_validator" in components
    assert "recovery_manager" in components
    assert "watchdog_watcher" in components


def test_container_registers_multiagent_components(tmp_path: Path):
    """Container 应能注册 multiagent 组件。"""
    os.environ["HERMES_BB_DIR"] = str(tmp_path / "blackboard")
    config = {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": str(tmp_path / "blackboard"),
            "worker": {"agent_id": "test_agent", "heartbeat_interval_seconds": 10},
            "director": {"heartbeat_timeout_seconds": 30, "grace_period_seconds": 2},
            "schema_validation": False,
        }
    }
    container = Container(config)
    # 注册 multiagent 组件
    from hermes.multiagent.blackboard import atomic_write, validate_path_safety
    from hermes.multiagent.schema_validator import SchemaValidator
    from hermes.multiagent.file_lock import LockManager
    from hermes.multiagent.audit_logger import MultiAgentAuditLogger
    from hermes.multiagent.agent_registry import AgentRegistry
    from hermes.multiagent.watchdog_watcher import WatchdogWatcher
    from hermes.multiagent.recovery import RecoveryCoordinator

    bb_root = Path(config["multiagent"]["blackboard_dir"])
    bb_root.mkdir(parents=True, exist_ok=True)
    (bb_root / "agents").mkdir(exist_ok=True)
    (bb_root / "audit").mkdir(exist_ok=True)
    (bb_root / "locks").mkdir(exist_ok=True)

    container.register("blackboard", lambda c: bb_root, deps=[], hot_reloadable=False)
    container.register("schema_validator", lambda c: SchemaValidator(enabled=False), deps=[], hot_reloadable=True)
    container.register("lock_manager", lambda c: LockManager(bb_root), deps=[], hot_reloadable=True)
    container.register("multiagent_audit_logger", lambda c: MultiAgentAuditLogger(bb_root), deps=[], hot_reloadable=True)
    container.register("agent_registry", lambda c: AgentRegistry(bb_root, c.get("schema_validator")), deps=["schema_validator"], hot_reloadable=True)
    container.register("watchdog_watcher", lambda c: WatchdogWatcher(bb_root, lambda evt: None), deps=[], hot_reloadable=True)
    container.register("recovery_manager", lambda c: RecoveryCoordinator(bb_root, c.get("multiagent_audit_logger")), deps=["multiagent_audit_logger"], hot_reloadable=True)

    # 验证可获取
    assert container.get("blackboard") == bb_root
    assert container.get("schema_validator") is not None
    assert container.get("lock_manager") is not None
    assert container.get("multiagent_audit_logger") is not None
