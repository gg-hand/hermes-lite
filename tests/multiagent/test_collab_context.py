"""Phase6 上下文与缓存测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from teage_liu.multiagent.blackboard import (
    append_collab_message, read_collab_summary, update_collab_index,
    write_collab_summary,
)


def test_collab_summary_roundtrip(bb_root: Path):
    """C-1:摘要写入 frontmatter 并可读回。"""
    cid = "c-sum"
    update_collab_index(bb_root, cid, title="t", status="active", participants=[])
    summary = {"consensus": ["点1"], "open": ["问题A"],
               "positions": {"w1": "立场X"}, "decisions": ["决1"]}
    asyncio.run(write_collab_summary(bb_root, cid, summary))
    got = asyncio.run(read_collab_summary(bb_root, cid))
    assert got == summary


def test_clear_collab_session_history_preserves_summary(bb_root: Path):
    """C-1:清协作历史保留摘要(不全清)。"""
    cid = "c-clear"
    update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"])
    asyncio.run(write_collab_summary(bb_root, cid, {"consensus": ["k"], "open": [],
                                                    "positions": {}, "decisions": []}))
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {"persist_state": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    # _clear_collab_session_history 保留摘要(清 verbatim 历史,摘要仍可读)
    w._clear_collab_session_history(f"multiagent_w1")
    assert asyncio.run(read_collab_summary(bb_root, cid))["consensus"] == ["k"]
