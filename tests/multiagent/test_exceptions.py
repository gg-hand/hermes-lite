"""multiagent 异常类基础测试。"""
from __future__ import annotations

import pytest

from teage_liu.agent.tool_error import ToolError, ErrorStage
from teage_liu.multiagent.exceptions import (
    A2AGatewayError,
    CASConflictError,
    CASVersionMismatchError,
    CapabilityNotInCardError,
    DirectorUnavailableError,
    FencingTokenMismatchError,
    GhostWriteAttemptError,
    LockAcquisitionError,
    NotMyTurnError,
)


def test_cas_conflict_error_inherits_tool_error():
    err = CASConflictError(
        lock_name="messages",
        expected_version=42,
        actual_version=43,
    )
    assert isinstance(err, ToolError)
    assert err.stage == ErrorStage.PROTOCOL
    assert err.category == "cas_conflict"
    assert "messages" in err.reason
    assert "42" in err.reason and "43" in err.reason


def test_fencing_token_mismatch_error_fields():
    err = FencingTokenMismatchError(
        lock_name="messages",
        expected_token=7,
        actual_token=6,
        writer_id="agent_a",
    )
    assert err.category == "fencing_token_mismatch"
    assert "7" in err.reason and "6" in err.reason


def test_lock_acquisition_error_fields():
    err = LockAcquisitionError(
        lock_name="messages",
        reason="held_by_other",
        current_holder="agent_b",
    )
    assert err.category == "lock_acquisition_failed"
    assert "agent_b" in err.reason


def test_not_my_turn_error_fields():
    err = NotMyTurnError(
        expected_agent="agent_a",
        actual_agent="agent_b",
        turn_started_at="2026-07-20T10:00:05Z",
    )
    assert err.category == "not_my_turn"
    assert "agent_a" in err.reason


def test_director_unavailable_error_fields():
    err = DirectorUnavailableError(
        last_tick="2026-07-20T10:00:00Z",
        age_seconds=120,
    )
    assert err.category == "director_unavailable"
    assert "120" in err.reason


def test_ghost_write_attempt_error_fields():
    err = GhostWriteAttemptError(
        writer_id="agent_a",
        lock_name="messages",
        fencing_token=5,
        current_token=7,
    )
    assert err.category == "ghost_write_attempt"


def test_capability_not_in_card_error_fields():
    err = CapabilityNotInCardError(
        tool_name="execute_command",
        agent_id="agent_a",
        declared_capabilities=["file_read", "file_write"],
    )
    assert err.category == "capability_not_in_card"
    assert "execute_command" in err.reason


def test_a2a_gateway_error_fields():
    err = A2AGatewayError(
        endpoint="http://remote:8001/a2a/message",
        reason="connection_refused",
    )
    assert err.category == "a2a_gateway"
