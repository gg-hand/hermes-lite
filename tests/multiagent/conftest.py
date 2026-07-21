"""multiagent 测试公共 fixtures。"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录骨架。"""
    bb = tmp_path / "blackboard"
    bb.mkdir()
    (bb / "agents").mkdir()
    (bb / "tasks").mkdir()
    (bb / "audit").mkdir()
    (bb / "locks").mkdir()
    (bb / "schemas").mkdir()
    (bb / "snapshots").mkdir()
    # 初始化空文件
    (bb / "messages.md").write_text("", encoding="utf-8")
    (bb / "messages.pending.md").write_text("", encoding="utf-8")
    (bb / "messages.replay_candidates.md").write_text("", encoding="utf-8")
    (bb / "audit" / "audit.jsonl").write_text("", encoding="utf-8")
    return bb


@pytest.fixture
def sample_status_json() -> dict:
    """status.json 初始样本。"""
    return {
        "protocol_version": "1.0.0",
        "session_id": "test_session",
        "phase": "initializing",
        "version": 0,
        "epoch": 1,
        "compat_mode": None,
        "current_turn": {"agent_id": "", "started_at": "", "deadline_at": "", "epoch": 1},
        "turn_history": [],
        "active_agents": [],
        "locks": {},
        "last_message_seq": 0,
        "last_heartbeat": {},
        "director_status": "active",
        "director_signature": "",
        "last_fencing_token": 0,
        "recovery_started_at": None,
        "recovery_progress": None,
        "recovery_stage": None,
        "extensions": {},
    }
