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
            "content": "非法消息",
        }, validate=True)

