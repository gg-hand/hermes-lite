"""LLM 层语义测试:重试判定 / 超时与取消不重试。"""

from __future__ import annotations

import asyncio

import pytest

from teage_liu2.core.llm import ActivityTimeout, _is_retryable


class _FakeStatusError(Exception):
    """模拟带 status_code 的 SDK 异常。"""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_activity_timeout_never_retry():
    """验收:ActivityTimeout 绝不重试(卡死不浪费 token)。"""
    assert _is_retryable(ActivityTimeout()) is False


def test_retryable_statuses():
    """验收:429 限流与 5xx 可重试,4xx 非 429 不重试。"""
    assert _is_retryable(_FakeStatusError(429)) is True
    assert _is_retryable(_FakeStatusError(500)) is True
    assert _is_retryable(_FakeStatusError(503)) is True
    assert _is_retryable(_FakeStatusError(400)) is False
    assert _is_retryable(_FakeStatusError(401)) is False
    assert _is_retryable(_FakeStatusError(403)) is False


def test_network_error_retryable():
    """验收:无状态码的网络错误可重试。"""

    class _ConnError(Exception):
        pass

    assert _is_retryable(_ConnError("connection reset")) is True


def test_retry_decorator_retries_transient_then_succeeds():
    """验收:async_retry_on_failure 对瞬态错误重试后成功(不重复调用副作用)。"""
    from teage_liu2.core.llm import async_retry_on_failure

    calls = {"n": 0}

    @async_retry_on_failure(max_retries=3, base_delay=0.01)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _FakeStatusError(429)
        return "ok"

    result = asyncio.run(flaky())
    assert result == "ok"
    assert calls["n"] == 2


def test_retry_decorator_gives_up_after_max():
    """验收:超过最大重试次数后抛出原始异常。"""
    from teage_liu2.core.llm import async_retry_on_failure

    @async_retry_on_failure(max_retries=2, base_delay=0.01)
    async def always_fail():
        raise _FakeStatusError(503)

    with pytest.raises(_FakeStatusError):
        asyncio.run(always_fail())


def test_retry_decorator_never_retries_activity_timeout():
    """验收:ActivityTimeout 不进入重试循环(直接抛出)。"""
    from teage_liu2.core.llm import async_retry_on_failure

    calls = {"n": 0}

    @async_retry_on_failure(max_retries=3, base_delay=0.01)
    async def timeout_always():
        calls["n"] += 1
        raise ActivityTimeout("60s 无输出")

    with pytest.raises(ActivityTimeout):
        asyncio.run(timeout_always())
    assert calls["n"] == 1  # 只调用一次,无重试


def test_activity_timeout_wrapper_passthrough():
    """验收:活跃超时 wrapper 对正常流逐项透传,不改内容。"""
    from teage_liu2.core.llm import _with_activity_timeout

    async def _fast():
        for c in "abc":
            yield c

    got: list = []

    async def run():
        async for item in _with_activity_timeout(_fast(), 1.0):
            got.append(item)

    asyncio.run(run())
    assert got == ["a", "b", "c"]


def test_activity_timeout_wrapper_calls_on_timeout_then_raises():
    """验收:空闲超时 → 先 await on_timeout(关流),再抛 ActivityTimeout。"""
    from teage_liu2.core.llm import _with_activity_timeout

    marks: list = []

    async def _slow():
        yield "a"
        await asyncio.sleep(5)
        yield "b"

    async def _on_timeout():
        marks.append("closed")

    async def run():
        async for _ in _with_activity_timeout(_slow(), 0.05, _on_timeout):
            pass

    with pytest.raises(ActivityTimeout):
        asyncio.run(run())
    assert marks == ["closed"]


def test_activity_timeout_wrapper_does_not_swallow_external_cancel():
    """验收:外部取消(任务 cancel)不得被误转成 ActivityTimeout。"""
    from teage_liu2.core.llm import _with_activity_timeout

    async def _blocked():
        yield "a"
        await asyncio.sleep(5)

    async def main():
        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "not-cancelled"

    async def _consume():
        async for _ in _with_activity_timeout(_blocked(), 30.0):
            pass

    assert asyncio.run(main()) == "cancelled"
