"""轮次管理 + 派生文件 flush 测试（Task 4）。

覆盖：
- flush_pending_messages: 推进轮次时按 from==target_agent_id 过滤并 flush
- flush_pending_messages: 分配全局 seq（基于 messages.md 最后一行 seq 递增）
- flush_pending_messages: 写 audit（reason=pending_flushed）
- flush_pending_messages: 仅 flush 目标 agent 记录，其他 agent 保留
- flush_replay_candidates: accept 的记录 flush 到 messages.md
- flush_replay_candidates: reject 的记录保留在 replay_candidates.md
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from hermes.multiagent.blackboard import (
    Blackboard,
    atomic_write,
    read_audit_records,
)
from hermes.multiagent.turn_manager import TurnManager


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


class TestTurnManagerFlush:
    """TurnManager 派生文件 flush 测试。"""

    @pytest.mark.asyncio
    async def test_flush_pending_messages_on_turn_advance(self, bb_root: Path):
        """Director 推进轮次时 flush messages.pending.md。"""
        # 写入 pending 消息
        pending_path = bb_root / "messages.pending.md"
        pending_content = """---
pending_seq: 1
from: worker_001
to: "*"
timestamp: 2026-07-21T10:00:08+00:00
turn_id: 1
epoch: 1
type: chat
content_type: markdown
fencing_token: null
pending_reason: out_of_turn_attempt
pending_at: 2026-07-21T10:00:08+00:00
---

hello from worker_001
"""
        await atomic_write(pending_path, pending_content)

        manager = TurnManager(bb_root, agent_id="director_001", epoch=1)
        await manager.flush_pending_messages(target_agent_id="worker_001")

        # pending.md 应清空或删除对应记录
        assert not pending_path.exists() or "hello from worker_001" not in pending_path.read_text(encoding="utf-8")

        # messages.md 应包含 flush 的消息
        messages_path = bb_root / "messages.md"
        messages_content = messages_path.read_text(encoding="utf-8")
        assert "hello from worker_001" in messages_content

    @pytest.mark.asyncio
    async def test_flush_pending_assigns_global_seq(self, bb_root: Path):
        """flush 时分配全局 seq（递增）。"""
        # 先在 messages.md 写入一条消息（seq=5）
        messages_path = bb_root / "messages.md"
        existing_content = """---
seq: 5
from: agent_a
to: "*"
timestamp: 2026-07-21T10:00:00+00:00
type: chat
---

existing message
"""
        await atomic_write(messages_path, existing_content)

        # 写入 pending 消息
        pending_path = bb_root / "messages.pending.md"
        pending_content = """---
pending_seq: 1
from: worker_001
to: "*"
timestamp: 2026-07-21T10:00:08+00:00
type: chat
pending_reason: out_of_turn_attempt
---

pending message
"""
        await atomic_write(pending_path, pending_content)

        manager = TurnManager(bb_root, agent_id="director_001", epoch=1)
        await manager.flush_pending_messages(target_agent_id="worker_001")

        # 验证分配的 seq=6
        messages_content = messages_path.read_text(encoding="utf-8")
        assert "seq: 6" in messages_content

    @pytest.mark.asyncio
    async def test_flush_pending_writes_audit(self, bb_root: Path):
        """flush 时写 audit（reason=pending_flushed）。"""
        pending_path = bb_root / "messages.pending.md"
        pending_content = """---
pending_seq: 1
from: worker_001
to: "*"
timestamp: 2026-07-21T10:00:08+00:00
type: chat
pending_reason: out_of_turn_attempt
---

hello
"""
        await atomic_write(pending_path, pending_content)

        manager = TurnManager(bb_root, agent_id="director_001", epoch=1)
        await manager.flush_pending_messages(target_agent_id="worker_001")

        records = await read_audit_records(bb_root)
        flush_audits = [
            r for r in records
            if r.get("details", {}).get("reason") == "pending_flushed"
        ]
        assert len(flush_audits) >= 1

    @pytest.mark.asyncio
    async def test_flush_pending_filters_by_agent_id(self, bb_root: Path):
        """flush 时按 from == target_agent_id 过滤。"""
        pending_path = bb_root / "messages.pending.md"
        pending_content = """---
pending_seq: 1
from: worker_001
to: "*"
timestamp: 2026-07-21T10:00:08+00:00
type: chat
pending_reason: out_of_turn_attempt
---

message from worker_001
---
pending_seq: 2
from: worker_002
to: "*"
timestamp: 2026-07-21T10:00:09+00:00
type: chat
pending_reason: out_of_turn_attempt
---

message from worker_002
"""
        await atomic_write(pending_path, pending_content)

        manager = TurnManager(bb_root, agent_id="director_001", epoch=1)
        await manager.flush_pending_messages(target_agent_id="worker_001")

        # messages.md 应只包含 worker_001 的消息
        messages_path = bb_root / "messages.md"
        messages_content = messages_path.read_text(encoding="utf-8")
        assert "message from worker_001" in messages_content
        assert "message from worker_002" not in messages_content

        # pending.md 应仍保留 worker_002 的消息
        pending_content_after = pending_path.read_text(encoding="utf-8")
        assert "message from worker_002" in pending_content_after

    @pytest.mark.asyncio
    async def test_flush_replay_candidates_accept(self, bb_root: Path):
        """仲裁为 accept 的 replay_candidate flush 到 messages.md。"""
        replay_path = bb_root / "messages.replay_candidates.md"
        replay_content = """---
pending_seq: 1
from: worker_001
to: "*"
timestamp: 2026-07-21T10:00:08+00:00
type: chat
fencing_token: 6
arbiter_decision: accept
arbiter_reason: valuable content
candidate_reason: fencing_token_mismatch
---

valuable message
"""
        await atomic_write(replay_path, replay_content)

        manager = TurnManager(bb_root, agent_id="director_001", epoch=1)
        await manager.flush_replay_candidates()

        messages_path = bb_root / "messages.md"
        messages_content = messages_path.read_text(encoding="utf-8")
        assert "valuable message" in messages_content

    @pytest.mark.asyncio
    async def test_flush_replay_candidates_reject_keeps_in_file(self, bb_root: Path):
        """仲裁为 reject 的记录保留在 replay_candidates.md。"""
        replay_path = bb_root / "messages.replay_candidates.md"
        replay_content = """---
pending_seq: 1
from: worker_001
to: "*"
timestamp: 2026-07-21T10:00:08+00:00
type: chat
fencing_token: 6
arbiter_decision: reject
arbiter_reason: spam
candidate_reason: fencing_token_mismatch
---

spam message
"""
        await atomic_write(replay_path, replay_content)

        manager = TurnManager(bb_root, agent_id="director_001", epoch=1)
        await manager.flush_replay_candidates()

        # messages.md 不应包含 reject 的消息
        messages_path = bb_root / "messages.md"
        messages_content = messages_path.read_text(encoding="utf-8")
        assert "spam message" not in messages_content

        # replay_candidates.md 应保留
        replay_content_after = replay_path.read_text(encoding="utf-8")
        assert "spam message" in replay_content_after
