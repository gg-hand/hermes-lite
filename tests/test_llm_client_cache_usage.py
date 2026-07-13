"""OpenAICompatBackend 缓存命中字段采集单元测试（P0b）。

验证 DeepSeek（OpenAI 兼容扩展）返回的 ``prompt_cache_hit_tokens`` /
``prompt_cache_miss_tokens`` 被正确映射到 ``LLMResponse.usage`` 的
``cache_read_input_tokens`` / ``cache_creation_input_tokens``。

测试策略：
- 直接测试 ``OpenAICompatBackend._extract_cache_usage`` 静态方法，覆盖
  DeepSeek 风格字段存在 / 缺失两种情况，避免 mock 整个 openai SDK 客户端。
- 通过 MagicMock 模拟 openai SDK，构造 ``OpenAICompatBackend`` 实例，
  并对 ``_convert_response`` 与 ``chat_stream`` 的 usage 提取链路做端到端验证。
- 同步验证 ``AnthropicBackend`` 仍使用 Anthropic 原生字段，未被本次改动影响。

运行方式：
    python -m unittest tests.test_llm_client_cache_usage -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.llm.client import LLMResponse, OpenAICompatBackend  # noqa: E402


class _Usage:
    """轻量 usage 对象，按关键字参数装配属性。

    用于模拟 OpenAI / DeepSeek 响应中的 usage 对象：仅设置传入的字段，
    未传入的字段不存在对应属性（用于测试 ``getattr`` 降级逻辑）。
    """

    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class TestExtractCacheUsage(unittest.TestCase):
    """直接测试 ``OpenAICompatBackend._extract_cache_usage`` 静态方法。"""

    def test_deepseek_cache_fields_mapped(self):
        """DeepSeek 风格字段存在时，正确映射到 Anthropic 风格字段。

        - ``prompt_cache_hit_tokens`` -> ``cache_read_input_tokens``
        - ``prompt_cache_miss_tokens`` -> ``cache_creation_input_tokens``
        """
        usage = _Usage(
            prompt_tokens=5500,
            completion_tokens=200,
            prompt_cache_hit_tokens=5000,
            prompt_cache_miss_tokens=500,
        )
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertEqual(result["cache_read_input_tokens"], 5000)
        self.assertEqual(result["cache_creation_input_tokens"], 500)

    def test_missing_cache_fields_degrade_to_zero(self):
        """标准 OpenAI 响应无缓存字段时，降级为 0 且不抛异常。"""
        usage = _Usage(
            prompt_tokens=100,
            completion_tokens=50,
        )
        # 不应抛出 AttributeError
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertEqual(result["cache_read_input_tokens"], 0)
        self.assertEqual(result["cache_creation_input_tokens"], 0)

    def test_none_like_usage_does_not_crash(self):
        """usage 对象完全无属性时也不抛异常（仅 0 降级）。"""
        usage = _Usage()
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertEqual(result["cache_read_input_tokens"], 0)
        self.assertEqual(result["cache_creation_input_tokens"], 0)

    def test_zero_cache_values_preserved(self):
        """显式为 0 的缓存字段应被保留（与缺失字段区分）。"""
        usage = _Usage(
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=0,
        )
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertEqual(result["cache_read_input_tokens"], 0)
        self.assertEqual(result["cache_creation_input_tokens"], 0)

    def test_only_hit_present(self):
        """仅命中字段存在时，未命中字段降级为 0。"""
        usage = _Usage(prompt_cache_hit_tokens=3000)
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertEqual(result["cache_read_input_tokens"], 3000)
        self.assertEqual(result["cache_creation_input_tokens"], 0)

    def test_only_miss_present(self):
        """仅未命中字段存在时，命中字段降级为 0。"""
        usage = _Usage(prompt_cache_miss_tokens=800)
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertEqual(result["cache_read_input_tokens"], 0)
        self.assertEqual(result["cache_creation_input_tokens"], 800)

    def test_return_type_is_dict_with_int_values(self):
        """返回值应为 dict，且字段值为 int 类型。"""
        usage = _Usage(
            prompt_cache_hit_tokens=1000,
            prompt_cache_miss_tokens=200,
        )
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result["cache_read_input_tokens"], int)
        self.assertIsInstance(result["cache_creation_input_tokens"], int)

    def test_magicmock_usage_compatible(self):
        """getattr 方式访问属性，兼容 MagicMock 测试替身。"""
        usage = MagicMock()
        usage.prompt_cache_hit_tokens = 42
        usage.prompt_cache_miss_tokens = 17
        result = OpenAICompatBackend._extract_cache_usage(usage)
        self.assertEqual(result["cache_read_input_tokens"], 42)
        self.assertEqual(result["cache_creation_input_tokens"], 17)


class TestConvertResponseCacheUsage(unittest.TestCase):
    """端到端验证 ``_convert_response`` 将缓存字段写入 ``LLMResponse.usage``。"""

    def _build_backend(self) -> OpenAICompatBackend:
        """构造一个 OpenAICompatBackend 实例，mock 掉 openai SDK 客户端。

        通过在 sys.modules 注入 mock openai 模块后实例化 Backend，
        避免依赖真实 API Key / 网络。
        """
        # OpenAICompatBackend.__init__ 中执行了 `from openai import OpenAI`，
        # 已安装真实 openai SDK 时可直接实例化；仅需伪造 api_key 即可。
        return OpenAICompatBackend(
            model="deepseek-chat",
            api_key="test-key-not-used",
            base_url="https://api.deepseek.com/v1",
            provider_name="deepseek",
        )

    def _build_openai_response(self, usage_obj: Any) -> MagicMock:
        """构造一个 mock 的 OpenAI ChatCompletion 响应对象。"""
        message = MagicMock()
        message.content = "hello"
        message.tool_calls = None

        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "stop"

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage_obj
        return response

    def test_convert_response_with_deepseek_cache_fields(self):
        """非流式响应：DeepSeek 缓存字段正确进入 LLMResponse.usage。"""
        backend = self._build_backend()
        usage = _Usage(
            prompt_tokens=5500,
            completion_tokens=200,
            prompt_cache_hit_tokens=5000,
            prompt_cache_miss_tokens=500,
        )
        response = self._build_openai_response(usage)

        llm_resp: LLMResponse = backend._convert_response(response)
        self.assertIsNotNone(llm_resp.usage)
        self.assertEqual(llm_resp.usage["input_tokens"], 5500)
        self.assertEqual(llm_resp.usage["output_tokens"], 200)
        self.assertEqual(llm_resp.usage["cache_read_input_tokens"], 5000)
        self.assertEqual(llm_resp.usage["cache_creation_input_tokens"], 500)

    def test_convert_response_without_cache_fields(self):
        """非流式响应：标准 OpenAI 响应无缓存字段时降级为 0。"""
        backend = self._build_backend()
        usage = _Usage(
            prompt_tokens=100,
            completion_tokens=50,
        )
        response = self._build_openai_response(usage)

        llm_resp: LLMResponse = backend._convert_response(response)
        self.assertIsNotNone(llm_resp.usage)
        self.assertEqual(llm_resp.usage["input_tokens"], 100)
        self.assertEqual(llm_resp.usage["output_tokens"], 50)
        self.assertEqual(llm_resp.usage["cache_read_input_tokens"], 0)
        self.assertEqual(llm_resp.usage["cache_creation_input_tokens"], 0)

    def test_convert_response_no_usage(self):
        """非流式响应：响应无 usage 对象时 usage_dict 为 None。"""
        backend = self._build_backend()
        response = self._build_openai_response(None)

        llm_resp: LLMResponse = backend._convert_response(response)
        # usage 为 None 时不构造 usage_dict
        self.assertIsNone(llm_resp.usage)


class _AsyncChunkStream:
    """Mock OpenAI/Anthropic async stream：async iterable + ``close()``。

    模拟 ``await client.chat.completions.create(stream=True, ...)`` 返回的
    stream 对象：支持 ``async for chunk in stream`` 与 ``await stream.close()``。
    ``chat_stream`` 改为 async generator（spec Task 3）后，本类替代旧的
    ``iter(chunks)`` 同步迭代器。
    """

    def __init__(self, chunks: List[Any]) -> None:
        self._chunks: List[Any] = list(chunks)
        self._index: int = 0
        self.close_call_count: int = 0

    def __aiter__(self) -> "_AsyncChunkStream":
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def close(self) -> None:
        self.close_call_count += 1


class TestChatStreamCacheUsage(unittest.IsolatedAsyncioTestCase):
    """端到端验证 ``chat_stream`` 末个 chunk 的缓存字段采集。

    通过 mock ``self._client.chat.completions.create`` 返回一个固定 chunk 列表，
    其中末个 chunk 携带 usage（含 DeepSeek 缓存字段），验证最终 done 事件中
    usage 字段正确映射。

    注：``chat_stream`` 已改为 async generator（spec Task 3），本类用
    :class:`unittest.IsolatedAsyncioTestCase` + ``async for`` 收集事件。
    """

    def _build_backend(self) -> OpenAICompatBackend:
        """构造一个 OpenAICompatBackend 实例，mock 掉 openai SDK 客户端。"""
        return OpenAICompatBackend(
            model="deepseek-chat",
            api_key="test-key-not-used",
            base_url="https://api.deepseek.com/v1",
            provider_name="deepseek",
        )

    def _make_chunk(
        self,
        content: str = "",
        finish_reason: str = None,
        usage: Any = None,
    ) -> MagicMock:
        """构造一个 mock 的 OpenAI 流式 chunk。"""
        chunk = MagicMock()
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _collect_stream(self, backend: OpenAICompatBackend) -> List[Any]:
        """收集 ``backend.chat_stream`` 的全部事件（async generator → list）。"""
        events: List[Any] = []
        async for event in backend.chat_stream(
            messages=[{"role": "user", "content": "hi"}]
        ):
            events.append(event)
        return events

    async def test_stream_with_deepseek_cache_fields(self):
        """流式响应：末个 chunk 含 DeepSeek 缓存字段时正确映射。"""
        backend = self._build_backend()

        # chunk 序列：文本增量 + 末个 usage chunk（choices 为空）
        chunks: List[MagicMock] = [
            self._make_chunk(content="hel"),
            self._make_chunk(content="lo", finish_reason="stop"),
            self._make_chunk(
                usage=_Usage(
                    prompt_tokens=5500,
                    completion_tokens=10,
                    prompt_cache_hit_tokens=5000,
                    prompt_cache_miss_tokens=500,
                )
            ),
        ]
        # 末个 chunk choices 为空（OpenAI include_usage 行为）
        chunks[-1].choices = []

        backend._client.chat.completions.create = AsyncMock(
            return_value=_AsyncChunkStream(chunks)
        )

        events = await self._collect_stream(backend)
        done_event = events[-1]
        self.assertEqual(done_event["type"], "done")
        usage = done_event["usage"]
        self.assertIsNotNone(usage)
        self.assertEqual(usage["input_tokens"], 5500)
        self.assertEqual(usage["output_tokens"], 10)
        self.assertEqual(usage["cache_read_input_tokens"], 5000)
        self.assertEqual(usage["cache_creation_input_tokens"], 500)

    async def test_stream_without_cache_fields(self):
        """流式响应：标准 OpenAI usage 无缓存字段时降级为 0。"""
        backend = self._build_backend()

        chunks: List[MagicMock] = [
            self._make_chunk(content="hi", finish_reason="stop"),
            self._make_chunk(
                usage=_Usage(
                    prompt_tokens=100,
                    completion_tokens=20,
                )
            ),
        ]
        chunks[-1].choices = []

        backend._client.chat.completions.create = AsyncMock(
            return_value=_AsyncChunkStream(chunks)
        )

        events = await self._collect_stream(backend)
        done_event = events[-1]
        usage = done_event["usage"]
        self.assertIsNotNone(usage)
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["cache_read_input_tokens"], 0)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)

    async def test_stream_no_usage_chunk(self):
        """流式响应：无 usage chunk 时 done 事件 usage 为 None。"""
        backend = self._build_backend()

        chunks: List[MagicMock] = [
            self._make_chunk(content="hi", finish_reason="stop"),
        ]
        backend._client.chat.completions.create = AsyncMock(
            return_value=_AsyncChunkStream(chunks)
        )

        events = await self._collect_stream(backend)
        done_event = events[-1]
        self.assertIsNone(done_event["usage"])


class TestAnthropicBackendUnchanged(unittest.TestCase):
    """回归保护：确认 AnthropicBackend 仍使用 Anthropic 原生字段，未被改动。

    通过检查源文件中的关键字符串，确保 AnthropicBackend 的 usage 采集逻辑
    仍直接读取 ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
    属性（而非 DeepSeek 的 prompt_cache_* 字段）。
    """

    def test_anthropic_still_uses_native_cache_fields(self):
        """AnthropicBackend 应直接读取 Anthropic 风格字段。"""
        client_path = Path(_PROJECT_ROOT) / "hermes" / "llm" / "client.py"
        source = client_path.read_text(encoding="utf-8")

        # AnthropicBackend.chat 与 chat_stream 中的 usage 采集应使用 getattr
        # 读取 cache_creation_input_tokens / cache_read_input_tokens 属性
        anthropic_pattern = (
            '"cache_creation_input_tokens": getattr(usage_obj, '
            '"cache_creation_input_tokens", 0)'
        )
        # 应至少出现 2 次（chat + chat_stream）
        self.assertGreaterEqual(
            source.count(anthropic_pattern),
            2,
            "AnthropicBackend 应保留对 cache_creation_input_tokens 的直接读取",
        )

    def test_openai_uses_deepseek_cache_fields(self):
        """OpenAICompatBackend 应映射 DeepSeek 扩展字段。"""
        client_path = Path(_PROJECT_ROOT) / "hermes" / "llm" / "client.py"
        source = client_path.read_text(encoding="utf-8")

        # _extract_cache_usage 应映射 prompt_cache_miss_tokens / prompt_cache_hit_tokens
        self.assertIn(
            'getattr(usage_obj, "prompt_cache_miss_tokens", 0)',
            source,
            "OpenAICompatBackend 应通过 _extract_cache_usage 读取 prompt_cache_miss_tokens",
        )
        self.assertIn(
            'getattr(usage_obj, "prompt_cache_hit_tokens", 0)',
            source,
            "OpenAICompatBackend 应通过 _extract_cache_usage 读取 prompt_cache_hit_tokens",
        )

        # OpenAICompatBackend 不应再硬编码 cache_*_input_tokens 为 0
        self.assertNotIn(
            '"cache_creation_input_tokens": 0,',
            source,
            "OpenAICompatBackend 不应再硬编码 cache_creation_input_tokens 为 0",
        )
        self.assertNotIn(
            '"cache_read_input_tokens": 0,',
            source,
            "OpenAICompatBackend 不应再硬编码 cache_read_input_tokens 为 0",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
