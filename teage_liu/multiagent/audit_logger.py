"""multiagent 审计日志：append 串行化 + hash 链 + 损坏降级。

设计原则：
- audit.lock 改用 portalocker 文件锁（绕过 CAS），声明为叶子锁
- 直接 append 模式写入（无 .tmp + os.replace，保 append-only 语义）
- hash 链：每条记录的 prev_hash = 上一条 hash，hash = sha256(record_with_prev_hash)
- 损坏降级：JSON 解析失败行跳过 + 写 corrupt.log；hash 链断裂标记 suspect
- TTL 30 秒（比 CAS 锁高，避免 audit 频繁阻塞）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import portalocker

from teage_liu.logging_setup import logger
from teage_liu.multiagent.blackboard import append_jsonl


class MultiAgentAuditLogger:
    """multiagent 审计日志（与现有 teage_liu.agent.audit.AuditLogger 命名空间隔离）。

    容器注册键：multiagent_audit_logger（非 audit_logger）
    """

    def __init__(self, bb_root: Path) -> None:
        self._bb_root = bb_root
        self._audit_path = bb_root / "audit" / "audit.jsonl"
        self._corrupt_path = bb_root / "audit" / "audit.jsonl.corrupt"
        self._lock_path = bb_root / "locks" / "audit.lock"
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._executor_lock = asyncio.Lock()  # 进程内串行化（portalocker 已跨进程串行化）

    async def append_audit(self, record: dict) -> None:
        """获取 audit.lock（portalocker 文件锁，绕过 CAS）后追加记录，计算 hash 链。

        portalocker.Lock 是同步锁，用 run_in_executor 包装避免阻塞事件循环。
        audit.jsonl 是 append-only 文件，直接用 append 模式打开写入（不写 .tmp 再 rename，
        因为 rename 会覆盖已有内容，破坏 append-only 语义）。
        """
        async with self._executor_lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_with_lock, record)

    def _write_with_lock(self, record: dict) -> None:
        """同步写入（在 executor 中调用）。"""
        with portalocker.Lock(str(self._lock_path), timeout=30, fail_when_locked=False):
            # 1. 读取最后一行计算 prev_hash
            prev_hash = self.read_last_hash()
            record["prev_hash"] = prev_hash
            # 2. 计算 hash = sha256(record_with_prev_hash)
            record_for_hash = {k: v for k, v in record.items() if k != "hash"}
            record["hash"] = hashlib.sha256(
                json.dumps(record_for_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            # 3. 直接 append 模式写入（portalocker 保证串行化，append 在同文件原子）
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def read_last_hash(self) -> str:
        """读取最后一行的 hash。空文件返回空字符串。"""
        if not self._audit_path.exists():
            return ""
        try:
            with open(self._audit_path, "rb") as f:
                # seek 到末尾前一段读取
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return ""
                # 读取最后 4KB
                read_size = min(size, 4096)
                f.seek(-read_size, 2)
                tail = f.read().decode("utf-8", errors="ignore")
                lines = tail.strip().split("\n")
                if not lines or not lines[-1].strip():
                    return ""
                try:
                    last_record = json.loads(lines[-1])
                    return last_record.get("hash", "")
                except json.JSONDecodeError:
                    return ""
        except OSError:
            return ""

    def read_records(
        self,
        filter_action: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """读取 audit 记录。

        Args:
            filter_action: 只读取指定 action 的记录
            limit: 最多读取多少条

        Returns:
            记录列表（按时间顺序）
        """
        if not self._audit_path.exists():
            return []

        records = []
        prev_hash = ""
        corrupt_lines: list[str] = []

        with open(self._audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    corrupt_lines.append(line)
                    continue

                # hash 链校验
                if rec.get("prev_hash") != prev_hash:
                    rec["_suspect"] = True
                    logger.warning(f"audit hash chain broken at ts={rec.get('ts')}")
                prev_hash = rec.get("hash", "")

                # 过滤
                if filter_action and rec.get("action") != filter_action:
                    continue
                records.append(rec)

                if len(records) >= limit:
                    break

        # 损坏行写入 corrupt.log
        if corrupt_lines:
            self._corrupt_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._corrupt_path, "a", encoding="utf-8") as f:
                for line in corrupt_lines:
                    f.write(line + "\n")
            logger.warning(f"audit corrupt lines: {len(corrupt_lines)} written to {self._corrupt_path}")

        return records
