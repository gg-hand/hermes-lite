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


# ---------------------------------------------------------------------------
# 体积增量记账(2026-09-10 性能完善):预算检查不得每次全量序列化快照
# ---------------------------------------------------------------------------
def test_incremental_size_matches_ground_truth():
    """验收:增量记账值必须逐点等于 len(json.dumps(to_dict()))(记账正确性唯一锚)。

    覆盖:纯追加(apply_action_batch)、**revision 进位**(9→10 的整数位数变化)、
    with_round 的位数变化、base 失效后的重算、以及**截尾**路径(loop 终止清理会走)。
    """
    from teage_liu2.core.snapshot import snapshot_bytes

    cur = _base()
    assert snapshot_bytes(cur) == _size(cur)
    for content in ("a" * 10, "b" * 1000, "c" * 50_000):
        cur, bad = _append(cur, content)
        assert not bad
        assert snapshot_bytes(cur) == _size(cur)

    # revision 进位:apply_action_batch 会把 revision +1,而 revision 在 to_dict 中
    # 是整数 —— 9→10 会让 JSON 长度 +1,记账必须同步(否则偏差 1 字节)。
    cur = dataclasses.replace(cur, revision=8, _size_cache=None)
    cur, _ = _append(cur, "y")            # revision 9,缓存以 revision=9 建立
    assert cur.revision == 9
    cur, _ = _append(cur, "z")            # revision 10 → 进位必须被记账
    assert cur.revision == 10
    assert snapshot_bytes(cur) == _size(cur)

    cur = cur.with_round(9)
    assert snapshot_bytes(cur) == _size(cur)
    cur = cur.with_round(1234)          # 位数变化 → base 分量必须精确跟随
    assert snapshot_bytes(cur) == _size(cur)
    cur = cur.with_stop(True, "拦截")   # base 失效 → 精确重算
    assert snapshot_bytes(cur) == _size(cur)
    cur = cur.with_extra({"a.b": "c"})
    assert snapshot_bytes(cur) == _size(cur)
    cur = cur.with_messages(cur.messages[:-1])   # 截尾 → 缓存失效并精确重算
    assert snapshot_bytes(cur) == _size(cur)
    cur = cur.with_messages(cur.messages + [{"role": "user", "content": "尾"}] * 3)
    assert snapshot_bytes(cur) == _size(cur)


def test_budget_check_does_not_serialize_whole_snapshot(monkeypatch):
    """验收:缓存建立后,稳态批次不再对整快照序列化(99% 开销来源)。

    锚定口径说明:全量序列化入口是 ``snapshot._json_len(整快照 dict)`` ——
    ``_json_len`` 定义在 types 模块但被 ``snapshot`` 以模块级名字引用,故 patch
    ``snapshot._json_len`` 即可精确拦截"整快照序列化";首次预算检查允许一次
    base 精确计算(缓存为空),之后必须为零。
    """
    from teage_liu2.core import snapshot as snap_mod

    calls = {"n": 0}
    real_len = snap_mod._json_len

    def counting_len(obj):
        if isinstance(obj, dict) and "messages" in obj:
            calls["n"] += 1
        return real_len(obj)

    monkeypatch.setattr(snap_mod, "_json_len", counting_len)
    cur = _base()
    cur, _ = _append(cur, "x" * 100)     # 首次:允许一次 base 精确计算
    calls["n"] = 0
    for _ in range(3):
        cur, _ = _append(cur, "x" * 100)
    assert calls["n"] == 0, "缓存建立后,追加批次不得整快照序列化"
    assert snap_mod.snapshot_bytes(cur) == _size(cur)
