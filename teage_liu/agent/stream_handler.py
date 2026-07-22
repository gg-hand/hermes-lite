"""流式响应处理辅助函数。

从 ReactLoop 提取的纯函数/半纯函数，用于 SSE 流处理：
- ``block_to_dict``: anthropic content block → dict 转换
- ``build_reasoning_stats``: done 事件的 reasoning_stats 字段构造
- ``build_done_event``: 统一构造 done 事件 dict

这些函数无 ``self`` 依赖（``build_done_event`` 调用 ``build_reasoning_stats``），
可独立测试。``run_stream`` 主体仍保留在 ReactLoop 中，因其与 ReactLoop
实例状态深度耦合（llm_client / tool_registry / policy_engine 等 10+ 依赖）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..llm.reasoning_profiles import ReasoningConfig

logger = logging.getLogger(__name__)


def block_to_dict(block: Any) -> Dict[str, Any]:
    """将 anthropic 响应的 content block 转为可序列化的 dict。

    anthropic SDK 返回的 content block 是类型对象（如 TextBlock、
    ToolUseBlock），回传给 messages.create 时虽支持对象，但统一转为
    dict 便于日志、序列化与一致处理。

    参数:
        block: anthropic content block 对象或 dict。

    返回:
        转换后的 dict。
    """
    if isinstance(block, dict):
        return block

    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {
            "type": "text",
            "text": getattr(block, "text", ""),
        }
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    if block_type == "thinking":
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", ""),
            "signature": getattr(block, "signature", ""),
        }
    return {"type": block_type or "unknown"}


def build_reasoning_stats(
    reasoning_cfg: Optional["ReasoningConfig"],
    usage: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """构造 done 事件的 reasoning_stats 字段（SubTask 10.8）。

    合并 effort/budget/reasoning_tokens 到单个 dict，避免与
    ``done.usage.reasoning_tokens`` 重复且消除"done 后再更新思考区头部"
    的闪烁。

    参数:
        reasoning_cfg: 本轮使用的 ReasoningConfig（可能为 None）。
        usage: backend done 事件提取的 usage dict（含 reasoning_tokens）。

    返回:
        ``{"effort": str, "budget_tokens": int|None, "reasoning_tokens": int}``
        或 None（reasoning 未开启时）。
    """
    if reasoning_cfg is None or not reasoning_cfg.enabled:
        return None
    reasoning_tokens = 0
    if usage and isinstance(usage, dict):
        reasoning_tokens = usage.get("reasoning_tokens", 0) or 0
    return {
        "effort": reasoning_cfg.effort,
        "budget_tokens": reasoning_cfg.budget_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def build_done_event(
    *,
    response: str,
    messages: List[Dict[str, Any]],
    is_complete: bool,
    termination_reason: str,
    usage: Optional[Dict[str, Any]] = None,
    content_blocks: Optional[List[Dict[str, Any]]] = None,
    stop_reason: Optional[str] = None,
    reasoning_cfg: Optional["ReasoningConfig"] = None,
    current_round_text: str = "",
    current_round_reasoning: str = "",
) -> Dict[str, Any]:
    """统一构造 done 事件，保证字段完整性（spec SubTask 10.5/22.11/22.32）。

    所有 done 事件 yield 点必须调用此工厂，禁止手写 dict。
    字段兜底：usage=None→{}，content_blocks=None→从 current_round_text 重建，
    stop_reason=None→"end_turn"，reasoning_stats 调用 build_reasoning_stats。
    """
    final_usage = usage if usage is not None else {}
    if content_blocks is None:
        content_blocks = []
        if current_round_text:
            content_blocks.append({"type": "text", "text": current_round_text})
    final_stop_reason = stop_reason if stop_reason is not None else "end_turn"
    reasoning_stats = build_reasoning_stats(
        reasoning_cfg, final_usage if final_usage else None
    )
    return {
        "type": "done",
        "response": response,
        "messages": messages,
        "is_complete": is_complete,
        "termination_reason": termination_reason,
        "usage": final_usage,
        "content_blocks": content_blocks,
        "stop_reason": final_stop_reason,
        "reasoning_stats": reasoning_stats,
    }


async def generate_max_loops_summary(
    llm_client: Any,
    messages: List[Dict[str, Any]],
    last_text: str,
    session_id: Optional[str] = None,
) -> str:
    """达到 max_loops 时调用 LLM 生成总结性回复。

    不传 tools 参数，强制 LLM 返回纯文本总结。失败时降级返回
    ``last_text`` 并记录 error 日志。
    """
    try:
        summary_prompt = (
            "已达循环上限，请总结当前进展与未完成原因，不要调用工具。"
            f"最后回复：{last_text}"
        )
        summary_messages = messages + [
            {"role": "user", "content": summary_prompt}
        ]
        response = await llm_client.chat_main(
            messages=summary_messages,
            system=None,
            tools=None,
        )
        content_blocks = getattr(response, "content", []) or []
        text_parts: List[str] = []
        for block in content_blocks:
            block_dict = block_to_dict(block)
            if block_dict.get("type") == "text":
                text = block_dict.get("text", "")
                if text:
                    text_parts.append(text)
        summary_text = "".join(text_parts)
        if not summary_text:
            logger.warning(
                "max_loops 总结调用返回空文本，降级返回 last_text"
            )
            return last_text
        return summary_text
    except Exception as e:
        logger.error("max_loops 总结调用失败，降级返回 last_text: %s", e)
        return last_text
