"""Phase4 持久化与重建测试。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


def test_round_state_persisted_across_restart(bb_root: Path):
    """H-1:重启后 collab_max_rounds / collab_last_sent_round 准确恢复。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    from teage_liu.multiagent.worker_state import WorkerStateStore
    cfg = {"multiagent": {"worker": {"persist_state": True}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    w._collab_max_rounds = {"c1": 16}
    w._collab_last_sent_round = {"c1": 5}
    asyncio.run(w._persist_round_state("c1"))
    # 模拟重启:新 worker 加载
    w2 = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    asyncio.run(w2._load_state_on_start())
    assert w2._collab_max_rounds.get("c1") == 16
    assert w2._collab_last_sent_round.get("c1") == 5


def test_rebuild_round_state_per_cid_no_cross_talk(bb_root: Path):
    """A-2:rebuild 按 cid 独立重建 round 状态,多 cid 不串扰。"""
    from teage_liu.multiagent.blackboard import append_collab_message, update_collab_index
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    for cid in ("cA", "cB"):
        asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    # cA:本 worker 发过 round 3 的 response + 收到 1 个 extend
    asyncio.run(append_collab_message(bb_root, {"from": "w1", "type": "response",
        "content": "a", "collab_id": "cA", "collab_round": 3}, collab_id="cA"))
    asyncio.run(append_collab_message(bb_root, {"from": "w2", "type": "extend",
        "content": "more", "collab_id": "cA", "collab_round": 3}, collab_id="cA"))
    # cB:本 worker 发过 round 2
    asyncio.run(append_collab_message(bb_root, {"from": "w1", "type": "response",
        "content": "b", "collab_id": "cB", "collab_round": 2}, collab_id="cB"))
    cfg = {"multiagent": {"worker": {
        "persist_state": False, "worker_collab_decentralized_max_rounds": 8}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    asyncio.run(w._rebuild_state_from_history())
    assert w._collab_last_sent_round.get("cA") == 3
    assert w._collab_last_sent_round.get("cB") == 2
    base = 8
    assert w._collab_max_rounds.get("cA") == base + 8  # 1 个 extend
    assert w._collab_max_rounds.get("cB") == base      # 无 extend
