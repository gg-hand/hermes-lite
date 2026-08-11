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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import portalocker

from teage_liu.logging_setup import logger
from teage_liu.multiagent.blackboard import atomic_write, read_json
from teage_liu.multiagent.exceptions import (
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

    def __init__(self, bb_root: Path, agent_id: str | None = None) -> None:
        """初始化锁管理器。

        Args:
            bb_root: 黑板根目录
            agent_id: 可选的持有者标识（用于 Director 强制释放场景的审计）
        """
        self._bb_root = bb_root
        self._agent_id = agent_id
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

    async def release(self, lock_name: str, holder: str, fencing_token: int, force: bool = False) -> None:
        """释放锁。校验 fencing_token。

        Args:
            lock_name: 锁名
            holder: 持有者标识
            fencing_token: acquire 时返回的 fencing_token
            force: Director 强制释放标志；为 True 时跳过 fencing_token 校验
                  （用于 Director 强制释放 Worker 持有的锁场景）
        """
        async with self._write_lock:
            for attempt in range(_CAS_RETRY_LIMIT + 1):
                status = read_json(self._status_path)
                lock = status.get("locks", {}).get(lock_name)
                if not lock:
                    return  # 锁已不存在

                if not force and lock.get("fencing_token") != fencing_token:
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
                if force:
                    lock["force_releasing"] = True
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


class FileLock:
    """跨进程文件锁，基于 portalocker.LOCK_EX。

    用于 agent_card.md / director.md / status.json 跨进程并发写保护。

    与 LockManager（进程内 CAS 锁，作用于 status.json.locks 字段）的区别：
    - FileLock 是 OS 级文件锁（portalocker.LOCK_EX），跨进程可见
    - 锁文件路径：{目标文件路径}.lock（与目标同目录，便于清理）
    - 用于保护单文件原子写场景（如 agent_card.md 的 frontmatter 更新）

    用法（异步上下文管理器）：
        async with FileLock(agent_file):
            ...  # 临界区：读-改-写 agent_file
    """

    def __init__(self, target_path: Path, timeout: float = 5.0) -> None:
        """初始化跨进程文件锁。

        Args:
            target_path: 被保护的目标文件路径（锁文件会建在其旁边）
            timeout: 获取锁的超时秒数（超时抛 portalocker.LockException）
        """
        self._target_path = Path(target_path)
        # 锁文件路径：在目标文件同目录下追加 .lock 后缀
        # 例：agents/worker_001.md → agents/worker_001.md.lock
        self._lock_path = self._target_path.with_suffix(
            self._target_path.suffix + ".lock"
        )
        self._timeout = timeout
        self._lock: portalocker.Lock | None = None

    async def __aenter__(self) -> "FileLock":
        """获取锁（异步，同步 acquire 丢到 executor 避免阻塞事件循环）。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._acquire_sync)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """释放锁。"""
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception as e:
                logger.warning("FileLock 释放失败 (%s): %s", self._lock_path, e)
            self._lock = None

    def _acquire_sync(self) -> None:
        """同步获取锁（在 executor 中调用）。

        与 audit_logger 一致使用 mode='a'（append，文件不存在时创建），
        fail_when_locked=False 让 portalocker 在 timeout 内轮询等待。
        """
        # 确保锁文件父目录存在（agent_card.md 可能尚未创建）
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = portalocker.Lock(
            str(self._lock_path),
            mode="a",
            timeout=self._timeout,
            fail_when_locked=False,
        )
        self._lock.acquire()
