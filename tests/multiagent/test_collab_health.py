"""Phase2 CollabHealthMonitor 测试。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from teage_liu.multiagent.blackboard import (
    append_collab_message, archive_collab, read_collab_index, update_collab_index,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_monitor(bb_root: Path, agent_id: str = "w1"):
    from teage_liu.multiagent.collab_health import CollabHealthMonitor
    class _FakeWorker:
        def __init__(self):
            self._bb_root = bb_root
            self._agent_id = agent_id
            self._collab_max_rounds = {}
            self._collab_last_sent_round = {"c1": 1}  # 本 worker 参与 c1
            self._archived_collabs = set()
            self._config = {"collab_health": {
                "enabled": True, "stall_warn_seconds": 90,
                "stall_grace_seconds": 60, "error_round_limit": 3,
                "monitor_scan_seconds": 15,
            }}
    fw = _FakeWorker()
    return CollabHealthMonitor(fw), fw


def test_consecutive_error_rounds_triggers_archive(bb_root: Path):
    """E-1:连续 3 轮 error 直接归档(不走宽限期)。"""
    cid = "c1"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    # 写 3 条不同 round 的 error 消息
    for r in (1, 2, 3):
        asyncio.run(append_collab_message(bb_root, {
            "from": "w2", "type": "response", "content": "err",
            "collab_id": cid, "collab_round": r, "error": True,
            "timestamp": _now_iso(),
        }, collab_id=cid))
    mon, fw = _make_monitor(bb_root)
    archived = asyncio.run(mon._scan_once())
    assert cid in archived
    assert cid in fw._archived_collabs
    # index 已 archived
    entries = asyncio.run(read_collab_index(bb_root))
    assert next(e for e in entries if e["collab_id"] == cid)["status"] == "archived"


def test_stall_warning_then_grace_archives(bb_root: Path, monkeypatch):
    """A-1:停滞 N=90s warning → 再 W=60s 无进展 → 归档。"""
    cid = "c1"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    # 写一条 100s 前的消息(超过 warn 阈值 90s,但未达 warn+grace=150s)
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
    asyncio.run(append_collab_message(bb_root, {
        "from": "w2", "type": "response", "content": "old",
        "collab_id": cid, "collab_round": 1, "timestamp": old_ts,
    }, collab_id=cid))
    mon, fw = _make_monitor(bb_root)
    # 第一次扫描:进入 warning(age=100s > 90s,但 < 150s 不直接归档)
    asyncio.run(mon._scan_once())
    assert cid in mon._warnings
    # 推进模拟时间越过宽限期(monkeypatch now)
    import teage_liu.multiagent.collab_health as ch
    base = datetime.now(timezone.utc)
    future = base.timestamp() + 200
    monkeypatch.setattr(ch.time, "time", lambda: future)
    archived = asyncio.run(mon._scan_once())
    assert cid in archived


def test_multi_worker_archive_idempotent(bb_root: Path, monkeypatch):
    """多 worker 同时检测停滞:仅一个真正归档,其余 no-op。"""
    cid = "c1"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1", "w2"]))
    old_ts = (datetime.now(timezone.utc).isoformat())
    asyncio.run(append_collab_message(bb_root, {
        "from": "w2", "type": "response", "content": "old",
        "collab_id": cid, "collab_round": 1, "timestamp": old_ts,
    }, collab_id=cid))
    mon1, fw1 = _make_monitor(bb_root)
    mon2, fw2 = _make_monitor(bb_root)
    fw2._agent_id = "w2"
    fw2._collab_last_sent_round = {"c1": 1}
    import teage_liu.multiagent.collab_health as ch
    future = datetime.now(timezone.utc).timestamp() + 200
    monkeypatch.setattr(ch.time, "time", lambda: future)
    a1 = asyncio.run(mon1._scan_once())
    a2 = asyncio.run(mon2._scan_once())
    assert cid in a1
    # 第二个 no-op(已归档)
    assert cid not in a2
