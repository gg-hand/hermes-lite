"""director_cli main_async 僵尸修复测试（任务 1.4，B2 修复）。

验证：
- _loop_task 异常退出时 main_async 不僵尸（2s 内返回）
- main_async 退出后 director.stop() 被调用（锁释放）
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from teage_liu.multiagent import director_cli


@pytest.fixture
def _patch_blackboard(monkeypatch):
    """patch Blackboard 为无 IO 的 mock。"""
    mock_bb = MagicMock()
    mock_bb.init_blackboard = AsyncMock()
    monkeypatch.setattr(director_cli, "Blackboard", lambda root: mock_bb)
    return mock_bb


@pytest.mark.asyncio
async def test_director_cli_exits_on_loop_task_failure(
    tmp_path: Path, monkeypatch, _patch_blackboard
):
    """_loop_task 异常退出时 main_async 在 2s 内返回（不僵尸）。

    B2 修复：旧逻辑只等 SIGINT/SIGTERM，_loop_task 异常退出后子进程僵尸
    （仍持 director.lock）。watcher 监听 _loop_task 退出后 set stop_event，
    使 main_async 走 finally 调 director.stop() 释放锁。
    """
    # 创建会抛 RuntimeError 的 loop_task
    async def _failing_loop():
        raise RuntimeError("loop boom")

    loop_task = asyncio.create_task(_failing_loop())

    mock_director = MagicMock()
    mock_director.start = AsyncMock()
    mock_director.stop = AsyncMock()
    mock_director._loop_task = loop_task
    monkeypatch.setattr(director_cli, "DirectorEngine", lambda **kw: mock_director)

    # main_async 应在 2s 内返回（watcher 检测到 _loop_task 异常后 set stop_event）
    await asyncio.wait_for(
        director_cli.main_async(tmp_path, mode="script"),
        timeout=2.0,
    )

    # director.stop() 应在 finally 中被调用（释放锁）
    mock_director.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_director_lock_released_on_exit(
    tmp_path: Path, monkeypatch, _patch_blackboard
):
    """main_async 退出后 director.stop() 被调用（锁释放）。

    验证 _loop_task 正常完成后，watcher 仍会 set stop_event，使 main_async
    走 finally 调 director.stop()，避免子进程僵尸。
    """
    # 创建正常完成的 loop_task
    async def _completing_loop():
        return None

    loop_task = asyncio.create_task(_completing_loop())

    mock_director = MagicMock()
    mock_director.start = AsyncMock()
    mock_director.stop = AsyncMock()
    mock_director._loop_task = loop_task
    monkeypatch.setattr(director_cli, "DirectorEngine", lambda **kw: mock_director)

    await asyncio.wait_for(
        director_cli.main_async(tmp_path, mode="script"),
        timeout=2.0,
    )

    # director.stop() 必须被调用，确保 director.lock 被释放
    mock_director.stop.assert_awaited_once()
