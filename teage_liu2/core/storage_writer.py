"""StorageWriter 异步单写者(§18.1 定案,阶段 1 落地)。

根治现状真实缺陷:pipeline._persist 同步调 sqlite3(含 fsync)于请求路径
阻塞事件循环(违反项目 async 铁律)+ 多连接写锁竞争。

架构(零新依赖,天然 FIFO + 天然单写者):
- 专用写线程独占写连接;``queue.Queue`` 保序;``Future`` 桥接事件循环
- 持久性分级:
  * ``enqueue_flush(fn) -> Future``:调用方 await 该写操作在队列中执行完毕
    (事件循环仅挂起协程,fsync 在写线程内执行)——仅 user 前置落盘用
    (保"断连不丢输入"契约;flush 语义 = await commit,NORMAL 下 WAL 已写,
    进程崩溃不丢,非逐次 FULL fsync)
  * ``enqueue_background(fn)``:fire-and-forget,入队即返,失败重试 + error 日志
    ——assistant / tool_result / 审计 / 扩展低频写全部走此级
- 有界写队列(§15-A6 → 阶段 1):容量上限,满则丢弃最旧 + 丢弃计数
  (防无界堆积防 DoS;被丢弃的 flush future 以异常 resolve,调用方可见)
- 队列深度/延迟可观测(计数器)

注意:不采用 ``run_in_executor`` 线程池(多线程破坏 FIFO 保序);
不采用 ``aiosqlite``(新依赖且与现有连接模型不兼容)。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from .errors import STORAGE_WRITE_FAILED

logger = logging.getLogger(__name__)

# 队列容量上限(§15-A6:防无界堆积;阈值进 types 域 schema 为协议常量)
DEFAULT_MAX_QUEUE_SIZE = 4096

# background 失败重试次数(§18.1:失败重试 + error 日志)
_BACKGROUND_RETRIES = 1

#: 哨兵:写线程退出信号
_SENTINEL = object()


class _WriteTask:
    """一个待执行的写操作(函数 + 可选 Future 桥接)。"""

    __slots__ = ("fn", "future", "loop", "is_flush", "enqueued_at")

    def __init__(
        self,
        fn: Callable[[], Any],
        future: Optional[asyncio.Future],
        loop: Optional[asyncio.AbstractEventLoop],
        is_flush: bool,
    ) -> None:
        self.fn = fn
        self.future = future
        self.loop = loop
        self.is_flush = is_flush
        self.enqueued_at = time.monotonic()

    def resolve(self, result: Any = None, error: Optional[BaseException] = None) -> None:
        """将执行结果/异常桥接回事件循环(写线程调用,线程安全)。"""
        if self.future is None or self.loop is None:
            return
        try:
            if error is not None:
                self.loop.call_soon_threadsafe(self.future.set_exception, error)
            else:
                self.loop.call_soon_threadsafe(self.future.set_result, result)
        except RuntimeError:
            # 事件循环已关闭(应用 shutdown 中):future 无法送达,仅记日志
            logger.warning(
                "StorageWriter 无法送达 future 结果(事件循环可能已关闭): %r",
                error or result,
            )


class StorageWriter:
    """异步单写者:全部 SQLite 写经此队列串行执行。

    使用示例::

        writer = StorageWriter()
        future = writer.enqueue_flush(lambda: history_store.log_message(...))
        await future   # 保"断连不丢输入"
        writer.enqueue_background(lambda: history_store.log_message(...))
        ...
        writer.close()  # 幂等,等待队列排空后停止写线程
    """

    def __init__(self, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE) -> None:
        if max_queue_size < 1:
            raise ValueError(f"max_queue_size 必须是正整数,实际 {max_queue_size!r}")
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue_size)
        self._max_queue_size = max_queue_size
        self._closed = False
        self._close_lock = threading.Lock()
        # 丢弃最旧计数(§15-A6 可观测信号:持续丢弃 = 消费过慢)
        self._dropped_count = 0
        self._dropped_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="storage-writer"
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def enqueue_flush(self, fn: Callable[[], Any]) -> asyncio.Future:
        """入队一个 flush 写操作,返回 await 到写完成的 Future。

        flush 语义 = await commit(NORMAL 下 WAL 已写,进程崩溃不丢)。
        仅 user 前置落盘用(保"断连不丢输入"契约)。
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._enqueue(_WriteTask(fn, future, loop, is_flush=True))
        return future

    def enqueue_background(self, fn: Callable[[], Any]) -> None:
        """入队一个 background 写操作,入队即返(fire-and-forget)。

        失败重试(1 次)+ error 日志;assistant / tool_result / 审计 /
        扩展低频写全部走此级。
        """
        self._enqueue(_WriteTask(fn, None, None, is_flush=False))

    # ------------------------------------------------------------------
    # 可观测性(§18.1 队列深度/延迟可观测)
    # ------------------------------------------------------------------
    @property
    def queue_depth(self) -> int:
        """当前队列深度(近似,多线程下不精确)。"""
        return self._queue.qsize()

    @property
    def max_queue_size(self) -> int:
        return self._max_queue_size

    @property
    def dropped_count(self) -> int:
        """累计丢弃最旧的任务数(背压可观测信号)。"""
        with self._dropped_lock:
            return self._dropped_count

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _enqueue(self, task: _WriteTask) -> None:
        with self._close_lock:
            if self._closed:
                raise RuntimeError("StorageWriter 已关闭,拒绝新写操作")
        while True:
            try:
                self._queue.put_nowait(task)
                return
            except queue.Full:
                # 有界队列满:丢弃最旧(§15-A6),绝不阻塞事件循环
                try:
                    oldest = self._queue.get_nowait()
                except queue.Empty:
                    continue
                with self._dropped_lock:
                    self._dropped_count += 1
                logger.error(
                    "%s: StorageWriter 队列已满(%d),丢弃最旧写操作(丢弃累计 %d)",
                    STORAGE_WRITE_FAILED, self._max_queue_size, self._dropped_count,
                )
                if isinstance(oldest, _WriteTask):
                    oldest.resolve(
                        error=RuntimeError(
                            "StorageWriter 队列满,写操作被丢弃(背压)"
                        )
                    )

    def _run(self) -> None:
        """写线程主循环:串行执行队列任务(天然单写者 + FIFO)。"""
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                break
            if not isinstance(item, _WriteTask):
                self._queue.task_done()
                continue
            self._execute(item)
            self._queue.task_done()

    def _execute(self, task: _WriteTask) -> None:
        """执行单个写任务;background 失败重试一次;flush 失败直接 error。"""
        try:
            result = task.fn()
        except Exception as e:
            if not task.is_flush and _BACKGROUND_RETRIES > 0:
                for attempt in range(1, _BACKGROUND_RETRIES + 1):
                    try:
                        result = task.fn()
                        break
                    except Exception as retry_e:
                        if attempt == _BACKGROUND_RETRIES:
                            logger.error(
                                "%s: StorageWriter background 写失败(重试 %d 次后): %r",
                                STORAGE_WRITE_FAILED, _BACKGROUND_RETRIES, retry_e,
                            )
                            task.resolve(error=retry_e)
                            return
                else:
                    return
            else:
                logger.error("StorageWriter flush 写失败: %r", e)
                task.resolve(error=e)
                return
        else:
            task.resolve(result=result)

    # ------------------------------------------------------------------
    # 关闭(幂等)
    # ------------------------------------------------------------------
    def close(self) -> None:
        """停止写线程并等待队列排空(幂等,可多次调用)。

        先入哨兵:队列中已有任务(含 flush future)都会被执行完毕,
        不会静默丢弃。
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=10)
