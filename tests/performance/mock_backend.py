"""Mock LLM Backend for performance benchmarking.

Provides deterministic, configurable mock responses for the Teage Liu LLM
backend interface, eliminating network variability from profiling measurements.
"""

from __future__ import annotations

import threading
import time
import logging
from typing import Any, Dict, List, Optional, Generator

logger = logging.getLogger(__name__)


class LLMResponse:
    """Mirrors the real LLMResponse from llm/client.py."""

    def __init__(
        self,
        content: List[Dict[str, Any]],
        stop_reason: str,
        usage: Optional[Dict[str, Any]] = None,
        raw: Optional[Any] = None,
        reasoning_content: Optional[str] = None,
        thinking_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.content: List[Dict[str, Any]] = content
        self.stop_reason: str = stop_reason
        self.usage: Optional[Dict[str, Any]] = usage
        self.raw: Optional[Any] = raw
        # spec integrate-llm-reasoning-mode Task 21：reasoning 扩展字段
        # reasoning_content: DeepSeek/Qwen/GLM 风格的推理文本
        # thinking_blocks: Anthropic 风格的 thinking block 列表（含 signature）
        self.reasoning_content: Optional[str] = reasoning_content
        self.thinking_blocks: Optional[List[Dict[str, Any]]] = thinking_blocks


class MockBackend:
    """Deterministic mock LLM backend.

    Supports multiple response modes for different benchmark scenarios.
    Latency simulation is configurable to model real LLM call costs.
    """

    def __init__(
        self,
        model: str = "mock-model",
        api_key: str = "mock-key",
        base_url: Optional[str] = None,
        scenario: str = "simple_qa",
        simulated_latency_ms: float = 50.0,
        stream_chunk_interval_ms: float = 5.0,
        max_loops: int = 50,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.scenario = scenario
        self.simulated_latency_ms = simulated_latency_ms
        self.stream_chunk_interval_ms = stream_chunk_interval_ms
        self.max_loops = max_loops
        self.call_count = 0
        self._responses: List[LLMResponse] = []
        self._response_index = 0

    def add_response(self, response: LLMResponse) -> None:
        """Pre-register a specific response sequence."""
        self._responses.append(response)

    def _simulate_latency(self) -> None:
        """Simulate LLM API latency with configurable delay."""
        if self.simulated_latency_ms > 0:
            time.sleep(self.simulated_latency_ms / 1000.0)

    def _get_next_response(self) -> LLMResponse:
        """Get the next response from pre-registered list or auto-generate."""
        self.call_count += 1

        if self._response_index < len(self._responses):
            resp = self._responses[self._response_index]
            self._response_index += 1
            return resp

        return self._auto_generate_response()

    def _auto_generate_response(self) -> LLMResponse:
        """Auto-generate response based on scenario and call count."""
        if self.scenario == "simple_qa":
            # Return valid JSON that the consolidation engine can parse
            # (even though this is the main backend, consolidation engine
            # may still receive this response in some code paths)
            return LLMResponse(
                content=[{"type": "text", "text": '{"facts": [{"content": "用户发送了一条测试消息。", "type": "fact", "importance": 0.5}]}'}],
                stop_reason="end_turn",
                usage={"input_tokens": 50, "output_tokens": 10},
            )

        elif self.scenario == "tool_intensive":
            # First 3 calls: return tool_use blocks
            if self.call_count <= 3:
                return LLMResponse(
                    content=[
                        {
                            "type": "tool_use",
                            "id": f"tu_{self.call_count}_1",
                            "name": "file_read",
                            "input": {"path": "/tmp/test_file.txt"},
                        },
                        {
                            "type": "tool_use",
                            "id": f"tu_{self.call_count}_2",
                            "name": "bash_exec",
                            "input": {"command": "echo 'hello'"},
                        },
                    ],
                    stop_reason="tool_use",
                    usage={"input_tokens": 100, "output_tokens": 50},
                )
            else:
                return LLMResponse(
                    content=[{"type": "text", "text": "所有工具已完成。"}],
                    stop_reason="end_turn",
                    usage={"input_tokens": 150, "output_tokens": 15},
                )

        elif self.scenario == "consolidation":
            # Return a consolidation-style response with extracted facts
            return LLMResponse(
                content=[{
                    "type": "text",
                    "text": (
                        '{"facts": ['
                        '{"content": "用户是一名软件工程师。", "type": "user_profile", "importance": 0.8},'
                        '{"content": "用户使用Python进行开发工作。", "type": "user_profile", "importance": 0.7},'
                        '{"content": "用户偏好使用VS Code编辑器。", "type": "preference", "importance": 0.6}'
                        "]}"
                    ),
                }],
                stop_reason="end_turn",
                usage={"input_tokens": 500, "output_tokens": 80},
            )

        elif self.scenario == "max_loops":
            # Keep returning tool_use to trigger max_loops exhaustion
            return LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": f"tu_loop_{self.call_count}",
                        "name": "file_read",
                        "input": {"path": "/tmp/loop_test.txt"},
                    },
                ],
                stop_reason="tool_use",
                usage={"input_tokens": 100, "output_tokens": 30},
            )

        elif self.scenario == "reasoning":
            # spec Task 21.1/21.2：reasoning 事件流 + signature 模拟
            return LLMResponse(
                content=[{"type": "text", "text": "这是经过深度思考后的回答。"}],
                stop_reason="end_turn",
                usage={"input_tokens": 50, "output_tokens": 20, "reasoning_tokens": 100},
                reasoning_content="让我分析一下这个问题...\n首先考虑...\n然后...\n最终结论是...",
                thinking_blocks=[
                    {
                        "type": "thinking",
                        "thinking": "让我分析一下这个问题...",
                        "signature": "mock_sig_" + str(self.call_count),
                    }
                ],
            )

        elif self.scenario == "interleaved_thinking":
            # spec Task 21.3：interleaved thinking 多 block
            return LLMResponse(
                content=[
                    {"type": "thinking", "thinking": "第一步思考...", "signature": "sig_a"},
                    {"type": "text", "text": "中间文本。"},
                    {"type": "thinking", "thinking": "第二步思考...", "signature": "sig_b"},
                    {"type": "text", "text": "最终回答。"},
                ],
                stop_reason="end_turn",
                usage={"input_tokens": 80, "output_tokens": 40, "reasoning_tokens": 150},
            )

        else:
            return LLMResponse(
                content=[{"type": "text", "text": "Default mock response."}],
                stop_reason="end_turn",
            )

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        reasoning_cfg: Optional[Any] = None,
    ) -> LLMResponse:
        """Synchronous mock chat."""
        self._simulate_latency()
        return self._get_next_response()

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        activity_timeout: float = 60.0,
        stream_manager: Optional[Any] = None,
        session_id: Optional[str] = None,
        reasoning_cfg: Optional[Any] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Streaming mock chat - yields reasoning + text chunks then done event.

        spec Task 21.1：支持注入 reasoning 事件流（在 text 事件之前 yield）。
        spec Task 21.2：thinking block 含 signature（Anthropic 模拟）。
        spec Task 21.3：interleaved thinking 按 content 顺序 yield。
        """
        response = self._get_next_response()

        # Yield reasoning events first（DeepSeek 风格 reasoning_content）
        if response.reasoning_content:
            reasoning_text = response.reasoning_content
            chunk_size = max(1, len(reasoning_text) // 4)
            for i in range(0, len(reasoning_text), chunk_size):
                if self.stream_chunk_interval_ms > 0:
                    time.sleep(self.stream_chunk_interval_ms / 1000.0)
                yield {"type": "reasoning", "text": reasoning_text[i : i + chunk_size], "signature": None}

        # Yield text/thinking chunks for content blocks（含 interleaved thinking）
        for block in response.content:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                chunk_size = max(1, len(text) // 3)
                for i in range(0, len(text), chunk_size):
                    if self.stream_chunk_interval_ms > 0:
                        time.sleep(self.stream_chunk_interval_ms / 1000.0)
                    yield {"type": "text", "text": text[i : i + chunk_size]}
            elif btype == "thinking":
                # Anthropic 风格 thinking delta（含 signature 在 done 事件的 content_blocks 中）
                thinking_text = block.get("thinking", "")
                if thinking_text:
                    chunk_size = max(1, len(thinking_text) // 3)
                    for i in range(0, len(thinking_text), chunk_size):
                        if self.stream_chunk_interval_ms > 0:
                            time.sleep(self.stream_chunk_interval_ms / 1000.0)
                        yield {"type": "reasoning", "text": thinking_text[i : i + chunk_size], "signature": None}

        # Yield done event
        yield {
            "type": "done",
            "stop_reason": response.stop_reason,
            "content_blocks": response.content,
            "usage": response.usage,
        }


class MockLLMClient:
    """Mock LLMClient that orchestrates main + consolidation mock backends.

    Mirrors the interface of llm.client.LLMClient for drop-in replacement
    during performance testing.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        metrics_collector: Optional[Any] = None,
        **backend_kwargs,
    ):
        self.metrics_collector = metrics_collector
        self.max_context_tokens = 200000
        self.context_threshold = 0.8
        self.main_model = backend_kwargs.pop("main_model", "mock-main")
        self.consolidation_model = backend_kwargs.pop("consolidation_model", "mock-consolidation")

        scenario = backend_kwargs.pop("scenario", "simple_qa")
        latency = backend_kwargs.pop("simulated_latency_ms", 50.0)

        self._main = MockBackend(
            model=self.main_model,
            api_key="mock-key",
            scenario=scenario,
            simulated_latency_ms=latency,
            **backend_kwargs,
        )
        self._consolidation = MockBackend(
            model=self.consolidation_model,
            api_key="mock-key",
            scenario="consolidation",
            simulated_latency_ms=latency * 2,  # consolidation typically slower
            **backend_kwargs,
        )

    @property
    def main_backend(self) -> MockBackend:
        return self._main

    def chat_main(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        reasoning_cfg: Optional[Any] = None,
        is_cron: bool = False,
    ) -> LLMResponse:
        return self._main.chat(messages, tools, system, max_tokens, reasoning_cfg=reasoning_cfg)

    def chat_main_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        session_id: Optional[str] = None,
        reasoning_cfg: Optional[Any] = None,
        is_cron: bool = False,
    ) -> Generator[Dict[str, Any], None, None]:
        yield from self._main.chat_stream(
            messages, tools, system, max_tokens,
            cancel_event=cancel_event,
            session_id=session_id,
            reasoning_cfg=reasoning_cfg,
        )

    def chat_consolidation(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        return self._consolidation.chat(messages, system=system, max_tokens=max_tokens)

    def count_tokens(self, text: str) -> int:
        """Rough token count estimate (matches production fallback)."""
        return len(text) // 3

    def count_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Estimate tokens for a list of messages."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(block.get("text", "") or str(block)) // 3
                    elif isinstance(block, str):
                        total += len(block) // 3
            elif isinstance(content, str):
                total += len(content) // 3
            total += 10  # overhead per message
        return total
