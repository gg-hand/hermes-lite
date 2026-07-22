"""LLM 推理模式（Reasoning/Thinking Mode）Provider Profile 抽象层。

每个 Provider 一个 Profile 子类，封装该 Provider 的推理模式适配规则：
- 请求参数构造（build_request_kwargs）
- 流式增量提取（extract_delta）
- thinking content block 构造（build_thinking_block）
- 跨 provider 历史转换（adapt_messages_for_provider）

新增 provider 只需新增 Profile 子类并注册到 REASONING_PROFILES，核心流程零改动。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ReasoningConfig:
    """Reasoning 配置（请求级透传，禁止实例级状态，保证 backend 层并发安全）。

    通过 chat_stream(reasoning_cfg=...) 参数透传，不存放在 backend 实例上。
    override > config > profile.default_enabled 优先级链由 LLMClient 层解析，
    到达 backend 时 enabled 已是最终值。
    """

    enabled: bool = False
    effort: str = "medium"  # low / medium / high
    budget_tokens: Optional[int] = None
    preserve_history: bool = True
    display: bool = True
    persist_thinking: bool = False  # 默认隐私保护优先
    interleave: bool = False


class ReasoningProfile(ABC):
    """Provider 推理模式适配抽象基类。

    8 个字段描述 provider 静态特性，4 个抽象方法封装 provider 特定逻辑。
    子类通过类属性覆盖字段，实现抽象方法。
    """

    provider_id: str = ""
    # delta 对象上的字段名（reasoning_content / thinking）
    streaming_field: str = ""
    # 历史保留策略：required_on_tool / required_with_signature / optional / never
    history_policy: str = "never"
    default_enabled: bool = False
    # 采样参数策略：strip_all / force_default / force_glm / strip_temperature_only / preserve
    sampling_strategy: str = "preserve"
    max_tokens_field: str = "max_tokens"
    interleave_supported: bool = False
    # reasoning_tokens 是否为 output_tokens 的子集（默认 True，Anthropic/OpenAI o3）
    reasoning_tokens_included_in_output: bool = True

    @abstractmethod
    def build_request_kwargs(
        self,
        reasoning_cfg: Optional[ReasoningConfig],
        base_kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """构造 provider 特定的请求参数。

        按 reasoning_cfg.enabled/effort/budget_tokens 修改 base_kwargs，
        按 sampling_strategy 处理采样参数。返回修改后的 kwargs（可原地修改）。
        """

    @abstractmethod
    def extract_delta(self, delta: Any) -> Optional[str]:
        """从流式 delta 提取 reasoning 增量文本，无 reasoning 增量返回 None。"""

    @abstractmethod
    def build_thinking_block(
        self,
        text: str,
        signature: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """构造 provider 特定的 thinking content block。

        返回 None 表示该 provider 不使用 content block 形式
        （如 DeepSeek 用 reasoning_content 字段而非 block）。
        """

    @abstractmethod
    def adapt_messages_for_provider(
        self,
        messages: List[Dict[str, Any]],
        preserve_history: bool,
    ) -> List[Dict[str, Any]]:
        """跨 provider 历史转换：将 thinking block 转换为目标 provider 格式。

        按 history_policy 与 preserve_history 决定 thinking block 的
        保留/转换/丢弃策略。返回新列表（不修改入参）。
        """

    def apply_sampling_strategy(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """按 sampling_strategy 处理采样参数（具体方法，子类继承使用）。"""
        strategy = self.sampling_strategy
        if strategy == "strip_all":
            kwargs.pop("temperature", None)
            kwargs.pop("top_p", None)
            kwargs.pop("top_k", None)
        elif strategy == "strip_temperature_only":
            kwargs.pop("temperature", None)
        # force_default / force_glm / preserve: 不特殊处理
        return kwargs


class DeepSeekProfile(ReasoningProfile):
    """DeepSeek-V4 推理模式 Profile。

    - reasoning 默认开启（default_enabled=True）
    - 流式增量字段：delta.reasoning_content
    - history_policy=required_on_tool：含 tool_use 时转 reasoning_content 字段
    - sampling_strategy=strip_all：reasoning 模式下移除所有采样参数
    - reasoning_content 完整保留不截断（思考是 LLM 主动思维）
    """

    provider_id = "deepseek"
    streaming_field = "reasoning_content"
    history_policy = "required_on_tool"
    default_enabled = True
    sampling_strategy = "strip_all"
    max_tokens_field = "max_tokens"
    interleave_supported = False
    reasoning_tokens_included_in_output = True

    def build_request_kwargs(self, reasoning_cfg, base_kwargs):
        kwargs = self.apply_sampling_strategy(dict(base_kwargs))
        if reasoning_cfg is None or not reasoning_cfg.enabled:
            # 显式关闭：DeepSeek 默认开启 reasoning，需注入 thinking:disabled
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["thinking"] = {"type": "disabled"}
            kwargs["extra_body"] = extra_body
        # enabled=True 时无需特殊参数，DeepSeek 默认开启 reasoning
        return kwargs

    def extract_delta(self, delta):
        return getattr(delta, "reasoning_content", None)

    def build_thinking_block(self, text, signature=None):
        # DeepSeek 用 reasoning_content 字段，不用 content block 形式
        return None

    def adapt_messages_for_provider(self, messages, preserve_history):
        # required_on_tool：含 tool_use 的 assistant 消息保留 reasoning_content 字段
        # 不受 preserve_history 影响（required_* 策略优先级高于 preserve_history）
        result: List[Dict[str, Any]] = []
        for msg in messages:
            new_msg = dict(msg)
            content = new_msg.get("content")
            if not isinstance(content, list):
                result.append(new_msg)
                continue

            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )
            thinking_blocks = [
                b for b in content
                if isinstance(b, dict) and b.get("type") == "thinking"
            ]

            if not thinking_blocks:
                result.append(new_msg)
                continue

            # 提取 thinking 文本并过滤 thinking block
            reasoning_text = "".join(
                b.get("thinking", "") for b in thinking_blocks
            )
            new_content = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking")
            ]

            if has_tool_use and reasoning_text:
                # required_on_tool：含 tool_use 时转 reasoning_content 字段（不截断）
                new_msg["content"] = new_content
                new_msg["reasoning_content"] = reasoning_text
            else:
                # 无 tool_use：清理 thinking（避免历史膨胀）
                new_msg["content"] = new_content
            result.append(new_msg)
        return result


class OpenAIProfile(ReasoningProfile):
    """OpenAI o3 推理模式 Profile。

    - reasoning 默认关闭（default_enabled=False）
    - 通过 reasoning_effort 顶层参数控制
    - history_policy=never：不持久化 thinking 到历史
    - max_tokens_field=max_completion_tokens
    """

    provider_id = "openai"
    streaming_field = "reasoning_content"
    history_policy = "never"
    default_enabled = False
    sampling_strategy = "preserve"
    max_tokens_field = "max_completion_tokens"
    interleave_supported = False
    reasoning_tokens_included_in_output = True

    def build_request_kwargs(self, reasoning_cfg, base_kwargs):
        kwargs = dict(base_kwargs)
        if reasoning_cfg is not None and reasoning_cfg.enabled:
            kwargs["reasoning_effort"] = reasoning_cfg.effort
        # enabled=False 时无需特殊处理（OpenAI 默认不开启 reasoning）
        return kwargs

    def extract_delta(self, delta):
        return getattr(delta, "reasoning_content", None)

    def build_thinking_block(self, text, signature=None):
        # OpenAI o3 不持久化 thinking 到 content blocks
        return None

    def adapt_messages_for_provider(self, messages, preserve_history):
        # history_policy=never：始终丢弃所有 thinking block / reasoning_content
        # preserve_history 不影响 never 策略
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


class AnthropicProfile(ReasoningProfile):
    """Anthropic Claude extended thinking Profile。

    - thinking 参数：{"type":"enabled","budget_tokens":N} 或 {"type":"adaptive"}
    - history_policy=required_with_signature：原样保留 thinking block（含 signature）
    - 支持 interleaved thinking
    - signature 完整性校验，半截 thinking block（无 signature）丢弃
    """

    provider_id = "anthropic"
    streaming_field = "thinking"
    history_policy = "required_with_signature"
    default_enabled = False
    sampling_strategy = "preserve"
    max_tokens_field = "max_tokens"
    interleave_supported = True
    reasoning_tokens_included_in_output = True

    def build_request_kwargs(self, reasoning_cfg, base_kwargs):
        kwargs = dict(base_kwargs)
        if reasoning_cfg is not None and reasoning_cfg.enabled:
            if reasoning_cfg.budget_tokens:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": reasoning_cfg.budget_tokens,
                }
                # max_tokens > budget_tokens 校验（不足时自动调整）
                max_tokens = kwargs.get("max_tokens") or 0
                if max_tokens and max_tokens <= reasoning_cfg.budget_tokens:
                    kwargs["max_tokens"] = reasoning_cfg.budget_tokens + 4096
            else:
                # adaptive 模式（无 budget_tokens，跳过 max_tokens 校验）
                kwargs["thinking"] = {"type": "adaptive"}
        # enabled=False 时不传 thinking 参数（Anthropic 默认不开启）
        return kwargs

    def extract_delta(self, delta):
        # Anthropic thinking_delta 事件的 delta.thinking 字段
        return getattr(delta, "thinking", None)

    def build_thinking_block(self, text, signature=None):
        if not text:
            return None
        block: Dict[str, Any] = {"type": "thinking", "thinking": text}
        if signature:
            block["signature"] = signature
        return block

    def adapt_messages_for_provider(self, messages, preserve_history):
        # required_with_signature：原样保留 thinking block（含 signature）
        # preserve_history 不影响 required_with_signature 策略
        result: List[Dict[str, Any]] = []
        for msg in messages:
            new_msg = dict(msg)
            content = new_msg.get("content")

            if not isinstance(content, list):
                # 可能是 reasoning_content 字段（来自 DeepSeek 历史）
                reasoning = new_msg.pop("reasoning_content", None)
                if reasoning and preserve_history:
                    # 无 signature 无法构造完整 thinking block（Anthropic 会 400）
                    # 转为 text block 保留语义
                    new_msg["content"] = [{"type": "text", "text": reasoning}]
                else:
                    new_msg["content"] = content or []
                result.append(new_msg)
                continue

            # content 是 list
            has_thinking = any(
                isinstance(b, dict) and b.get("type") == "thinking"
                for b in content
            )
            if has_thinking:
                # required_with_signature：原样保留 thinking block（含 signature）
                new_msg.pop("reasoning_content", None)
                result.append(new_msg)
            else:
                # 检查 reasoning_content 字段（来自 DeepSeek 历史）
                reasoning = new_msg.pop("reasoning_content", None)
                if reasoning and preserve_history:
                    # 无 signature 转为 text block
                    new_msg["content"] = list(content) + [
                        {"type": "text", "text": reasoning}
                    ]
                result.append(new_msg)
        return result


REASONING_PROFILES: Dict[str, ReasoningProfile] = {
    "deepseek": DeepSeekProfile(),
    "openai": OpenAIProfile(),
    "anthropic": AnthropicProfile(),
}
