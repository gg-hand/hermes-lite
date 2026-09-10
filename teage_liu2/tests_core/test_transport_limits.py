"""transport 序列化边界(§15-A7):深度校验健壮性 + 体积口径唯一化。"""
from __future__ import annotations

import json

import pytest

from teage_liu2.core.transport import (
    FRAME_MAX_BYTES,
    JSON_MAX_DEPTH,
    TransportFrame,
    check_frame_depth,
    json_depth,
)


def _ref_depth(obj, depth=0):
    """递归参考实现(契约定义):深度校验必须与它逐值一致。"""
    if isinstance(obj, dict):
        return depth + 1 if not obj else max(_ref_depth(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return depth + 1 if not obj else max(_ref_depth(v, depth + 1) for v in obj)
    return depth


def test_json_depth_matches_reference():
    """验收:迭代实现与递归定义逐值一致(含空容器/纯标量/混合嵌套)。"""
    samples = [
        1, "x", None, [], {}, [[]], [{}], {"a": {}},
        {"a": [{"b": [1, {"c": 2}]}]},
        {"x": [1, 2, 3], "y": {"z": {"w": []}}},
    ]
    for obj in samples:
        assert json_depth(obj) == _ref_depth(obj), obj


def test_json_depth_survives_deep_nesting():
    """验收:深嵌套不再抛 RecursionError(旧实现数百层即崩)。"""
    obj = 1
    for _ in range(2000):
        obj = [obj]
    # 深度 = 最外层到最内层容器的层数(标量不加深度):2000 层列表 → 2000
    assert json_depth(obj) == 2000


def test_decode_rejects_deep_frame_as_value_error():
    """验收:超深帧被拒且异常类型是 ValueError(旧实现在此处抛 RecursionError)。"""
    deep = '{"type":"heartbeat","payload":{"a":' + "[" * 20000 + "]" * 20000 + \
           '},"encoding":"full","protocol_version":"v1.0.0"}'
    with pytest.raises(ValueError):
        TransportFrame.decode(deep)


def test_decode_rejects_frame_over_wire_bytes():
    """验收:体积以线上字节数计(与 stdio 读循环的 len(line_bytes) 同口径)。"""
    padding = {"type": "heartbeat", "payload": {"p": "x" * (FRAME_MAX_BYTES + 10)},
               "encoding": "full", "protocol_version": "v1.0.0"}
    line = json.dumps(padding)
    with pytest.raises(ValueError):
        TransportFrame.decode(line)


def test_decode_accepts_frame_under_limits():
    """验收:合法帧正常解码,raw_bytes 参数不改变结果。"""
    line = json.dumps({"type": "heartbeat", "payload": {"ok": True},
                       "encoding": "full", "protocol_version": "v1.0.0"})
    assert TransportFrame.decode(line).type == "heartbeat"
    assert TransportFrame.decode(line, raw_bytes=len(line.encode("utf-8"))).type == "heartbeat"


def test_check_frame_depth_boundary():
    """验收:深度 64 合法、65 拒绝(常量不得变动)。"""
    ok = {"a": 1}
    for _ in range(JSON_MAX_DEPTH - 1):
        ok = {"a": ok}
    assert check_frame_depth(ok) is None        # 深度恰为 64
    too_deep = {"a": ok}
    assert check_frame_depth(too_deep) is not None   # 深度 65 → 拒绝


def test_check_frame_limits_semantics():
    """验收:check_frame_limits(宿主出站/编程式)按**对象 dumps 体积**校验。

    与 :meth:`TransportFrame.decode` 的**线上字节**口径不同(见其 docstring 对照):
    本用例锁定它的返回语义(深度越界 / 体积越界 / 不可序列化)。2026-09-10 执行后
    审计 D-3:该函数在 decode 改用 `check_frame_depth` 后成为零调用方,故补此锚点,
    避免"保留 API 无覆盖"。
    """
    from teage_liu2.core.transport import check_frame_limits

    assert check_frame_limits({"a": 1}) is None

    too_deep = {"a": 1}
    for _ in range(JSON_MAX_DEPTH):
        too_deep = {"a": too_deep}
    assert "深度" in check_frame_limits(too_deep)

    assert "体积" in check_frame_limits({"p": "x" * (FRAME_MAX_BYTES + 1)})
    assert "序列化" in check_frame_limits({"p": object()})
