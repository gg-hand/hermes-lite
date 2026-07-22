"""agent_registry.py 测试。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from teage_liu.multiagent.agent_registry import AgentRegistry


@pytest.fixture
def registry(bb_root: Path) -> AgentRegistry:
    from teage_liu.multiagent.schema_validator import SchemaValidator
    # 使用 enabled=True 以便 test_observer_requires_heartbeat_fields 能通过 schema 校验触发
    return AgentRegistry(bb_root, SchemaValidator())


def _make_agent_card(agent_id: str = "agent_a", role: str = "worker") -> dict:
    return {
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "created_at": "2026-07-20T09:55:00Z",
        "last_heartbeat": "2026-07-20T10:00:00Z",
        "heartbeat_interval_seconds": 10,
        "status": "registering",
        "role": role,
        "endpoint": "http://localhost:8000",
        "owner": "user_a",
        "capabilities": ["file_read", "file_write"],
        "specialties": [],
        "auth_method": "local",
        "trust_score": 100,
        "trust_history": [],
        "extensions": {},
        "leave_reason": "",
        "left_at": "",
    }


@pytest.mark.asyncio
async def test_register_creates_agent_card(registry: AgentRegistry, bb_root: Path):
    card = _make_agent_card()
    await registry.register(card)
    agent_file = bb_root / "agents" / "agent_a.md"
    assert agent_file.exists()
    content = agent_file.read_text(encoding="utf-8")
    assert "agent_id: agent_a" in content


@pytest.mark.asyncio
async def test_register_duplicate_rejected(registry: AgentRegistry):
    card = _make_agent_card()
    await registry.register(card)
    with pytest.raises(Exception, match="already exists|already_registered"):
        await registry.register(card)


@pytest.mark.asyncio
async def test_unregister_marks_offline(registry: AgentRegistry, bb_root: Path):
    card = _make_agent_card()
    await registry.register(card)
    await registry.unregister("agent_a", leave_reason="test_done")
    agent_file = bb_root / "agents" / "agent_a.md"
    content = agent_file.read_text(encoding="utf-8")
    assert "status: offline" in content
    assert "test_done" in content


@pytest.mark.asyncio
async def test_update_heartbeat(registry: AgentRegistry, bb_root: Path):
    card = _make_agent_card()
    await registry.register(card)
    await registry.update_heartbeat("agent_a")
    agent_file = bb_root / "agents" / "agent_a.md"
    content = agent_file.read_text(encoding="utf-8")
    # last_heartbeat 应被更新为非空
    assert "last_heartbeat:" in content


@pytest.mark.asyncio
async def test_list_active_agents(registry: AgentRegistry):
    await registry.register(_make_agent_card("agent_a"))
    await registry.register(_make_agent_card("agent_b"))
    actives = await registry.list_active_agents()
    assert len(actives) == 2
    agent_ids = {a["agent_id"] for a in actives}
    assert agent_ids == {"agent_a", "agent_b"}


@pytest.mark.asyncio
async def test_list_active_agents_excludes_offline(registry: AgentRegistry):
    await registry.register(_make_agent_card("agent_a"))
    await registry.register(_make_agent_card("agent_b"))
    await registry.unregister("agent_b")
    actives = await registry.list_active_agents()
    assert len(actives) == 1
    assert actives[0]["agent_id"] == "agent_a"


@pytest.mark.asyncio
async def test_get_agent(registry: AgentRegistry):
    card = _make_agent_card()
    await registry.register(card)
    result = await registry.get_agent("agent_a")
    assert result is not None
    assert result["agent_id"] == "agent_a"


@pytest.mark.asyncio
async def test_get_agent_not_found(registry: AgentRegistry):
    result = await registry.get_agent("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_observer_requires_heartbeat_fields(registry: AgentRegistry):
    """observer role 必须有 last_heartbeat + heartbeat_interval_seconds（P1-1）。"""
    card = _make_agent_card(role="observer")
    # 故意删除 heartbeat_interval_seconds，应注册失败
    del card["heartbeat_interval_seconds"]
    with pytest.raises(Exception, match="heartbeat_interval"):
        await registry.register(card)
