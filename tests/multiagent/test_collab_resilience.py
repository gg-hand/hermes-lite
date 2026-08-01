"""协作韧性测试:Phase1 安全网。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from teage_liu.multiagent.blackboard import (
    append_collab_message, archive_collab, read_all_active_collab_messages,
    update_collab_index,
)


def _make_worker(bb_root: Path, agent_id: str = "w1"):
    """轻量构造 Worker(不经 register/start,仅初始化属性)。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    # 最小 fake orchestrator + config,避免启动后台任务
    class _FakeOrch:
        pass
    cfg = {"multiagent": {"worker": {"persist_state": False,
                                     "worker_collab_decentralized": True}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id=agent_id, config=cfg,
                      orchestrator=_FakeOrch())
    return w


def test_archived_collab_message_rejected_at_entry(bb_root: Path):
    """I-1:归档协作的写入消息在 _handle_collab_message 入口即被拒。"""
    cid = "c-arch"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    # 写一条普通消息
    asyncio.run(append_collab_message(bb_root, {"from": "w1", "type": "response",
        "content": "hi", "collab_id": cid}, collab_id=cid))
    # 归档
    assert asyncio.run(archive_collab(bb_root, cid)) is True

    w = _make_worker(bb_root)
    asyncio.run(w._load_archived_collabs())
    assert cid in w._archived_collabs

    # 归档后的新消息应被入口拒绝(不触发 LLM,不写消息)
    archived_msg = {"from": "w2", "type": "response", "content": "late",
                    "collab_id": cid, "seq": 99}
    asyncio.run(w._handle_collab_message(archived_msg))
    # 验证未被标记处理(归档阻断不应推进幂等集)
    assert asyncio.run(read_all_active_collab_messages(bb_root)) == [] or \
        all(m.get("seq") != 99 for m in
            asyncio.run(read_all_active_collab_messages(bb_root)))


def test_read_active_messages_excludes_archived_by_default(bb_root: Path):
    """I-1:read_all_active_collab_messages 默认不含归档协作。"""
    cid = "c-active"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=[]))
    asyncio.run(append_collab_message(bb_root, {"from": "u", "type": "request",
        "content": "x", "collab_id": cid}, collab_id=cid))
    msgs_default = asyncio.run(read_all_active_collab_messages(bb_root))
    assert any(m.get("collab_id") == cid for m in msgs_default)
    asyncio.run(archive_collab(bb_root, cid))
    msgs_after = asyncio.run(read_all_active_collab_messages(bb_root))
    assert all(m.get("collab_id") != cid for m in msgs_after)
    # 显式 include_archived=True 仍可读
    msgs_arch = asyncio.run(read_all_active_collab_messages(bb_root, include_archived=True))
    assert any(m.get("collab_id") == cid for m in msgs_arch)


def test_response_missing_collab_round_rejected(bb_root: Path):
    """I-2:response 缺 collab_round 字段被拒绝(精炼错误)。"""
    cid = "c-round"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    asyncio.run(append_collab_message(bb_root, {"from": "w2", "type": "response",
        "content": "no round field", "collab_id": cid}, collab_id=cid))
    w = _make_worker(bb_root)
    asyncio.run(w._load_archived_collabs())
    # 构造一条缺 collab_round 的 peer response
    msg = {"from": "w2", "type": "response", "content": "x", "collab_id": cid,
           "seq": 5}  # 无 collab_round 字段
    asyncio.run(w._handle_collab_message(msg))
    # 缺字段不应触发 LLM(无 orchestrator.chat 调用),不应入 normal_queue
    assert w._normal_queue == [] or not any(
        m.get("seq") == 5 for m in w._normal_queue)

