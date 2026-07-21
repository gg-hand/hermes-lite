"""轮次管理 + 派生文件 flush。

TurnManager 负责：
1. flush messages.pending.md：Director 推进轮次时，扫描 from == target_agent_id
   的 pending 记录，分配全局 seq 后追加到 messages.md
2. flush messages.replay_candidates.md：仲裁为 accept 的记录追加到 messages.md，
   reject 的记录保留在 replay_candidates.md

flush 流程幂等：每条 audit 记录 op_id 去重。

全链路异步：所有 IO 操作均通过 aiofiles / await append_message / await append_audit。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import yaml

from hermes.multiagent.blackboard import (
    append_audit,
    append_message,
    atomic_write,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """当前 UTC 时间 ISO 格式。"""
    return datetime.now(timezone.utc).isoformat()


class TurnManager:
    """轮次管理 + 派生文件 flush。"""

    def __init__(self, bb_root: Path, agent_id: str = "director_001", epoch: int = 1):
        """初始化 TurnManager。

        Args:
            bb_root: 黑板根目录
            agent_id: 执行 flush 的 agent（通常是 Director）
            epoch: 当前 epoch，用于 audit 记录
        """
        self._bb_root = bb_root
        self._agent_id = agent_id
        self._epoch = epoch

    async def flush_pending_messages(self, target_agent_id: str) -> int:
        """flush messages.pending.md 中 from == target_agent_id 的记录。

        流程：
        1. 读取 pending.md 全部记录
        2. 过滤 from == target_agent_id 的记录，按 pending_seq 升序
        3. 读取 messages.md 最后一行 seq 作为起始
        4. 逐条分配 seq + append_message + append_audit（reason=pending_flushed）
        5. 从 pending.md 移除已 flush 记录（其他 agent 记录保留）

        幂等性：每条 audit 携带唯一 op_id，重复 flush 会产生新 op_id，
        但 pending.md 已无对应记录（移除后不会再 flush）。

        Args:
            target_agent_id: 目标 agent_id（仅 flush 该 agent 的 pending 记录）

        Returns:
            flush 的记录数
        """
        pending_path = self._bb_root / "messages.pending.md"
        if not pending_path.exists():
            return 0

        records = await self._read_multi_record_file(pending_path)
        if not records:
            return 0

        target_records = [
            r for r in records
            if r.get("frontmatter", {}).get("from") == target_agent_id
        ]
        if not target_records:
            return 0

        target_records.sort(
            key=lambda r: r.get("frontmatter", {}).get("pending_seq", 0)
        )

        last_seq = await self._read_last_message_seq()

        flushed_count = 0
        for record in target_records:
            last_seq += 1
            fm = record["frontmatter"]
            body = record.get("body", "")

            # 构造正式消息：移除 pending_* 字段，新增 seq
            # 关键：message["content"] = body 让 append_message 把正文写入 messages.md
            message = {
                "seq": last_seq,
                "from": fm.get("from", ""),
                "to": fm.get("to", "*"),
                "timestamp": fm.get("timestamp", _now_iso()),
                "type": fm.get("type", "chat"),
                "content_type": fm.get("content_type", "markdown"),
                "epoch": fm.get("epoch", self._epoch),
                "content": body,
            }

            await append_message(self._bb_root, message)

            await append_audit(self._bb_root, {
                "ts": _now_iso(),
                "actor": self._agent_id,
                "action": "write",
                "target": "messages.md",
                "op_id": str(uuid.uuid4()),
                "epoch": self._epoch,
                "details": {
                    "reason": "pending_flushed",
                    "seq": last_seq,
                    "original_pending_seq": fm.get("pending_seq"),
                    "from": message["from"],
                },
                "prev_hash": "",
                "hash": "",
                "signature": "",
            })

            flushed_count += 1

        # 从 pending.md 移除已 flush 的记录（保留其他 agent 的记录）
        remaining_records = [
            r for r in records
            if r.get("frontmatter", {}).get("from") != target_agent_id
        ]
        await self._write_multi_record_file(pending_path, remaining_records)

        logger.info(
            "flush %d pending messages for agent %s",
            flushed_count,
            target_agent_id,
        )
        return flushed_count

    async def flush_replay_candidates(self) -> int:
        """flush messages.replay_candidates.md 中 arbiter_decision=accept 的记录。

        流程：
        1. 读取 replay_candidates.md 全部记录
        2. 过滤 arbiter_decision=accept 的记录
        3. 逐条分配 seq + append_message + append_audit（reason=replay_candidate_flushed）
        4. 移除 accept 记录，保留 reject 记录

        Returns:
            flush 的记录数
        """
        replay_path = self._bb_root / "messages.replay_candidates.md"
        if not replay_path.exists():
            return 0

        records = await self._read_multi_record_file(replay_path)
        if not records:
            return 0

        accept_records = [
            r for r in records
            if r.get("frontmatter", {}).get("arbiter_decision") == "accept"
        ]
        if not accept_records:
            return 0

        last_seq = await self._read_last_message_seq()

        flushed_count = 0
        for record in accept_records:
            last_seq += 1
            fm = record["frontmatter"]
            body = record.get("body", "")

            message = {
                "seq": last_seq,
                "from": fm.get("from", ""),
                "to": fm.get("to", "*"),
                "timestamp": fm.get("timestamp", _now_iso()),
                "type": fm.get("type", "chat"),
                "content_type": fm.get("content_type", "markdown"),
                "epoch": fm.get("epoch", self._epoch),
                "content": body,
            }

            await append_message(self._bb_root, message)

            await append_audit(self._bb_root, {
                "ts": _now_iso(),
                "actor": self._agent_id,
                "action": "write",
                "target": "messages.md",
                "op_id": str(uuid.uuid4()),
                "epoch": self._epoch,
                "details": {
                    "reason": "replay_candidate_flushed",
                    "seq": last_seq,
                    "arbiter_decision": "accept",
                    "arbiter_reason": fm.get("arbiter_reason", ""),
                    "candidate_reason": fm.get("candidate_reason", ""),
                },
                "prev_hash": "",
                "hash": "",
                "signature": "",
            })

            flushed_count += 1

        # 保留 reject 记录，移除 accept 记录
        remaining_records = [
            r for r in records
            if r.get("frontmatter", {}).get("arbiter_decision") != "accept"
        ]
        await self._write_multi_record_file(replay_path, remaining_records)

        logger.info("flush %d replay candidates (accept)", flushed_count)
        return flushed_count

    async def _read_last_message_seq(self) -> int:
        """读取 messages.md 最后一条消息的 seq。

        Returns:
            最后一条消息的 seq；若文件为空或不存在返回 0
        """
        messages_path = self._bb_root / "messages.md"
        if not messages_path.exists():
            return 0

        records = await self._read_multi_record_file(messages_path)
        if not records:
            return 0

        return records[-1].get("frontmatter", {}).get("seq", 0)

    async def _read_multi_record_file(self, file_path: Path) -> list[dict]:
        """读取包含多条 frontmatter 记录的文件。

        格式：每条记录由 --- 分隔的 frontmatter + 正文组成。
        文件结构：---\n<yaml>\n---\n\n<body>\n\n---\n<yaml>\n---\n\n<body>\n\n...

        Returns:
            [{"frontmatter": dict, "body": str}, ...]
        """
        if not file_path.exists():
            return []

        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()

        if not content.strip():
            return []

        records = []
        parts = content.split("---\n")
        # 文件以 "---\n" 开头时，parts[0] 为空，parts[1] 为第一个 frontmatter
        i = 1
        while i < len(parts) - 1:
            frontmatter_str = parts[i].strip()
            if not frontmatter_str:
                i += 2
                continue

            try:
                fm = yaml.safe_load(frontmatter_str)
                if fm is None:
                    fm = {}
            except yaml.YAMLError:
                i += 2
                continue

            # 下一个 part 是正文（可能含尾随 ---）
            if i + 1 < len(parts):
                body = parts[i + 1].strip()
            else:
                body = ""

            records.append({"frontmatter": fm, "body": body})
            i += 2

        return records

    async def _write_multi_record_file(
        self, file_path: Path, records: list[dict]
    ) -> None:
        """写入多条 frontmatter 记录到文件（原子写入）。

        Args:
            file_path: 目标文件路径
            records: [{"frontmatter": dict, "body": str}, ...]
        """
        if not records:
            # 清空文件
            await atomic_write(file_path, "")
            return

        parts = []
        for record in records:
            fm = record.get("frontmatter", {})
            body = record.get("body", "")
            frontmatter = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
            parts.append(f"---\n{frontmatter}---\n\n{body}\n")

        content = "\n".join(parts)
        await atomic_write(file_path, content)
