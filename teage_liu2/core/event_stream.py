"""事件流三层(§3.2,阶段 2 落地):L1 直传 / L2 摘要 / L3 拦截点。

| 层 | 内容 | 传输方式 | 消费方 |
|---|---|---|---|
| L1 热路径 | text_delta / reasoning_delta 逐字增量 | 宿主实现内部直传,绝不外发 | 外壳(同进程零拷贝) |
| L2 结构化摘要 | StepSummary / AfterResponse | 钩子调用(冷路径,快照+action) | 全部扩展 |
| L3 观测通知 | tool_use / tool_result / step_end(原始事件) | 异步批处理(阶段 3 transport 落地) | 观测类扩展 |

不变量(§3.2):
- L1 绝不进 transport:逐字增量只活在宿主内,任何扩展不接收
- L2 是扩展获取对话内容的唯一语义通道
- L3 是异步旁路:可批处理/可背压(best-effort),绝不影响主对话流
- 外壳直通事件(§3.2 v1.11):step_start/done/error 宿主内部直传,不进 transport、
  不投 L3;done/error 是终态事件,必须可靠送达外壳(不可丢弃)

阶段 2 落地范围(§16):L1/L2 行为对拍 + L3 schema 对拍(拦截点定义与事件
归类);L3 批处理窗口/有界队列/背压丢弃随阶段 3 transport 落地(§18.5)。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from .types import (
    EV_ERROR,
    EV_DONE,
    EV_REASONING_DELTA,
    EV_STEP_END,
    EV_STEP_START,
    EV_TEXT_DELTA,
    EV_TOOL_RESULT,
    EV_TOOL_USE,
)

logger = logging.getLogger(__name__)

#: L3 批处理窗口(§18.5:50ms)
L3_BATCH_WINDOW_SECONDS: float = 0.05
#: L3 批处理条数上限(§18.5:64 条先到触发)
L3_BATCH_MAX_EVENTS: int = 64
#: 每观测扩展有界队列容量(§18.5/§15-A6:满则丢弃最旧 + 计数)
L3_QUEUE_MAX_SIZE: int = 1024

#: L1 热路径事件(逐字增量,宿主内部直传,绝不进 transport/不投 L3)
L1_EVENT_TYPES = frozenset({EV_TEXT_DELTA, EV_REASONING_DELTA})

#: L3 观测通知事件(异步旁路,阶段 3 起经 transport 投递观测扩展)
L3_EVENT_TYPES = frozenset({EV_TOOL_USE, EV_TOOL_RESULT, EV_STEP_END})

#: 外壳直通事件(宿主内部直传,不进 transport、不投 L3;done/error 终态不可丢弃)
SHELL_DIRECT_EVENT_TYPES = frozenset({EV_STEP_START, EV_DONE, EV_ERROR})


def is_l1_event(event_type: str) -> bool:
    """判断事件是否属于 L1 热路径(绝不进 transport)。"""
    return event_type in L1_EVENT_TYPES


def is_l3_event(event_type: str) -> bool:
    """判断事件是否属于 L3 观测通知(异步旁路;L3 拦截点判定)。"""
    return event_type in L3_EVENT_TYPES


def is_shell_direct_event(event_type: str) -> bool:
    """判断事件是否为外壳直通事件(不进 transport、不投 L3)。"""
    return event_type in SHELL_DIRECT_EVENT_TYPES


class EventStream:
    """事件流引擎(阶段 2:分类 + L3 拦截点占位)。

    - L1:事件直接 yield 给外壳(async generator 直传,零拷贝)
    - L2:StepSummary/AfterResponse 由 pipeline/loop 构造后经钩子传递
    - L3:拦截点定义 —— 事件产生时调用 :meth:`route_l3` 判断是否应投 L3
      (阶段 2 仅记录,阶段 3 接入 BatchBuffer 批处理旁路)
    """

    def __init__(self, l3_sink: Optional[Any] = None) -> None:
        #: L3 观测通知接收器(阶段 3 接线;当前为 None = 仅拦截不投递)
        self.l3_sink: Optional[Any] = l3_sink

    def route_l3(self, event: Dict[str, Any]) -> None:
        """L3 拦截点:事件产生时调用,判断是否应投 L3 观测旁路。

        阶段 2:仅分类记录(L3 schema 对拍锚点);阶段 3 经 BatchBuffer
        批处理(50ms/64 条)+ 每观测扩展有界队列(1024)投递(§18.5)。
        """
        etype = event.get("type", "")
        if not is_l3_event(etype):
            return
        if self.l3_sink is None:
            logger.debug("L3 观测事件(阶段 2 仅拦截): %s", etype)
            return
        try:
            self.l3_sink.accept(event)
        except Exception as e:
            logger.error("L3 观测投递失败(旁路,不阻断对话): %s", e)

    def register_l3_sink(self, sink: Any) -> None:
        """注册 L3 观测通知接收器(阶段 3 transport 接线用)。"""
        self.l3_sink = sink


# ---------------------------------------------------------------------------
# L3 观测批处理旁路(§18.5,阶段 3 落地)
# ---------------------------------------------------------------------------
class L3ObserverQueue:
    """每观测扩展的有界投递队列(§18.5/§15-A6):容量上限,满则丢弃最旧 + 计数。

    投递失败(通道关闭/写失败)→ 整批计数丢弃(旁路 best-effort,绝不阻断主对话流)。
    """

    def __init__(
        self,
        extension_name: str,
        deliver_fn: Callable[[List[Dict[str, Any]]], Any],
        max_size: int = L3_QUEUE_MAX_SIZE,
    ) -> None:
        self.extension_name = extension_name
        self._deliver_fn = deliver_fn
        self.max_size = max_size
        self._events: deque = deque()
        #: 累计丢弃数(有界队列满丢弃最旧 + 投递失败丢弃)
        self.dropped: int = 0

    def put(self, event: Dict[str, Any]) -> None:
        """入队(非阻塞);满则丢弃最旧 + 计数(§15-A6 背压信号)。"""
        if len(self._events) >= self.max_size:
            self._events.popleft()
            self.dropped += 1
        self._events.append(event)

    async def deliver(self) -> None:
        """批量投递队列内全部事件(投递失败 → 整批计数丢弃,不重试不阻塞)。"""
        if not self._events:
            return
        batch = list(self._events)
        self._events.clear()
        try:
            result = self._deliver_fn(batch)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error("L3 观测投递失败(扩展 %s, %d 条): %s", self.extension_name, len(batch), e)
            self.dropped += len(batch)

    @property
    def pending(self) -> int:
        return len(self._events)


class L3BatchSink:
    """L3 观测批处理旁路(§18.5):BatchBuffer(50ms/64 条先到触发)+ 每观测扩展有界队列。

    不变量:L3 是异步旁路 —— :meth:`accept` 只追加缓冲 + 触发 flush task,
    绝不 await 网络/队列;丢弃仅计数,绝不阻断主对话流(§3.2)。
    """

    def __init__(
        self,
        batch_window: float = L3_BATCH_WINDOW_SECONDS,
        batch_max: int = L3_BATCH_MAX_EVENTS,
        queue_max: int = L3_QUEUE_MAX_SIZE,
    ) -> None:
        self.batch_window = batch_window
        self.batch_max = batch_max
        self.queue_max = queue_max
        #: 冲刷前全局缓冲上限(§15-A6 有界原则:观测旁路任何环节不无界)
        self.buffer_max: int = queue_max
        self._buffer: "deque[Dict[str, Any]]" = deque()
        self._observers: Dict[str, L3ObserverQueue] = {}
        self._flush_task: Optional[asyncio.Task] = None
        #: 定时窗口任务(与"在途冲刷"区分:立即冲刷只取消定时窗口,绝不取消在途冲刷)
        self._timer_task: Optional[asyncio.Task] = None
        self._closed = False
        #: 全局缓冲满丢弃最旧计数(观测者挂起等异常场景的背压信号)
        self._buffer_dropped: int = 0

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------
    def subscribe(self, extension_name: str, deliver_fn: Callable[[List[Dict[str, Any]]], Any]) -> None:
        """订阅 L3 观测通知(observe capability 扩展;重复订阅覆盖投递目标)。"""
        self._observers[extension_name] = L3ObserverQueue(
            extension_name, deliver_fn, max_size=self.queue_max
        )

    def unsubscribe(self, extension_name: str) -> None:
        """退订(投递前冲掉该队列中未投递事件)。"""
        queue = self._observers.pop(extension_name, None)
        if queue is not None and queue.pending:
            logger.info("L3 退订扩展 %s: 冲掉未投递 %d 条", extension_name, queue.pending)

    # ------------------------------------------------------------------
    # 接收(非阻塞,绝不阻断主对话流)
    # ------------------------------------------------------------------
    def accept(self, event: Dict[str, Any]) -> None:
        """接收 L3 事件(异步旁路):追加缓冲,满 64 立即 flush / 否则定时 50ms 冲刷。"""
        if self._closed or not self._observers:
            return
        if len(self._buffer) >= self.buffer_max:
            # 满则丢弃最旧 + 计数(§15-A6):观测者投递受阻时缓冲不无界堆积
            self._buffer.popleft()
            self._buffer_dropped += 1
        self._buffer.append(event)
        if len(self._buffer) >= self.batch_max:
            self._schedule_flush(immediate=True)
        else:
            self._start_timer()

    def _start_timer(self) -> None:
        """启动定时窗口(已有活跃窗口则不重复启动)。

        ``_timer_task`` 在**任务创建时**登记(而非任务首次运行时)—— 否则同一事件循环
        tick 内"先建窗口、再达 batch_max"会把未启动的定时任务误判为在途冲刷,
        导致即时冲刷被跳过(实测:即时触发失效)。
        """
        if self._timer_task is not None and not self._timer_task.done():
            return
        self._timer_task = asyncio.create_task(self._timer())
        self._flush_task = self._timer_task

    def _schedule_flush(self, immediate: bool = False) -> None:
        """冲刷调度:immediate=True 只取消**待触发的定时窗口**并立刻冲刷(§18.5 64 条先到)。

        在途冲刷(已取走缓冲、正在投递)绝不被取消 —— 其 ``while`` 循环会继续消费
        当前缓冲,故此时无需另起任务。
        """
        if not immediate:
            self._start_timer()
            return
        timer = self._timer_task
        if timer is not None and not timer.done():
            timer.cancel()
        self._timer_task = None
        current = self._flush_task
        if current is None or current is timer or current.done():
            self._flush_task = asyncio.create_task(self._flush())

    async def _timer(self) -> None:
        """定时窗口(batch_window):到期冲刷缓冲。"""
        me = asyncio.current_task()
        try:
            await asyncio.sleep(self.batch_window)
        finally:
            # 只清理"仍指向自己"的引用:期间若已调度了立即冲刷/新窗口,不得清别人的
            if self._timer_task is me:
                self._timer_task = None
            if self._flush_task is me:
                self._flush_task = None
        if self._buffer and not self._closed:
            await self._flush()

    async def _flush(self) -> None:
        """冲刷缓冲 → 分发各观测扩展有界队列 → 批量投递。

        循环消费到缓冲为空:投递期间新到达的事件(``accept`` 在 await 间隙追加)
        由同一任务继续送达,避免"缓冲非空却无待触发任务"的滞留。
        取缓冲是"拷贝 + 清空"的原子段(无 await),并发冲刷不会重复投递同一批。
        """
        while self._buffer:
            batch = list(self._buffer)
            self._buffer.clear()
            for queue in list(self._observers.values()):
                for ev in batch:
                    queue.put(ev)
                await queue.deliver()

    # ------------------------------------------------------------------
    # 可观测(丢弃计数随心跳上报,§15-A6)
    # ------------------------------------------------------------------
    def dropped_for(self, extension_name: str) -> int:
        """指定扩展的 L3 丢弃计数(队列满丢弃 + 投递失败)。"""
        queue = self._observers.get(extension_name)
        return queue.dropped if queue is not None else 0

    def total_dropped(self) -> int:
        return sum(q.dropped for q in self._observers.values()) + self._buffer_dropped

    async def close(self) -> None:
        """关闭(幂等):停收新事件 → **等完在途冲刷** → 冲刷缓冲 → 逐观察者兜底投递。

        只取消**定时窗口**;在途冲刷不取消而是 ``await`` 其完成(该批事件由它自己送达),
        之后已入队但未投递的事件再由兜底循环送达 —— 旁路 best-effort,但"已接收事件
        不因关闭而静默丢失"。

        2026-09-10 执行后审计 D-2:此前把 ``_flush_task`` 直接置 ``None`` 而不 await,
        而兜底循环因 ``queue.pending == 0`` 空转 ⇒ 已被 ``_flush`` 取走、正在投递的那批
        事件被静默丢弃(shutdown 期单次投递最长可达 5s,窗口真实存在)。
        """
        if self._closed:
            return
        self._closed = True
        timer, self._timer_task = self._timer_task, None
        if timer is not None and not timer.done():
            timer.cancel()
        inflight, self._flush_task = self._flush_task, None
        try:
            if inflight is not None and not inflight.done():
                try:
                    await inflight
                except asyncio.CancelledError:
                    # 只吞"我们自己刚取消的定时窗口任务";外部取消 close() 必须放行
                    if not inflight.cancelled():
                        raise
                except Exception as e:
                    logger.warning("L3 关闭等待在途冲刷异常(best-effort): %s", e)
            await self._flush()
            for queue in list(self._observers.values()):
                if queue.pending:
                    await queue.deliver()
        except Exception as e:
            logger.warning("L3 旁路关闭冲刷失败(best-effort,不阻断 shutdown): %s", e)
