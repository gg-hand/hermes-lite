"""blackboard.py 测试：原子写入 / 路径沙箱 / YAML safe_load。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from teage_liu.multiagent.blackboard import (
    append_jsonl,
    atomic_write,
    read_json,
    read_yaml_frontmatter,
    validate_path_safety,
)
from teage_liu.multiagent.exceptions import PathSafetyError


def _can_create_symlink(tmp_path: Path) -> bool:
    """检测当前环境是否可以创建 symlink（Windows 可能需要管理员权限）。"""
    try:
        link = tmp_path / "_symlink_probe"
        target = tmp_path / "_symlink_target"
        target.write_text("probe", encoding="utf-8")
        link.symlink_to(target)
        link.unlink()
        target.unlink()
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.mark.asyncio
async def test_atomic_write_creates_file(bb_root: Path):
    target = bb_root / "status.json"
    await atomic_write(target, '{"version": 1}')
    assert target.read_text(encoding="utf-8") == '{"version": 1}'


@pytest.mark.asyncio
async def test_atomic_write_overwrites_existing(bb_root: Path):
    target = bb_root / "status.json"
    target.write_text('{"old": true}', encoding="utf-8")
    await atomic_write(target, '{"new": true}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


@pytest.mark.asyncio
async def test_atomic_write_no_tmp_residue(bb_root: Path):
    """原子写入后不应残留 .tmp 文件。"""
    target = bb_root / "status.json"
    await atomic_write(target, '{"v": 1}')
    assert not (bb_root / "status.json.tmp").exists()


def test_validate_path_safety_absolute_rejected(bb_root: Path):
    """绝对路径应被拒绝。"""
    with pytest.raises(PathSafetyError, match="absolute"):
        validate_path_safety(bb_root, Path("/etc/passwd"))


def test_validate_path_safety_traversal_rejected(bb_root: Path):
    """.. 穿越应被拒绝。"""
    with pytest.raises(PathSafetyError, match="traversal"):
        validate_path_safety(bb_root, bb_root / ".." / ".." / "etc" / "passwd")


def test_validate_path_safety_symlink_escape_rejected(bb_root: Path, tmp_path: Path):
    """symlink 逃逸应被拒绝。"""
    if not _can_create_symlink(tmp_path):
        pytest.skip("symlink creation not available on this platform (Windows admin required)")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = bb_root / "escape_link"
    link.symlink_to(outside)
    with pytest.raises(PathSafetyError, match="symlink"):
        validate_path_safety(bb_root, link)


def test_validate_path_safety_symlink_parent_rejected(bb_root: Path, tmp_path: Path):
    """父目录 symlink 应被拒绝。"""
    if not _can_create_symlink(tmp_path):
        pytest.skip("symlink creation not available on this platform (Windows admin required)")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
    # 在 bb_root 外创建 symlink 指向 outside_dir，然后通过 bb_root/symlink_dir/secret.txt 访问
    symlink_dir = bb_root / "symlink_dir"
    symlink_dir.symlink_to(outside_dir)
    target = symlink_dir / "secret.txt"
    with pytest.raises(PathSafetyError, match="symlink"):
        validate_path_safety(bb_root, target)


def test_validate_path_safety_relative_path_ok(bb_root: Path):
    """相对路径（在 bb_root 内）应通过。"""
    target = bb_root / "agents" / "agent_a.md"
    result = validate_path_safety(bb_root, target)
    assert result == target.resolve()


def test_read_json_parses_valid(bb_root: Path):
    target = bb_root / "status.json"
    target.write_text('{"version": 42}', encoding="utf-8")
    assert read_json(target) == {"version": 42}


def test_read_yaml_frontmatter_parses(bb_root: Path):
    target = bb_root / "agents" / "agent_a.md"
    target.write_text(
        "---\nagent_id: agent_a\nstatus: active\n---\n\n# Agent A\n简介\n",
        encoding="utf-8",
    )
    frontmatter, body = read_yaml_frontmatter(target)
    assert frontmatter == {"agent_id": "agent_a", "status": "active"}
    assert "# Agent A" in body


def test_read_yaml_frontmatter_no_frontmatter(bb_root: Path):
    target = bb_root / "agents" / "plain.md"
    target.write_text("just body", encoding="utf-8")
    frontmatter, body = read_yaml_frontmatter(target)
    assert frontmatter == {}
    assert body == "just body"


@pytest.mark.asyncio
async def test_append_jsonl_appends_line(bb_root: Path):
    audit_path = bb_root / "audit" / "audit.jsonl"
    await append_jsonl(audit_path, {"seq": 1, "action": "write"})
    await append_jsonl(audit_path, {"seq": 2, "action": "read"})
    lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2


@pytest.mark.asyncio
async def test_append_jsonl_no_tmp_residue(bb_root: Path):
    audit_path = bb_root / "audit" / "audit.jsonl"
    await append_jsonl(audit_path, {"seq": 1})
    assert not (bb_root / "audit" / "audit.jsonl.tmp").exists()
    assert not (bb_root / "audit" / "audit.jsonl.append").exists()


# =============================================================================
# Task 0: append_message 自动分配 seq + 可选 schema 验证
# =============================================================================


@pytest.mark.asyncio
async def test_append_message_auto_assigns_seq(bb_root):
    """append_message 在 message 缺失 seq 时自动分配（last_seq + 1）。"""
    from teage_liu.multiagent.blackboard import append_message, read_messages

    await append_message(bb_root, {
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task",
        "content": "第一条任务",
        "task_op_id": "task-001",
    })

    await append_message(bb_root, {
        "from": "director_001",
        "to": "worker_001",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "assign",
        "content": "分派任务",
        "reply_to": 1,
        "task_op_id": "task-001",
    })

    messages = await read_messages(bb_root)
    assert len(messages) == 2
    assert messages[0]["seq"] == 1
    assert messages[1]["seq"] == 2
    assert messages[1]["reply_to"] == 1


@pytest.mark.asyncio
async def test_append_message_preserves_explicit_seq(bb_root):
    """append_message 在 message 已有 seq 时保留原值。"""
    from teage_liu.multiagent.blackboard import append_message, read_messages

    await append_message(bb_root, {
        "seq": 100,
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task",
        "content": "显式 seq",
    })

    messages = await read_messages(bb_root)
    assert messages[0]["seq"] == 100


@pytest.mark.asyncio
async def test_append_message_with_validate_passes_compliant(bb_root):
    """validate=True 时合规消息通过验证。"""
    from teage_liu.multiagent.blackboard import append_message, read_messages

    await append_message(bb_root, {
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task",
        "content": "合规消息",
    }, validate=True)

    messages = await read_messages(bb_root)
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_append_message_with_validate_rejects_invalid(bb_root):
    """validate=True 时非法 type 被拒绝。"""
    from teage_liu.multiagent.blackboard import append_message
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        await append_message(bb_root, {
            "from": "user_dispatch",
            "to": "*",
            "timestamp": "2026-07-23T10:00:00+00:00",
            "type": "invalid_type",
            "content": "闈炴硶娑堟伅",
        }, validate=True)


# ---------------- Task 3: read_json 异常保护 ----------------


def test_read_json_empty_file_returns_empty_dict(tmp_path: Path):
    """read_json 读到空文件时返回空 dict，不抛异常。"""
    empty_file = tmp_path / "empty.json"
    empty_file.write_text("", encoding="utf-8")
    result = read_json(empty_file)
    assert result == {}, f"空文件应返回空 dict，实际: {result}"


def test_read_json_corrupt_file_returns_empty_dict(tmp_path: Path):
    """read_json 读到损坏 JSON 时返回空 dict，不抛异常。"""
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{broken", encoding="utf-8")
    result = read_json(corrupt_file)
    assert result == {}


def test_read_json_missing_file_returns_empty_dict(tmp_path: Path):
    """read_json 文件不存在时返回空 dict，不抛异常。"""
    missing_file = tmp_path / "missing.json"
    result = read_json(missing_file)
    assert result == {}


def test_read_json_valid_file_returns_content(tmp_path: Path):
    """read_json 正常文件返回解析后的 dict。"""
    valid_file = tmp_path / "valid.json"
    valid_file.write_text('{"key": "value", "num": 42}', encoding="utf-8")
    result = read_json(valid_file)
    assert result == {"key": "value", "num": 42}


@pytest.mark.asyncio
async def test_append_collab_message_dedups_repeated_consensus(bb_root: Path):
    """P3-2：同 collab_id 下已有 consensus 终止信号时，新的 consensus 写入被熔断丢弃。

    场景：双方达成共识后互发 consensus 导致风暴。_find_terminator 检测到已有
    consensus/end 后，第 2 条及以后的 consensus 返回旧 seq + deduplicated=True，
    不新增消息。
    """
    from teage_liu.multiagent.blackboard import (
        append_collab_message, read_collab_messages,
    )

    collab_id = "collab_p32_dedup"
    # 第 1 条 consensus：正常写入
    msg1 = {
        "from": "worker_001", "to": "*",
        "type": "consensus", "content": "达成共识：火锅",
        "collab_id": collab_id,
    }
    seq1, dedup1 = await append_collab_message(bb_root, msg1, collab_id=collab_id)
    assert dedup1 is False
    assert seq1 == 1

    # 第 2 条 consensus（对端也发）：应被熔断，返回旧 seq，deduplicated=True
    msg2 = {
        "from": "worker_002", "to": "*",
        "type": "consensus", "content": "我也确认火锅",
        "collab_id": collab_id,
    }
    seq2, dedup2 = await append_collab_message(bb_root, msg2, collab_id=collab_id)
    assert dedup2 is True, "重复 consensus 应被熔断"
    assert seq2 == seq1, "熔断时返回已存在的终止信号 seq"

    # 文件中应只有 1 条 consensus
    msgs = await read_collab_messages(bb_root, collab_id=collab_id)
    consensus_msgs = [m for m in msgs if m.get("type") == "consensus"]
    assert len(consensus_msgs) == 1, "重复 consensus 应被熔断，仅保留首条"


@pytest.mark.asyncio
async def test_append_collab_message_allows_first_consensus_after_responses(bb_root: Path):
    """P3-2：response 消息不受 consensus 熔断影响，首条 consensus 正常写入。"""
    from teage_liu.multiagent.blackboard import append_collab_message

    collab_id = "collab_p32_first"
    # 先写 response（不触发 consensus 熔断）
    resp = {
        "from": "worker_001", "to": "*", "type": "response",
        "content": "我提议火锅", "collab_id": collab_id,
    }
    seq_r, dedup_r = await append_collab_message(bb_root, resp, collab_id=collab_id)
    assert dedup_r is False
    # 首条 consensus 正常写入（_find_terminator 无先验终止信号）
    cons = {
        "from": "worker_002", "to": "*", "type": "consensus",
        "content": "同意火锅", "collab_id": collab_id,
    }
    seq_c, dedup_c = await append_collab_message(bb_root, cons, collab_id=collab_id)
    assert dedup_c is False
    assert seq_c == 2


@pytest.mark.asyncio
async def test_consensus_dedup_blocks_response_after_terminator(bb_root: Path):
    """P3-2 扩展：consensus 终止信号后，response 也被熔断丢弃。

    原仅拦截 consensus 类型，但 LLM 常在文本说"终止协作"却不调工具，
    fallback 代写为 response，导致 consensus 后继续多轮 response 循环
    （协作 0b11ed517e1f seq15-18 根因）。
    """
    from teage_liu.multiagent.blackboard import (
        append_collab_message, read_collab_messages,
    )

    collab_id = "collab_consensus_then_response"
    # 先写 consensus（终止信号）
    cons = {
        "from": "teagent-lu", "to": "*", "type": "consensus",
        "content": "共识达成：1.火锅 2.今晚", "collab_id": collab_id,
    }
    seq_c, dedup_c = await append_collab_message(bb_root, cons, collab_id=collab_id)
    assert dedup_c is False
    # 后续 response 应被熔断（不再允许 consensus 后继续协作）
    resp = {
        "from": "teagent-liu-2", "to": "*", "type": "response",
        "content": "我再确认一下", "collab_id": collab_id,
    }
    seq_r, dedup_r = await append_collab_message(bb_root, resp, collab_id=collab_id)
    assert dedup_r is True, "consensus 后的 response 应被熔断"
    # 验证只有 1 条消息
    msgs = await read_collab_messages(bb_root, collab_id=collab_id)
    assert len(msgs) == 1, "consensus 后 response 被熔断，仅保留 consensus"


# ========== P3-3: 同 round 同 from 写入层闸门（防止同 round 连发） ==========

@pytest.mark.asyncio
async def test_append_blocks_same_round_same_from_burst(bb_root: Path):
    """P3-3：同一 worker 在同一 collab_round 内连发 response，第 2 条被写入层闸门丢弃。

    验收失败场景：teagent-lu round1 连发 3 条 response。工具层 _collab_round_sent
    是 per-LLM-call 重置，无法跨 LLM 调用去重，故在写入层兜底：若该 collab 已有
    (from==本worker 且 collab_round==本次round 且 type in response/consensus)，
    丢弃新写入，返回旧 seq + deduplicated=True。
    """
    from teage_liu.multiagent.blackboard import (
        append_collab_message, read_collab_messages,
    )

    collab_id = "collab_p33_burst"
    # 第 1 条 response round=1：正常写入
    msg1 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "今晚吃火锅",
        "collab_id": collab_id, "collab_round": 1,
    }
    seq1, dedup1 = await append_collab_message(bb_root, msg1, collab_id=collab_id)
    assert dedup1 is False
    assert seq1 == 1

    # 第 2 条 response 同 from 同 round：应被闸门丢弃
    msg2 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "补充：再加一份毛肚",
        "collab_id": collab_id, "collab_round": 1,
    }
    seq2, dedup2 = await append_collab_message(bb_root, msg2, collab_id=collab_id)
    assert dedup2 is True, "同 round 同 from 连发应被写入层闸门丢弃"
    assert seq2 == seq1, "闸门丢弃时返回已存在的 seq"

    # 第 3 条同 from 同 round：仍被丢弃
    msg3 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "再补充：鸭血",
        "collab_id": collab_id, "collab_round": 1,
    }
    seq3, dedup3 = await append_collab_message(bb_root, msg3, collab_id=collab_id)
    assert dedup3 is True
    assert seq3 == seq1

    # 文件中 teagent-lu round1 response 应只有 1 条
    msgs = await read_collab_messages(bb_root, collab_id=collab_id)
    lu_r1 = [m for m in msgs
             if m.get("from") == "teagent-lu"
             and m.get("collab_round") == 1
             and m.get("type") == "response"]
    assert len(lu_r1) == 1, "同 round 同 from 连发应仅保留首条"


@pytest.mark.asyncio
async def test_append_allows_different_workers_same_round(bb_root: Path):
    """P3-3：同 round 不同 from 的 response 互不阻断（一来一回正常进行）。"""
    from teage_liu.multiagent.blackboard import append_collab_message

    collab_id = "collab_p33_diff_workers"
    # teagent-lu round=1 response
    msg1 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "我提议火锅",
        "collab_id": collab_id, "collab_round": 1,
    }
    seq1, dedup1 = await append_collab_message(bb_root, msg1, collab_id=collab_id)
    assert dedup1 is False
    # teagent-liu-2 同 round=1 response（不同 from）应正常写入
    msg2 = {
        "from": "teagent-liu-2", "to": "*",
        "type": "response", "content": "同意火锅",
        "collab_id": collab_id, "collab_round": 1,
    }
    seq2, dedup2 = await append_collab_message(bb_root, msg2, collab_id=collab_id)
    assert dedup2 is False, "不同 from 的同 round response 不应被闸门阻断"
    assert seq2 == 2


@pytest.mark.asyncio
async def test_append_allows_same_worker_different_rounds(bb_root: Path):
    """P3-3：同 from 不同 round 的 response 正常写入（round 推进后可再发）。"""
    from teage_liu.multiagent.blackboard import append_collab_message

    collab_id = "collab_p33_diff_rounds"
    # round=1
    msg1 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "round1 提议",
        "collab_id": collab_id, "collab_round": 1,
    }
    seq1, dedup1 = await append_collab_message(bb_root, msg1, collab_id=collab_id)
    assert dedup1 is False
    # 同 from round=2（新回合）应正常写入
    msg2 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "round2 补充",
        "collab_id": collab_id, "collab_round": 2,
    }
    seq2, dedup2 = await append_collab_message(bb_root, msg2, collab_id=collab_id)
    assert dedup2 is False, "同 from 不同 round 的 response 不应被闸门阻断"
    assert seq2 == 2


@pytest.mark.asyncio
async def test_append_blocks_same_round_consensus_after_response(bb_root: Path):
    """P3-3：同 from 同 round 已发 response 后，consensus 也被闸门丢弃（连发兜底）。"""
    from teage_liu.multiagent.blackboard import append_collab_message

    collab_id = "collab_p33_resp_then_cons"
    resp = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "我提议火锅",
        "collab_id": collab_id, "collab_round": 1,
    }
    await append_collab_message(bb_root, resp, collab_id=collab_id)
    # 同 from 同 round 的 consensus（连发）应被 P3-3 闸门丢弃
    cons = {
        "from": "teagent-lu", "to": "*",
        "type": "consensus", "content": "达成共识：火锅",
        "collab_id": collab_id, "collab_round": 1,
    }
    seq_c, dedup_c = await append_collab_message(bb_root, cons, collab_id=collab_id)
    assert dedup_c is True, "同 round 同 from 已发 response 后，consensus 应被连发闸门丢弃"
    assert seq_c == 1


@pytest.mark.asyncio
async def test_append_no_round_field_not_blocked(bb_root: Path):
    """P3-3：无 collab_round 字段的消息（如 request/旧消息）不受闸门影响。"""
    from teage_liu.multiagent.blackboard import append_collab_message

    collab_id = "collab_p33_no_round"
    # 无 collab_round 的 response 不应被闸门阻断
    msg1 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "无 round 字段",
        "collab_id": collab_id,
    }
    seq1, dedup1 = await append_collab_message(bb_root, msg1, collab_id=collab_id)
    assert dedup1 is False
    msg2 = {
        "from": "teagent-lu", "to": "*",
        "type": "response", "content": "仍无 round 字段",
        "collab_id": collab_id,
    }
    seq2, dedup2 = await append_collab_message(bb_root, msg2, collab_id=collab_id)
    assert dedup2 is False, "无 collab_round 字段时闸门不应触发"
    assert seq2 == 2


@pytest.mark.asyncio
async def test_append_same_round_gate_disabled_when_flag_off(bb_root: Path):
    """P3-3：关闭开关后连发不再被阻断（回滚开关可用）。"""
    from teage_liu.multiagent.blackboard import (
        append_collab_message, set_collab_same_round_dedup_enabled,
    )

    collab_id = "collab_p33_disabled"
    set_collab_same_round_dedup_enabled(False)
    try:
        msg1 = {
            "from": "teagent-lu", "to": "*",
            "type": "response", "content": "第一条",
            "collab_id": collab_id, "collab_round": 1,
        }
        seq1, dedup1 = await append_collab_message(bb_root, msg1, collab_id=collab_id)
        assert dedup1 is False
        msg2 = {
            "from": "teagent-lu", "to": "*",
            "type": "response", "content": "第二条连发",
            "collab_id": collab_id, "collab_round": 1,
        }
        seq2, dedup2 = await append_collab_message(bb_root, msg2, collab_id=collab_id)
        assert dedup2 is False, "关闭 P3-3 开关后连发应正常写入"
        assert seq2 == 2
    finally:
        set_collab_same_round_dedup_enabled(True)

