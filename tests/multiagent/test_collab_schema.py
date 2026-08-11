"""协作消息 schema 验证测试"""
import pytest
from pathlib import Path
from teage_liu.multiagent.schema_validator import SchemaValidator


@pytest.fixture
def validator():
    schema_path = Path("data/schemas/multiagent")
    return SchemaValidator(schema_dir=schema_path, enabled=True)


def test_relay_message_valid(validator):
    """relay 消息应通过验证"""
    msg = {
        "from": "agent_A",
        "to": "agent_B",
        "type": "relay",
        "content": "测试消息",
        "seq": 1,
    }
    validator.validate_messages_record(msg)


def test_request_message_with_collab_id(validator):
    """request 消息带 collab_id 应通过验证"""
    msg = {
        "from": "agent_A",
        "type": "request",
        "content": "需要协作",
        "collab_id": "collab_001",
        "collab_type": "instant",
        "message_id": "msg_001",
        "seq": 2,
    }
    validator.validate_messages_record(msg)


def test_directive_message_valid(validator):
    """directive 消息应通过验证"""
    msg = {
        "from": "director",
        "type": "directive",
        "content": "按顺序执行",
        "rule_type": "ordering",
        "target": "*",
        "seq": 3,
    }
    validator.validate_messages_record(msg)


def test_announce_message_valid(validator):
    """announce 消息应通过验证"""
    msg = {
        "from": "agent_A",
        "type": "announce",
        "action": "online",
        "capabilities": ["sentiment_analysis"],
        "endpoint": "http://localhost:8001",
        "seq": 4,
    }
    validator.validate_messages_record(msg)


def test_status_message_valid(validator):
    """status 消息应通过验证"""
    msg = {
        "from": "agent_A",
        "type": "status",
        "collab_id": "collab_001",
        "status": "completed",
        "seq": 5,
    }
    validator.validate_messages_record(msg)


def test_a2a_forwarded_message_valid(validator):
    """A2A 转发消息带 via/forwarded_by 应通过验证"""
    msg = {
        "from": "agent_A",
        "to": "agent_B",
        "type": "relay",
        "content": "A2A 消息",
        "via": "a2a",
        "forwarded_by": "agent_B",
        "message_id": "msg_001",
        "seq": 6,
    }
    validator.validate_messages_record(msg)


def test_directive_with_priority_and_deadline(validator):
    """directive 消息带 priority/deadline/issued_by 应通过验证"""
    msg = {
        "from": "director",
        "type": "directive",
        "content": "按顺序执行",
        "rule_type": "ordering",
        "target": "*",
        "priority": "high",
        "deadline": 300,
        "issued_by": "AgentDirector",
        "seq": 7,
    }
    validator.validate_messages_record(msg)


def test_intervention_directive_valid(validator):
    """intervention 类 directive 应通过验证（紧急插队用）"""
    msg = {
        "from": "director",
        "type": "directive",
        "content": "检测到死锁，立即停止",
        "rule_type": "intervention",
        "target": "*",
        "priority": "high",
        "issued_by": "ScriptDirector",
        "seq": 8,
    }
    validator.validate_messages_record(msg)


def test_director_from_field_valid(validator):
    """from=director 应通过验证（Director 广播）"""
    msg = {
        "from": "director",
        "type": "request",
        "content": "帮我读弹幕",
        "seq": 9,
    }
    validator.validate_messages_record(msg)


def test_response_message_valid(validator):
    """response 消息应通过验证"""
    msg = {
        "from": "agent_B",
        "type": "response",
        "content": "我可以做",
        "reply_to": 2,
        "accept": True,
        "seq": 10,
    }
    validator.validate_messages_record(msg)
