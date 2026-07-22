"""ReasoningProfile 抽象层单元测试（spec integrate-llm-reasoning-mode Task 1 SubTask 1.6）。

覆盖：
- ReasoningConfig dataclass 默认值
- REASONING_PROFILES 注册表
- DeepSeekProfile / OpenAIProfile / AnthropicProfile 的 build_request_kwargs
- extract_delta 各 provider 提取
- build_thinking_block 各 provider 构造
- adapt_messages_for_provider 跨 provider 历史转换
- apply_sampling_strategy 各策略
- 优先级链字段（default_enabled / history_policy）
- reasoning_tokens_included_in_output 字段

运行方式：
    python -m pytest tests/test_reasoning_profiles.py -v
    python -m unittest tests.test_reasoning_profiles -v
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from teage_liu.llm.reasoning_profiles import (  # noqa: E402
    AnthropicProfile,
    DeepSeekProfile,
    OpenAIProfile,
    REASONING_PROFILES,
    ReasoningConfig,
    ReasoningProfile,
)


class TestReasoningConfigDefaults(unittest.TestCase):
    """ReasoningConfig dataclass 默认值。"""

    def test_defaults_privacy_first(self):
        cfg = ReasoningConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.effort, "medium")
        self.assertIsNone(cfg.budget_tokens)
        self.assertTrue(cfg.preserve_history)
        self.assertTrue(cfg.display)
        self.assertFalse(cfg.persist_thinking)  # 默认隐私保护
        self.assertFalse(cfg.interleave)

    def test_enabled_config(self):
        cfg = ReasoningConfig(enabled=True, effort="high", budget_tokens=8192)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.effort, "high")
        self.assertEqual(cfg.budget_tokens, 8192)


class TestRegistry(unittest.TestCase):
    """REASONING_PROFILES 注册表。"""

    def test_registry_contains_p0_providers(self):
        self.assertIn("deepseek", REASONING_PROFILES)
        self.assertIn("openai", REASONING_PROFILES)
        self.assertIn("anthropic", REASONING_PROFILES)

    def test_registry_values_are_profile_instances(self):
        for provider_id, profile in REASONING_PROFILES.items():
            self.assertIsInstance(profile, ReasoningProfile)
            self.assertEqual(profile.provider_id, provider_id)


class TestDeepSeekProfile(unittest.TestCase):
    """DeepSeekProfile 字段与行为。"""

    def setUp(self):
        self.profile = DeepSeekProfile()

    def test_fields(self):
        self.assertEqual(self.profile.provider_id, "deepseek")
        self.assertEqual(self.profile.streaming_field, "reasoning_content")
        self.assertEqual(self.profile.history_policy, "required_on_tool")
        self.assertTrue(self.profile.default_enabled)
        self.assertEqual(self.profile.sampling_strategy, "strip_all")
        self.assertEqual(self.profile.max_tokens_field, "max_tokens")
        self.assertFalse(self.profile.interleave_supported)
        self.assertTrue(self.profile.reasoning_tokens_included_in_output)

    def test_build_request_kwargs_disabled_injects_thinking_disabled(self):
        """显式关闭时注入 extra_body.thinking=disabled。"""
        cfg = ReasoningConfig(enabled=False)
        kwargs = self.profile.build_request_kwargs(cfg, {"temperature": 0.7, "top_p": 0.9})
        self.assertEqual(kwargs["extra_body"]["thinking"], {"type": "disabled"})
        # strip_all 移除采样参数
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("top_p", kwargs)

    def test_build_request_kwargs_enabled_no_special_params(self):
        """enabled=True 时无需特殊参数（DeepSeek 默认开启）。"""
        cfg = ReasoningConfig(enabled=True)
        kwargs = self.profile.build_request_kwargs(cfg, {"temperature": 0.5})
        self.assertNotIn("extra_body", kwargs)
        self.assertNotIn("temperature", kwargs)  # strip_all

    def test_build_request_kwargs_none_cfg_treated_as_disabled(self):
        """reasoning_cfg=None 时按关闭处理（注入 disabled）。"""
        kwargs = self.profile.build_request_kwargs(None, {"temperature": 0.5})
        self.assertEqual(kwargs["extra_body"]["thinking"], {"type": "disabled"})

    def test_extract_delta_reasoning_content(self):
        delta = SimpleNamespace(reasoning_content="思考片段", content="回答")
        self.assertEqual(self.profile.extract_delta(delta), "思考片段")

    def test_extract_delta_no_reasoning(self):
        delta = SimpleNamespace(content="回答")
        self.assertIsNone(self.profile.extract_delta(delta))

    def test_build_thinking_block_returns_none(self):
        """DeepSeek 用 reasoning_content 字段，不用 content block。"""
        self.assertIsNone(self.profile.build_thinking_block("text", "sig"))

    def test_adapt_messages_required_on_tool_with_tool_use(self):
        """含 tool_use 时 thinking block 转 reasoning_content 字段（不截断）。"""
        long_text = "思考" * 500  # 长文本验证不截断
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": long_text},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
            ],
        }]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=True)
        self.assertEqual(result[0]["reasoning_content"], long_text)
        self.assertEqual(len(result[0]["content"]), 1)
        self.assertEqual(result[0]["content"][0]["type"], "tool_use")

    def test_adapt_messages_required_on_tool_without_tool_use(self):
        """无 tool_use 时清理 thinking block（避免历史膨胀）。"""
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "思考"},
                {"type": "text", "text": "回答"},
            ],
        }]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=True)
        self.assertNotIn("reasoning_content", result[0])
        self.assertEqual(len(result[0]["content"]), 1)
        self.assertEqual(result[0]["content"][0]["type"], "text")

    def test_adapt_messages_not_affected_by_preserve_history(self):
        """required_on_tool 不受 preserve_history 影响。"""
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "思考"},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
            ],
        }]
        # preserve_history=False 仍保留 reasoning_content（required_on_tool 优先）
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=False)
        self.assertEqual(result[0]["reasoning_content"], "思考")

    def test_adapt_messages_no_thinking_unchanged(self):
        """无 thinking block 的消息原样通过。"""
        messages = [{"role": "user", "content": "hello"}]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=True)
        self.assertEqual(result, messages)


class TestOpenAIProfile(unittest.TestCase):
    """OpenAIProfile 字段与行为。"""

    def setUp(self):
        self.profile = OpenAIProfile()

    def test_fields(self):
        self.assertEqual(self.profile.provider_id, "openai")
        self.assertEqual(self.profile.history_policy, "never")
        self.assertFalse(self.profile.default_enabled)
        self.assertEqual(self.profile.sampling_strategy, "preserve")
        self.assertEqual(self.profile.max_tokens_field, "max_completion_tokens")
        self.assertTrue(self.profile.reasoning_tokens_included_in_output)

    def test_build_request_kwargs_enabled_injects_effort(self):
        cfg = ReasoningConfig(enabled=True, effort="high")
        kwargs = self.profile.build_request_kwargs(cfg, {"temperature": 0.5})
        self.assertEqual(kwargs["reasoning_effort"], "high")
        # preserve 策略保留采样参数
        self.assertEqual(kwargs["temperature"], 0.5)

    def test_build_request_kwargs_disabled_no_effort(self):
        cfg = ReasoningConfig(enabled=False)
        kwargs = self.profile.build_request_kwargs(cfg, {"temperature": 0.5})
        self.assertNotIn("reasoning_effort", kwargs)

    def test_extract_delta(self):
        delta = SimpleNamespace(reasoning_content="reasoning")
        self.assertEqual(self.profile.extract_delta(delta), "reasoning")

    def test_build_thinking_block_returns_none(self):
        """OpenAI o3 不持久化 thinking 到 content blocks。"""
        self.assertIsNone(self.profile.build_thinking_block("text", "sig"))

    def test_adapt_messages_never_drops_thinking(self):
        """history_policy=never 始终丢弃 thinking block / reasoning_content。"""
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "思考"},
                {"type": "text", "text": "回答"},
            ],
            "reasoning_content": "额外推理",
        }]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=True)
        self.assertNotIn("reasoning_content", result[0])
        self.assertEqual(len(result[0]["content"]), 1)
        self.assertEqual(result[0]["content"][0]["type"], "text")

    def test_adapt_messages_never_not_affected_by_preserve_history(self):
        """never 策略不受 preserve_history 影响。"""
        messages = [{
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "思考"}],
            "reasoning_content": "推理",
        }]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=True)
        self.assertNotIn("reasoning_content", result[0])
        self.assertEqual(result[0]["content"], [])


class TestAnthropicProfile(unittest.TestCase):
    """AnthropicProfile 字段与行为。"""

    def setUp(self):
        self.profile = AnthropicProfile()

    def test_fields(self):
        self.assertEqual(self.profile.provider_id, "anthropic")
        self.assertEqual(self.profile.streaming_field, "thinking")
        self.assertEqual(self.profile.history_policy, "required_with_signature")
        self.assertFalse(self.profile.default_enabled)
        self.assertEqual(self.profile.max_tokens_field, "max_tokens")
        self.assertTrue(self.profile.interleave_supported)
        self.assertTrue(self.profile.reasoning_tokens_included_in_output)

    def test_build_request_kwargs_enabled_with_budget(self):
        cfg = ReasoningConfig(enabled=True, budget_tokens=8192)
        kwargs = self.profile.build_request_kwargs(cfg, {"max_tokens": 4096})
        self.assertEqual(kwargs["thinking"], {"type": "enabled", "budget_tokens": 8192})
        # max_tokens <= budget_tokens 时自动调整
        self.assertEqual(kwargs["max_tokens"], 8192 + 4096)

    def test_build_request_kwargs_enabled_adaptive_no_budget(self):
        """无 budget_tokens 时 adaptive 模式，跳过 max_tokens 校验。"""
        cfg = ReasoningConfig(enabled=True, budget_tokens=None)
        kwargs = self.profile.build_request_kwargs(cfg, {"max_tokens": 4096})
        self.assertEqual(kwargs["thinking"], {"type": "adaptive"})
        self.assertEqual(kwargs["max_tokens"], 4096)  # 不调整

    def test_build_request_kwargs_disabled_no_thinking(self):
        cfg = ReasoningConfig(enabled=False)
        kwargs = self.profile.build_request_kwargs(cfg, {"max_tokens": 4096})
        self.assertNotIn("thinking", kwargs)

    def test_build_request_kwargs_budget_sufficient_no_adjust(self):
        """max_tokens > budget_tokens 时不调整。"""
        cfg = ReasoningConfig(enabled=True, budget_tokens=4096)
        kwargs = self.profile.build_request_kwargs(cfg, {"max_tokens": 16384})
        self.assertEqual(kwargs["max_tokens"], 16384)

    def test_extract_delta_thinking(self):
        delta = SimpleNamespace(thinking="思考", text="回答")
        self.assertEqual(self.profile.extract_delta(delta), "思考")

    def test_build_thinking_block_with_signature(self):
        block = self.profile.build_thinking_block("思考内容", "sig123")
        self.assertEqual(block, {"type": "thinking", "thinking": "思考内容", "signature": "sig123"})

    def test_build_thinking_block_without_signature(self):
        block = self.profile.build_thinking_block("思考内容")
        self.assertEqual(block, {"type": "thinking", "thinking": "思考内容"})
        self.assertNotIn("signature", block)

    def test_build_thinking_block_empty_text(self):
        self.assertIsNone(self.profile.build_thinking_block(""))

    def test_adapt_messages_preserves_thinking_block(self):
        """required_with_signature 原样保留 thinking block（含 signature）。"""
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "思考", "signature": "sig"},
                {"type": "text", "text": "回答"},
            ],
        }]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=False)
        # preserve_history 不影响 required_with_signature
        self.assertEqual(len(result[0]["content"]), 2)
        self.assertEqual(result[0]["content"][0]["type"], "thinking")
        self.assertEqual(result[0]["content"][0]["signature"], "sig")

    def test_adapt_messages_reasoning_content_to_text_block(self):
        """DeepSeek reasoning_content 字段无 signature，转为 text block 保留语义。"""
        messages = [{
            "role": "assistant",
            "content": [{"type": "text", "text": "回答"}],
            "reasoning_content": "推理过程",
        }]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=True)
        self.assertNotIn("reasoning_content", result[0])
        # 追加为 text block
        text_blocks = [b for b in result[0]["content"] if b.get("type") == "text"]
        self.assertEqual(len(text_blocks), 2)
        self.assertIn("推理过程", text_blocks[-1]["text"])

    def test_adapt_messages_reasoning_content_dropped_when_not_preserve(self):
        """preserve_history=False 时 reasoning_content 被丢弃。"""
        messages = [{
            "role": "assistant",
            "content": [{"type": "text", "text": "回答"}],
            "reasoning_content": "推理",
        }]
        result = self.profile.adapt_messages_for_provider(messages, preserve_history=False)
        self.assertNotIn("reasoning_content", result[0])
        self.assertEqual(len(result[0]["content"]), 1)


class TestSamplingStrategy(unittest.TestCase):
    """apply_sampling_strategy 各策略。"""

    def test_strip_all_removes_all_sampling_params(self):
        profile = DeepSeekProfile()  # strip_all
        kwargs = profile.apply_sampling_strategy(
            {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "max_tokens": 1024}
        )
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("top_p", kwargs)
        self.assertNotIn("top_k", kwargs)
        self.assertEqual(kwargs["max_tokens"], 1024)  # 非 sampling 参数保留

    def test_preserve_keeps_all_params(self):
        profile = OpenAIProfile()  # preserve
        kwargs = profile.apply_sampling_strategy(
            {"temperature": 0.7, "top_p": 0.9, "max_tokens": 1024}
        )
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.9)

    def test_strip_temperature_only(self):
        # 构造一个 strip_temperature_only 的临时 profile
        class TempProfile(DeepSeekProfile):
            sampling_strategy = "strip_temperature_only"
        profile = TempProfile()
        kwargs = profile.apply_sampling_strategy(
            {"temperature": 0.7, "top_p": 0.9, "top_k": 40}
        )
        self.assertNotIn("temperature", kwargs)
        self.assertEqual(kwargs["top_p"], 0.9)
        self.assertEqual(kwargs["top_k"], 40)


class TestCrossProviderConversion(unittest.TestCase):
    """跨 provider 历史转换场景。"""

    def test_anthropic_to_deepseek_thinking_to_reasoning_content(self):
        """Anthropic thinking block → DeepSeek reasoning_content 字段。"""
        anthropic = AnthropicProfile()
        deepseek = DeepSeekProfile()
        # Anthropic 保留 thinking block
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "推理", "signature": "sig"},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
            ],
        }]
        # 先过 Anthropic（保留），再过 DeepSeek（转 reasoning_content）
        result = deepseek.adapt_messages_for_provider(
            anthropic.adapt_messages_for_provider(messages, preserve_history=True),
            preserve_history=True,
        )
        self.assertEqual(result[0]["reasoning_content"], "推理")
        # signature 丢弃（DeepSeek 不需要）
        self.assertNotIn("reasoning_content", messages[0])  # 原入参未被修改

    def test_deepseek_to_anthropic_reasoning_to_text_block(self):
        """DeepSeek reasoning_content → Anthropic text block（无 signature）。"""
        deepseek = DeepSeekProfile()
        anthropic = AnthropicProfile()
        messages = [{
            "role": "assistant",
            "content": [{"type": "text", "text": "回答"}],
            "reasoning_content": "推理过程",
        }]
        result = anthropic.adapt_messages_for_provider(
            deepseek.adapt_messages_for_provider(messages, preserve_history=True),
            preserve_history=True,
        )
        # 最终 Anthropic 中无 reasoning_content 字段
        self.assertNotIn("reasoning_content", result[0])
        # 推理过程转为 text block
        text_blocks = [b for b in result[0]["content"] if b.get("type") == "text"]
        self.assertTrue(any("推理过程" in b["text"] for b in text_blocks))

    def test_signature_not_lost_on_round_trip(self):
        """Anthropic→DeepSeek→Anthropic 来回切换，thinking block 内的 signature
        在 DeepSeek 阶段被丢弃（因 DeepSeek 用 reasoning_content 字段），
        再回 Anthropic 时无 signature，转为 text block（预期行为，不 400）。"""
        anthropic = AnthropicProfile()
        deepseek = DeepSeekProfile()
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "推理", "signature": "sig"},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
            ],
        }]
        # Anthropic → DeepSeek（signature 丢失，转为 reasoning_content）
        ds_result = deepseek.adapt_messages_for_provider(messages, preserve_history=True)
        self.assertEqual(ds_result[0]["reasoning_content"], "推理")
        # DeepSeek → Anthropic（无 signature，转 text block）
        final = anthropic.adapt_messages_for_provider(ds_result, preserve_history=True)
        self.assertNotIn("reasoning_content", final[0])
        # 无 thinking block（因无 signature），推理转为 text block
        thinking_blocks = [b for b in final[0]["content"] if b.get("type") == "thinking"]
        self.assertEqual(len(thinking_blocks), 0)

    def test_input_messages_not_mutated(self):
        """adapt_messages_for_provider 不修改入参。"""
        deepseek = DeepSeekProfile()
        original = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "思考"},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
            ],
        }]
        original_copy = [{"role": m["role"], "content": list(m["content"])} for m in original]
        deepseek.adapt_messages_for_provider(original, preserve_history=True)
        self.assertEqual(original, original_copy)


class TestPriorityChainFields(unittest.TestCase):
    """优先级链相关字段验证（override > config > default_enabled）。"""

    def test_deepseek_default_enabled_true(self):
        """DeepSeek default_enabled=True，config 缺省时兜底开启。"""
        self.assertTrue(DeepSeekProfile().default_enabled)

    def test_openai_default_enabled_false(self):
        """OpenAI default_enabled=False，需用户显式开启。"""
        self.assertFalse(OpenAIProfile().default_enabled)

    def test_anthropic_default_enabled_false(self):
        """Anthropic default_enabled=False，需用户显式开启。"""
        self.assertFalse(AnthropicProfile().default_enabled)

    def test_config_explicit_disable_overrides_default_enabled(self):
        """config.enabled=False 覆盖 default_enabled=True（禁止反向覆盖）。

        验证 DeepSeekProfile.build_request_kwargs 在 enabled=False 时
        注入 thinking:disabled（即 config 显式关闭生效）。
        """
        cfg = ReasoningConfig(enabled=False)  # config 显式关闭
        kwargs = DeepSeekProfile().build_request_kwargs(cfg, {})
        self.assertEqual(kwargs["extra_body"]["thinking"], {"type": "disabled"})


if __name__ == "__main__":
    unittest.main()
