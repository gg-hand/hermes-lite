"""types 域 T-8 资源上限契约（2026-09-10 深度审计 F-5 修复）。

三项上限必须一律"拒绝"：快照体积超限此前仅记 error 日志却照常推进，
与消息条数 / 单条体积两项语义不一致；本文件锁定修复后的拒绝语义。
"""
from __future__ import annotations

import dataclasses
import json

from teage_liu2.core.actions import make_action
from teage_liu2.core.snapshot import apply_action_batch
from teage_liu2.core.types import RESOURCE_LIMITS, Snapshot


def _base() -> Snapshot:
    return Snapshot(
        session_id="s-limits",
        user_input="u",
        round=0,
        started_at="2026-09-10T00:00:00",
        history=[],
        system_text="",
        messages=[],
        tools=[],
        extra={},
        revision=0,
    )


def _append(cur: Snapshot, content: str):
    return apply_action_batch(
        cur, [make_action("AppendMessage", message={"role": "user", "content": content})]
    )


def test_append_over_message_bytes_is_rejected():
    """单条消息体积超限 → 该 action 被拒绝(不进入快照)。"""
    too_big = "x" * (RESOURCE_LIMITS["max_message_bytes"] + 1024)
    cur, bad = _append(_base(), too_big)
    assert bad, "单条消息体积超限必须被拒绝"
    assert cur.messages == [], "被拒绝的 action 不得进入快照"


def test_message_count_limit_is_rejected():
    """消息条数上限 → 拒绝(超限 action 被跳过,快照规模不变)。"""
    limit = RESOURCE_LIMITS["max_messages_per_conversation"]
    full = dataclasses.replace(
        _base(), messages=[{"role": "user", "content": "x"}] * limit
    )
    cur, bad = _append(full, "y")
    assert bad, "消息条数超上限必须被拒绝"
    assert len(cur.messages) == limit


def test_snapshot_over_budget_is_rejected_not_applied():
    """快照总体积超限 → append 类被拒绝,快照体积不越过上限。"""
    big = "x" * (RESOURCE_LIMITS["max_message_bytes"] - 4096)
    cur = _base()
    rejected = False
    for _ in range(10):
        cur, bad = _append(cur, big)
        if bad:
            rejected = True
            break
    assert rejected, "快照体积超限必须被拒绝(§types T-8)"
    size = len(json.dumps(cur.to_dict(), ensure_ascii=False))
    assert size <= RESOURCE_LIMITS["max_snapshot_bytes"]


#: 单条消息内容尺寸:使"4 条在预算内、5 条越限"(粒度必须卡在这个区间)
MID = 470_000


def _size(snap: Snapshot) -> int:
    return len(json.dumps(snap.to_dict(), ensure_ascii=False))


def _batch(content: str):
    """guardrails 真实批次形态:AppendMessage + SetExtra + SetStop(拦截)。"""
    return [
        make_action("AppendMessage", message={"role": "user", "content": content}),
        make_action("SetExtra", key="guardrails.denied", value=True),
        make_action("SetStop", reason="拦截"),
    ]


def test_set_stop_survives_when_batch_append_exceeds_budget():
    """同批 [AppendMessage 超限, SetExtra, SetStop] → append 被拒,拦截必须保留。

    回归防护(2026-09-10 评审 FIX-1):整批回滚会把 SetStop 一起丢掉 →
    安全拦截静默失效。
    """
    base = dataclasses.replace(
        _base(), messages=[{"role": "user", "content": "x" * MID}] * 4
    )  # 入参仍在预算内;批次追加一条即越限
    assert _size(base) <= RESOURCE_LIMITS["max_snapshot_bytes"], "用例前置:入参须在预算内"

    cur, bad = apply_action_batch(base, _batch("x" * MID))
    assert bad, "超限 append 应被拒绝"
    assert cur.stop is True, "SetStop 拦截不得因体积超限而丢失"
    assert cur.extra.get("guardrails.denied") is True, "SetExtra 应保留"
    assert len(cur.messages) == len(base.messages), "被拒绝的 append 不得进入快照"


def test_over_budget_input_refuses_appends_but_keeps_set_actions():
    """入参已超限 → 仍然只拒绝 append、保留 set 类(拦截不得失效,消息不得再增长)。

    该用例锁住 FIX-1 的核心不变量:体积拒绝**绝不吞掉安全语义**,
    同时不让已超限的快照继续增长(内存边界)。
    """
    over = dataclasses.replace(
        _base(), messages=[{"role": "user", "content": "x" * MID}] * 5
    )  # 入参本身已越限
    assert _size(over) > RESOURCE_LIMITS["max_snapshot_bytes"], "用例前置:入参须已越限"

    cur, bad = apply_action_batch(over, _batch("y"))
    assert bad, "超限批次应被记录(HOOK_INVALID_ACTION)"
    assert cur.stop is True, "入参超限时拦截仍必须生效"
    assert cur.extra.get("guardrails.denied") is True, "SetExtra 必须保留"
    assert len(cur.messages) == len(over.messages), "已超限快照不得再长(append 被拒绝)"
