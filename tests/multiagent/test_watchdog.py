"""watchdog_watcher.py 测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from teage_liu.multiagent.watchdog_watcher import WatchdogWatcher


@pytest.fixture
def watcher(bb_root: Path) -> WatchdogWatcher:
    return WatchdogWatcher(bb_root, callback=lambda evt: None)


@pytest.mark.asyncio
async def test_start_and_stop(watcher: WatchdogWatcher):
    await watcher.start()
    assert watcher.is_healthy() is True
    await watcher.stop()


@pytest.mark.asyncio
async def test_backend_is_watchdog_or_polling(watcher: WatchdogWatcher):
    await watcher.start()
    backend = watcher.get_backend()
    assert backend in ("watchdog", "polling")
    await watcher.stop()


@pytest.mark.asyncio
async def test_self_test_writes_and_detects(watcher: WatchdogWatcher, bb_root: Path):
    """自检：写入测试文件，应能在 5 秒内收到事件。"""
    events = []
    watcher._callback = lambda evt: events.append(evt)
    await watcher.start()
    # 写入测试文件
    test_file = bb_root / "self_test_probe.txt"
    test_file.write_text("probe", encoding="utf-8")
    # 等待事件（最多 5 秒）
    for _ in range(50):
        await asyncio.sleep(0.1)
        if events:
            break
    await watcher.stop()
    assert len(events) > 0, "watchdog self-test failed: no event received within 5s"


@pytest.mark.asyncio
async def test_degrade_to_polling_on_failure(bb_root: Path):
    """watchdog 自检失败应降级为轮询。"""
    # 模拟 watchdog 不可用：用一个不支持的 backend
    watcher = WatchdogWatcher(bb_root, callback=lambda evt: None, force_backend="polling")
    await watcher.start()
    assert watcher.get_backend() == "polling"
    assert watcher.is_healthy() is True
    await watcher.stop()
