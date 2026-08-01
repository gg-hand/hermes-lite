"""Phase3 并发原子性测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from teage_liu.multiagent.agent_registry import AgentRegistry
from teage_liu.multiagent.blackboard import read_yaml_frontmatter


def _registry(bb_root: Path) -> AgentRegistry:
    from teage_liu.multiagent.schema_validator import SchemaValidator
    reg = AgentRegistry(bb_root, SchemaValidator(enabled=False))
    return reg


def _register_card(bb_root: Path, agent_id: str = "w1"):
    card = {"agent_id": agent_id, "status": "active", "capabilities": [],
            "endpoint": "", "last_heartbeat": "", "pid": 1, "host": "h"}
    asyncio.run(_registry(bb_root).register(card))


def test_update_fields_atomic_no_overwrite(bb_root: Path):
    """N-1:并发 update_fields 不同字段无覆盖。"""
    _register_card(bb_root, "w1")
    reg = _registry(bb_root)

    async def bump_heartbeat():
        for _ in range(20):
            await reg.update_fields("w1", last_heartbeat="hb-A")

    async def bump_status():
        for _ in range(20):
            await reg.update_fields("w1", status="degraded", leave_reason="x")

    async def main():
        await asyncio.gather(bump_heartbeat(), bump_status())

    asyncio.run(main())
    fm, _ = read_yaml_frontmatter(bb_root / "agents" / "w1.md")
    # 两字段都应保留(未被覆盖)
    assert fm.get("last_heartbeat") == "hb-A"
    assert fm.get("status") == "degraded"
    assert fm.get("leave_reason") == "x"


def test_worker_heartbeat_routes_through_registry(bb_root: Path):
    """N-1:Worker._update_heartbeat 委托 registry.update_fields。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {"persist_state": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    _register_card(bb_root, "w1")
    asyncio.run(w._update_heartbeat())
    fm, _ = read_yaml_frontmatter(bb_root / "agents" / "w1.md")
    assert fm.get("last_heartbeat")
    assert fm.get("status") == "active"
