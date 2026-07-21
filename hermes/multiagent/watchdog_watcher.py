"""文件监听 + 自检。

设计原则：
- 优先用 watchdog（inotify/FSEvents/ReadDirectoryChangesV）
- 自检失败降级为轮询（每 2 秒扫描目录）
- 自检：启动时写入测试文件，5 秒内未收到事件则降级
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

logger = logging.getLogger(__name__)


class _EventHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[FileSystemEvent], None]) -> None:
        self._callback = callback

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._callback(event)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._callback(event)


class WatchdogWatcher:
    """文件监听器，支持自检和降级。"""

    def __init__(
        self,
        bb_root: Path,
        callback: Callable[[FileSystemEvent], None],
        force_backend: Optional[str] = None,
    ) -> None:
        self._bb_root = bb_root
        self._callback = callback
        self._force_backend = force_backend
        self._observer: Optional[Observer] = None
        self._backend: str = "watchdog"
        self._healthy: bool = False
        self._polling_task: Optional[asyncio.Task] = None
        self._polling_interval: float = 2.0
        self._self_test_received: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        """启动监听 + 自检。"""
        self._loop = asyncio.get_event_loop()
        backend = self._force_backend or "watchdog"
        if backend == "watchdog":
            try:
                self._self_test_received = asyncio.Event()
                self._observer = Observer()
                self._observer.schedule(
                    _EventHandler(self._dispatch),
                    str(self._bb_root),
                    recursive=True,
                )
                self._observer.start()
                self._backend = "watchdog"
                # 自检
                if await self._self_test():
                    self._healthy = True
                else:
                    logger.warning("watchdog self-test failed, degrading to polling")
                    self._observer.stop()
                    self._observer = None
                    await self._start_polling()
            except Exception as e:
                logger.warning(f"watchdog start failed: {e}, degrading to polling")
                self._observer = None
                await self._start_polling()
        else:
            await self._start_polling()

    def _dispatch(self, evt: FileSystemEvent) -> None:
        """分发事件到 callback + self-test 检测。

        watchdog 回调在 watchdog 线程中执行，需用 call_soon_threadsafe 唤醒 asyncio.Event。
        """
        self._callback(evt)
        if (
            self._self_test_received is not None
            and self._loop is not None
            and evt.src_path.endswith("self_test_probe.txt")
        ):
            try:
                self._loop.call_soon_threadsafe(self._self_test_received.set)
            except RuntimeError:
                pass

    async def _self_test(self) -> bool:
        """自检：写入测试文件，5 秒内收到事件则通过。"""
        if self._observer is None or self._self_test_received is None:
            return False

        probe_path = self._bb_root / "self_test_probe.txt"
        try:
            probe_path.write_text("probe", encoding="utf-8")
        except OSError as e:
            logger.warning(f"self-test probe write failed: {e}")
            return False

        try:
            await asyncio.wait_for(self._self_test_received.wait(), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            # 清理 probe 文件
            try:
                if probe_path.exists():
                    probe_path.unlink()
            except OSError:
                pass
            self._self_test_received = None

    async def _start_polling(self) -> None:
        """降级为轮询。"""
        self._backend = "polling"
        self._healthy = True
        self._polling_task = asyncio.create_task(self._polling_loop())

    async def _polling_loop(self) -> None:
        """轮询循环。"""
        last_snapshot: dict[Path, float] = {}
        try:
            for path in self._bb_root.rglob("*"):
                if path.is_file():
                    last_snapshot[path] = path.stat().st_mtime
        except OSError:
            pass

        while True:
            await asyncio.sleep(self._polling_interval)
            current_snapshot: dict[Path, float] = {}
            try:
                for path in self._bb_root.rglob("*"):
                    if path.is_file():
                        mtime = path.stat().st_mtime
                        current_snapshot[path] = mtime
                        if path not in last_snapshot or last_snapshot[path] != mtime:
                            # 模拟 FileSystemEvent
                            from watchdog.events import FileModifiedEvent, FileCreatedEvent
                            event_cls = FileCreatedEvent if path not in last_snapshot else FileModifiedEvent
                            self._callback(event_cls(str(path)))
            except OSError:
                pass

            # 检测删除
            for path in list(last_snapshot.keys()):
                if path not in current_snapshot:
                    from watchdog.events import FileDeletedEvent
                    self._callback(FileDeletedEvent(str(path)))

            last_snapshot = current_snapshot

    async def stop(self) -> None:
        """停止监听。"""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=1.0)
            self._observer = None
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
        self._healthy = False

    def is_healthy(self) -> bool:
        return self._healthy

    def get_backend(self) -> str:
        return self._backend
