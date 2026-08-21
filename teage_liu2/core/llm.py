"""LLM 客户端封装(多 Provider 支持,异步实现)。

搬自老系统 teage_liu/llm/client.py,M1 裁剪说明:
- 保留:per-token 活跃超时(ActivityTimeout)/ 重试语义(_is_retryable)/
  多 provider 抽象(anthropic + openai 兼容:openai/deepseek/qwen)/
  工具 schema 双格式互转 / 防 400 消息清洗(孤立 tool_result / 空 assistant /
  thinking block 剥离)
- 裁剪:reasoning 模式(ReasoningProfile 全套,M2+ 枝干带回)、
  consolidation 后端(记忆枝干带回)、StreamManager 取消回调(M2 带回)、
  sync wrapper(线程池路径,M2+ 带回)
- SDK import 延迟到 backend 构造时,import 本模块不强制要求 anthropic/openai 已安装

对外统一返回 :class:`LLMResponse`(兼容 anthropic.types.Message 接口):
- ``response.content``:content block 列表(Anthropic 风格)
- ``response.stop_reason``:``"tool_use"`` / ``"end_turn"`` / ``"max_tokens"``
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
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS_MAIN = 4096

# 各 Provider 默认 base_url / 环境变量名(OpenAI 兼容类)
_PROVIDER_DEFAULT_BASE_URL: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
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


class ActivityTimeout(Exception):
    """per-token 活跃超时:LLM 在 ``activity_timeout`` 秒内未返回任何 token。

    被 :func:`async_retry_on_failure` 捕获时不重试(见 ``_is_retryable``),
    区别于网络错误/限流等可重试异常。
    """
    pass


class StreamCancelled(Exception):
    """用户取消流式调用(主动断流 / cancel_event 兜底)。"""


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否可重试。

    429 限流、5xx 服务端错误、网络连接错误(无 ``status_code``)可重试;
    4xx 客户端错误(非 429)不重试。``ActivityTimeout`` 绝不重试。
    """
    if isinstance(exc, ActivityTimeout):
        return False
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status == 429 or status >= 500
    return True  # 无状态码(网络连接错误等)可重试


async def _with_activity_timeout(
    aiter: AsyncIterator[Any],
    timeout_sec: float,
    on_timeout: Optional[Callable[[], Any]] = None,
) -> AsyncIterator[Any]:
    """通用 per-item 活跃超时 wrapper(async generator)。

    对输入逐项用 ``asyncio.wait_for`` 等待,每项之间最多等 ``timeout_sec`` 秒。
    超时则调用 ``on_timeout``(失败仅告警,不掩盖 ActivityTimeout),然后 raise。
    """
    ait = aiter.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(ait.__anext__(), timeout=timeout_sec)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            if on_timeout is not None:
                try:
                    await on_timeout()
                except Exception as close_exc:
                    logger.warning("on_timeout 回调失败(已忽略): %r", close_exc)
            raise ActivityTimeout(
                f"LLM 在 {timeout_sec}s 内未返回任何 token"
            )
        yield item


def async_retry_on_failure(max_retries: int = 3, base_delay: float = 1.0):
    """异步 LLM 调用指数退避重试装饰器。

    对 429 / 5xx / 网络错误重试(由 ``_is_retryable`` 判定),退避
    ``base_delay * 2**attempt``;对 async generator 仅在未 yield 任何
    item 时重试(避免重复输出已 yield 的内容)。
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if not _is_retryable(e):
                        raise
                    attempt += 1
                    if attempt > max_retries:
                        logger.error("LLM 调用重试 %d 次仍失败: %r", max_retries, e)
                        raise
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "LLM 调用失败(attempt=%d/%d, delay=%.1fs): %r",
                        attempt, max_retries, delay, e,
                    )
                    await asyncio.sleep(delay)

        @functools.wraps(func)
        async def wrapper_gen(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    yielded_any = False
                    async for item in func(*args, **kwargs):
                        yielded_any = True
                        yield item
                    return
                except Exception as e:
                    if not _is_retryable(e):
                        raise
                    # 已 yield 过内容不重试(避免重复输出)
                    if yielded_any:
                        raise
                    attempt += 1
                    if attempt > max_retries:
                        raise
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "LLM 流式调用失败(attempt=%d/%d, delay=%.1fs): %r",
                        attempt, max_retries, delay, e,
                    )
                    await asyncio.sleep(delay)

        if inspect.isasyncgenfunction(func):
            return wrapper_gen
        return wrapper

    return decorator


class LLMResponse:
    """统一 LLM 响应包装,接口兼容 ``anthropic.types.Message``。"""

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

    对外接口使用 Anthropic 风格的消息与工具 schema,由子类内部完成格式转换。
    流式事件格式(统一):
    - ``{"type": "text", "text": str}``:文本增量
    - ``{"type": "reasoning", "text": str, "signature": str|None}``:推理增量
    - ``{"type": "done", "stop_reason": str, "content_blocks": list, "usage": dict|None}``
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        provider_id: str = "",
    ) -> None:
        self.model: str = model
        self.api_key: str = api_key
        self.base_url: Optional[str] = base_url
        self.provider_id: str = provider_id

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        activity_timeout: float = 60.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        raise NotImplementedError
        yield {}  # pragma: no cover


def _convert_content_blocks(blocks: List[Any]) -> List[Dict[str, Any]]:
    """将 SDK 返回的 content block 对象统一转为 dict(Anthropic 风格)。

    - ``text`` → {"type": "text", "text": str}
    - ``tool_use`` → {"type": "tool_use", "id", "name", "input"}
    - ``thinking`` → {"type": "thinking", "thinking", "signature"}
      (thinking 保留 signature 是防 400 的关键:Anthropic 要求已发 thinking
       block 带 signature 重发,否则 400)
    - 其他 → {"type": str}
    """
    out: List[Dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict):
            out.append(block)
            continue
        block_type = getattr(block, "type", None)
        if block_type == "text":
            out.append({"type": "text", "text": getattr(block, "text", "")})
        elif block_type == "tool_use":
            out.append({
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}) or {},
            })
        elif block_type == "thinking":
            out.append({
                "type": "thinking",
                "thinking": getattr(block, "thinking", ""),
                "signature": getattr(block, "signature", ""),
            })
        else:
            out.append({"type": block_type or "unknown"})
    return out


def _extract_anthropic_usage(usage_obj: Any) -> Dict[str, int]:
    """从 Anthropic usage 对象提取 usage dict。"""
    usage_dict: Dict[str, int] = {
        "input_tokens": getattr(usage_obj, "input_tokens", 0),
        "output_tokens": getattr(usage_obj, "output_tokens", 0),
        "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0),
    }
    reasoning_tokens = getattr(usage_obj, "reasoning_tokens", None)
    if reasoning_tokens is None:
        details = getattr(usage_obj, "output_tokens_details", None)
        if details is not None:
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
    if reasoning_tokens:
        usage_dict["reasoning_tokens"] = reasoning_tokens
    return usage_dict


class AsyncAnthropicBackend(AsyncBaseBackend):
    """异步 Anthropic 后端,使用 ``anthropic.AsyncAnthropic`` SDK。"""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        provider_id: str = "anthropic",
    ) -> None:
        super().__init__(model, api_key, base_url, provider_id=provider_id)
        import anthropic  # 延迟导入:SDK 缺失时仅在构造该 backend 时报错

        client_kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        try:
            self._client = anthropic.AsyncAnthropic(**client_kwargs)
        except anthropic.AnthropicError as e:
            raise RuntimeError(f"初始化 anthropic 客户端失败: {e}") from e

    @staticmethod
    def _strip_thinking_blocks(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清理 messages 中的 thinking block。

        Anthropic API 不接受 messages 中的 thinking block(需 signature 配对),
        M1 无 reasoning 模式,历史中若残留 thinking block 必须剥离,否则 400。
        """
        result: List[Dict[str, Any]] = []
        for msg in messages:
            new_msg = dict(msg)
            new_msg.pop("reasoning_content", None)
            content = new_msg.get("content")
            if isinstance(content, list):
                new_msg["content"] = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                ]
            result.append(new_msg)
        return result

    @async_retry_on_failure()
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        messages = self._strip_thinking_blocks(messages)
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
        content_blocks = _convert_content_blocks(getattr(response, "content", []) or [])
        usage_dict = None
        usage_obj = getattr(response, "usage", None)
        if usage_obj is not None:
            usage_dict = _extract_anthropic_usage(usage_obj)
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
    ) -> AsyncIterator[Dict[str, Any]]:
        """Anthropic 异步流式调用(``client.messages.stream``)。

        取消双路径(与老系统一致):
        - 主路径:stream.close 主动断开(经 stream_manager, M2 带回)
        - 兜底路径:cancel_event.is_set() 在收到 chunk 后检查
        """
        messages = self._strip_thinking_blocks(messages)
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
            async def _close_stream() -> None:
                try:
                    await stream.close()
                except Exception as e:
                    logger.warning("stream.close 失败: %r", e)

            reasoning_parts: List[str] = []
            try:
                async for event in _with_activity_timeout(
                    stream, activity_timeout, _close_stream
                ):
                    if cancel_event and cancel_event.is_set():
                        raise StreamCancelled("User cancelled the stream")
                    if getattr(event, "type", None) == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        delta_type = getattr(delta, "type", None)
                        if delta_type == "text_delta":
                            text = getattr(delta, "text", "")
                            if text:
                                yield {"type": "text", "text": text}
                        elif delta_type == "thinking_delta":
                            thinking_text = getattr(delta, "thinking", "")
                            if thinking_text:
                                reasoning_parts.append(thinking_text)
                                yield {
                                    "type": "reasoning",
                                    "text": thinking_text,
                                    "signature": None,
                                }
                final_message = await stream.get_final_message()
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
                    raise StreamCancelled(f"User cancelled, stream closed: {e!r}") from e
                raise

            content_blocks = _convert_content_blocks(
                getattr(final_message, "content", []) or []
            )
            usage_dict = None
            usage_obj = getattr(final_message, "usage", None)
            if usage_obj is not None:
                usage_dict = _extract_anthropic_usage(usage_obj)
            yield {
                "type": "done",
                "stop_reason": getattr(final_message, "stop_reason", "end_turn") or "end_turn",
                "content_blocks": content_blocks,
                "usage": usage_dict,
            }


class AsyncOpenAICompatBackend(AsyncBaseBackend):
    """OpenAI 兼容后端(openai / deepseek / qwen),使用 ``AsyncOpenAI`` SDK。"""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        provider_name: str = "openai",
    ) -> None:
        super().__init__(model, api_key, base_url, provider_id=provider_name)
        import openai  # 延迟导入

        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    # ------------------------------------------------------------------
    # 防 400 消息清洗(坑知识,原样保留)
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_orphan_tool_results(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清理孤立 tool_result 消息(前面无对应 tool_use 的)。

        OpenAI 400: "Messages with role 'tool' must be a response to a
        preceding message with 'tool_calls'"。
        同时清理孤立 assistant(tool_calls)(无任何 tool_result 应答的整条)。
        """
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

        cleaned: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "user" and isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result:
                    kept_blocks: List[Dict[str, Any]] = []
                    for block in content:
                        if not isinstance(block, dict):
                            kept_blocks.append(block)
                            continue
                        if block.get("type") == "tool_result":
                            tid = block.get("tool_use_id")
                            if tid not in valid_tool_use_ids:
                                logger.warning("清理孤立 tool_result: tool_use_id=%s", tid)
                                continue
                        kept_blocks.append(block)
                    if kept_blocks:
                        cleaned.append({**msg, "content": kept_blocks})
                    else:
                        logger.warning("清理整条孤立 tool_result user 消息")
                    continue
            cleaned.append(msg)

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
                        logger.warning("清理孤立 assistant(tool_calls): %s", tool_use_ids)
                        continue
            result.append(msg)
        return result

    @staticmethod
    def _clean_empty_assistant_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清理空 assistant 消息(无文本且无 tool_use)。

        OpenAI 400: "Invalid assistant message: content or tool_calls must be set"。
        """
        result = []
        for msg in messages:
            if msg.get("role") != "assistant":
                result.append(msg)
                continue
            content = msg.get("content")
            if isinstance(content, str) and not content.strip():
                logger.warning("清理空 assistant 消息(空字符串 content)")
                continue
            if isinstance(content, list) and not content:
                logger.warning("清理空 assistant 消息(空列表 content)")
                continue
            if not content:
                logger.warning("清理空 assistant 消息(content=%r)", content)
                continue
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
                    logger.warning("清理空 assistant 消息(content 列表无 tool_use/text)")
                    continue
            result.append(msg)
        return result

    @staticmethod
    def _clean_orphan_reasoning(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清理转换残留的空 thinking block / 孤立 reasoning_content 字段。"""
        result: List[Dict[str, Any]] = []
        for msg in messages:
            new_msg = dict(msg)
            if "reasoning_content" in new_msg:
                rc = new_msg.get("reasoning_content")
                if not rc or (isinstance(rc, str) and not rc.strip()):
                    new_msg.pop("reasoning_content", None)
                elif new_msg.get("role") != "assistant":
                    new_msg.pop("reasoning_content", None)
            content = new_msg.get("content")
            if isinstance(content, list):
                new_content = []
                for b in content:
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "thinking"
                        and not b.get("thinking", "").strip()
                        and not b.get("signature", "").strip()
                    ):
                        logger.warning("清理空 thinking block")
                        continue
                    new_content.append(b)
                new_msg["content"] = new_content
            result.append(new_msg)
        return result

    def _convert_messages(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str],
    ) -> List[Dict[str, Any]]:
        """将 Anthropic 风格消息列表转为 OpenAI 消息列表。

        - 顶层 system 注入为 OpenAI role=system 消息(前置)
        - assistant: text 块拼 content, tool_use 块转 tool_calls
        - user: tool_result 块拆为多条 role=tool 消息
        """
        messages = self._clean_orphan_tool_results(messages)
        messages = self._clean_empty_assistant_messages(messages)
        messages = self._clean_orphan_reasoning(messages)

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
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(
                                    block.get("input", {}) or {},
                                    ensure_ascii=False,
                                ),
                            },
                        })
                msg_out: Dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                out.append(msg_out)
            elif role == "user":
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
                                result_content = "\n".join(
                                    sub.get("text", "")
                                    for sub in result_content
                                    if isinstance(sub, dict) and sub.get("type") == "text"
                                )
                            out.append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": str(result_content),
                            })
                        elif btype == "text":
                            text = block.get("text", "")
                            if text:
                                out.append({"role": "user", "content": text})
                else:
                    text_parts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    out.append({
                        "role": "user",
                        "content": "\n".join(text_parts) if text_parts else "",
                    })
            else:
                out.append({"role": role, "content": str(content) if content else ""})
        return out

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将 Anthropic 工具 schema 转为 OpenAI 工具格式。"""
        out: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and "function" in tool:
                out.append(tool)
                continue
            schema = tool.get("input_schema") or tool.get("parameters") or {
                "type": "object",
                "properties": {},
            }
            out.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": schema,
                },
            })
        return out

    @staticmethod
    def _extract_cache_usage(usage_obj: Any) -> Dict[str, int]:
        """从 OpenAI/DeepSeek usage 对象提取缓存字段(DeepSeek 扩展)。"""
        return {
            "cache_creation_input_tokens": getattr(usage_obj, "prompt_cache_miss_tokens", 0),
            "cache_read_input_tokens": getattr(usage_obj, "prompt_cache_hit_tokens", 0),
        }

    @staticmethod
    def _convert_response(response: Any) -> LLMResponse:
        """将 OpenAI ChatCompletion 响应转为 LLMResponse。"""
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
                    logger.warning("tool_call args JSON 解析失败: %s", args_str[:200])
                    input_args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": getattr(tc, "id", ""),
                    "name": getattr(fn, "name", ""),
                    "input": input_args,
                })

        stop_reason = _FINISH_REASON_MAP.get(finish_reason, "end_turn")
        if tool_calls and stop_reason != "tool_use":
            stop_reason = "tool_use"

        usage_dict: Optional[Dict[str, int]] = None
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
    ) -> AsyncIterator[Dict[str, Any]]:
        openai_messages = self._convert_messages(messages, system)
        openai_tools = self._convert_tools(tools) if tools else None
        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS_MAIN,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if openai_tools:
            request_kwargs["tools"] = openai_tools

        stream = await self._client.chat.completions.create(**request_kwargs)

        async def _close_stream() -> None:
            try:
                await stream.close()
            except Exception as e:
                logger.warning("stream.close 失败: %r", e)

        full_text_parts: List[str] = []
        tool_calls_acc: Dict[int, Dict[str, str]] = {}
        finish_reason: str = "stop"
        stream_usage: Optional[Any] = None

        try:
            async for chunk in _with_activity_timeout(stream, activity_timeout, _close_stream):
                if cancel_event and cancel_event.is_set():
                    raise StreamCancelled("User cancelled the stream")
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    stream_usage = chunk_usage
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                delta_content = getattr(delta, "content", None)
                if delta_content:
                    full_text_parts.append(delta_content)
                    yield {"type": "text", "text": delta_content}
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
                raise StreamCancelled(f"User cancelled, stream closed: {e!r}") from e
            raise
        finally:
            try:
                await stream.close()
            except Exception:
                pass

        content_blocks: List[Dict[str, Any]] = []
        full_text = "".join(full_text_parts)
        if full_text:
            content_blocks.append({"type": "text", "text": full_text})
        for idx in sorted(tool_calls_acc.keys()):
            slot = tool_calls_acc[idx]
            args_str = slot.get("arguments_str", "") or "{}"
            try:
                input_args = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                logger.warning("tool_call args JSON 解析失败: %s", args_str[:200])
                input_args = {}
            content_blocks.append({
                "type": "tool_use",
                "id": slot.get("id", ""),
                "name": slot.get("name", ""),
                "input": input_args,
            })

        stop_reason = _FINISH_REASON_MAP.get(finish_reason, "end_turn")
        if tool_calls_acc and stop_reason != "tool_use":
            stop_reason = "tool_use"

        usage_dict: Optional[Dict[str, int]] = None
        if stream_usage is not None:
            usage_dict = {
                "input_tokens": getattr(stream_usage, "prompt_tokens", 0),
                "output_tokens": getattr(stream_usage, "completion_tokens", 0),
                **self._extract_cache_usage(stream_usage),
            }
        yield {
            "type": "done",
            "stop_reason": stop_reason,
            "content_blocks": content_blocks,
            "usage": usage_dict,
        }


def _create_backend(
    provider: str,
    model: str,
    api_key: str,
    base_url: Optional[str] = None,
) -> AsyncBaseBackend:
    """根据 provider 创建对应异步 Backend 实例。"""
    provider_lower = (provider or "").lower().strip()
    if not provider_lower:
        raise ValueError("provider 未设置,请在 config.yaml 中配置 llm.main_provider")

    if provider_lower == "anthropic":
        return AsyncAnthropicBackend(model=model, api_key=api_key, base_url=base_url)
    if provider_lower in ("openai", "deepseek", "qwen"):
        effective_base_url = base_url or _PROVIDER_DEFAULT_BASE_URL.get(provider_lower)
        return AsyncOpenAICompatBackend(
            model=model,
            api_key=api_key,
            base_url=effective_base_url,
            provider_name=provider_lower,
        )
    raise ValueError(
        f"不支持的 provider: {provider_lower!r},目前支持: anthropic / openai / deepseek / qwen"
    )


# 向后兼容别名
BaseBackend = AsyncBaseBackend
AnthropicBackend = AsyncAnthropicBackend
OpenAICompatBackend = AsyncOpenAICompatBackend


class LLMClient:
    """LLM 客户端(M1:主对话后端;consolidation 由记忆枝干带回)。"""

    def __init__(
        self,
        config_path: str = "config.yaml",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """初始化 LLM 客户端。

        异常(主干契约:配置开了但初始化失败 → 抛出使启动失败,不静默降级):
        - ValueError: llm 段缺失 / main_model 未设置 / main_api_key 未设置
        """
        from .config import get_llm_timeouts, load_config

        self._config_path = config_path
        if config is None:
            config = load_config(config_path)
        self._config = config

        llm_config = config.get("llm", {})
        if not llm_config:
            raise ValueError("配置中缺少 llm 段或为空")

        self.main_provider: str = llm_config.get("main_provider", "anthropic")
        self.main_model: str = llm_config.get("main_model", "")
        self.main_api_key: str = self._resolve_api_key(
            llm_config.get("main_api_key"), self.main_provider
        )
        self.main_base_url: Optional[str] = llm_config.get("main_base_url") or None
        self.max_context_tokens: int = int(llm_config.get("max_context_tokens", 200000))
        self._activity_timeout, self._stream_total_timeout = get_llm_timeouts(config)

        if not self.main_model:
            raise ValueError("配置 llm.main_model 未设置")
        if not self.main_api_key:
            raise ValueError(
                f"主对话 LLM API Key 未设置:请在环境变量 "
                f"{_PROVIDER_DEFAULT_ENV_KEY.get(self.main_provider, 'API_KEY')} 中配置"
            )

        self._main_backend: AsyncBaseBackend = _create_backend(
            provider=self.main_provider,
            model=self.main_model,
            api_key=self.main_api_key,
            base_url=self.main_base_url,
        )
        # 多角色路由表(§5 invoke_llm / §17 老系统 consolidation 承接):
        # role → backend;v1.0 仅 main 必配;consolidation 可选(记忆巩固类扩展,
        # 配置缺失时降级 main 后端 —— 主对话不中断,§0 降级优先)
        self._role_backends: Dict[str, AsyncBaseBackend] = {
            "main": self._main_backend,
        }
        consolidation = self._build_consolidation_backend(llm_config)
        if consolidation is not None:
            self._role_backends["consolidation"] = consolidation
            logger.info("LLMClient 多角色路由: consolidation=%s/%s", consolidation.provider_id, consolidation.model)
        logger.info(
            "LLMClient 初始化完成: main=%s/%s, roles=%s, activity_timeout=%.1fs, "
            "stream_total_timeout=%.1fs",
            self.main_provider, self.main_model, sorted(self._role_backends),
            self._activity_timeout, self._stream_total_timeout,
        )

    def _build_consolidation_backend(
        self, llm_config: Dict[str, Any]
    ) -> Optional[AsyncBaseBackend]:
        """构建 consolidation 角色后端(§17:承接老系统记忆巩固 LLM 调用)。

        配置齐全(provider/model/api_key)才构建;缺失 → None(降级 main,不静默失败)。
        """
        provider = (llm_config.get("consolidation_provider") or "").strip()
        model = (llm_config.get("consolidation_model") or "").strip()
        api_key = llm_config.get("consolidation_api_key") or ""
        if not provider or not model:
            return None
        if not api_key:
            env_name = _PROVIDER_DEFAULT_ENV_KEY.get(provider.lower(), "API_KEY")
            api_key = os.environ.get(env_name, "")
        if not api_key:
            logger.warning(
                "llm.consolidation_* 已配置但 API Key 缺失(consolidation 后端不可用,"
                "invoke_llm role=consolidation 将降级 main 后端)"
            )
            return None
        try:
            return _create_backend(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=llm_config.get("consolidation_base_url") or None,
            )
        except (ValueError, RuntimeError) as e:
            logger.warning("consolidation 后端初始化失败,降级 main 后端: %s", e)
            return None

    @staticmethod
    def _resolve_api_key(config_value: Optional[str], provider: str) -> str:
        """解析 API Key:优先 config 值,缺失时回退到 provider 对应的环境变量。"""
        if config_value:
            return config_value
        env_name = _PROVIDER_DEFAULT_ENV_KEY.get(provider, "")
        return os.environ.get(env_name, "") if env_name else ""

    @property
    def activity_timeout(self) -> float:
        return self._activity_timeout

    @property
    def stream_total_timeout(self) -> float:
        return self._stream_total_timeout

    async def chat_role(
        self,
        role: str,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """invoke_llm 通道(§5/§17):按 role 多角色路由的非流式 LLM 调用。

        路由规则:v1.0 已配置的 role(main / consolidation)→ 对应后端;
        未知/未配置 role → 降级 main 后端(降级优先,主对话不中断)。
        非流式(LLMAdapter 直调,协议级防重入 —— 不进 pipeline/钩子链)。
        """
        backend = self._role_backends.get(role) or self._main_backend
        if role not in self._role_backends:
            logger.debug("invoke_llm role=%r 后端未配置,降级 main 后端", role)
        return await backend.chat(
            messages=messages, tools=tools, system=system, max_tokens=max_tokens,
        )

    async def chat_main(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """主对话 LLM 调用(async)。"""
        return await self._main_backend.chat(
            messages=messages, tools=tools, system=system, max_tokens=max_tokens,
        )

    async def chat_main_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        activity_timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """主对话 LLM 异步流式调用。

        事件格式(backend 层):
        - {"type": "text", "text": str}
        - {"type": "reasoning", "text": str, "signature": str|None}
        - {"type": "done", "stop_reason", "content_blocks", "usage"}
        """
        if activity_timeout is None:
            activity_timeout = self._activity_timeout
        async for event in self._main_backend.chat_stream(
            messages=messages,
            tools=tools,
            system=system,
            max_tokens=max_tokens,
            cancel_event=cancel_event,
            activity_timeout=activity_timeout,
        ):
            yield event

    # ------------------------------------------------------------------
    # token 估算(tiktoken 延迟导入,不可用时返回 0)
    # ------------------------------------------------------------------
    def count_tokens(self, text: str) -> int:
        """使用 tiktoken 估算文本 token 数(cl100k_base 近似)。"""
        if not text:
            return 0
        try:
            import tiktoken
        except ImportError:
            return 0
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))

    def count_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """估算消息列表总 token 数(每条消息附加固定包装开销 4)。"""
        if not messages:
            return 0
        try:
            import tiktoken
        except ImportError:
            return 0
        enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for message in messages:
            total += 4
            content = message.get("content", "")
            if isinstance(content, str):
                total += len(enc.encode(content)) if content else 0
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text = block.get("text", "")
                        total += len(enc.encode(text)) if text else 0
                    elif btype == "tool_use":
                        name = block.get("name", "")
                        input_str = json.dumps(block.get("input", {}) or {}, ensure_ascii=False)
                        total += len(enc.encode(name)) if name else 0
                        total += len(enc.encode(input_str)) if input_str else 0
                    elif btype == "tool_result":
                        result_content = block.get("content", "")
                        if isinstance(result_content, str):
                            total += len(enc.encode(result_content)) if result_content else 0
                        elif isinstance(result_content, list):
                            for sub in result_content:
                                if isinstance(sub, dict) and sub.get("type") == "text":
                                    text = sub.get("text", "")
                                    total += len(enc.encode(text)) if text else 0
        return total

    def close(self) -> None:
        """关闭底层客户端(释放连接池)。

        注意:anthropic/openai SDK 的 ``client.close()`` 返回 coroutine;
        这里不做 await(接口保持同步),改为调度到事件循环,避免
        "coroutine was never awaited" RuntimeWarning。
        """
        for attr in ("_main_backend",):
            backend = getattr(self, attr, None)
            client = getattr(backend, "_client", None)
            if client is not None and hasattr(client, "close"):
                try:
                    result = client.close()
                    if asyncio.iscoroutine(result):
                        try:
                            asyncio.get_running_loop().create_task(result)
                        except RuntimeError:
                            # 事件循环已关闭(进程退出中):连接由 GC 回收,忽略
                            pass
                except Exception as e:
                    logger.warning("关闭 LLM 客户端失败: %s", e)
