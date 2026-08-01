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
