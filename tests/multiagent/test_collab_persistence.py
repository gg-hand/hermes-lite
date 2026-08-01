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


def test_consensus_write_failure_no_archive(bb_root: Path, monkeypatch):
    """H-3:consensus 写入失败 → 不 archive,记精炼错误。"""
    from teage_liu.multiagent.blackboard import append_collab_message, update_collab_index, read_collab_index
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cid = "c-h3"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    asyncio.run(append_collab_message(bb_root, {"from": "w2", "type": "consensus",
        "content": "done", "collab_id": cid, "collab_round": 5, "seq": 1}, collab_id=cid))
    cfg = {"multiagent": {"worker": {"persist_state": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    asyncio.run(w._load_archived_collabs())
    # 让 archive_collab 失败
    import teage_liu.multiagent.worker_adapter as wa
    async def _fail_archive(*a, **k):
        raise RuntimeError("archive boom")
    monkeypatch.setattr(wa, "archive_collab", _fail_archive)
    # 处理 consensus 消息:archive 失败应被捕获,不抛
    asyncio.run(w._handle_collab_message({"from": "w2", "type": "consensus",
        "content": "done", "collab_id": cid, "collab_round": 5, "seq": 1}))
    # 协作仍未归档(archive 失败)
    entries = asyncio.run(read_collab_index(bb_root))
    assert next(e for e in entries if e["collab_id"] == cid)["status"] == "active"


def test_startup_heals_unarchived_consensus(bb_root: Path):
    """H-3:启动自愈——有 consensus 终止信号但未归档的协作,补 archive。"""
    from teage_liu.multiagent.blackboard import append_collab_message, update_collab_index, read_collab_index
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cid = "c-heal"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    asyncio.run(append_collab_message(bb_root, {"from": "w2", "type": "consensus",
        "content": "done", "collab_id": cid, "collab_round": 5, "seq": 1}, collab_id=cid))
    cfg = {"multiagent": {"worker": {"persist_state": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    asyncio.run(w._heal_unarchived_consensus())
    # 自愈后协作应被归档
    entries = asyncio.run(read_collab_index(bb_root))
    assert next(e for e in entries if e["collab_id"] == cid)["status"] == "archived"
    assert cid in w._archived_collabs


def test_consensus_intent_pattern_configurable(bb_root: Path):
    """H-2:consensus_intent_pattern 可配置,覆盖默认 6 字符窗口。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {
        "persist_state": False,
        "consensus_intent_pattern": r"(探讨|争取).{0,20}(达成共识|达成一致)",
    }}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    # 配置正则窗口 20,应排除更长距离的意向短语
    assert w._detect_consensus("探讨很多步骤之后最终达成共识") is False
    # 真实共识仍命中
    assert w._detect_consensus("本轮协作达成共识,终止。") is True


def test_cleanup_archived_collabs_by_ttl(bb_root: Path):
    """L-4:超 TTL 的归档协作文件被清理,index 记录保留。"""
    from teage_liu.multiagent.blackboard import (
        append_collab_message, archive_collab, cleanup_archived_collabs,
        read_collab_index, update_collab_index,
    )
    cid = "c-ttl"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=[]))
    asyncio.run(append_collab_message(bb_root, {"from": "w", "type": "response",
        "content": "x", "collab_id": cid}, collab_id=cid))
    asyncio.run(archive_collab(bb_root, cid))
    # 把 index 中该条目 updated_at 改成 40 天前(模拟归档超 TTL)
    from datetime import datetime, timedelta, timezone
    import yaml as _y
    entries = asyncio.run(read_collab_index(bb_root))
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    for e in entries:
        if e.get("collab_id") == cid:
            e["updated_at"] = old
    index_path = bb_root / "collabs" / "index.md"
    content = ""
    for e in entries:
        content += f"---\n{_y.safe_dump(e, allow_unicode=True, default_flow_style=False, sort_keys=False)}---\n\n"
    index_path.write_text(content, encoding="utf-8")
    # 清理
    removed = asyncio.run(cleanup_archived_collabs(bb_root, ttl_days=30))
    assert cid in removed
    # 文件已删
    assert not (bb_root / "collabs" / f"{cid}.md").exists()
    # index 记录仍保留(便于审计)
    entries2 = asyncio.run(read_collab_index(bb_root))
    assert any(e.get("collab_id") == cid for e in entries2)


def test_cleanup_archived_collabs_skips_recent(bb_root: Path):
    """L-4:未超 TTL 的归档协作文件不被清理。"""
    from teage_liu.multiagent.blackboard import (
        append_collab_message, archive_collab, cleanup_archived_collabs,
        update_collab_index,
    )
    cid = "c-recent"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=[]))
    asyncio.run(append_collab_message(bb_root, {"from": "w", "type": "response",
        "content": "x", "collab_id": cid}, collab_id=cid))
    asyncio.run(archive_collab(bb_root, cid))
    # 刚归档(未超 TTL)
    removed = asyncio.run(cleanup_archived_collabs(bb_root, ttl_days=30))
    assert cid not in removed
    assert (bb_root / "collabs" / f"{cid}.md").exists()

