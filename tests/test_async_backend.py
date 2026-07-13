"""异步 Backend 单元测试（spec async-llm-backend Task 3 SubTask 3.12）。

验证 :class:`AsyncOpenAICompatBackend.chat_stream` 与超时/取消机制的三个核心场景：

- 场景 A：LLM 卡死（不返 chunk）→ :class:`ActivityTimeout`
- 场景 B：正常流式 → 不超时，事件格式正确（text + done）
- 场景 C：``cancel_event.set()`` → :class:`StreamCancelled`

并覆盖：

- ``_with_activity_timeout`` wrapper 的超时/回调语义（spec SubTask 3.2 / 3.14）
- ``_is_retryable`` 对 ``ActivityTimeout`` / ``StreamCancelled`` 不重试（spec SubTask 3.3）
- ``async_retry_on_failure`` 装饰器对 async generator 的"已 yield 后不重试"语义
  （spec SubTask 3.15）

运行方式：
    python -m pytest tests/test_async_backend.py -v
    python -m unittest tests.test_async_backend -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.llm.client import (  # noqa: E402
    ActivityTimeout,
    AsyncOpenAICompatBackend,
    _is_retryable,
    _with_activity_timeout,
    async_retry_on_failure,
)
from hermes.stream_manager import StreamCancelled  # noqa: E402


# ---------------------------------------------------------------------------
# Mock async stream helpers
# ---------------------------------------------------------------------------


class _MockAsyncStream:
    """Mock OpenAI/Anthropic async stream：async iterable + ``close()``。

    模拟 ``await client.chat.completions.create(stream=True, ...)`` 返回的
    stream 对象：支持 ``async for chunk in stream`` 与 ``await stream.close()``。
    """

    def __init__(self, chunks: List[Any]) -> None:
        self._chunks: List[Any] = list(chunks)
        self._index: int = 0
        self.close_call_count: int = 0

    def __aiter__(self) -> "_MockAsyncStream":
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def close(self) -> None:
        self.close_call_count += 1


class _HangingAsyncStream:
    """Mock stream 永远不 yield（模拟 LLM 卡死）。

    ``__anext__`` 等待一个永不 set 的 :class:`asyncio.Event`，
    被 :func:`asyncio.wait_for` 超时取消时由 ``Event.wait`` 抛
    ``CancelledError``，``wait_for`` 转为 ``TimeoutError``，
    :func:`_with_activity_timeout` 再转为 :class:`ActivityTimeout`。
    """

    def __init__(self) -> None:
        self._event: asyncio.Event = asyncio.Event()
        self.close_call_count: int = 0

    def __aiter__(self) -> "_HangingAsyncStream":
        return self

    async def __anext__(self) -> Any:
        # 永久阻塞，直到被 wait_for 取消
        await self._event.wait()
        raise StopAsyncIteration  # pragma: no cover

    async def close(self) -> None:
        self.close_call_count += 1


def _make_text_chunk(
    content: str, finish_reason: Optional[str] = None
) -> MagicMock:
    """构造 OpenAI 风格的流式 chunk（含 ``delta.content``）。"""
    chunk = MagicMock()
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = None
    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk.choices = [choice]
    chunk.usage = None
    return chunk


def _build_backend() -> AsyncOpenAICompatBackend:
    """构造一个 ``AsyncOpenAICompatBackend`` 实例（真实 openai SDK 注入）。

    实例化仅需 ``api_key`` 字符串，不依赖真实网络。后续测试通过替换
    ``backend._client.chat.completions.create`` 注入 mock stream。
    """
    return AsyncOpenAICompatBackend(
        model="deepseek-chat",
        api_key="test-key-not-used",
        base_url="https://api.deepseek.com/v1",
        provider_name="deepseek",
    )


def _install_mock_stream(backend: AsyncOpenAICompatBackend, stream: Any) -> None:
    """让 ``backend._client.chat.completions.create`` 返回给定 stream。

    ``chat_stream`` 内部执行 ``stream = await self._client.chat.completions.create(...)``
    后 ``async for chunk in stream``，因此 ``create`` 必须是 async callable
    返回 async iterable。
    """
    backend._client.chat.completions.create = AsyncMock(return_value=stream)


# ---------------------------------------------------------------------------
# Test: _with_activity_timeout wrapper（spec SubTask 3.2 / 3.14）
# ---------------------------------------------------------------------------


class TestWithActivityTimeout(unittest.IsolatedAsyncioTestCase):
    """直接测试 ``_with_activity_timeout`` wrapper 语义。"""

    async def test_normal_iteration_no_timeout(self):
        """正常迭代不超时，原样透传每个 item。"""

        async def gen():
            for i in range(3):
                yield i

        result: List[int] = []
        async for item in _with_activity_timeout(gen(), timeout_sec=5.0):
            result.append(item)
        self.assertEqual(result, [0, 1, 2])

    async def test_timeout_raises_activity_timeout(self):
        """per-item 超时 → raise ActivityTimeout。"""

        async def gen():
            yield 1
            await asyncio.sleep(10)  # 第二个 item 永不到达
            yield 2  # pragma: no cover

        with self.assertRaises(ActivityTimeout):
            async for _ in _with_activity_timeout(gen(), timeout_sec=0.3):
                pass

    async def test_on_timeout_callback_invoked(self):
        """超时时调用 ``on_timeout`` 回调（用于 ``stream.close``）。"""
        callback_calls: List[str] = []

        async def on_timeout():
            callback_calls.append("called")

        async def gen():
            await asyncio.sleep(10)
            yield 1  # pragma: no cover

        with self.assertRaises(ActivityTimeout):
            async for _ in _with_activity_timeout(
                gen(), timeout_sec=0.2, on_timeout=on_timeout
            ):
                pass
        self.assertEqual(callback_calls, ["called"])

    async def test_on_timeout_failure_does_not_mask_activity_timeout(self):
        """``on_timeout`` 抛异常时不掩盖 :class:`ActivityTimeout`（spec SubTask 3.14）。"""

        async def bad_on_timeout():
            raise RuntimeError("close failed")

        async def gen():
            await asyncio.sleep(10)
            yield 1  # pragma: no cover

        with self.assertRaises(ActivityTimeout):
            async for _ in _with_activity_timeout(
                gen(), timeout_sec=0.2, on_timeout=bad_on_timeout
            ):
                pass

    async def test_empty_iterable_completes_cleanly(self):
        """空 iterable 立即结束，不超时。"""

        async def gen():
            return
            yield  # pragma: no cover

        result: List[Any] = []
        async for item in _with_activity_timeout(gen(), timeout_sec=5.0):
            result.append(item)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Test: _is_retryable（spec SubTask 3.3）
# ---------------------------------------------------------------------------


class TestIsRetryable(unittest.TestCase):
    """验证 ``_is_retryable`` 对 ``ActivityTimeout`` / ``StreamCancelled`` 不重试。"""

    def test_activity_timeout_not_retryable(self):
        """``ActivityTimeout`` 不重试（卡死超时不应浪费 token 重试）。"""
        self.assertFalse(_is_retryable(ActivityTimeout("stuck")))

    def test_stream_cancelled_not_retryable(self):
        """``StreamCancelled`` 不重试（用户主动取消）。"""
        self.assertFalse(_is_retryable(StreamCancelled("user cancel")))

    def test_429_retryable(self):
        """429 限流可重试。"""
        e = Exception("rate limited")
        e.status_code = 429  # type: ignore[attr-defined]
        self.assertTrue(_is_retryable(e))

    def test_500_retryable(self):
        """5xx 服务端错误可重试。"""
        e = Exception("server error")
        e.status_code = 500  # type: ignore[attr-defined]
        self.assertTrue(_is_retryable(e))

    def test_400_not_retryable(self):
        """4xx 客户端错误（非 429）不重试。"""
        e = Exception("bad request")
        e.status_code = 400  # type: ignore[attr-defined]
        self.assertFalse(_is_retryable(e))

    def test_no_status_code_retryable(self):
        """无 status_code（网络连接错误）可重试。"""
        self.assertTrue(_is_retryable(ConnectionError("network down")))


# ---------------------------------------------------------------------------
# Test: AsyncOpenAICompatBackend.chat_stream 三个核心场景（spec SubTask 3.12）
# ---------------------------------------------------------------------------


class TestAsyncOpenAICompatBackendChatStream(unittest.IsolatedAsyncioTestCase):
    """验证 ``AsyncOpenAICompatBackend.chat_stream`` 的三个核心场景。"""

    # ── 场景 A：LLM 卡死 → ActivityTimeout ──

    async def test_scenario_a_llm_hang_raises_activity_timeout(self):
        """LLM 卡死（不返 chunk）→ :class:`ActivityTimeout`。

        mock ``AsyncOpenAI`` 的 ``chat.completions.create`` 返回
        :class:`_HangingAsyncStream`（永不 yield），用很短的
        ``activity_timeout=0.5`` 加速测试，断言抛 :class:`ActivityTimeout`。
        """
        backend = _build_backend()
        hanging_stream = _HangingAsyncStream()
        _install_mock_stream(backend, hanging_stream)

        events: List[Any] = []
        with self.assertRaises(ActivityTimeout):
            async for event in backend.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                activity_timeout=0.5,
            ):
                events.append(event)

        # 卡死期间未收到任何事件
        self.assertEqual(events, [])
        # on_timeout 回调应已触发 stream.close（_with_activity_timeout 内）
        self.assertGreaterEqual(
            hanging_stream.close_call_count,
            1,
            "超时应触发 on_timeout 回调调用 stream.close",
        )

    # ── 场景 B：正常流式 → 不超时，事件格式正确 ──

    async def test_scenario_b_normal_stream_no_timeout(self):
        """正常流式 → 收到 text 事件 + done 事件，不超时。

        mock ``AsyncOpenAI`` 返回 2 个含 ``delta.content`` 的 chunk，
        断言收到 2 个 ``{"type":"text"}`` 事件 + 1 个 ``{"type":"done"}`` 事件，
        且 done 事件结构正确（``stop_reason`` / ``content_blocks`` / ``usage``）。
        """
        backend = _build_backend()
        chunks = [
            _make_text_chunk("hel"),
            _make_text_chunk("lo", finish_reason="stop"),
        ]
        stream = _MockAsyncStream(chunks)
        _install_mock_stream(backend, stream)

        events: List[Any] = []
        async for event in backend.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            activity_timeout=5.0,
        ):
            events.append(event)

        # 前 2 个是 text 事件，最后 1 个是 done 事件
        self.assertEqual(len(events), 3)
        text_events = [e for e in events if e["type"] == "text"]
        done_events = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(text_events), 2)
        self.assertEqual(len(done_events), 1)

        # text 事件内容
        self.assertEqual(text_events[0]["text"], "hel")
        self.assertEqual(text_events[1]["text"], "lo")

        # done 事件结构校验
        done = done_events[0]
        self.assertEqual(done["stop_reason"], "end_turn")
        self.assertIn("content_blocks", done)
        self.assertIn("usage", done)

        # 累积文本应出现在 content_blocks
        text_blocks = [
            b for b in done["content_blocks"] if b.get("type") == "text"
        ]
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(text_blocks[0]["text"], "hello")

        # finally 块应调用 stream.close 释放连接
        self.assertGreaterEqual(
            stream.close_call_count,
            1,
            "正常结束时 finally 应调用 stream.close",
        )

    async def test_scenario_b_no_text_chunks_done_event_still_emitted(self):
        """LLM 返回空流（无 text chunk）→ 仍发 done 事件，content_blocks 为空。"""
        backend = _build_backend()
        # 单个 chunk 无 content、无 finish_reason（占位用）
        empty_chunk = MagicMock()
        empty_delta = MagicMock()
        empty_delta.content = None
        empty_delta.tool_calls = None
        empty_choice = MagicMock()
        empty_choice.delta = empty_delta
        empty_choice.finish_reason = "stop"
        empty_chunk.choices = [empty_choice]
        empty_chunk.usage = None

        stream = _MockAsyncStream([empty_chunk])
        _install_mock_stream(backend, stream)

        events: List[Any] = []
        async for event in backend.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            activity_timeout=5.0,
        ):
            events.append(event)

        # 仅 done 事件，无 text 事件
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "done")
        # 无文本累积 → content_blocks 为空
        self.assertEqual(events[0]["content_blocks"], [])

    # ── 场景 C：cancel_event.set() → StreamCancelled ──

    async def test_scenario_c_cancel_event_pre_set_raises_stream_cancelled(self):
        """``cancel_event`` 在调用前已 set → 第一个 chunk 后抛 :class:`StreamCancelled`。

        mock ``AsyncOpenAI`` 返回一个会先 yield 1 个 chunk 然后阻塞的 stream；
        ``cancel_event`` 在调用前已 set，第一个 chunk 抵达后立即触发
        ``cancel_event.is_set()`` 检查并 raise :class:`StreamCancelled`。
        """
        backend = _build_backend()
        chunks = [_make_text_chunk("partial")]
        stream = _MockAsyncStream(chunks)
        _install_mock_stream(backend, stream)

        cancel_event = threading.Event()
        cancel_event.set()  # 调用前已 set

        with self.assertRaises(StreamCancelled):
            async for _ in backend.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                cancel_event=cancel_event,
                activity_timeout=5.0,
            ):
                pass

    async def test_scenario_c_cancel_after_first_chunk_raises_stream_cancelled(self):
        """``cancel_event`` 在第一个 chunk 之后 set → 第二轮检测到并抛 :class:`StreamCancelled`。

        更贴近真实场景：流式输出若干 chunk 后用户点取消，
        下一次 ``cancel_event.is_set()`` 检查时中断。
        """
        backend = _build_backend()
        chunks = [
            _make_text_chunk("first"),
            _make_text_chunk("second"),
        ]
        stream = _MockAsyncStream(chunks)
        _install_mock_stream(backend, stream)

        cancel_event = threading.Event()

        received: List[Any] = []
        with self.assertRaises(StreamCancelled):
            async for event in backend.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                cancel_event=cancel_event,
                activity_timeout=5.0,
            ):
                received.append(event)
                # 收到第一个 chunk 后触发 cancel
                if event["type"] == "text" and event["text"] == "first":
                    cancel_event.set()

        # 应只收到第一个 text 事件，第二个未到达（被 cancel 中断）
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "text")
        self.assertEqual(received[0]["text"], "first")


# ---------------------------------------------------------------------------
# Test: async_retry_on_failure 装饰器（spec SubTask 3.3 / 3.15）
# ---------------------------------------------------------------------------


class TestAsyncRetryOnFailure(unittest.IsolatedAsyncioTestCase):
    """验证 ``async_retry_on_failure`` 装饰器的重试与不重试语义。"""

    async def test_activity_timeout_not_retried(self):
        """``ActivityTimeout`` 不重试（spec SubTask 3.3）。"""
        call_count = 0

        @async_retry_on_failure(max_retries=3, base_delay=0.01)
        async def gen():
            nonlocal call_count
            call_count += 1
            raise ActivityTimeout("stuck")
            yield  # pragma: no cover

        with self.assertRaises(ActivityTimeout):
            async for _ in gen():
                pass
        self.assertEqual(call_count, 1, "ActivityTimeout 不应触发重试")

    async def test_stream_cancelled_not_retried(self):
        """``StreamCancelled`` 不重试。"""
        call_count = 0

        @async_retry_on_failure(max_retries=3, base_delay=0.01)
        async def gen():
            nonlocal call_count
            call_count += 1
            raise StreamCancelled("user cancel")
            yield  # pragma: no cover

        with self.assertRaises(StreamCancelled):
            async for _ in gen():
                pass
        self.assertEqual(call_count, 1, "StreamCancelled 不应触发重试")

    async def test_retryable_error_before_yield_retries(self):
        """可重试错误（连接错误，无 ``status_code``）在未 yield 时重试到成功。"""
        call_count = 0

        @async_retry_on_failure(max_retries=3, base_delay=0.01)
        async def gen():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("network down")
            yield "ok"

        result: List[str] = []
        async for item in gen():
            result.append(item)
        self.assertEqual(call_count, 3)
        self.assertEqual(result, ["ok"])

    async def test_error_after_yield_not_retried(self):
        """已 yield 后失败不重试（spec SubTask 3.15）。

        避免重试生成新流导致前端重复显示已输出内容。
        """
        call_count = 0

        @async_retry_on_failure(max_retries=3, base_delay=0.01)
        async def gen():
            nonlocal call_count
            call_count += 1
            yield "first"
            raise ConnectionError("network down after yield")

        result: List[str] = []
        with self.assertRaises(ConnectionError):
            async for item in gen():
                result.append(item)
        self.assertEqual(call_count, 1, "已 yield 后失败不应重试")
        self.assertEqual(result, ["first"])

    async def test_coroutine_retryable_error_retries(self):
        """``async def`` 协程（非 async generator）的重试语义。"""
        call_count = 0

        @async_retry_on_failure(max_retries=3, base_delay=0.01)
        async def coro():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("network down")
            return "done"

        result = await coro()
        self.assertEqual(call_count, 2)
        self.assertEqual(result, "done")

    async def test_coroutine_non_retryable_error_not_retried(self):
        """``async def`` 协程遇到 ``ActivityTimeout`` 不重试。"""
        call_count = 0

        @async_retry_on_failure(max_retries=3, base_delay=0.01)
        async def coro():
            nonlocal call_count
            call_count += 1
            raise ActivityTimeout("stuck")

        with self.assertRaises(ActivityTimeout):
            await coro()
        self.assertEqual(call_count, 1)

    def test_decorator_rejects_sync_function(self):
        """装饰非 async 函数应抛 ``TypeError``。"""

        with self.assertRaises(TypeError):

            @async_retry_on_failure(max_retries=2)
            def sync_fn():  # pragma: no cover
                return 1

            sync_fn()  # 装饰器在 apply 时即校验，不会执行到这里


# ---------------------------------------------------------------------------
# Test: backend chat_stream 通过 async_retry_on_failure 装饰器的端到端语义
# ---------------------------------------------------------------------------


class TestBackendChatStreamRetryIntegration(unittest.IsolatedAsyncioTestCase):
    """验证 ``chat_stream`` 经 ``@async_retry_on_failure()`` 装饰后的端到端语义。"""

    async def test_first_call_connection_error_retried_then_succeeds(self):
        """首次连接失败（未 yield）→ 重试成功（spec SubTask 3.15）。

        ``chat_stream`` 被 ``@async_retry_on_failure()`` 默认装饰（3 次重试，
        base_delay=1.0）。测试中用 monkey-patch 缩短退避以加速。
        """
        backend = _build_backend()
        chunks = [_make_text_chunk("hi", finish_reason="stop")]
        stream = _MockAsyncStream(chunks)
        _install_mock_stream(backend, stream)

        # 第一次 create 抛连接错误（可重试），第二次返回 stream
        original_create = backend._client.chat.completions.create
        call_count = 0

        async def flaky_create(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("first attempt network down")
            return await original_create(*args, **kwargs)

        backend._client.chat.completions.create = flaky_create

        # 用 monkey-patch 把 base_delay 缩短到 0.01s（避免 1s 退避拖慢测试）
        # 直接调用底层未装饰的方法不可行（装饰器已 wrap），改为临时替换
        # _is_retryable 的退避时长不可行；改为直接构造一个快速装饰器实例验证
        # 语义。这里改为：直接验证 chat_stream 默认装饰器在首次失败后会重试，
        # 接受 1s 退避（测试耗时 ~1s）。
        events: List[Any] = []
        async for event in backend.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            activity_timeout=5.0,
        ):
            events.append(event)

        self.assertEqual(call_count, 2, "首次失败应触发一次重试")
        text_events = [e for e in events if e["type"] == "text"]
        self.assertEqual(len(text_events), 1)
        self.assertEqual(text_events[0]["text"], "hi")

    async def test_activity_timeout_no_retry_via_decorator(self):
        """``chat_stream`` 抛 ``ActivityTimeout`` 时不重试，直接冒泡。"""
        backend = _build_backend()
        hanging_stream = _HangingAsyncStream()
        _install_mock_stream(backend, hanging_stream)

        create_call_count = 0
        original_create = backend._client.chat.completions.create

        async def counting_create(*args, **kwargs):
            nonlocal create_call_count
            create_call_count += 1
            return await original_create(*args, **kwargs)

        backend._client.chat.completions.create = counting_create

        with self.assertRaises(ActivityTimeout):
            async for _ in backend.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                activity_timeout=0.3,
            ):
                pass

        # ActivityTimeout 不重试 → create 仅调用 1 次
        self.assertEqual(
            create_call_count,
            1,
            "ActivityTimeout 不应触发 create 重试",
        )


# ======================================================================
# spec integrate-llm-reasoning-mode Task 22
# P0-5: _strip_thinking_blocks 测试 + Task 16: REASONING_CONFIG_INVALID 测试
# ======================================================================

class TestStripThinkingBlocks(unittest.TestCase):
    """P0-5: AsyncAnthropicBackend._strip_thinking_blocks 测试。

    验证 reasoning 未启用时清理 thinking block，避免 Anthropic 400 错误。
    """

    def test_removes_thinking_blocks_from_content(self):
        """thinking block 从 content 列表中移除。"""
        from hermes.llm.client import AsyncAnthropicBackend
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "secret", "signature": "sig123"},
                {"type": "text", "text": "hello"},
            ],
        }]
        result = AsyncAnthropicBackend._strip_thinking_blocks(msgs)
        types = [b.get("type") for b in result[0]["content"]]
        self.assertNotIn("thinking", types)
        self.assertIn("text", types)

    def test_removes_reasoning_content_field(self):
        """reasoning_content 字段从消息中移除。"""
        from hermes.llm.client import AsyncAnthropicBackend
        msgs = [{
            "role": "assistant",
            "content": "hello",
            "reasoning_content": "secret reasoning",
        }]
        result = AsyncAnthropicBackend._strip_thinking_blocks(msgs)
        self.assertNotIn("reasoning_content", result[0])

    def test_preserves_string_content(self):
        """字符串 content 原样保留。"""
        from hermes.llm.client import AsyncAnthropicBackend
        msgs = [{"role": "user", "content": "hello world"}]
        result = AsyncAnthropicBackend._strip_thinking_blocks(msgs)
        self.assertEqual(result[0]["content"], "hello world")

    def test_preserves_tool_use_blocks(self):
        """tool_use block 保留。"""
        from hermes.llm.client import AsyncAnthropicBackend
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "think", "signature": "sig"},
                {"type": "tool_use", "id": "tu1", "name": "file_read", "input": {}},
            ],
        }]
        result = AsyncAnthropicBackend._strip_thinking_blocks(msgs)
        types = [b.get("type") for b in result[0]["content"]]
        self.assertNotIn("thinking", types)
        self.assertIn("tool_use", types)

    def test_does_not_modify_input(self):
        """不修改入参（返回新列表）。"""
        from hermes.llm.client import AsyncAnthropicBackend
        original = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "secret", "signature": "sig"},
            ],
        }]
        AsyncAnthropicBackend._strip_thinking_blocks(original)
        # 原始列表未被修改
        self.assertEqual(len(original[0]["content"]), 1)
        self.assertEqual(original[0]["content"][0]["type"], "thinking")


class TestReasoningConfigInvalidClassification(unittest.TestCase):
    """Task 16: REASONING_CONFIG_INVALID 错误分类测试。"""

    def test_budget_tokens_error(self):
        """budget_tokens 关键词识别。"""
        from hermes.agent.error_classifier import ErrorClassifier, ErrorClass
        ec, _ = ErrorClassifier.classify(
            "llm_call", {},
            "Error: thinking budget_tokens must be at least 1024"
        )
        self.assertEqual(ec, ErrorClass.REASONING_CONFIG_INVALID)

    def test_max_tokens_budget_error(self):
        """max_tokens < budget 错误识别。"""
        from hermes.agent.error_classifier import ErrorClassifier, ErrorClass
        ec, _ = ErrorClassifier.classify(
            "llm_call", {},
            "max_tokens must be greater than budget_tokens"
        )
        self.assertEqual(ec, ErrorClass.REASONING_CONFIG_INVALID)

    def test_thinking_type_enabled_error(self):
        """thinking type enabled 错误识别。"""
        from hermes.agent.error_classifier import ErrorClassifier, ErrorClass
        ec, _ = ErrorClassifier.classify(
            "llm_call", {},
            "thinking.type must be 'enabled' or 'disabled'"
        )
        self.assertEqual(ec, ErrorClass.REASONING_CONFIG_INVALID)

    def test_reasoning_effort_error(self):
        """reasoning effort 错误识别。"""
        from hermes.agent.error_classifier import ErrorClassifier, ErrorClass
        ec, _ = ErrorClassifier.classify(
            "llm_call", {},
            "invalid reasoning effort 'ultra'"
        )
        self.assertEqual(ec, ErrorClass.REASONING_CONFIG_INVALID)

    def test_normal_error_not_misclassified(self):
        """普通错误不被误分类为 REASONING_CONFIG_INVALID。"""
        from hermes.agent.error_classifier import ErrorClassifier, ErrorClass
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {},
            "文件不存在: /tmp/test.txt"
        )
        self.assertNotEqual(ec, ErrorClass.REASONING_CONFIG_INVALID)


class TestChatConsolidationNoSideEffect(unittest.IsolatedAsyncioTestCase):
    """P0-4: chat_consolidation 不修改入参 reasoning_cfg 对象。

    验证 dataclasses.replace 创建新对象，原入参 enabled 不被污染。
    """

    async def test_original_reasoning_cfg_not_modified(self):
        """传入的 reasoning_cfg.enabled 在调用后保持原值。"""
        from hermes.llm.reasoning_profiles import ReasoningConfig
        from hermes.llm.client import LLMClient
        from unittest.mock import MagicMock, AsyncMock

        original_cfg = ReasoningConfig(enabled=True, effort="high", budget_tokens=10000)
        mock_backend = MagicMock()
        mock_backend.chat = AsyncMock(return_value=MagicMock())

        # 直接构造实例，不走 __init__（避免依赖外部配置）
        client = LLMClient.__new__(LLMClient)
        client._consolidation_backend = mock_backend
        client._consolidation_reasoning_cfg = ReasoningConfig(enabled=False)

        await client.chat_consolidation(
            messages=[{"role": "user", "content": "test"}],
            reasoning_cfg=original_cfg,
        )

        # 原入参对象未被修改
        self.assertTrue(original_cfg.enabled, "原 reasoning_cfg.enabled 应保持 True")
        self.assertEqual(original_cfg.effort, "high")
        self.assertEqual(original_cfg.budget_tokens, 10000)
        # 传给 backend 的 reasoning_cfg.enabled 应为 False
        actual_cfg = mock_backend.chat.call_args.kwargs.get("reasoning_cfg")
        self.assertFalse(actual_cfg.enabled)
        # 且是新对象（非同一引用）
        self.assertIsNot(actual_cfg, original_cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
