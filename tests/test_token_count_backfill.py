"""token_count 回填测试（批次 2.4）。

验证：
- ReactLoop.last_usage 属性存在且默认为 None
- sync_runner.run 每次 LLM 调用后累积 usage 到 self.loop.last_usage
- _extract_token_count 从 usage dict 提取 input+output token 总数
- chat_handler 非流式路径读取 last_usage 回填 token_count
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.agent.react_loop import ReactLoop  # noqa: E402


def _make_llm_response_with_usage(
    text: str = "Hello",
    stop_reason: str = "end_turn",
    usage: dict = None,
    tool_use_blocks=None,
) -> MagicMock:
    """构造带 usage 字段的 mock LLM 响应。"""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_use_blocks:
        content.extend(tool_use_blocks)
    response = MagicMock()
    response.content = content
    response.stop_reason = stop_reason
    response.usage = usage
    return response


class TestLastUsageAccumulation:
    """验证 ReactLoop.last_usage 累积逻辑。"""

    def test_last_usage_defaults_to_none(self):
        """ReactLoop 实例化后 last_usage 默认为 None。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()
        loop = ReactLoop(llm_client=mock_llm, max_loops=5)
        assert hasattr(loop, "last_usage")
        assert loop.last_usage is None

    @pytest.mark.asyncio
    async def test_run_accumulates_usage_from_single_call(self):
        """单次 LLM 调用后 last_usage 等于该次调用的 usage。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(return_value=_make_llm_response_with_usage(
            text="Hi",
            stop_reason="end_turn",
            usage={"input_tokens": 100, "output_tokens": 50},
        ))
        loop = ReactLoop(llm_client=mock_llm, max_loops=5)
        await loop.run("Hello")
        assert loop.last_usage is not None
        assert loop.last_usage["input_tokens"] == 100
        assert loop.last_usage["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_run_accumulates_usage_across_multiple_llm_calls(self):
        """多轮工具调用时，usage 累积（input + output 求和）。"""
        mock_llm = MagicMock()
        mock_tool_registry = MagicMock()
        mock_tool_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "search", "input_schema": {}},
        ]
        mock_tool_registry.execute_tool.return_value = "result"

        mock_llm.chat_main = AsyncMock(side_effect=[
            _make_llm_response_with_usage(
                text="Let me search",
                stop_reason="tool_use",
                usage={"input_tokens": 100, "output_tokens": 30},
                tool_use_blocks=[{
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "search",
                    "input": {"q": "test"},
                }],
            ),
            _make_llm_response_with_usage(
                text="Done",
                stop_reason="end_turn",
                usage={"input_tokens": 80, "output_tokens": 20},
            ),
        ])
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_tool_registry,
            max_loops=5,
        )
        await loop.run("Search for test")
        # 累积：100+80=180 input, 30+20=50 output
        assert loop.last_usage is not None
        assert loop.last_usage["input_tokens"] == 180
        assert loop.last_usage["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_run_resets_usage_at_start(self):
        """每次 run 开始时重置 last_usage，避免跨调用污染。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(return_value=_make_llm_response_with_usage(
            text="Hi",
            stop_reason="end_turn",
            usage={"input_tokens": 10, "output_tokens": 5},
        ))
        loop = ReactLoop(llm_client=mock_llm, max_loops=5)
        await loop.run("first")
        assert loop.last_usage["input_tokens"] == 10
        await loop.run("second")
        # 第二次调用后 usage 应为第二次的值，不累积第一次
        assert loop.last_usage["input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_run_handles_missing_usage(self):
        """LLM 响应无 usage 时 last_usage 保持 None，不抛异常。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(return_value=_make_llm_response_with_usage(
            text="Hi",
            stop_reason="end_turn",
            usage=None,
        ))
        loop = ReactLoop(llm_client=mock_llm, max_loops=5)
        await loop.run("Hello")
        # 无 usage 时 last_usage 为 None
        assert loop.last_usage is None


class TestExtractTokenCount:
    """验证 _extract_token_count 辅助函数。"""

    def test_extract_with_full_usage(self):
        """usage 包含 input_tokens 和 output_tokens 时返回两者之和。"""
        from teage_liu.orchestrator.chat_handler import _extract_token_count
        usage = {"input_tokens": 100, "output_tokens": 50}
        assert _extract_token_count(usage) == 150

    def test_extract_with_none(self):
        """usage 为 None 时返回 0。"""
        from teage_liu.orchestrator.chat_handler import _extract_token_count
        assert _extract_token_count(None) == 0

    def test_extract_with_empty_dict(self):
        """usage 为空 dict 时返回 0。"""
        from teage_liu.orchestrator.chat_handler import _extract_token_count
        assert _extract_token_count({}) == 0

    def test_extract_with_missing_fields(self):
        """usage 缺少某个字段时用 0 兜底。"""
        from teage_liu.orchestrator.chat_handler import _extract_token_count
        assert _extract_token_count({"input_tokens": 100}) == 100
        assert _extract_token_count({"output_tokens": 50}) == 50

    def test_extract_with_non_dict_input(self):
        """usage 不是 dict 时返回 0（容错）。"""
        from teage_liu.orchestrator.chat_handler import _extract_token_count
        assert _extract_token_count("not a dict") == 0
        assert _extract_token_count(123) == 0
