"""LocalDirectorManager 自动重启 watchdog 测试（任务 1.5，B3 修复）。

验证：
- 子进程退出后 watchdog 自动调用 start() 重启
- 连续崩溃超过 max_restarts 次后 watchdog 停止
- 崩溃后锁文件在重启前被删除（_cleanup_stale_state）
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teage_liu.multiagent.director_manager import LocalDirectorManager


def _make_manager(bb_root: str, config: dict | None = None) -> LocalDirectorManager:
    """构造一个不触发 stale PID 清理的 manager（bb_root 为空目录）。"""
    return LocalDirectorManager(bb_root, config=config or {})


@pytest.mark.asyncio
async def test_auto_restart_on_subprocess_exit(tmp_path: Path, monkeypatch):
    """子进程退出后 watchdog 自动调用 start() 重启。"""
    bb_root = str(tmp_path)
    (tmp_path / "locks").mkdir()
    manager = _make_manager(bb_root)

    # 模拟已崩溃的子进程（poll 返回非 None = 已退出）
    crashed_process = MagicMock()
    crashed_process.poll.return_value = 1
    manager._process = crashed_process

    # 模拟重启后的新子进程（正常运行）
    new_process = MagicMock()
    new_process.poll.return_value = None

    start_calls: list[int] = []

    async def mock_start():
        start_calls.append(1)
        manager._process = new_process  # 重启后新进程正常运行
        return {"ok": True, "message": "restarted", "pid": 22222}

    manager.start = mock_start  # type: ignore[assignment]

    # 加速 asyncio.sleep：第 3 次调用时取消 watchdog
    sleep_count = [0]

    async def fast_sleep(seconds: float):
        sleep_count[0] += 1
        if sleep_count[0] >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    manager._watchdog_running = True
    manager._watchdog_task = MagicMock()  # 非 None，防止 start() 内重复创建 watchdog
    with pytest.raises(asyncio.CancelledError):
        await manager._watchdog()

    # 断言 start() 被调用一次（自动重启）
    assert len(start_calls) == 1


@pytest.mark.asyncio
async def test_max_restarts_limit(tmp_path: Path, monkeypatch):
    """连续崩溃超过 max_restarts 次后 watchdog 停止。"""
    bb_root = str(tmp_path)
    (tmp_path / "locks").mkdir()
    config = {"max_restarts": 3, "restart_window_seconds": 300}
    manager = _make_manager(bb_root, config=config)

    # 每次重启后进程立即崩溃
    def make_crashed_process():
        p = MagicMock()
        p.poll.return_value = 1
        return p

    manager._process = make_crashed_process()

    start_calls: list[int] = []

    async def mock_start():
        start_calls.append(1)
        manager._process = make_crashed_process()  # 重启后又崩溃
        return {"ok": True, "message": "restarted", "pid": 22222}

    manager.start = mock_start  # type: ignore[assignment]

    # asyncio.sleep 立即返回（no-op），让循环快速跑完
    async def fast_sleep(seconds: float):
        return

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    manager._watchdog_running = True
    manager._watchdog_task = MagicMock()
    await manager._watchdog()

    # 3 次重启后达到上限，watchdog 停止
    assert len(start_calls) == 3
    assert manager._watchdog_running is False


@pytest.mark.asyncio
async def test_stale_lock_cleanup(tmp_path: Path, monkeypatch):
    """崩溃后锁文件在重启前被删除（_cleanup_stale_state）。"""
    bb_root = str(tmp_path)
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir()

    config = {"max_restarts": 3}
    # 先构造 manager（bb_root 无 pid 文件，避免 __init__ stale PID 清理干扰）
    manager = _make_manager(bb_root, config=config)

    # 构造后再创建 stale 锁文件 / pid 文件（模拟上次崩溃残留）
    director_lock = locks_dir / "director.lock"
    audit_lock = locks_dir / "audit.lock"
    director_lock.write_text("stale", encoding="utf-8")
    audit_lock.write_text("stale", encoding="utf-8")
    pid_file = tmp_path / "director.pid"
    pid_file.write_text("99999", encoding="utf-8")

    # 确认文件存在
    assert director_lock.exists()
    assert audit_lock.exists()
    assert pid_file.exists()

    # 模拟已崩溃的子进程
    crashed_process = MagicMock()
    crashed_process.poll.return_value = 1
    manager._process = crashed_process

    # 模拟重启后的新子进程（正常运行）
    new_process = MagicMock()
    new_process.poll.return_value = None

    start_calls: list[int] = []

    async def mock_start():
        start_calls.append(1)
        manager._process = new_process
        return {"ok": True, "message": "restarted", "pid": 22222}

    manager.start = mock_start  # type: ignore[assignment]

    # 加速 asyncio.sleep：第 3 次调用时取消 watchdog
    sleep_count = [0]

    async def fast_sleep(seconds: float):
        sleep_count[0] += 1
        if sleep_count[0] >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    manager._watchdog_running = True
    manager._watchdog_task = MagicMock()
    with pytest.raises(asyncio.CancelledError):
        await manager._watchdog()

    # 断言锁文件和 pid 文件在重启前被删除
    assert not director_lock.exists(), "director.lock 未被清理"
    assert not audit_lock.exists(), "audit.lock 未被清理"
    assert not pid_file.exists(), "director.pid 未被清理"
    # 重启了一次
    assert len(start_calls) == 1
    # _pid_file 路径与 pid_file 一致（确认 _cleanup_stale_state 逻辑正确）
    assert manager._pid_file == os.path.join(bb_root, "director.pid")
