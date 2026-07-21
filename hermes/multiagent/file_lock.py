"""CAS + fencing_token + grace_period 锁管理。

设计原则：
- LockManager 是 Worker 进程内单例（不跨进程）
- _next_fencing_token 不维护内存计数器，每次读 status.json（与 P0-1 联动）
- CAS 写入重试上限 2 次（对齐 project_memory 硬约束第 17 行）
- grace_period：Director force_release 时写 grace_until + force_releasing 标记
- fencing_token 单调递增：release 后重新 acquire 也会递增（防幽灵写入）
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hermes.logging_setup import logger
from hermes.multiagent.blackboard import atomic_write, read_json
from hermes.multiagent.exceptions import (
    CASVersionMismatchError,
    FencingTokenMismatchError,
    LockAcquisitionError,
)

# CAS 重试上限（对齐 project_memory 硬约束）
_CAS_RETRY_LIMIT = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_plus_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class LockManager:
    """Worker 进程内锁管理单例。

    所有操作通过 CAS 写入 status.json.locks 字段实现。
    fencing_token 从 status.json.last_fencing_token 读取并递增。
    """

    def __init__(self, bb_root: Path) -> None:
        self._bb_root = bb_root
        self._status_path = bb_root / "status.json"
        self._write_lock = asyncio.Lock()  # 进程内串行化 CAS 写入

    async def acquire(self, lock_name: str, holder: str, ttl_seconds: int) -> int:
        """获取锁。返回 fencing_token。

        CAS 重试上限 2 次：version 不匹配时重读重试。
        """
        async with self._write_lock:
            for attempt in range(_CAS_RETRY_LIMIT + 1):
                status = read_json(self._status_path)
                locks = status.get("locks", {})

                # 检查锁是否已被持有
                existing = locks.get(lock_name)
                if existing and existing.get("holder"):
                    # 检查 TTL 是否过期
                    expires_at = existing.get("expires_at")
                    if expires_at and _now_iso() < expires_at:
                        raise LockAcquisitionError(
                            lock_name=lock_name,
                            reason="held_by_other",
                            current_holder=existing["holder"],
                        )

                # 分配新 fencing_token
                new_token = status.get("last_fencing_token", 0) + 1

                # CAS 写入
                expected_version = status["version"]
                locks[lock_name] = {
                    "holder": holder,
                    "acquired_at": _now_iso(),
                    "expires_at": _now_plus_seconds(ttl_seconds),
                    "fencing_token": new_token,
                    "epoch": status.get("epoch", 1),
                    "grace_until": None,
                    "force_releasing": False,
                }
                status["locks"] = locks
                status["last_fencing_token"] = new_token

                try:
                    await self._cas_write(status, expected_version)
                    return new_token
                except CASVersionMismatchError:
                    if attempt >= _CAS_RETRY_LIMIT:
                        raise
                    logger.debug(f"CAS retry {attempt + 1}/{_CAS_RETRY_LIMIT} for lock '{lock_name}'")
                    await asyncio.sleep(0.01)
                    continue
            # 不应到达
            raise LockAcquisitionError(lock_name=lock_name, reason="cas_exhausted")

    async def release(self, lock_name: str, holder: str, fencing_token: int) -> None:
        """释放锁。校验 fencing_token。"""
        async with self._write_lock:
            for attempt in range(_CAS_RETRY_LIMIT + 1):
                status = read_json(self._status_path)
                lock = status.get("locks", {}).get(lock_name)
                if not lock:
                    return  # 锁已不存在

                if lock.get("fencing_token") != fencing_token:
                    raise FencingTokenMismatchError(
                        lock_name=lock_name,
                        expected_token=lock.get("fencing_token", 0),
                        actual_token=fencing_token,
                        writer_id=holder,
                    )

                expected_version = status["version"]
                # 释放锁：保留 fencing_token（防幽灵写入检测），清空 holder
                lock["holder"] = None
                lock["acquired_at"] = None
                lock["expires_at"] = None
                # fencing_token / grace_until / force_releasing 保留用于审计
                status["locks"][lock_name] = lock

                try:
                    await self._cas_write(status, expected_version)
                    return
                except CASVersionMismatchError:
                    if attempt >= _CAS_RETRY_LIMIT:
                        raise
                    await asyncio.sleep(0.01)
                    continue

    async def renew(self, lock_name: str, holder: str, fencing_token: int, new_ttl: int) -> int:
        """续期锁。返回原 fencing_token（不递增）。"""
        async with self._write_lock:
            status = read_json(self._status_path)
            lock = status.get("locks", {}).get(lock_name)
            if not lock or lock.get("holder") != holder:
                raise LockAcquisitionError(lock_name=lock_name, reason="not_holder")

            if lock.get("fencing_token") != fencing_token:
                raise FencingTokenMismatchError(
                    lock_name=lock_name,
                    expected_token=lock.get("fencing_token", 0),
                    actual_token=fencing_token,
                    writer_id=holder,
                )

            expected_version = status["version"]
            lock["expires_at"] = _now_plus_seconds(new_ttl)
            status["locks"][lock_name] = lock

            try:
                await self._cas_write(status, expected_version)
                return fencing_token
            except CASVersionMismatchError:
                # renew 不重试（高频操作，失败由调用方决策）
                raise

    async def force_release(self, lock_name: str, reason: str) -> None:
        """Director 强制释放。先写 grace_period 标记。

        对齐 O3 默认值：v1.0.3 用 best-effort 模式。
        """
        async with self._write_lock:
            status = read_json(self._status_path)
            lock = status.get("locks", {}).get(lock_name)
            if not lock:
                return

            expected_version = status["version"]
            # 写 grace_period 标记（5 秒缓冲）
            lock["force_releasing"] = True
            lock["grace_until"] = _now_plus_seconds(5)
            status["locks"][lock_name] = lock

            try:
                await self._cas_write(status, expected_version)
                # 延迟清除（5 秒后）
                asyncio.create_task(self._delayed_clear(lock_name))
            except CASVersionMismatchError:
                raise

    async def _delayed_clear(self, lock_name: str) -> None:
        """延迟清除锁（grace_period 后）。"""
        await asyncio.sleep(5)
        async with self._write_lock:
            status = read_json(self._status_path)
            lock = status.get("locks", {}).get(lock_name)
            if not lock:
                return
            expected_version = status["version"]
            lock["holder"] = None
            lock["acquired_at"] = None
            lock["expires_at"] = None
            lock["force_releasing"] = False
            lock["grace_until"] = None
            status["locks"][lock_name] = lock
            try:
                await self._cas_write(status, expected_version)
            except CASVersionMismatchError:
                logger.warning(f"_delayed_clear CAS failed for '{lock_name}'")

    def is_locked(self, lock_name: str) -> bool:
        """检查锁是否被持有（非阻塞读）。"""
        if not self._status_path.exists():
            return False
        status = read_json(self._status_path)
        lock = status.get("locks", {}).get(lock_name)
        if not lock:
            return False
        if not lock.get("holder"):
            return False
        # 检查 TTL
        expires_at = lock.get("expires_at")
        if expires_at and _now_iso() > expires_at:
            return False
        return True

    async def _cas_write(self, new_status: dict, expected_version: int) -> None:
        """CAS 写入 status.json。"""
        current = read_json(self._status_path)
        if current["version"] != expected_version:
            raise CASVersionMismatchError(
                expected=expected_version,
                actual=current["version"],
            )
        new_status["version"] = expected_version + 1
        await atomic_write(self._status_path, json.dumps(new_status, ensure_ascii=False))
