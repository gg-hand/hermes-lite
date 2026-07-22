"""历史压缩器（Condenser）。

在 LLM 调用前对 history 进行压缩，减少上下文 token 消耗同时保留工具调用
元信息，避免跨轮次工具调用结果完全不可见导致 LLM 重复 read_file /
http_request 等调用。

提供可插拔的接口与两种实现：
- ``MaskingCondenser``（默认策略）：将 ``keep_recent_n`` 之外的旧
  ``tool_result`` content block 替换为轻量占位符，保留 ``tool_use`` 块
  原样（含 input 关键参数），保留纯文本消息完整。
- ``LLMSummarizingCondenser``（可选策略）：masking 后 token 数仍超阈值时，
  用 consolidation LLM 对 ``keep_recent_n`` 之前的旧历史做结构化摘要，
  压缩为一条 ``[历史摘要]`` 消息。LLM 调用失败时降级为纯 masking 结果。

所有实现均为无状态纯函数式（除 LLMSummarizingCondenser 持有 llm_client
引用），输入输出均为 ``{"role": str, "content": str | list}`` 消息列表。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from ..llm.client import LLMClient

# ReasoningConfig 运行时需要使用（condense 调用 consolidation LLM 时显式禁用 reasoning）
from teage_liu.llm.reasoning_profiles import ReasoningConfig
logger = logging.getLogger(__name__)


class Condenser:
    """历史压缩器抽象基类。

    子类 SHALL 实现 :meth:`condense`，接收完整 history 消息列表，
    返回压缩后的消息列表（可为同一列表的浅拷贝或新列表）。

    实现应满足：
    - 幂等：对已压缩的 history 再次 condense 不应二次破坏（masking 占位符
      为纯文本，二次 masking 不会触发 tool_result 分支）。
    - 不修改入参列表：返回新列表，避免污染 history_buffer 内部状态。
    - ``len(messages) <= keep_recent_n`` 时原样返回。
    """

    def condense(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        raise NotImplementedError


def _extract_text(content: Any) -> str:
    """从消息 content（str 或 Anthropic content block 列表）提取纯文本。

    用于 token 粗估与归档，将 list 形式的 content blocks 拼接为字符串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if isinstance(text, str):
                        parts.append(text)
                elif btype == "tool_use":
                    name = block.get("name", "")
                    parts.append(str(name))
                elif btype == "tool_result":
                    inner = block.get("content", "")
                    parts.append(_extract_text(inner))
        return "".join(parts)
    return str(content) if content is not None else ""


def _default_token_counter(messages: List[Dict[str, Any]]) -> int:
    """基于字符数的粗估 token 计数器（token ≈ chars / 3）。"""
    total_chars = 0
    for msg in messages:
        total_chars += len(_extract_text(msg.get("content", "")))
    return total_chars // 3


class MaskingCondenser(Condenser):
    """默认压缩策略：旧 tool_result 替换为占位符，保留最近 N 条完整。

    压缩规则（仅对 ``keep_recent_n`` 之外、``keep_first`` 之外的“旧区”消息生效）：
    - ``tool_result`` content block：``content`` 替换为
      ``[tool_result: {tool_name}, 原始 {N} chars，已归档]``，并附加
      ``is_compressed: true``；``tool_use_id`` 保留以便 LLM 关联。
    - ``tool_use`` content block：原样保留（含 ``input`` 关键参数，LLM 需要
      知道之前调用了哪些工具及参数）。
    - 纯文本消息（content 为 str）：完整保留。

    ``tool_name`` 通过扫描同批 messages 中的 ``tool_use`` 块按
    ``tool_use_id`` 映射得到；映射缺失时回退为 ``unknown``。

    参数:
        keep_recent_n: 保留最近 N 条消息完整（含其所有 content blocks）。
        keep_first: 始终保留前 N 条消息完整（通常为首条 user + 首条 assistant）。
    """

    def __init__(self, keep_recent_n: int = 6, keep_first: int = 2) -> None:
        self.keep_recent_n = max(0, int(keep_recent_n))
        self.keep_first = max(0, int(keep_first))

    def condense(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """压缩 history，返回新列表（不修改入参）。

        - 消息数 ≤ ``keep_recent_n`` 时原样返回浅拷贝。
        - 否则对“旧区”消息中的 tool_result block 做 masking。
        """
        if len(messages) <= self.keep_recent_n:
            return [dict(m) for m in messages]

        tool_name_map = self._build_tool_name_map(messages)
        n = len(messages)
        recent_start = n - self.keep_recent_n
        result: List[Dict[str, Any]] = []
        for i, msg in enumerate(messages):
            # 保护区：前 keep_first 条 + 最近 keep_recent_n 条，完整保留
            if i < self.keep_first or i >= recent_start:
                result.append(dict(msg))
            else:
                result.append(self._mask_message(msg, tool_name_map))
        return result

    @staticmethod
    def _build_tool_name_map(
        messages: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """扫描所有消息，构建 ``tool_use_id -> tool_name`` 映射。"""
        name_map: Dict[str, str] = {}
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_id = block.get("id", "")
                    tool_name = block.get("name", "")
                    if tool_use_id:
                        name_map[tool_use_id] = tool_name
        return name_map

    def _mask_message(
        self, msg: Dict[str, Any], tool_name_map: Dict[str, str]
    ) -> Dict[str, Any]:
        """对单条消息做 masking：tool_result block 替换为占位符，其余原样。"""
        content = msg.get("content")
        if not isinstance(content, list):
            # 纯文本消息完整保留
            return dict(msg)
        new_blocks: List[Dict[str, Any]] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and not block.get("is_compressed")
            ):
                new_blocks.append(self._mask_tool_result(block, tool_name_map))
            else:
                # text / tool_use / 已压缩的 tool_result / 其他 block 原样保留
                # is_compressed 守卫保证幂等：二次 condense 不二次破坏占位符
                new_blocks.append(block)
        return {"role": msg.get("role"), "content": new_blocks}

    @staticmethod
    def _mask_tool_result(
        block: Dict[str, Any], tool_name_map: Dict[str, str]
    ) -> Dict[str, Any]:
        """将 tool_result block 的 content 替换为轻量占位符。"""
        tool_use_id = block.get("tool_use_id", "")
        original_content = block.get("content", "")
        orig_len = len(_extract_text(original_content))
        tool_name = tool_name_map.get(tool_use_id, "unknown")
        placeholder = (
            f"[tool_result: {tool_name}, 原始 {orig_len} chars，已归档]"
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": placeholder,
            "is_compressed": True,
        }


class LLMSummarizingCondenser(Condenser):
    """可选压缩策略：masking 后仍超 token 阈值时用 LLM 摘要旧历史。

    流程：
    1. 先调用 :class:`MaskingCondenser` 做 masking；
    2. 估算 masking 后的 token 数，≤ ``llm_summary_threshold`` 时直接返回；
    3. 超阈值时，将 ``keep_first`` 与 ``keep_recent_n`` 之间的旧消息交给
       consolidation LLM 压缩为一条 ``[历史摘要]`` 消息；
    4. 最终结构 = ``keep_first`` 条完整 + 摘要消息 + ``keep_recent_n`` 条完整。

    LLM 调用失败时降级为纯 masking 结果（不阻塞主流程），并记录 warning。

    参数:
        keep_recent_n: 保留最近 N 条消息完整（透传给 MaskingCondenser）。
        keep_first: 始终保留前 N 条完整（透传给 MaskingCondenser）。
        llm_summary_threshold: masking 后 token 数超此值才触发 LLM 摘要。
        llm_client: consolidation LLM 客户端，为 None 时无法摘要，恒降级为 masking。
        token_counter: token 计数器 ``(messages) -> int``。为 None 时用字符粗估。
    """

    def __init__(
        self,
        keep_recent_n: int = 6,
        keep_first: int = 2,
        llm_summary_threshold: int = 100000,
        llm_client: Optional["LLMClient"] = None,
        token_counter: Optional[Callable[[List[Dict[str, Any]]], int]] = None,
    ) -> None:
        self.masking = MaskingCondenser(
            keep_recent_n=keep_recent_n, keep_first=keep_first
        )
        self.llm_summary_threshold = int(llm_summary_threshold)
        self.llm_client = llm_client
        self._token_counter: Callable[[List[Dict[str, Any]]], int] = (
            token_counter or _default_token_counter
        )

    @property
    def keep_recent_n(self) -> int:
        return self.masking.keep_recent_n

    @property
    def keep_first(self) -> int:
        return self.masking.keep_first

    def condense(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """先 masking，超阈值时再 LLM 摘要旧历史。"""
        masked = self.masking.condense(messages)

        # masking 后未超阈值：直接返回
        try:
            token_count = self._token_counter(masked)
        except Exception as e:
            logger.warning("condenser token 计数失败，跳过 LLM 摘要: %s", e)
            return masked
        if token_count <= self.llm_summary_threshold:
            return masked

        # 无 LLM 客户端：无法摘要，降级返回 masking 结果
        if self.llm_client is None:
            return masked

        # 切出旧区（keep_first 与 keep_recent_n 之间）
        n = len(masked)
        keep_first = self.keep_first
        recent_start = n - self.keep_recent_n
        if recent_start <= keep_first:
            # 旧区为空，无法摘要
            return masked
        old_messages = masked[keep_first:recent_start]

        try:
            summary = self._summarize_with_llm(old_messages)
        except Exception as e:
            logger.warning("LLM 摘要失败，降级为纯 masking 结果: %s", e)
            return masked

        if not summary:
            return masked

        summary_msg = {"role": "user", "content": f"[历史摘要]\n{summary}"}
        return masked[:keep_first] + [summary_msg] + masked[recent_start:]

    def _summarize_with_llm(self, old_messages: List[Dict[str, Any]]) -> str:
        """调用 consolidation LLM 对旧历史做结构化摘要，返回摘要文本。"""
        # 将旧消息展平为可读文本喂给 LLM
        serialized = self._serialize_messages(old_messages)
        prompt = (
            "请将以下对话历史（含工具调用）压缩为结构化摘要，"
            "保留关键信息（读取的文件路径、调用的 API、得出的结论、"
            "未完成的任务），丢弃冗余细节。不超过 800 字。\n\n"
            f"{serialized}"
        )
        response = self.llm_client.chat_consolidation_sync(
            [{"role": "user", "content": prompt}],
            system="你是一个对话历史摘要助手。",
            reasoning_cfg=ReasoningConfig(enabled=False),
        )
        # 提取文本（兼容 response.content 为 dict 列表或对象列表）
        parts: List[str] = []
        content_blocks = getattr(response, "content", []) or []
        for block in content_blocks:
            block_dict = block if isinstance(block, dict) else {
                "type": getattr(block, "type", None),
                "text": getattr(block, "text", ""),
            }
            if block_dict.get("type") == "text":
                text = block_dict.get("text", "")
                if text:
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _serialize_messages(messages: List[Dict[str, Any]]) -> str:
        """将消息列表序列化为 LLM 可读的纯文本。"""
        lines: List[str] = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            text = _extract_text(msg.get("content", ""))
            # 截断超长单条，避免摘要请求本身超限
            if len(text) > 2000:
                text = text[:2000] + "...[截断]"
            lines.append(f"[{i}] {role}: {text}")
        return "\n".join(lines)


def create_condenser_from_config(
    condenser_config: Optional[Dict[str, Any]],
    llm_client: Optional["LLMClient"] = None,
    token_counter: Optional[Callable[[List[Dict[str, Any]]], int]] = None,
) -> Optional[Condenser]:
    """根据配置创建 Condenser 实例。

    参数:
        condenser_config: ``config.yaml`` 中 ``memory.condenser`` 段字典。
            为 None 或 ``enabled: false`` 时返回 None（向后兼容）。
        llm_client: consolidation LLM 客户端，仅 ``llm_summary`` 策略需要。
        token_counter: token 计数器，仅 ``llm_summary`` 策略需要。

    返回:
        Condenser 实例；禁用或策略未知时返回 None。
    """
    if not condenser_config:
        return None
    if not bool(condenser_config.get("enabled", False)):
        return None

    strategy = condenser_config.get("strategy", "masking")
    keep_recent_n = int(condenser_config.get("keep_recent_n", 6))
    keep_first = int(condenser_config.get("keep_first", 2))

    if strategy == "masking":
        return MaskingCondenser(
            keep_recent_n=keep_recent_n, keep_first=keep_first
        )
    if strategy == "llm_summary":
        threshold = int(condenser_config.get("llm_summary_threshold", 100000))
        return LLMSummarizingCondenser(
            keep_recent_n=keep_recent_n,
            keep_first=keep_first,
            llm_summary_threshold=threshold,
            llm_client=llm_client,
            token_counter=token_counter,
        )
    logger.warning("未知 condenser strategy: %s，禁用 condenser", strategy)
    return None


if __name__ == "__main__":
    # —— 简单验证逻辑 ——
    print("=== Condenser 验证 ===\n")

    # 1. MaskingCondenser: 未超阈值不压缩
    mc = MaskingCondenser(keep_recent_n=6, keep_first=2)
    small = [{"role": "user", "content": "hi"}] * 4
    out = mc.condense(small)
    assert len(out) == 4 and out[0]["content"] == "hi", "未超阈值应原样返回"
    print("[Masking] 未超阈值不压缩 ✓")

    # 2. MaskingCondenser: 正常压缩
    msgs = [
        {"role": "user", "content": "首条 user"},
        {"role": "assistant", "content": "首条 assistant"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "file_read",
             "input": {"path": "src/x.py"}}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "content": "x" * 5000}
        ]},
        {"role": "user", "content": "中间文本"},
        {"role": "assistant", "content": "中间回复"},
        {"role": "user", "content": "最近1"},
        {"role": "assistant", "content": "最近2"},
        {"role": "user", "content": "最近3"},
        {"role": "assistant", "content": "最近4"},
        {"role": "user", "content": "最近5"},
        {"role": "assistant", "content": "最近6"},
    ]
    out = mc.condense(msgs)
    assert len(out) == len(msgs), "压缩后条数不变"
    # 前 2 条完整保留
    assert out[0]["content"] == "首条 user"
    assert out[1]["content"] == "首条 assistant"
    # 最近 6 条完整保留
    assert out[-1]["content"] == "最近6"
    # 旧区 tool_use 原样保留
    tool_use_msg = out[2]
    assert tool_use_msg["content"][0]["type"] == "tool_use"
    assert tool_use_msg["content"][0]["input"]["path"] == "src/x.py"
    # 旧区 tool_result 被 masking
    tool_result_msg = out[3]
    block = tool_result_msg["content"][0]
    assert block["type"] == "tool_result"
    assert block["is_compressed"] is True
    assert "file_read" in block["content"]
    assert "5000 chars" in block["content"]
    # 纯文本消息完整保留
    assert out[4]["content"] == "中间文本"
    print("[Masking] 正常压缩（tool_result 占位、tool_use 保留、keep_first/recent）✓")

    # 3. tool_name 映射缺失回退 unknown
    msgs2 = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "missing_id",
             "content": "y" * 100}
        ]},
        {"role": "user", "content": "r1"},
        {"role": "user", "content": "r2"},
        {"role": "user", "content": "r3"},
        {"role": "user", "content": "r4"},
        {"role": "user", "content": "r5"},
        {"role": "user", "content": "r6"},
    ]
    out2 = mc.condense(msgs2)
    block2 = out2[2]["content"][0]
    assert "unknown" in block2["content"], f"应回退 unknown，实际: {block2['content']}"
    print("[Masking] tool_name 映射缺失回退 unknown ✓")

    # 4. LLMSummarizingCondenser: 未超阈值不触发 LLM
    calls = []

    class FakeLLM:
        def chat_consolidation_sync(self, messages, system=None, reasoning_cfg=None):
            calls.append(messages)
            class R:
                content = [{"type": "text", "text": "摘要内容"}]
            return R()

    llm_sc = LLMSummarizingCondenser(
        keep_recent_n=6, keep_first=2,
        llm_summary_threshold=100000, llm_client=FakeLLM(),
    )
    out3 = llm_sc.condense(msgs)
    assert not calls, "未超阈值不应调用 LLM"
    assert out3[3]["content"][0].get("is_compressed") is True
    print("[LLMSummary] 未超阈值不触发 LLM ✓")

    # 5. LLMSummarizingCondenser: 超阈值触发 LLM 摘要
    llm_sc2 = LLMSummarizingCondenser(
        keep_recent_n=6, keep_first=2,
        llm_summary_threshold=10, llm_client=FakeLLM(),
    )
    out4 = llm_sc2.condense(msgs)
    assert calls, "超阈值应调用 LLM"
    # 结构：keep_first(2) + 摘要(1) + keep_recent_n(6) = 9
    assert len(out4) == 9, f"期望 9 条，实际 {len(out4)}"
    assert out4[2]["role"] == "user"
    assert "[历史摘要]" in out4[2]["content"]
    assert "摘要内容" in out4[2]["content"]
    print("[LLMSummary] 超阈值触发 LLM 摘要 ✓")

    # 6. LLM 摘要失败降级
    class FailLLM:
        def chat_consolidation_sync(self, messages, system=None, reasoning_cfg=None):
            raise RuntimeError("LLM 故障")

    llm_sc3 = LLMSummarizingCondenser(
        keep_recent_n=6, keep_first=2,
        llm_summary_threshold=10, llm_client=FailLLM(),
    )
    out5 = llm_sc3.condense(msgs)
    # 降级为纯 masking，条数与原 msgs 相同
    assert len(out5) == len(msgs), "降级应返回 masking 结果"
    print("[LLMSummary] LLM 失败降级为 masking ✓")

    # 7. 禁用
    assert create_condenser_from_config(None) is None
    assert create_condenser_from_config({"enabled": False}) is None
    assert create_condenser_from_config({"enabled": True, "strategy": "masking"}) is not None
    print("[Factory] 禁用 / 启用 ✓")

    print("\n=== 所有验证通过 ===")
