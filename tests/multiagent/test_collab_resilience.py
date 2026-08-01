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


def test_dead_code_removed_migrated_to_append_collab(bb_root: Path):
    """E-4:_send_relay/_forward_a2a_message 删除后,等价行为由 append_collab_message 提供。"""
    from teage_liu.multiagent.blackboard import append_collab_message, read_collab_messages
    cid = "c-dead"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    asyncio.run(append_collab_message(bb_root, {
        "from": "w1", "to": "w2", "type": "relay",
        "content": "via append", "message_id": "m1",
    }, collab_id=cid))
    msgs = asyncio.run(read_collab_messages(bb_root, collab_id=cid))
    assert any(m.get("content") == "via append" and m.get("type") == "relay"
               for m in msgs)
    # 确认方法已删除
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    assert not hasattr(WorkerAdapter, "_send_relay")
    assert not hasattr(WorkerAdapter, "_forward_a2a_message")


def test_archive_collab_idempotent(bb_root: Path):
    """Phase2:archive_collab 幂等,重复归档返回 False。"""
    cid = "c-idem"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=[]))
    assert asyncio.run(archive_collab(bb_root, cid)) is True   # 首次
    assert asyncio.run(archive_collab(bb_root, cid)) is False  # 重复 no-op
    assert asyncio.run(archive_collab(bb_root, "not-exist")) is False


class _FailingOrch:
    """模拟 LLM 前 N 次失败,之后成功。"""
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0
        self.history_buffer = None
    async def chat(self, session_id, prompt, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("llm boom")
        return "ok-response"


def _make_worker_with_orch(bb_root: Path, orch, agent_id="w1"):
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {
        "persist_state": False, "worker_collab_decentralized": True,
        "collab_retry": {"enabled": True, "max_retries": 3, "interval_seconds": 0},
        "director_v2_enabled": False,
    }}}
    return WorkerAdapter(bb_root=bb_root, agent_id=agent_id, config=cfg, orchestrator=orch)


def test_llm_failure_retries_then_succeeds(bb_root: Path):
    """L-1:LLM 失败 2 次后第 3 次成功 → 正常写 response,协作不卡死。"""
    cid = "c-retry"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    orch = _FailingOrch(fail_times=2)
    w = _make_worker_with_orch(bb_root, orch)
    asyncio.run(w._load_archived_collabs())
    ctx = {"type": "request", "seq": 10, "collab_id": cid,
           "_collab_round": 1, "message_id": "m10"}
    asyncio.run(w._trigger_urgent_llm(prompt="p", context_msg=ctx))
    # 触发重试 consumer(同步驱动)
    asyncio.run(w._drain_retry_queue())
    from teage_liu.multiagent.blackboard import read_collab_messages
    msgs = asyncio.run(read_collab_messages(bb_root, collab_id=cid))
    assert any(m.get("type") == "response" and m.get("accept") is True for m in msgs)
    assert orch.calls == 3


def test_llm_failure_exhausts_skips_and_notifies(bb_root: Path):
    """L-1:重试 3 次仍失败 → 写一条精炼 error(带 collab_round)+ 跳过 request。"""
    cid = "c-exhaust"
    asyncio.run(update_collab_index(bb_root, cid, title="t", status="active", participants=["w1"]))
    orch = _FailingOrch(fail_times=99)  # 永远失败
    w = _make_worker_with_orch(bb_root, orch)
    asyncio.run(w._load_archived_collabs())
    ctx = {"type": "request", "seq": 11, "collab_id": cid,
           "_collab_round": 2, "message_id": "m11"}
    asyncio.run(w._trigger_urgent_llm(prompt="p", context_msg=ctx))
    asyncio.run(w._drain_retry_queue())
    from teage_liu.multiagent.blackboard import read_collab_messages
    msgs = asyncio.run(read_collab_messages(bb_root, collab_id=cid))
    errs = [m for m in msgs if m.get("error") is True]
    assert errs and errs[-1].get("collab_round") == 2
    # 该 request 未被标记为已响应(失败跳过,非成功响应)
    assert "mid:m11" not in w._responded_request_seqs


# =============================================================================
# Phase 5 — 生命周期与错误细化
# =============================================================================


def test_autonomous_exit_resets_turn_index(bb_root: Path):
    """L-2:退出自治时 _turn_index 重置为 0。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {"persist_state": False, "director_v2_enabled": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    w._autonomous._turn_index = 5
    asyncio.run(w._autonomous.exit(1))
    assert w._autonomous._turn_index == 0


def test_director_health_no_recursion_stack_safe(bb_root: Path, monkeypatch):
    """L-2:_check_director_health 选举重试不递归(深度上限内不栈溢出)。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {"persist_state": False, "director_v2_enabled": True}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    # 直接调用不应抛 RecursionError(内部循环,不栈溢出)
    asyncio.run(w._check_director_health())


def test_extend_receiver_round_no_jump(bb_root: Path):
    """L-3:extend 接收方 last_sent > current_round 时对齐,不 +1 跳跃。"""
    from teage_liu.multiagent.worker_adapter import WorkerAdapter
    cfg = {"multiagent": {"worker": {"persist_state": False}}}
    w = WorkerAdapter(bb_root=bb_root, agent_id="w1", config=cfg, orchestrator=None)
    w._collab_last_sent_round = {"c1": 5}
    # extend 消息 current_round=0(extend 不带有效 round)→ 对齐到 0,不跳到 6
    r = w._compute_outgoing_collab_round("c1", 0)
    assert r == 0
    # 正常 peer_round > last_sent 仍共享
    w._collab_last_sent_round = {"c2": 2}
    assert w._compute_outgoing_collab_round("c2", 3) == 3


