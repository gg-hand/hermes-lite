"""Phase6 上下文与缓存测试。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from teage_liu.multiagent.blackboard import (
    append_collab_message, read_collab_summary, update_collab_index,
    write_collab_summary,
)


def _register_agent(bb_root: Path, agent_id: str, capabilities: list[str]) -> None:
    """注册一个 active agent 到 agents/ 目录（供 partner_context 发现伙伴）。"""
    from teage_liu.multiagent.agent_registry import AgentRegistry
    from teage_liu.multiagent.schema_validator import SchemaValidator

    card = {
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "created_at": "2026-07-31T10:00:00Z",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "heartbeat_interval_seconds": 10,
        "status": "active",
        "role": "worker",
        "endpoint": "http://localhost:8001",
        "owner": "user_a",
        "capabilities": capabilities,
        "specialties": [],
        "auth_method": "local",
        "trust_score": 100,
        "trust_history": [],
        "extensions": {},
        "leave_reason": "",
        "left_at": "",
    }
    registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))

    async def _go():
        await registry.register(card)

    asyncio.run(_go())


def test_collab_summary_roundtrip(bb_root: Path):
    """C-1:摘要写入 frontmatter 并可读回。"""
    cid = "c-sum"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=[]))
    summary = {"consensus": ["点1"], "open": ["问题A"],
               "positions": {"w1": "立场X"}, "decisions": ["决1"]}
    asyncio.run(write_collab_summary(bb_root, cid, summary))
    got = asyncio.run(read_collab_summary(bb_root, cid))
    assert got == summary


def test_clear_collab_session_history_preserves_summary(bb_root: Path):
    """C-1:清协作历史保留摘要(不全清)。"""
    cid = "c-clear"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    asyncio.run(write_collab_summary(bb_root, cid, {"consensus": ["k"], "open": [],
                                                    "positions": {}, "decisions": []}))
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {"persist_state": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    # _clear_collab_session_history 保留摘要(清 verbatim 历史,摘要仍可读)
    w._clear_collab_session_history(f"multiagent_w1")
    assert asyncio.run(read_collab_summary(bb_root, cid))["consensus"] == ["k"]


def test_partner_context_global_timeline(bb_root: Path):
    """C-2:partner_context 默认全局时间线(按 seq 排序,含所有参与方)。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cid = "c-tl"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1", "w2"]))
    # 注册 w2 为在线伙伴（partner_context 据此发现参与方）
    _register_agent(bb_root, "w2", ["file_read"])
    asyncio.run(append_collab_message(bb_root, {"from": "w1", "type": "response",
        "content": "ALPHA_FROM_W1", "collab_id": cid, "collab_round": 1}, collab_id=cid))
    asyncio.run(append_collab_message(bb_root, {"from": "w2", "type": "response",
        "content": "BETA_FROM_W2", "collab_id": cid, "collab_round": 1}, collab_id=cid))
    cfg = {"multiagent": {"worker": {"persist_state": False, "partner_context_grouped": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    ctx = asyncio.run(w._build_collab_partner_context(collab_id=cid))
    # 全局时间线:w1 和 w2 消息都出现,按 seq 顺序(ALPHA 在 BETA 之前)
    assert "ALPHA_FROM_W1" in ctx and "BETA_FROM_W2" in ctx
    assert ctx.index("ALPHA_FROM_W1") < ctx.index("BETA_FROM_W2")  # seq 顺序
