"""L3 观测批处理旁路(§18.5):50ms 窗口 / 64 条先到触发 / 有界队列 / close 兜底。"""
from __future__ import annotations

import asyncio
from collections import deque

from teage_liu2.core.event_stream import L3BatchSink


def _sink(window: float, batch_max: int, queue_max: int = 1024):
    sink = L3BatchSink(batch_window=window, batch_max=batch_max, queue_max=queue_max)
    got: list = []

    async def deliver(batch):
        got.extend(batch)

    sink.subscribe("obs", deliver)
    return sink, got


def test_batch_max_triggers_immediate_flush():
    """验收:达到 batch_max 立即冲刷,不等窗口(窗口设 10s 以示区分)。

    断言必须在 close() **之前** —— close() 自身会冲刷缓冲,放在其后则修复前后
    都通过(该用例将失去判别力)。
    """
    sink, got = _sink(10.0, 4)

    async def run():
        for i in range(4):
            sink.accept({"type": "tool_result", "i": i})
        await asyncio.sleep(0.05)   # 远小于 10s 窗口:只有即时触发才可能此刻已投递
        delivered = [e["i"] for e in got]
        buffered = len(sink._buffer)
        await sink.close()
        return delivered, buffered

    delivered, buffered = asyncio.run(run())
    assert delivered == [0, 1, 2, 3], "达到 batch_max 必须立即冲刷(不等窗口)"
    assert buffered == 0, "即时冲刷后缓冲必须为空"


def test_immediate_flush_does_not_cancel_inflight_delivery():
    """验收:在途冲刷不被取消,其批次照常送达(取消在途会丢事件)。"""
    sink = L3BatchSink(batch_window=10.0, batch_max=2)
    got: list = []

    async def deliver(batch):
        await asyncio.sleep(0.02)   # 制造"在途"窗口
        got.extend(batch)

    sink.subscribe("obs", deliver)

    async def run():
        sink.accept({"type": "tool_result", "i": 0})
        sink.accept({"type": "tool_result", "i": 1})   # 触发即时冲刷(开始投递)
        await asyncio.sleep(0.005)                      # 此刻冲刷在途
        sink.accept({"type": "tool_result", "i": 2})
        sink.accept({"type": "tool_result", "i": 3})   # 再次触发即时冲刷
        await asyncio.sleep(0.2)
        delivered = sorted(e["i"] for e in got)         # ← 断言取在 close() 之前
        await sink.close()
        return delivered

    assert asyncio.run(run()) == [0, 1, 2, 3], "在途冲刷不得被取消/丢事件"


def test_buffer_is_deque_and_drops_oldest():
    """验收:缓冲为 deque(O(1) 丢弃);满则丢最旧并计数。"""
    sink, got = _sink(10.0, 1000, queue_max=3)

    async def run():
        for i in range(5):
            sink.accept({"type": "tool_result", "i": i})
        assert isinstance(sink._buffer, deque)
        assert sink.total_dropped() == 2
        await sink.close()

    asyncio.run(run())
    assert [e["i"] for e in got] == [2, 3, 4], "满缓冲丢最旧,保留最新"


def test_timer_window_still_flushes():
    """验收:不足 batch_max 时仍由窗口定时冲刷,且冲刷后窗口可再次建立。"""
    sink, got = _sink(0.02, 64)

    async def run():
        sink.accept({"type": "tool_result", "i": 0})
        await asyncio.sleep(0.06)
        assert [e["i"] for e in got] == [0]
        sink.accept({"type": "tool_result", "i": 1})
        await asyncio.sleep(0.06)
        await sink.close()

    asyncio.run(run())
    assert [e["i"] for e in got] == [0, 1], "第二次窗口必须照常建立"


def test_close_delivers_everything():
    """验收:close 冲刷缓冲与各观察者队列,不丢事件。"""
    sink, got = _sink(10.0, 1000)

    async def run():
        for i in range(5):
            sink.accept({"type": "tool_result", "i": i})
        await sink.close()

    asyncio.run(run())
    assert [e["i"] for e in got] == [0, 1, 2, 3, 4]


def test_close_awaits_inflight_flush():
    """验收:close 必须等待**在途冲刷**完成 —— 已接收事件不因关闭而静默丢失。

    旧实现把 ``_flush_task`` 直接置 None(不 await),而兜底循环因 ``queue.pending == 0``
    空转 ⇒ 已被 ``_flush`` 取走、正在投递的那批事件静默丢失(2026-09-10 执行后审
    计 D-2)。close 时不存在缓冲残留时该用例唯一锚定"在途"窗口。
    """
    sink = L3BatchSink(batch_window=10.0, batch_max=1)
    got: list = []

    async def deliver(batch):
        await asyncio.sleep(0.05)   # 制造"close 时投递仍在途"的窗口
        got.extend(batch)

    sink.subscribe("obs", deliver)

    async def run():
        sink.accept({"type": "tool_result", "i": 0})   # batch_max=1 → 立刻冲刷
        await asyncio.sleep(0.005)                      # 此刻投递在途
        await sink.close()
        return [e["i"] for e in got]

    assert asyncio.run(run()) == [0], "close 必须等待在途冲刷,事件不得静默丢失"


def test_inflight_flush_continues_draining_new_events():
    """验收:在途冲刷期间新到达的事件由**同一任务**续送,不得滞留到 close。

    ``_flush`` 的 ``while`` 消费语义:若退化为 ``if``(取一批就返回),在途期间到达的
    事件会滞留在缓冲且无待触发任务(直到窗口/close 才送出)——2026-09-10 审计的
    覆盖缺口 #20。断言取在 close() **之前**,否则 close 会兜底送出而失去判别力。
    """
    sink = L3BatchSink(batch_window=10.0, batch_max=1)
    got: list = []

    async def deliver(batch):
        await asyncio.sleep(0.01)   # 每次投递制造"在途"窗口
        got.extend(batch)

    sink.subscribe("obs", deliver)

    async def run():
        for i in range(20):
            sink.accept({"type": "tool_result", "i": i})
            await asyncio.sleep(0.002)   # 让前几批进入"在途冲刷"
        await asyncio.sleep(0.5)          # 远小于 10s 窗口:只有续送才可能全部送达
        delivered = sorted(e["i"] for e in got)
        buffered = len(sink._buffer)
        await sink.close()
        return delivered, buffered

    delivered, buffered = asyncio.run(run())
    assert delivered == list(range(20)), "在途冲刷期间新到达的事件必须由同一任务续送"
    assert buffered == 0, "续送后缓冲必须为空"
