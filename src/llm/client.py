"""LLM 客户端封装（多 Provider 支持，异步实现）。

封装主对话 LLM 与 consolidation LLM 两个客户端，支持多种 Provider：

- ``anthropic``：原生 Anthropic Claude（claude-sonnet / claude-haiku 等）
- ``openai``：OpenAI GPT 系列
- ``deepseek``：DeepSeek（OpenAI 兼容 API）
- ``qwen``：阿里通义千问（DashScope OpenAI 兼容模式）

DeepSeek / Qwen 均提供 OpenAI 兼容 API，复用同一 ``AsyncOpenAICompatBackend``
实现，仅 ``base_url`` 与默认环境变量不同。

异步化要点（spec: async-llm-backend）：

- 所有 Backend 改用 ``anthropic.AsyncAnthropic`` / ``openai.AsyncOpenAI``，
  ``chat`` 为 ``async def``，``chat_stream`` 为 async generator。
- 新增 :class:`ActivityTimeout` 与 :func:`_with_activity_timeout` 实现
  per-token 活跃超时，LLM 卡死时立即中断。
- 新增 :func:`async_retry_on_failure` 用 ``asyncio.sleep`` 退避，不阻塞
  事件循环；对 async generator 仅在未 yield 任何 item 时重试。
- :class:`LLMClient` 的 ``chat_main`` / ``chat_consolidation`` / ``chat_main_stream``
  改 async；新增 ``chat_main_sync`` / ``chat_consolidation_sync`` 供
  workflow / memory 线程池路径调用（内部 ``asyncio.run``）。

为保持 ``react_loop`` 不变，所有 Backend 对外统一返回 ``LLMResponse``
对象，其接口兼容 ``anthropic.types.Message``：

- ``response.content``：content block 列表，每项为
  ``{"type": "text", "text": ...}`` 或
  ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``
- ``response.stop_reason``：``"tool_use"`` / ``"end_turn"`` / ``"max_tokens"``

工具 schema 在内部完成 Anthropic ↔ OpenAI 格式互转，
React 循环外部接口保持 Anthropic 风格不变。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import anthropic
import tiktoken

try:
    from ..stream_manager import StreamCancelled
except ImportError:
    import sys
    from pathlib import Path
    _SRC_DIR = str(Path(__file__).resolve().parent.parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from stream_manager import StreamCancelled  # type: ignore

if TYPE_CHECKING:
    try:
        from ..monitoring.metrics import MetricsCollector
        from ..stream_manager import StreamManager
    except ImportError:
        from monitoring.metrics import MetricsCollector  # type: ignore
        from stream_manager import StreamManager  # type: ignore

# 兼容相对导入与直接运行两种方式
try:
    from ..config import get_llm_timeouts, load_config
except ImportError:  # pragma: no cover - 直接运行模块时回退
    import sys
    from pathlib import Path

    _SRC_DIR = str(Path(__file__).resolve().parent.parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from config import get_llm_timeouts, load_config  # type: ignore

logger = logging.getLogger(__name__)

# 默认输出 token 上限
_DEFAULT_MAX_TOKENS_MAIN = 4096
_DEFAULT_MAX_TOKENS_CONSOLIDATION = 8192

# tiktoken 编码器（分词器近似估算）
_ENCODING: Optional[tiktoken.Encoding] = None


# 各 Provider 的默认 base_url（OpenAI 兼容类）
_PROVIDER_DEFAULT_BASE_URL: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}

# 各 Provider 默认的环境变量名（API Key）
_PROVIDER_DEFAULT_ENV_KEY: Dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
}

# OpenAI finish_reason → Anthropic stop_reason 映射
_FINISH_REASON_MAP: Dict[str, str] = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}


def _get_encoding() -> tiktoken.Encoding:
    """惰性加载 tiktoken 编码器（单例）。"""
    global _ENCODING
    if _ENCODING is None:
        # cl100k_base 对主流模型是一个合理的近似估算
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


class ActivityTimeout(Exception):
    """per-token 活跃超时：LLM 在 ``activity_timeout`` 秒内未返回任何 token。

    被 :func:`async_retry_on_failure` 装饰器捕获时不重试（``_is_retryable``
    中检查），区别于网络错误/限流等可重试异常。冒泡至 server.py SSE handler
    后 yield ``{"type":"error","message":"LLM 响应超时（Ns 无输出）"}``。
    """
    pass


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否可重试。

    429 限流、5xx 服务端错误、网络连接错误（无 ``status_code``）可重试；
    4xx 客户端错误（非 429）不重试，立即抛出。

    对 ``StreamCancelled``（用户取消）与 ``ActivityTimeout``（卡死超时）
    直接返回 False，避免重试浪费 token 或重复显示已输出内容。

    对 Anthropic SDK 异常（``RateLimitError`` / ``APIStatusError`` /
    ``APIConnectionError``）与 OpenAI 兼容 SDK 异常统一处理：
    优先检查 ``status_code`` 属性，无 ``status_code`` 视为连接错误可重试。
    """
    # 用户主动取消 / 卡死超时 → 绝对不重试
    if isinstance(exc, (StreamCancelled, ActivityTimeout)):
        return False
    status = getattr(exc, "status_code", None)
    if status is not None:
        # 有 HTTP 状态码：429 或 5xx 重试，4xx 非 429 不重试
        return status == 429 or status >= 500
    # 无状态码（网络连接错误等）：重试
    return True


async def _with_activity_timeout(
    aiter: AsyncIterator[Any],
    timeout_sec: float,
    on_timeout: Optional[Callable[[], Awaitable[None]]] = None,
) -> AsyncIterator[Any]:
    """通用 per-item 活跃超时 wrapper（async generator）。

    对输入 async iterable 逐项用 ``asyncio.wait_for`` 等待，每个 item 之间
    最多等 ``timeout_sec`` 秒。超时则调用 ``on_timeout``（用 try/except
    保护，``on_timeout`` 失败记录 warning 但不掩盖 :class:`ActivityTimeout`，
    例如连接已断开时 ``stream.close()`` 抛异常），然后 raise
    :class:`ActivityTimeout`。

    参数:
        aiter: 被包装的 async iterable（如 ``stream`` 或 ``async for chunk in stream``）。
        timeout_sec: per-item 超时秒数。每次 ``__anext__`` 重新计时，
                     即"相邻 token 间隔"超时（非总时长）。
        on_timeout: 超时回调（async callable，一般是 ``stream.close``），
                    None 表示不调用清理逻辑。

    Yields:
        输入 iterable 的每个 item（原样透传）。

    Raises:
        ActivityTimeout: 任一 item 等待超过 ``timeout_sec`` 秒。
    """
    ait = aiter.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(ait.__anext__(), timeout=timeout_sec)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            # per-token 活跃超时：先尝试清理（关闭流释放 HTTP 连接），
            # close 失败也不掩盖 ActivityTimeout（spec SubTask 3.14）。
            if on_timeout is not None:
                try:
                    await on_timeout()
                except Exception as close_exc:
                    logger.warning(
                        "on_timeout 回调失败（已忽略，不掩盖 ActivityTimeout）: %r",
                        close_exc,
                    )
            raise ActivityTimeout(
                f"LLM 在 {timeout_sec}s 内未返回任何 token"
            )
        yield item


def async_retry_on_failure(max_retries: int = 3, base_delay: float = 1.0):
    """异步 LLM 调用指数退避重试装饰器。

    对 429 限流、5xx 服务端错误、网络连接错误进行指数退避重试，
    4xx 客户端错误（非 429）立即抛出不重试。退避用 ``asyncio.sleep``，
    不阻塞事件循环。

    自动兼容：
    - ``async def`` 协程：包装为重试协程。
    - async generator（如 :meth:`AsyncBaseBackend.chat_stream`）：包装为
      重试 async generator。

    **已知限制（spec SubTask 3.15）**：对 async generator，仅在**未 yield
    任何 item** 时重试（首次连接失败可重试）；已 yield 后的失败直接抛出
    （避免重试生成新流导致前端重复显示已输出内容）。

    ``StreamCancelled`` / ``ActivityTimeout`` 不重试（``_is_retryable``
    中检查），用户取消与卡死超时立即冒泡。

    参数:
        max_retries: 最大尝试次数（含首次调用）。
        base_delay: 基础退避延迟（秒），实际延迟为 ``base_delay * (2 ** attempt)``。
    """

    def decorator(func):
        if inspect.isasyncgenfunction(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                last_exc: Optional[Exception] = None
                for attempt in range(max_retries):
                    has_yielded = False
                    try:
                        async for item in func(*args, **kwargs):
                            has_yielded = True
                            yield item
                        return
                    except Exception as e:
                        # 已 yield 后失败：不重试，直接抛出（spec SubTask 3.15）
                        if has_yielded:
                            raise
                        if not _is_retryable(e):
                            raise
                        last_exc = e
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.warning(
                                "触发限流/错误，%.1fs 后重试 (attempt %d/%d)",
                                delay,
                                attempt + 1,
                                max_retries,
                            )
                            await asyncio.sleep(delay)
                raise RuntimeError(
                    f"API 调用在 {max_retries} 次重试后仍失败"
                ) from last_exc

            return wrapper
        elif inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                last_exc: Optional[Exception] = None
                for attempt in range(max_retries):
                    try:
                        return await func(*args, **kwargs)
                    except Exception as e:
                        if not _is_retryable(e):
                            raise
                        last_exc = e
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.warning(
                                "触发限流/错误，%.1fs 后重试 (attempt %d/%d)",
                                delay,
                                attempt + 1,
                                max_retries,
                            )
                            await asyncio.sleep(delay)
                raise RuntimeError(
                    f"API 调用在 {max_retries} 次重试后仍失败"
                ) from last_exc

            return wrapper
        else:
            raise TypeError(
                f"async_retry_on_failure 仅支持 async def / async generator，"
                f"实际装饰的函数 {func!r} 不是协程或异步生成器"
            )

    return decorator


class LLMResponse:
    """统一 LLM 响应包装，接口兼容 ``anthropic.types.Message``。

    Attributes:
        content: content block 列表，每项为 dict：
                 ``{"type": "text", "text": str}`` 或
                 ``{"type": "tool_use", "id", "name", "input"}``。
        stop_reason: 停止原因，``"tool_use"`` / ``"end_turn"`` / ``"max_tokens"``。
        usage: 可选的 token 用量信息。
        raw: 原始响应对象（用于调试 / 额外字段访问）。
    """

    def __init__(
        self,
        content: List[Dict[str, Any]],
        stop_reason: str,
        usage: Optional[Dict[str, Any]] = None,
        raw: Optional[Any] = None,
    ) -> None:
        self.content: List[Dict[str, Any]] = content
        self.stop_reason: str = stop_reason
        self.usage: Optional[Dict[str, Any]] = usage
        self.raw: Optional[Any] = raw

    def __repr__(self) -> str:
        return (
            f"LLMResponse(stop_reason={self.stop_reason!r}, "
            f"blocks={len(self.content)})"
        )


class AsyncBaseBackend:
    """异步 LLM 后端抽象基类。

    子类需实现 :meth:`chat`（async def，返回 :class:`LLMResponse`）与
    :meth:`chat_stream`（async generator，yield 事件 dict）。

    对外接口使用 Anthropic 风格的消息与工具 schema，由子类内部完成
    必要的格式转换。
    """

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None) -> None:
        self.model: str = model
        self.api_key: str = api_key
        self.base_url: Optional[str] = base_url

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """调用 LLM 并返回统一响应。子类必须实现。"""
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        activity_timeout: float = 60.0,
        stream_manager: Optional["StreamManager"] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LLM，async generator 逐个 yield 事件 dict。

        事件类型（统一格式）：
            - ``{"type": "text", "text": "<增量文本>"}``：
              文本增量片段，调用方应累加显示。
            - ``{"type": "done", "stop_reason": str, "content_blocks": list, "usage": dict|None}``：
              流结束事件，``content_blocks`` 为本次 LLM 响应的完整 content block
              列表（含 text 与 tool_use 块，Anthropic 风格），
              供调用方（如 ReactLoop）判断是否需要继续工具调用循环。

        参数:
            messages: Anthropic 风格消息列表。
            tools: 可选工具定义列表（Anthropic 风格）。
            system: 可选系统提示词。
            max_tokens: 输出 token 上限。
            cancel_event: 可选 ``threading.Event``，用于同步工具 handler 中断
                          （``_cancel_context`` ContextVar 机制不变）。流迭代中
                          检测 ``is_set()`` 时 raise :class:`StreamCancelled`。
            activity_timeout: per-token 活跃超时秒数。相邻 token 间隔超过此值
                              则 raise :class:`ActivityTimeout`。
            stream_manager: 可选 :class:`StreamManager`，用于注册 ``cancel_callback``
                            （Task 9 主路径：``/chat/cancel`` immediate 调
                            ``trigger_cancel`` 主动断流）。
            session_id: 可选会话 ID，与 ``stream_manager`` 配对使用。

        子类必须实现。
        """
        raise NotImplementedError
        # 仅为 mypy 把本方法标记为 async generator（实际由子类实现）
        yield {}  # pragma: no cover


class AsyncAnthropicBackend(AsyncBaseBackend):
    """异步 Anthropic 后端，使用 ``anthropic.AsyncAnthropic`` SDK。

    消息 / 工具 schema 直接使用 Anthropic 格式，无需转换。

    流式调用使用 ``async with client.messages.stream(...) as stream:``，
    外层用 :func:`_with_activity_timeout` 包装实现 per-token 活跃超时。
    ``cancel_event.is_set()`` 检查保留作为兜底路径；``stream.close`` 引发的
    异常（``asyncio.CancelledError`` / SDK 连接关闭异常）转换为
    :class:`StreamCancelled` 冒泡（spec SubTask 3.13）。
    """

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None) -> None:
        super().__init__(model, api_key, base_url)
        client_kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        try:
            self._client = anthropic.AsyncAnthropic(**client_kwargs)
        except anthropic.AnthropicError as e:
            raise RuntimeError(f"初始化 anthropic 客户端失败: {e}") from e

    @async_retry_on_failure()
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS_MAIN,
            "messages": messages,
        }
        if system is not None:
            request_kwargs["system"] = system
        if tools:
            request_kwargs["tools"] = tools

        response = await self._client.messages.create(**request_kwargs)

        # 将 anthropic content block 对象统一转为 dict
        content_blocks: List[Dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            if isinstance(block, dict):
                content_blocks.append(block)
                continue
            block_type = getattr(block, "type", None)
            if block_type == "text":
                content_blocks.append(
                    {"type": "text", "text": getattr(block, "text", "")}
                )
            elif block_type == "tool_use":
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}) or {},
                    }
                )
            else:
                content_blocks.append({"type": block_type or "unknown"})

        usage_dict: Optional[Dict[str, Any]] = None
        usage_obj = getattr(response, "usage", None)
        if usage_obj is not None:
            usage_dict = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0),
                "output_tokens": getattr(usage_obj, "output_tokens", 0),
                "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0),
            }

        return LLMResponse(
            content=content_blocks,
            stop_reason=getattr(response, "stop_reason", "end_turn") or "end_turn",
            usage=usage_dict,
            raw=response,
        )

    @async_retry_on_failure()
    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        activity_timeout: float = 60.0,
        stream_manager: Optional["StreamManager"] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Anthropic 异步流式调用，使用 ``client.messages.stream``。

        通过 ``stream.get_final_message()`` 在流结束后获取完整 message，
        其中包含 content blocks（含 tool_use）与 stop_reason。

        取消双路径设计（spec SubTask 3.13）：
        - 主路径：``/chat/cancel`` → ``stream_manager.trigger_cancel`` →
          ``await stream.close()`` 主动断开 HTTP 连接，``async for`` 立即
          抛异常 → 本方法 try/except 捕获并转 :class:`StreamCancelled`。
        - 兜底路径：``cancel_event.is_set()`` 在收到 chunk 后检查（防止
          trigger_cancel 失败或 stream.close 未注册的边界情况）。
        """
        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS_MAIN,
            "messages": messages,
        }
        if system is not None:
            request_kwargs["system"] = system
        if tools:
            request_kwargs["tools"] = tools

        async with self._client.messages.stream(**request_kwargs) as stream:
            # 注册 cancel_callback（Task 9 主路径），用 hasattr 守护兼容
            # StreamManager 未实现 set_cancel_callback 的旧版本。
            async def _close_stream() -> None:
                try:
                    await stream.close()
                except Exception as e:
                    logger.warning("stream.close 失败: %r", e)

            cancel_cb_registered = False
            if stream_manager is not None and session_id is not None:
                set_cb = getattr(stream_manager, "set_cancel_callback", None)
                if set_cb is not None:
                    try:
                        await set_cb(session_id, _close_stream)
                        cancel_cb_registered = True
                    except Exception as e:
                        logger.warning("set_cancel_callback 失败: %r", e)

            final_message: Any = None
            try:
                async for event in _with_activity_timeout(
                    stream, activity_timeout, _close_stream
                ):
                    # 兜底路径：cancel_event.is_set() 检查（trigger_cancel
                    # 未注册或 stream.close 失败时的兜底）
                    if cancel_event and cancel_event.is_set():
                        raise StreamCancelled("User cancelled the stream")
                    # 文本增量事件
                    if getattr(event, "type", None) == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "type", None) == "text_delta":
                            text = getattr(delta, "text", "")
                            if text:
                                yield {"type": "text", "text": text}
                final_message = await stream.get_final_message()
            except StreamCancelled:
                raise
            except ActivityTimeout:
                raise
            except asyncio.CancelledError:
                # trigger_cancel 路径：stream.close 后 async for 引发 CancelledError
                if cancel_event and cancel_event.is_set():
                    raise StreamCancelled("User cancelled via trigger_cancel")
                raise
            except Exception as e:
                # 其他 SDK 异常：检查是否因用户取消（stream.close 引发的连接异常）
                if cancel_event and cancel_event.is_set():
                    raise StreamCancelled(
                        f"User cancelled, stream closed: {e!r}"
                    ) from e
                raise
            finally:
                if cancel_cb_registered:
                    try:
                        await stream_manager.set_cancel_callback(session_id, None)  # type: ignore[union-attr]
                    except Exception:
                        pass

            # 将 final_message 的 content blocks 统一转为 dict
            content_blocks: List[Dict[str, Any]] = []
            for block in getattr(final_message, "content", []) or []:
                if isinstance(block, dict):
                    content_blocks.append(block)
                    continue
                block_type = getattr(block, "type", None)
                if block_type == "text":
                    content_blocks.append(
                        {"type": "text", "text": getattr(block, "text", "")}
                    )
                elif block_type == "tool_use":
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": getattr(block, "input", {}) or {},
                        }
                    )
                else:
                    content_blocks.append({"type": block_type or "unknown"})

            usage_dict: Optional[Dict[str, Any]] = None
            usage_obj = getattr(final_message, "usage", None)
            if usage_obj is not None:
                usage_dict = {
                    "input_tokens": getattr(usage_obj, "input_tokens", 0),
                    "output_tokens": getattr(usage_obj, "output_tokens", 0),
                    "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0),
                }

            yield {
                "type": "done",
                "stop_reason": getattr(final_message, "stop_reason", "end_turn") or "end_turn",
                "content_blocks": content_blocks,
                "usage": usage_dict,
            }


class AsyncOpenAICompatBackend(AsyncBaseBackend):
    """异步 OpenAI 兼容后端，使用 ``openai.AsyncOpenAI`` SDK。

    支持 openai / deepseek / qwen，仅 ``base_url`` 与默认环境变量不同。

    内部完成 Anthropic 风格消息 / 工具 schema → OpenAI 格式的转换，
    并将 OpenAI 响应包装回 :class:`LLMResponse`（Anthropic 风格）。

    流式调用 ``stream = await client.chat.completions.create(stream=True, ...)``
    + ``async for chunk in stream``，外层用 :func:`_with_activity_timeout`
    包装。取消双路径设计同 :class:`AsyncAnthropicBackend`。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        provider_name: str = "openai",
    ) -> None:
        super().__init__(model, api_key, base_url)
        self.provider_name: str = provider_name
        try:
            from openai import AsyncOpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "未安装 openai SDK（>=1.50.0），请运行: pip install 'openai>=1.50.0'"
            ) from e

        client_kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**client_kwargs)

    @async_retry_on_failure()
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        openai_messages = self._convert_messages(messages, system)
        openai_tools = self._convert_tools(tools) if tools else None

        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS_MAIN,
            "messages": openai_messages,
        }
        if openai_tools:
            request_kwargs["tools"] = openai_tools

        response = await self._client.chat.completions.create(**request_kwargs)

        return self._convert_response(response)

    @async_retry_on_failure()
    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        activity_timeout: float = 60.0,
        stream_manager: Optional["StreamManager"] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """OpenAI 兼容异步流式调用（``stream=True``）。

        增量文本通过 ``delta.content`` 累积并 yield；工具调用通过
        ``delta.tool_calls`` 累积，流结束后组装为完整 content_blocks。

        取消双路径设计同 :class:`AsyncAnthropicBackend`。
        """
        openai_messages = self._convert_messages(messages, system)
        openai_tools = self._convert_tools(tools) if tools else None

        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS_MAIN,
            "messages": openai_messages,
            "stream": True,
            # 启用流式 usage 返回，末个 chunk 含 usage 字段（供指标采集）
            "stream_options": {"include_usage": True},
        }
        if openai_tools:
            request_kwargs["tools"] = openai_tools

        stream = await self._client.chat.completions.create(**request_kwargs)

        # 注册 cancel_callback（Task 9 主路径）
        async def _close_stream() -> None:
            try:
                await stream.close()
            except Exception as e:
                logger.warning("stream.close 失败: %r", e)

        cancel_cb_registered = False
        if stream_manager is not None and session_id is not None:
            set_cb = getattr(stream_manager, "set_cancel_callback", None)
            if set_cb is not None:
                try:
                    await set_cb(session_id, _close_stream)
                    cancel_cb_registered = True
                except Exception as e:
                    logger.warning("set_cancel_callback 失败: %r", e)

        # 累积文本与工具调用
        full_text_parts: List[str] = []
        # tool_calls 累积结构：{index: {"id", "name", "arguments_str"}}
        tool_calls_acc: Dict[int, Dict[str, str]] = {}
        finish_reason: str = "stop"
        # 流式 usage（末个 chunk 携带，include_usage=True 时返回）
        stream_usage: Optional[Any] = None

        try:
            async for chunk in _with_activity_timeout(
                stream, activity_timeout, _close_stream
            ):
                # 兜底路径：cancel_event.is_set() 检查
                if cancel_event and cancel_event.is_set():
                    raise StreamCancelled("User cancelled the stream")
                # 末个 chunk 可能含 usage 但 choices 为空
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    stream_usage = chunk_usage
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                # 文本增量
                delta_content = getattr(delta, "content", None)
                if delta_content:
                    full_text_parts.append(delta_content)
                    yield {"type": "text", "text": delta_content}

                # 工具调用增量
                delta_tool_calls = getattr(delta, "tool_calls", None)
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        idx = getattr(tc, "index", 0) or 0
                        slot = tool_calls_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments_str": ""}
                        )
                        tc_id = getattr(tc, "id", None)
                        if tc_id:
                            slot["id"] = tc_id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            fn_name = getattr(fn, "name", None)
                            if fn_name:
                                slot["name"] = fn_name
                            args_delta = getattr(fn, "arguments", None)
                            if args_delta:
                                slot["arguments_str"] += args_delta

                # 记录 finish_reason（最后一个 chunk 才有）
                chunk_finish = getattr(choice, "finish_reason", None)
                if chunk_finish:
                    finish_reason = chunk_finish
        except StreamCancelled:
            raise
        except ActivityTimeout:
            raise
        except asyncio.CancelledError:
            if cancel_event and cancel_event.is_set():
                raise StreamCancelled("User cancelled via trigger_cancel")
            raise
        except Exception as e:
            if cancel_event and cancel_event.is_set():
                raise StreamCancelled(
                    f"User cancelled, stream closed: {e!r}"
                ) from e
            raise
        finally:
            if cancel_cb_registered:
                try:
                    await stream_manager.set_cancel_callback(session_id, None)  # type: ignore[union-attr]
                except Exception:
                    pass
            # 主动关闭 stream 释放底层 HTTP 连接（幂等，已关闭则无副作用）
            try:
                await stream.close()
            except Exception:
                pass

        # 组装 content_blocks（Anthropic 风格）
        content_blocks: List[Dict[str, Any]] = []
        full_text = "".join(full_text_parts)
        if full_text:
            content_blocks.append({"type": "text", "text": full_text})

        # 按 index 顺序组装 tool_use 块
        for idx in sorted(tool_calls_acc.keys()):
            slot = tool_calls_acc[idx]
            args_str = slot.get("arguments_str", "") or "{}"
            try:
                input_args = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "tool_call args JSON 解析失败 (stream), name=%s, raw=%.200s",
                    slot.get("name", "?"), args_str,
                )
                input_args = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": slot.get("id", ""),
                    "name": slot.get("name", ""),
                    "input": input_args,
                }
            )

        stop_reason = _FINISH_REASON_MAP.get(finish_reason, "end_turn")
        # 若累积到 tool_calls 但 finish_reason 不是 tool_calls，仍强制按 tool_use
        if tool_calls_acc and stop_reason != "tool_use":
            stop_reason = "tool_use"

        # 构建 usage_dict（从流式末个 chunk 提取，缓存字段映射 DeepSeek 扩展）
        usage_dict: Optional[Dict[str, Any]] = None
        if stream_usage is not None:
            usage_dict = {
                "input_tokens": getattr(stream_usage, "prompt_tokens", 0),
                "output_tokens": getattr(stream_usage, "completion_tokens", 0),
                **AsyncOpenAICompatBackend._extract_cache_usage(stream_usage),
            }

        yield {
            "type": "done",
            "stop_reason": stop_reason,
            "content_blocks": content_blocks,
            "usage": usage_dict,
        }

    @staticmethod
    def _clean_orphan_tool_results(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """清理孤立的 ``tool_result`` 消息（前面无对应 ``tool_use`` 的）。

        Phase 9 Task 6 方案 B：在 Anthropic → OpenAI 消息转换前扫描消息列表，
        删除所有 ``tool_use_id`` 未匹配到 assistant ``tool_use`` 块的
        ``tool_result`` 块，避免触发 OpenAI 400 错误：

            ``Messages with role 'tool' must be a response to a preceding
            message with 'tool_calls'``

        根因：``history_buffer`` 的 FIFO 淘汰曾单独删除 assistant(tool_use)
        而留下 user(tool_result)，导致下次 LLM 调用生成违规的孤立
        tool_result。本方法作为边界兜底，即使源头已修复也保持工作。

        处理规则：
        - 第一遍扫描：收集所有 assistant ``tool_use`` 块的 ``id``，
          记入 ``valid_tool_use_ids`` 集合。
        - 第二遍扫描：遍历 user 消息中的 ``tool_result`` 块，若
          ``tool_use_id`` 不在集合中则视为孤立，跳过该块并记录 warning。
        - 若 user 消息过滤后 content 列表为空（整条消息全是孤立
          tool_result），则该条消息整体跳过。
        - 非 tool_result 消息（assistant / 纯文本 user / system 等）原样保留。

        参数:
            messages: Anthropic 风格消息列表。

        返回:
            清理后的消息列表（浅拷贝，原列表不被修改）。
        """
        # 第一遍：收集所有 tool_use_id
        valid_tool_use_ids: set = set()
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    if tid:
                        valid_tool_use_ids.add(tid)

        # 第二遍：过滤孤立 tool_result 块
        cleaned: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            # 仅处理 user 消息且 content 为 list（含 tool_result 块形式）
            if role == "user" and isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result:
                    kept_blocks: List[Dict[str, Any]] = []
                    orphan_count = 0
                    for block in content:
                        if not isinstance(block, dict):
                            kept_blocks.append(block)
                            continue
                        if block.get("type") == "tool_result":
                            tid = block.get("tool_use_id")
                            if tid not in valid_tool_use_ids:
                                logger.warning(
                                    "清理孤立 tool_result: tool_use_id=%s", tid
                                )
                                orphan_count += 1
                                continue
                        kept_blocks.append(block)
                    if kept_blocks:
                        # 保留过滤后的块（浅拷贝 msg 以免修改原 dict）
                        cleaned.append({**msg, "content": kept_blocks})
                    else:
                        # 整条消息全是孤立 tool_result，整体跳过
                        logger.warning(
                            "清理整条孤立 tool_result user 消息（共 %d 个块）",
                            orphan_count,
                        )
                    continue
            cleaned.append(msg)

        # 第三遍（全列表扫描）：清理孤立 assistant(tool_calls)
        # Phase 9 修复：卡死终止 / 流式中断等路径会在 messages 中留下
        # 未匹配 tool_result 的 assistant(tool_calls)，DeepSeek/OpenAI
        # 严格要求 tool_calls 后必须紧跟 tool 消息，否则 400：
        #
        #     ``An assistant message with 'tool_calls' must be followed
        #     by tool messages responding to each 'tool_call_id'.``
        #
        # 全列表扫描，删除所有 tool_use_id 均无对应 tool_result 的
        # assistant(tool_calls) 整条消息。覆盖 history 中间或末尾的
        # 孤立 tool_calls（如 history_buffer 已被污染、下次请求加载
        # 后追加新 user 输入的场景）。仅删整条孤立（所有 tool_use_id
        # 都无对应），部分孤立（个别 tool_use_id 缺 result）不处理，
        # 因其罕见且删除会破坏配对。
        answered_ids: set = set()
        for msg in cleaned:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        answered_ids.add(tid)
        result: List[Dict[str, Any]] = []
        for msg in cleaned:
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    tool_use_ids = [
                        b.get("id")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    ]
                    if tool_use_ids and all(
                        tid not in answered_ids for tid in tool_use_ids
                    ):
                        logger.warning(
                            "清理孤立 assistant(tool_calls): tool_use_ids=%s",
                            tool_use_ids,
                        )
                        continue  # 跳过整条孤立 assistant(tool_calls)
            result.append(msg)
        return result

    @staticmethod
    def _clean_empty_assistant_messages(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """清理空的 assistant 消息（无文本内容且无 tool_use 块）。

        当 LLM 流被中断或工具执行出错后，历史中可能残留 assistant 消息
        其 content 为空字符串且无 tool_use 块。DeepSeek/OpenAI API 要求
        assistant 消息必须有 content 或 tool_calls，否则返回 400：

            ``Invalid assistant message: content or tool_calls must be set``

        此方法在消息转换前过滤掉这类消息，作为防御性兜底。
        """
        result = []
        for msg in messages:
            if msg.get("role") != "assistant":
                result.append(msg)
                continue
            content = msg.get("content")
            # 字符串内容为空 → 需要检查是否有 tool_use 块
            if isinstance(content, str) and not content.strip():
                # 纯字符串且为空 → 跳过（无内容、无 tool_calls）
                logger.warning("清理空 assistant 消息（空字符串 content）")
                continue
            if isinstance(content, list) and not content:
                # 空列表 → 跳过
                logger.warning("清理空 assistant 消息（空列表 content）")
                continue
            # content 为 None / 其他假值 → 检查是否有 tool_use
            if not content:
                logger.warning("清理空 assistant 消息（content=%r）", content)
                continue
            # content 为列表 → 检查是否至少有一个 tool_use 块
            # （纯文本空字符串 + 无 tool_use 也属于空消息）
            if isinstance(content, list):
                has_tool_use = any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content
                )
                has_text = any(
                    isinstance(b, dict)
                    and b.get("type") == "text"
                    and b.get("text", "").strip()
                    for b in content
                )
                if not has_tool_use and not has_text:
                    logger.warning(
                        "清理空 assistant 消息（content 列表无 tool_use 也无文本）"
                    )
                    continue
            result.append(msg)
        return result

    def _convert_messages(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str],
    ) -> List[Dict[str, Any]]:
        """将 Anthropic 风格消息列表转为 OpenAI 消息列表。

        转换规则：
        - 顶层 system 注入为 OpenAI ``role=system`` 消息（前置）。
        - ``content`` 为字符串时直接保留。
        - ``assistant`` 的 content blocks 中：
            * ``text`` 块拼为 content 字符串；
            * ``tool_use`` 块转为 ``tool_calls`` 列表。
        - ``user`` 的 content blocks 中：
            * ``tool_result`` 块拆分为多条 OpenAI ``role=tool`` 消息；
            * ``text`` 块转为 ``role=user`` 消息。

        Phase 9 Task 6 方案 B：转换前先调用
        :meth:`_clean_orphan_tool_results` 清理孤立 tool_result 消息
        （前面无对应 tool_use 的），作为边界兜底防御 LLM 400 错误，
        即使源头 (history_buffer FIFO 配对淘汰) 已修复也保持工作。
        """
        # Phase 9 Task 6 方案 B：转换前清理孤立 tool_result，避免 OpenAI 400
        messages = self._clean_orphan_tool_results(messages)
        messages = self._clean_empty_assistant_messages(messages)

        out: List[Dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue

            if not isinstance(content, list):
                # 兜底：转字符串
                out.append({"role": role, "content": str(content)})
                continue

            if role == "assistant":
                text_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text:
                            text_parts.append(text)
                    elif btype == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(
                                        block.get("input", {}) or {},
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        )
                msg_out: Dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                out.append(msg_out)
            elif role == "user":
                # user 消息可能含 tool_result 块（工具结果回传）
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result:
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                # content blocks 形式，提取文本
                                result_content = "\n".join(
                                    sub.get("text", "")
                                    for sub in result_content
                                    if isinstance(sub, dict) and sub.get("type") == "text"
                                )
                            out.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": block.get("tool_use_id", ""),
                                    "content": str(result_content),
                                }
                            )
                        elif btype == "text":
                            text = block.get("text", "")
                            if text:
                                out.append({"role": "user", "content": text})
                else:
                    # 纯文本 user 消息（content blocks 形式）
                    text_parts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    out.append(
                        {
                            "role": "user",
                            "content": "\n".join(text_parts) if text_parts else "",
                        }
                    )
            else:
                # 其他角色直接保留
                out.append({"role": role, "content": str(content) if content else ""})

        return out

    @staticmethod
    def _convert_tools(
        tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """将 Anthropic 工具 schema 转为 OpenAI 工具格式。

        Anthropic: ``{"name", "description", "input_schema"}``
        OpenAI:    ``{"type": "function", "function": {"name", "description", "parameters"}}``
        """
        out: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            # 兼容已经是 OpenAI 格式的情况
            if tool.get("type") == "function" and "function" in tool:
                out.append(tool)
                continue
            schema = tool.get("input_schema") or tool.get("parameters") or {
                "type": "object",
                "properties": {},
            }
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": schema,
                    },
                }
            )
        return out

    @staticmethod
    def _extract_cache_usage(usage_obj: Any) -> Dict[str, int]:
        """从 OpenAI/DeepSeek usage 对象提取缓存字段。

        DeepSeek（OpenAI 兼容扩展）在 usage 中返回：

        - ``prompt_cache_hit_tokens``：缓存命中 token 数（对应 Anthropic 的
          ``cache_read_input_tokens``）
        - ``prompt_cache_miss_tokens``：缓存未命中 token 数（对应 Anthropic 的
          ``cache_creation_input_tokens``）

        标准 OpenAI 响应不携带上述字段，此时通过 ``getattr`` 降级为 0，
        保证向后兼容且不抛异常。

        参数:
            usage_obj: OpenAI / DeepSeek 响应中的 usage 对象（任意对象，仅通过
                       ``getattr`` 访问属性，因此也兼容 MagicMock 等测试替身）。

        返回:
            包含 ``cache_creation_input_tokens`` 与 ``cache_read_input_tokens``
            两个字段的 dict。
        """
        return {
            "cache_creation_input_tokens": getattr(usage_obj, "prompt_cache_miss_tokens", 0),
            "cache_read_input_tokens": getattr(usage_obj, "prompt_cache_hit_tokens", 0),
        }

    @staticmethod
    def _convert_response(response: Any) -> LLMResponse:
        """将 OpenAI ChatCompletion 响应转为 :class:`LLMResponse`。"""
        if not getattr(response, "choices", None):
            return LLMResponse(
                content=[{"type": "text", "text": ""}],
                stop_reason="end_turn",
                raw=response,
            )
        choice = response.choices[0]
        message = getattr(choice, "message", None)
        finish_reason = getattr(choice, "finish_reason", "stop") or "stop"

        content_blocks: List[Dict[str, Any]] = []
        text_content = getattr(message, "content", None) if message else None
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        tool_calls = getattr(message, "tool_calls", None) if message else None
        if tool_calls:
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                if fn is None:
                    continue
                args_str = getattr(fn, "arguments", "{}") or "{}"
                try:
                    input_args = json.loads(args_str)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "tool_call args JSON 解析失败 (non-stream), name=%s, raw=%.200s",
                        getattr(fn, "name", "?"), args_str,
                    )
                    input_args = {}
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": getattr(tc, "id", ""),
                        "name": getattr(fn, "name", ""),
                        "input": input_args,
                    }
                )

        stop_reason = _FINISH_REASON_MAP.get(finish_reason, "end_turn")
        # 若有 tool_calls 但 finish_reason 不是 tool_calls，仍强制按 tool_use 处理
        if tool_calls and stop_reason != "tool_use":
            stop_reason = "tool_use"

        usage_dict: Optional[Dict[str, Any]] = None
        usage_obj = getattr(response, "usage", None)
        if usage_obj is not None:
            usage_dict = {
                "input_tokens": getattr(usage_obj, "prompt_tokens", 0),
                "output_tokens": getattr(usage_obj, "completion_tokens", 0),
                **AsyncOpenAICompatBackend._extract_cache_usage(usage_obj),
            }

        return LLMResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            usage=usage_dict,
            raw=response,
        )


def _create_backend(
    provider: str,
    model: str,
    api_key: str,
    base_url: Optional[str] = None,
) -> AsyncBaseBackend:
    """根据 provider 创建对应异步 Backend 实例。

    参数:
        provider: ``anthropic`` / ``openai`` / ``deepseek`` / ``qwen``。
        model: 模型名称。
        api_key: API Key。
        base_url: 可选自定义 base_url，仅对 OpenAI 兼容类生效。

    返回:
        :class:`AsyncBaseBackend` 子类实例。
    """
    provider_lower = (provider or "").lower().strip()
    if not provider_lower:
        raise ValueError("provider 未设置，请在 config.yaml 中配置 llm.main_provider")

    if provider_lower == "anthropic":
        return AsyncAnthropicBackend(model=model, api_key=api_key, base_url=base_url)

    if provider_lower in ("openai", "deepseek", "qwen"):
        # base_url 优先级：config 显式配置 > provider 默认值
        effective_base_url = base_url or _PROVIDER_DEFAULT_BASE_URL.get(provider_lower)
        return AsyncOpenAICompatBackend(
            model=model,
            api_key=api_key,
            base_url=effective_base_url,
            provider_name=provider_lower,
        )

    raise ValueError(
        f"不支持的 provider: {provider!r}，"
        f"目前支持: anthropic / openai / deepseek / qwen"
    )


# ---------------------------------------------------------------------------
# 向后兼容别名（spec: 删除 sync 版本，async 类别名指向 async 类）
# ---------------------------------------------------------------------------

BaseBackend = AsyncBaseBackend
AnthropicBackend = AsyncAnthropicBackend
OpenAICompatBackend = AsyncOpenAICompatBackend


class LLMClient:
    """LLM 客户端，封装主对话与 consolidation 两个异步 Backend。

    根据 ``config.yaml`` 的 ``llm`` 段配置创建两个独立 Backend：

    - 主对话 Backend：使用 ``main_provider`` / ``main_model`` / ``main_api_key`` /
      ``main_base_url``
    - consolidation Backend：使用 ``consolidation_provider`` /
      ``consolidation_model`` / ``consolidation_api_key`` / ``consolidation_base_url``

    对外接口（``chat_main`` / ``chat_consolidation`` / ``chat_main_stream``）
    均为 async，返回 :class:`LLMResponse`（兼容 ``anthropic.types.Message``
    接口）。

    供 workflow / memory 等线程池路径调用，提供 sync wrapper
    ``chat_main_sync`` / ``chat_consolidation_sync``，内部
    ``asyncio.run`` 在调用线程内创建临时事件循环。**不可**在主事件循环
    运行中的协程内直接调用 sync wrapper（会抛 RuntimeError）。
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        config: Optional[Dict[str, Any]] = None,
        metrics_collector: Optional["MetricsCollector"] = None,
    ) -> None:
        """初始化 LLM 客户端。

        参数:
            config_path: 配置文件路径，当 config 未提供时使用。
            config: 可选的已加载配置字典，提供时跳过文件读取。
            metrics_collector: 可选的指标采集器，用于上报 LLM 调用用量与延迟。
        """
        self._metrics_collector: Optional["MetricsCollector"] = metrics_collector
        self._config_path: str = config_path

        if config is None:
            config = load_config(config_path)
        self._config: Dict[str, Any] = config

        llm_config: Dict[str, Any] = config.get("llm", {})
        if not llm_config:
            raise ValueError("配置中缺少 llm 段或为空")

        # 主对话 LLM 配置
        self.main_provider: str = llm_config.get("main_provider", "anthropic")
        self.main_model: str = llm_config.get("main_model", "")
        self.main_api_key: str = self._resolve_api_key(
            llm_config.get("main_api_key"),
            self.main_provider,
        )
        self.main_base_url: Optional[str] = llm_config.get("main_base_url") or None

        # consolidation LLM 配置
        self.consolidation_provider: str = llm_config.get(
            "consolidation_provider", self.main_provider
        )
        self.consolidation_model: str = llm_config.get("consolidation_model", "")
        self.consolidation_api_key: str = self._resolve_api_key(
            llm_config.get("consolidation_api_key"),
            self.consolidation_provider,
        )
        self.consolidation_base_url: Optional[str] = (
            llm_config.get("consolidation_base_url") or None
        )

        # 上下文窗口配置
        self.max_context_tokens: int = int(
            llm_config.get("max_context_tokens", 200000)
        )
        self.context_threshold: float = float(
            llm_config.get("context_threshold", 0.8)
        )

        # LLM 超时配置（spec async-llm-backend Task 1）
        # activity_timeout: per-token 活跃超时（相邻 token 间隔超时）
        # stream_total_timeout: 流式总超时（兜底整个流式调用）
        self._activity_timeout, self._stream_total_timeout = get_llm_timeouts(config)

        # 校验必要配置
        if not self.main_model:
            raise ValueError("配置 llm.main_model 未设置")
        if not self.main_api_key:
            raise ValueError(
                f"主对话 LLM API Key 未设置：请在环境变量 "
                f"{_PROVIDER_DEFAULT_ENV_KEY.get(self.main_provider, 'API_KEY')} 中配置，"
                f"或在 config.yaml 中为 llm.main_api_key 指定值"
            )
        if not self.consolidation_model:
            raise ValueError("配置 llm.consolidation_model 未设置")
        if not self.consolidation_api_key:
            raise ValueError(
                f"consolidation LLM API Key 未设置：请在环境变量 "
                f"{_PROVIDER_DEFAULT_ENV_KEY.get(self.consolidation_provider, 'API_KEY')} 中配置"
            )

        # 创建 Backend 实例
        self._main_backend: AsyncBaseBackend = _create_backend(
            provider=self.main_provider,
            model=self.main_model,
            api_key=self.main_api_key,
            base_url=self.main_base_url,
        )
        self._consolidation_backend: AsyncBaseBackend = _create_backend(
            provider=self.consolidation_provider,
            model=self.consolidation_model,
            api_key=self.consolidation_api_key,
            base_url=self.consolidation_base_url,
        )

        logger.info(
            "LLMClient 初始化完成: main=%s/%s, consolidation=%s/%s, "
            "activity_timeout=%.1fs, stream_total_timeout=%.1fs",
            self.main_provider,
            self.main_model,
            self.consolidation_provider,
            self.consolidation_model,
            self._activity_timeout,
            self._stream_total_timeout,
        )

    @staticmethod
    def _resolve_api_key(config_value: Optional[str], provider: str) -> str:
        """解析 API Key：优先 config 值，缺失时回退到 provider 对应的环境变量。"""
        if config_value:
            return config_value
        env_name = _PROVIDER_DEFAULT_ENV_KEY.get(provider, "")
        return os.environ.get(env_name, "") if env_name else ""

    async def chat_main(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """主对话 LLM 调用（async）。

        参数:
            messages: 对话消息列表，Anthropic 风格
                      （``[{"role": "user", "content": "..."}]``，
                       content 可为字符串或 content block 列表）。
            tools: 可选工具定义列表，Anthropic 风格
                   （``{"name", "description", "input_schema"}``）。
            system: 可选系统提示词。
            max_tokens: 输出 token 上限，默认使用 _DEFAULT_MAX_TOKENS_MAIN。

        返回:
            :class:`LLMResponse` 对象，包含 content / stop_reason / usage 字段。
        """
        t0 = time.perf_counter()
        response = await self._main_backend.chat(
            messages=messages,
            tools=tools,
            system=system,
            max_tokens=max_tokens,
        )
        if self._metrics_collector is not None and response.usage is not None:
            latency_ms = (time.perf_counter() - t0) * 1000
            self._metrics_collector.observe_llm_usage(response.usage, latency_ms)
        return response

    async def chat_main_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        activity_timeout: Optional[float] = None,
        stream_manager: Optional["StreamManager"] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """主对话 LLM 异步流式调用，async generator yield 事件 dict。

        事件格式与 :meth:`AsyncBaseBackend.chat_stream` 一致：
            - ``{"type": "text", "text": str}``
            - ``{"type": "done", "stop_reason": str, "content_blocks": list, "usage": dict|None}``

        参数:
            activity_timeout: per-token 活跃超时秒数。None 表示使用
                              :meth:`__init__` 时从 config 读取的默认值。
                              支持热更新（调用方重新读取 config 后透传新值）。
            stream_manager: 可选 :class:`StreamManager`，用于注册 ``cancel_callback``
                            （Task 9 主路径）。
            session_id: 可选会话 ID，与 ``stream_manager`` 配对。
        """
        # activity_timeout 默认值：调用方透传 > config 默认值
        if activity_timeout is None:
            activity_timeout = self._activity_timeout

        t0 = time.perf_counter()
        async for event in self._main_backend.chat_stream(
            messages=messages,
            tools=tools,
            system=system,
            max_tokens=max_tokens,
            cancel_event=cancel_event,
            activity_timeout=activity_timeout,
            stream_manager=stream_manager,
            session_id=session_id,
        ):
            if event.get("type") == "done" and self._metrics_collector is not None:
                usage = event.get("usage")
                if usage is not None:
                    latency_ms = (time.perf_counter() - t0) * 1000
                    self._metrics_collector.observe_llm_usage(usage, latency_ms)
            yield event

    async def chat_consolidation(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """consolidation LLM 调用（async），用于记忆沉淀提取。

        参数:
            messages: 对话消息列表。
            system: 可选系统提示词（一般为 CONSOLIDATION_PROMPT）。
            max_tokens: 输出 token 上限，默认使用 _DEFAULT_MAX_TOKENS_CONSOLIDATION。

        返回:
            :class:`LLMResponse` 对象。
        """
        return await self._consolidation_backend.chat(
            messages=messages,
            tools=None,
            system=system,
            max_tokens=max_tokens or _DEFAULT_MAX_TOKENS_CONSOLIDATION,
        )

    # ── sync wrapper（供 workflow / memory 线程池路径调用）──

    def chat_main_sync(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """sync wrapper for :meth:`chat_main`，供线程池路径调用。

        内部 ``asyncio.run(self.chat_main(...))`` 在调用线程内创建临时
        事件循环。**不可**在主事件循环运行中的协程内直接调用（会抛
        ``RuntimeError: asyncio.run() cannot be called from a running
        event loop``）。仅可在以下上下文使用：

        - ``asyncio.to_thread`` 包裹的线程池线程内
        - workflow 的 ``_execute_workflow``（已由 scheduler 用
          ``asyncio.to_thread`` 调度）
        - 独立同步脚本 / 测试代码
        """
        return asyncio.run(
            self.chat_main(
                messages=messages,
                tools=tools,
                system=system,
                max_tokens=max_tokens,
            )
        )

    def chat_consolidation_sync(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """sync wrapper for :meth:`chat_consolidation`，供线程池路径调用。

        内部 ``asyncio.run(self.chat_consolidation(...))``，约束同
        :meth:`chat_main_sync`。
        """
        return asyncio.run(
            self.chat_consolidation(
                messages=messages,
                system=system,
                max_tokens=max_tokens,
            )
        )

    def count_tokens(self, text: str) -> int:
        """使用 tiktoken 估算文本的 token 数。

        注意：tiktoken 使用 cl100k_base 编码，对各家模型仅为近似估算，
        实际 token 数可能略有差异。
        """
        if not text:
            return 0
        encoding = _get_encoding()
        return len(encoding.encode(text))

    def count_messages_tokens(
        self, messages: List[Dict[str, Any]]
    ) -> int:
        """估算消息列表的总 token 数。

        对每条消息的 content 字段进行 token 计数，并附加每条消息的固定开销
        （近似消息包装开销）。

        参数:
            messages: 消息列表，content 可为字符串或 content block 列表。

        返回:
            估算的总 token 数。
        """
        if not messages:
            return 0

        encoding = _get_encoding()
        total = 0
        # 每条消息的固定包装开销（经验值）
        per_message_overhead = 4

        for message in messages:
            total += per_message_overhead
            content = message.get("content", "")

            if isinstance(content, str):
                total += len(encoding.encode(content)) if content else 0
            elif isinstance(content, list):
                # content block 列表形式
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        total += len(encoding.encode(text)) if text else 0
                    elif block_type == "tool_use":
                        # 工具调用：name + input 序列化后估算
                        name = block.get("name", "")
                        tool_input = block.get("input", {})
                        input_str = json.dumps(tool_input, ensure_ascii=False)
                        total += len(encoding.encode(name)) if name else 0
                        total += len(encoding.encode(input_str)) if input_str else 0
                    elif block_type == "tool_result":
                        # 工具结果：content 字段
                        result_content = block.get("content", "")
                        if isinstance(result_content, str):
                            total += (
                                len(encoding.encode(result_content))
                                if result_content
                                else 0
                            )
                        elif isinstance(result_content, list):
                            for sub in result_content:
                                if isinstance(sub, dict) and sub.get("type") == "text":
                                    text = sub.get("text", "")
                                    total += (
                                        len(encoding.encode(text)) if text else 0
                                    )

        return total
