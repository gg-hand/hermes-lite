"""audit_logger.py 测试：append 串行化 + hash 链 + 损坏降级。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from teage_liu.multiagent.audit_logger import MultiAgentAuditLogger


@pytest.fixture
def audit_logger(bb_root: Path) -> MultiAgentAuditLogger:
    return MultiAgentAuditLogger(bb_root)


@pytest.mark.asyncio
async def test_audit_append_serialization(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    """并发 append 应串行化，无交错损坏。"""
    records = [
        {"ts": "2026-07-20T10:00:00Z", "actor": "agent_a", "action": "write", "target": "messages.md", "details": {"seq": i}}
        for i in range(10)
    ]
    await asyncio.gather(*[audit_logger.append_audit(r) for r in records])

    lines = (bb_root / "audit" / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 10
    for line in lines:
        rec = json.loads(line)  # 每行必须是有效 JSON
        assert "hash" in rec
        assert "prev_hash" in rec


@pytest.mark.asyncio
async def test_audit_hash_chain(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    """prev_hash + hash 链完整性。"""
    await audit_logger.append_audit({"ts": "t1", "actor": "a", "action": "write", "target": "f", "details": {}})
    await audit_logger.append_audit({"ts": "t2", "actor": "a", "action": "read", "target": "f", "details": {}})

    lines = (bb_root / "audit" / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])

    assert rec1["prev_hash"] == ""  # 第一条 prev_hash 为空
    assert rec2["prev_hash"] == rec1["hash"]  # 第二条 prev_hash = 第一条 hash


@pytest.mark.asyncio
async def test_audit_corrupt_json_skip(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    """损坏 JSON 行跳过 + 写 corrupt.log。"""
    audit_path = bb_root / "audit" / "audit.jsonl"
    # 写入一条正常记录 + 一条损坏记录 + 一条正常记录
    audit_path.write_text(
        '{"ts":"t1","actor":"a","action":"write","target":"f","details":{},"prev_hash":"","hash":"h1"}\n'
        'CORRUPT_LINE_NOT_JSON\n'
        '{"ts":"t2","actor":"a","action":"read","target":"f","details":{},"prev_hash":"h1","hash":"h2"}\n',
        encoding="utf-8",
    )

    records = audit_logger.read_records()
    assert len(records) == 2  # 损坏行被跳过

    # 损坏行应写入 corrupt.log
    corrupt_log = bb_root / "audit" / "audit.jsonl.corrupt"
    assert corrupt_log.exists()
    assert "CORRUPT_LINE_NOT_JSON" in corrupt_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_audit_corrupt_hash_chain(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    """hash 链断裂记录 suspect。"""
    audit_path = bb_root / "audit" / "audit.jsonl"
    # 第一条 hash=h1，第二条 prev_hash=WRONG（不匹配）
    audit_path.write_text(
        '{"ts":"t1","actor":"a","action":"write","target":"f","details":{},"prev_hash":"","hash":"h1"}\n'
        '{"ts":"t2","actor":"a","action":"read","target":"f","details":{},"prev_hash":"WRONG","hash":"h2"}\n',
        encoding="utf-8",
    )

    records = audit_logger.read_records()
    assert len(records) == 2
    # 第二条应标记 suspect
    assert records[1].get("_suspect") is True or records[1].get("details", {}).get("suspect") is True


def test_read_records_filter_action(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    audit_path = bb_root / "audit" / "audit.jsonl"
    audit_path.write_text(
        '{"ts":"t1","actor":"a","action":"write","target":"f","details":{},"prev_hash":"","hash":"h1"}\n'
        '{"ts":"t2","actor":"a","action":"read","target":"f","details":{},"prev_hash":"h1","hash":"h2"}\n'
        '{"ts":"t3","actor":"a","action":"write","target":"g","details":{},"prev_hash":"h2","hash":"h3"}\n',
        encoding="utf-8",
    )
    writes = audit_logger.read_records(filter_action="write")
    assert len(writes) == 2


def test_read_records_limit(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    audit_path = bb_root / "audit" / "audit.jsonl"
    lines = []
    prev = ""
    for i in range(10):
        import hashlib
        rec_str = f'{{"ts":"t{i}","actor":"a","action":"write","target":"f","details":{{}},"prev_hash":"{prev}","hash":"h{i}"}}'
        lines.append(rec_str)
        prev = f"h{i}"
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = audit_logger.read_records(limit=3)
    assert len(records) == 3


@pytest.mark.asyncio
async def test_read_last_hash_empty_file(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    """空 audit.jsonl 的 read_last_hash 应返回空字符串。"""
    assert audit_logger.read_last_hash() == ""


@pytest.mark.asyncio
async def test_read_last_hash_after_append(bb_root: Path, audit_logger: MultiAgentAuditLogger):
    await audit_logger.append_audit({"ts": "t1", "actor": "a", "action": "write", "target": "f", "details": {}})
    last_hash = audit_logger.read_last_hash()
    assert last_hash != ""
