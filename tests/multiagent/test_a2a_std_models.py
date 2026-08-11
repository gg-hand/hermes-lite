"""标准 A2A 数据模型测试（models.py）。"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from teage_liu.multiagent.a2a_std.models import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    Message,
    SecurityScheme,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)


class TestTaskState:
    """TaskState 终态/中断判定。"""

    def test_terminal_states(self):
        assert TaskState.COMPLETED.is_terminal
        assert TaskState.FAILED.is_terminal
        assert TaskState.CANCELED.is_terminal
        assert TaskState.REJECTED.is_terminal

    def test_non_terminal_states(self):
        assert not TaskState.SUBMITTED.is_terminal
        assert not TaskState.WORKING.is_terminal
        assert not TaskState.INPUT_REQUIRED.is_terminal
        assert not TaskState.AUTH_REQUIRED.is_terminal

    def test_interrupt_states(self):
        assert TaskState.INPUT_REQUIRED.is_interrupt
        assert TaskState.AUTH_REQUIRED.is_interrupt
        assert not TaskState.COMPLETED.is_interrupt

    def test_wire_values_hyphen(self):
        """线协议值含连字符。"""
        assert TaskState.INPUT_REQUIRED.value == "input-required"
        assert TaskState.AUTH_REQUIRED.value == "auth-required"


class TestPartDiscriminator:
    """Part 判别联合。"""

    def test_text_part_roundtrip(self):
        part = TextPart(text="你好")
        assert part.model_dump(by_alias=True) == {"kind": "text", "text": "你好", "metadata": None}

    def test_file_part_alias(self):
        part = FilePart(name="a.pdf", mimeType="application/pdf", uri="https://x/a.pdf")
        dumped = part.model_dump(by_alias=True)
        assert dumped["mimeType"] == "application/pdf"
        assert dumped["kind"] == "file"

    def test_data_part(self):
        part = DataPart(data={"k": 1})
        assert part.kind == "data"
        assert part.data == {"k": 1}

    def test_parse_from_wire_camel_case(self):
        """从线协议（camelCase）解析。"""
        msg = Message.model_validate({
            "role": "agent",
            "messageId": "m-1",
            "parts": [{"kind": "text", "text": "hi"}],
        })
        assert msg.message_id == "m-1"
        assert msg.parts[0].kind == "text"

    def test_invalid_part_kind_rejected(self):
        with pytest.raises(ValidationError):
            Message.model_validate({
                "role": "user", "messageId": "m-1",
                "parts": [{"kind": "binary", "data": "x"}],
            })


class TestMessageTask:
    """Message / Task 序列化。"""

    def test_message_dump_camel_case(self):
        msg = Message(role="user", message_id="m-1", parts=[TextPart(text="hi")])
        dumped = json.loads(msg.model_dump_json(by_alias=True))
        assert dumped["messageId"] == "m-1"
        assert dumped["role"] == "user"
        assert dumped["parts"][0]["kind"] == "text"

    def test_task_roundtrip(self):
        status = TaskStatus(state=TaskState.WORKING, timestamp="2026-01-01T00:00:00Z")
        task = Task(id="t_abc", context_id="collab_1", status=status)
        restored = Task.model_validate(json.loads(task.model_dump_json(by_alias=True)))
        assert restored.id == "t_abc"
        assert restored.context_id == "collab_1"
        assert restored.status.state == TaskState.WORKING

    def test_task_with_artifacts(self):
        artifact = Artifact(name="response_1", parts=[TextPart(text="结果")], append=True)
        task = Task(id="t_1", status=TaskStatus(state=TaskState.COMPLETED), artifacts=[artifact])
        assert task.artifacts[0].name == "response_1"

    def test_extra_fields_tolerated(self):
        """未知字段容忍（extra=allow）。"""
        task = Task.model_validate({
            "id": "t_1", "contextId": None, "status": {"state": "submitted"},
            "futureField": {"x": 1},
        })
        assert task.id == "t_1"


class TestEvents:
    """SSE 事件模型。"""

    def test_status_update_event_alias(self):
        evt = TaskStatusUpdateEvent(
            id="t_1", status=TaskStatus(state=TaskState.COMPLETED),
        )
        dumped = evt.model_dump(by_alias=True)
        assert dumped["status"]["state"] == "completed"

    def test_artifact_update_event(self):
        evt = TaskArtifactUpdateEvent(
            id="t_1", artifacts=[Artifact(name="a1", parts=[TextPart(text="x")])],
        )
        assert len(evt.artifacts) == 1


class TestAgentCard:
    """Agent Card 模型。"""

    def test_card_roundtrip(self):
        card = AgentCard(
            name="teagent-lu",
            description="personal agent",
            url="http://localhost:8000/a2a/std/jsonrpc",
            capabilities=AgentCapabilities(streaming=True),
        )
        dumped = json.loads(card.model_dump_json(by_alias=True))
        assert dumped["protocolVersion"] == "1.0"
        assert dumped["preferredTransport"] == "JSONRPC"
        assert dumped["capabilities"]["streaming"] is True
        assert dumped["capabilities"]["pushNotifications"] is False
        restored = AgentCard.model_validate(dumped)
        assert restored.url == "http://localhost:8000/a2a/std/jsonrpc"

    def test_card_with_skills_and_security(self):
        card = AgentCard(
            name="a",
            description="d",
            url="u",
            skills=[AgentSkill(id="s1", name="S1", description="d1")],
            security_schemes=[SecurityScheme(scheme="bearer")],
            extensions=["heartbeat"],
        )
        assert card.skills[0].id == "s1"
        assert card.security_schemes[0].scheme == "bearer"
        assert card.extensions == ["heartbeat"]

    def test_card_default_input_output_modes(self):
        card = AgentCard(name="a", description="d", url="u")
        assert card.default_input_modes == ["text", "text/plain"]
        assert card.default_output_modes == ["text", "text/plain"]
