"""WorkerStateStore 单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from teage_liu.multiagent.worker_state import WorkerState, WorkerStateStore


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    bb = tmp_path / "blackboard"
    (bb / "agents").mkdir(parents=True)
    return bb


def test_load_returns_default_when_file_missing(bb_root):
    """文件不存在时返回默认空状态。"""
    store = WorkerStateStore(bb_root, "worker_001")
    state = store.load()
    assert state.last_collab_seq == 0
    assert state.responded_request_seqs == set()
    assert state.processed_msg_seqs == set()
    assert state.processed_urgent_seqs == set()
    assert state.executed_op_ids == set()


def test_save_and_load_roundtrip(bb_root):
    """保存后加载应保持数据一致。"""
    store = WorkerStateStore(bb_root, "worker_001")
    state = WorkerState(
        last_collab_seq=42,
        responded_request_seqs={1, 5, 10},
        processed_msg_seqs={1, 2, 3},
        processed_urgent_seqs={7},
        executed_op_ids={"op_abc", "op_def"},
    )
    store.save(state)

    loaded = store.load()
    assert loaded.last_collab_seq == 42
    assert loaded.responded_request_seqs == {1, 5, 10}
    assert loaded.processed_msg_seqs == {1, 2, 3}
    assert loaded.processed_urgent_seqs == {7}
    assert loaded.executed_op_ids == {"op_abc", "op_def"}


def test_load_returns_default_when_file_corrupt(bb_root, caplog):
    """文件损坏时返回默认空状态并记录 warning。"""
    state_path = bb_root / "agents" / "worker_001.state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    store = WorkerStateStore(bb_root, "worker_001")
    with caplog.at_level("WARNING"):
        state = store.load()
    assert state.last_collab_seq == 0
    assert any("state.json" in rec.message for rec in caplog.records)


def test_update_merges_fields(bb_root):
    """update 方法读取 → 合并 → 写入，事务性。"""
    store = WorkerStateStore(bb_root, "worker_001")
    store.update({
        "last_collab_seq": 10,
        "responded_request_seqs": {1, 2},
    })
    store.update({
        "last_collab_seq": 15,
        "executed_op_ids": {"op_x"},
    })

    state = store.load()
    assert state.last_collab_seq == 15
    assert state.responded_request_seqs == {1, 2}
    assert state.executed_op_ids == {"op_x"}


def test_state_file_path_is_agent_specific(bb_root):
    """不同 agent_id 的状态文件路径不同。"""
    store_a = WorkerStateStore(bb_root, "agent_A")
    store_b = WorkerStateStore(bb_root, "agent_B")
    store_a.save(WorkerState(last_collab_seq=100))
    store_b.save(WorkerState(last_collab_seq=200))

    assert store_a.load().last_collab_seq == 100
    assert store_b.load().last_collab_seq == 200
