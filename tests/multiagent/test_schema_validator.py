"""schema_validator.py 测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes.multiagent.schema_validator import SchemaValidator


@pytest.fixture
def validator() -> SchemaValidator:
    return SchemaValidator()


def test_validate_status_ok(validator: SchemaValidator, sample_status_json: dict):
    sample_status_json["phase"] = "active"
    sample_status_json["director_status"] = "active"
    validator.validate_status(sample_status_json)  # 不抛异常


def test_validate_status_missing_required_field(validator: SchemaValidator, sample_status_json: dict):
    del sample_status_json["version"]
    with pytest.raises(Exception, match="version"):
        validator.validate_status(sample_status_json)


def test_validate_status_invalid_phase_enum(validator: SchemaValidator, sample_status_json: dict):
    sample_status_json["phase"] = "invalid_phase"
    with pytest.raises(Exception, match="phase"):
        validator.validate_status(sample_status_json)


def test_validate_status_invalid_director_status_enum(validator: SchemaValidator, sample_status_json: dict):
    sample_status_json["director_status"] = "unknown"
    with pytest.raises(Exception, match="director_status"):
        validator.validate_status(sample_status_json)


def test_validate_agent_card_ok(validator: SchemaValidator):
    frontmatter = {
        "agent_id": "agent_a",
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "status": "active",
        "role": "worker",
        "last_heartbeat": "2026-07-20T10:00:00Z",
        "heartbeat_interval_seconds": 10,
        "capabilities": ["file_read"],
    }
    validator.validate_agent_card(frontmatter)  # 不抛异常


def test_validate_agent_card_invalid_status_enum(validator: SchemaValidator):
    frontmatter = {
        "agent_id": "agent_a",
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "status": "unknown_status",
        "role": "worker",
        "last_heartbeat": "2026-07-20T10:00:00Z",
        "heartbeat_interval_seconds": 10,
    }
    with pytest.raises(Exception, match="status"):
        validator.validate_agent_card(frontmatter)


def test_validate_agent_card_invalid_role_enum(validator: SchemaValidator):
    frontmatter = {
        "agent_id": "agent_a",
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "status": "active",
        "role": "invalid_role",
        "last_heartbeat": "2026-07-20T10:00:00Z",
        "heartbeat_interval_seconds": 10,
    }
    with pytest.raises(Exception, match="role"):
        validator.validate_agent_card(frontmatter)


def test_validate_agent_card_invalid_id_regex(validator: SchemaValidator):
    frontmatter = {
        "agent_id": "INVALID ID WITH SPACE",
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "status": "active",
        "role": "worker",
        "last_heartbeat": "2026-07-20T10:00:00Z",
        "heartbeat_interval_seconds": 10,
    }
    with pytest.raises(Exception, match="agent_id"):
        validator.validate_agent_card(frontmatter)


def test_validate_agent_card_observer_requires_heartbeat(validator: SchemaValidator):
    """observer role 必须有 last_heartbeat + heartbeat_interval_seconds（P1-1）。"""
    frontmatter = {
        "agent_id": "obs_1",
        "status": "active",
        "role": "observer",
        # 缺 last_heartbeat + heartbeat_interval_seconds
    }
    with pytest.raises(Exception, match="last_heartbeat|heartbeat_interval"):
        validator.validate_agent_card(frontmatter)


def test_validate_messages_record_ok(validator: SchemaValidator):
    record = {
        "seq": 1,
        "from": "agent_a",
        "to": "*",
        "timestamp": "2026-07-20T10:00:00Z",
        "type": "chat",
        "content": "hello",
    }
    validator.validate_messages_record(record)


def test_validate_messages_record_missing_seq(validator: SchemaValidator):
    record = {
        "from": "agent_a",
        "to": "*",
        "timestamp": "2026-07-20T10:00:00Z",
        "type": "chat",
        "content": "hello",
    }
    with pytest.raises(Exception, match="seq"):
        validator.validate_messages_record(record)


def test_validate_audit_record_ok(validator: SchemaValidator):
    record = {
        "ts": "2026-07-20T10:00:00Z",
        "actor": "agent_a",
        "action": "write",
        "target": "messages.md",
        "details": {"reason": "normal_write"},
        "prev_hash": "",
        "hash": "abc123",
    }
    validator.validate_audit_record(record)


def test_validate_audit_record_invalid_action(validator: SchemaValidator):
    record = {
        "ts": "2026-07-20T10:00:00Z",
        "actor": "agent_a",
        "action": "invalid_action",
        "target": "messages.md",
        "details": {},
        "prev_hash": "",
        "hash": "abc123",
    }
    with pytest.raises(Exception, match="action"):
        validator.validate_audit_record(record)


def test_validator_disabled_skips_validation():
    """enabled=False 时跳过校验（对齐 config.multiagent.schema_validation）。"""
    v = SchemaValidator(enabled=False)
    # 缺必填字段的 status 也不抛
    v.validate_status({"phase": "invalid_phase"})


def test_validator_loads_schemas_from_custom_dir(tmp_path: Path):
    """支持自定义 schema_dir，便于测试隔离。"""
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "status_json.schema.json").write_text(
        '{"type":"object","required":["foo"]}', encoding="utf-8"
    )
    v = SchemaValidator(schema_dir=schema_dir)
    v.validate_status({"foo": 1})  # 不抛
    with pytest.raises(Exception):
        v.validate_status({})  # 缺 foo
