"""stdio 读循环健壮性:单帧异常不得终止整条通道(§15-A7 拒绝语义)。"""
from __future__ import annotations

import asyncio

from teage_liu2.core.stdio import StdioChannel
from teage_liu2.core.transport import TransportFrame


class _FakeReader:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeWriter:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        return None


def _run(lines, handler):
    async def go():
        channel = StdioChannel("t", ["x"], handler)
        channel._reader = _FakeReader(lines)
        channel._writer = _FakeWriter()
        await channel._read_loop()

    asyncio.run(go())


def _collecting(seen):
    async def host_handler(name, frame):
        seen.append(frame.type)
        return {"result": {}}

    return host_handler


def test_read_loop_survives_deep_frame():
    """验收:超深帧(解析期 RecursionError)被丢弃,其后的有效帧仍被处理。"""
    seen = []
    deep = ('{"type":"heartbeat","payload":{"a":' + "[" * 20000 + "]" * 20000 +
            '},"encoding":"full","protocol_version":"v1.0.0"}')
    good = TransportFrame("heartbeat", {"ok": True}).encode()
    _run([deep.encode() + b"\n", good.encode() + b"\n", b""], _collecting(seen))
    assert seen == ["heartbeat"], "非法帧之后的有效帧必须仍被处理(读循环不得退出)"


def test_read_loop_survives_illegal_json():
    """验收:非 JSON 行被丢弃,后续有效帧仍被处理。"""
    seen = []
    good = TransportFrame("heartbeat", {"ok": True}).encode()
    _run([b"not-json\n", good.encode() + b"\n", b""], _collecting(seen))
    assert seen == ["heartbeat"]


def test_read_loop_survives_dispatch_error():
    """护栏:宿主处理器抛错被 ``_dispatch`` 内部隔离,读循环照常继续。

    说明(2026-09-10 执行后审计 D-12):本用例**对 task 3 的修复不承重** ——
    ``_dispatch`` 自身早已 ``try/except`` 包住宿主处理器(异常被转成 error 响应帧),
    故装载旧 ``stdio`` 实现时本用例仍绿。保留它是为锁定不变量"分发异常不得冒泡到
    读循环"。真正的红锚是 ``test_read_loop_survives_unhashable_type_field``。
    """
    seen = []
    good = TransportFrame("heartbeat", {"ok": True}).encode()
    boom = TransportFrame("heartbeat", {"boom": True}).encode()

    async def host_handler(name, frame):
        if frame.payload.get("boom"):
            raise RuntimeError("handler 炸了")
        seen.append(frame.type)
        return {"result": {}}

    _run([boom.encode() + b"\n", good.encode() + b"\n", b""], host_handler)
    assert seen == ["heartbeat"], "分发异常不得终止读循环"


def test_read_loop_survives_unhashable_type_field():
    """验收:type/encoding 字段为非法 JSON 类型(数组)时,decode 抛 TypeError。

    旧实现读循环只捕 ``ValueError``:``obj["type"] not in MESSAGE_TYPES`` 对不可
    哈希值(数组/对象)抛 TypeError → 逃出内层 except → 外层 except 终止整条
    读循环(通道死亡)。单帧容错必须覆盖一切异常(V-1 拒绝语义)。
    """
    seen = []
    bad = '{"type":["heartbeat"],"payload":{},"encoding":"full","protocol_version":"v1.0.0"}'
    good = TransportFrame("heartbeat", {"ok": True}).encode()
    _run([bad.encode() + b"\n", good.encode() + b"\n", b""], _collecting(seen))
    assert seen == ["heartbeat"], "不可哈希 type 字段不得终止读循环"
