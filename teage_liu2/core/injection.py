"""多层级注入平台(I1+I2,计划 §2 定案)。

五层全定义全组装 —— 子系统按内容稳定度自主选择注入位置:

    L0 STABLE_SYSTEM   system 稳定区(前缀缓存命中)     ← 画像主体/全局规则/工具说明
    L1 SYSTEM          system 末位(缓存失效仍有效)     ← 会话级指令/临时全局上下文
    L2 PREFIX          messages[0] 前置(缓存失效点)    ← 检索记忆/环境信息/任务状态
    L3 MID             历史中间(按位插入)              ← 对话背景/长期上下文说明
    L4 BEFORE_INPUT    当前输入前(最动态)              ← 意图引导/瞬时指令

core 只做收集、分层、预算裁剪、组装;注入内容**永不落盘**(瞬时上下文,
由调用方保证只进 effective_messages,不写历史库)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 层级常量
L_STABLE_SYSTEM = "STABLE_SYSTEM"
L_SYSTEM = "SYSTEM"
L_PREFIX = "PREFIX"
L_MID = "MID"
L_BEFORE_INPUT = "BEFORE_INPUT"

_ALL_LAYERS = (L_STABLE_SYSTEM, L_SYSTEM, L_PREFIX, L_MID, L_BEFORE_INPUT)

# 分层预算(字符,默认可配):层内 priority 降序 + 注册序稳定;整段丢弃不截半
DEFAULT_LAYER_BUDGETS: Dict[str, int] = {
    L_STABLE_SYSTEM: 4000,
    L_SYSTEM: 2000,
    L_PREFIX: 8000,
    L_MID: 2000,
    L_BEFORE_INPUT: 2000,
}


@dataclass
class Injection:
    """子系统声明的一条注入项:位置 + 内容 + 优先级(自主控制)。"""

    layer: str
    content: str
    priority: int = 0
    key: Optional[str] = None


def _dedupe_by_key(items: List[Injection]) -> List[Injection]:
    """同 key 去重:后声明覆盖先声明(保持首次出现位置)。"""
    seen: Dict[str, int] = {}
    result: List[Injection] = []
    for item in items:
        if item.key is None:
            result.append(item)
            continue
        if item.key in seen:
            result[seen[item.key]] = item  # 后声明覆盖
        else:
            seen[item.key] = len(result)
            result.append(item)
    return result


def _select_by_budget(items: List[Injection], budget: int) -> List[Injection]:
    """层内预算裁剪:priority 降序 + 注册序稳定(稳定排序);累计超预算整段丢。"""
    ordered = sorted(items, key=lambda i: i.priority, reverse=True)
    selected: List[Injection] = []
    total = 0
    for item in ordered:
        size = len(item.content)
        if total + size > budget:
            continue  # 整段丢弃,不截半
        selected.append(item)
        total += size
    return selected


def _join_contents(items: List[Injection]) -> str:
    return "\n\n".join(i.content for i in items)


def _merge_user_contents(a: dict, b: dict) -> dict:
    """合并两条 user 消息的内容:字符串拼接;list content 混合 text 与 blocks。"""
    ca, cb = a.get("content"), b.get("content")
    if isinstance(ca, str) and isinstance(cb, str):
        return {**a, "content": ca + "\n\n" + cb}
    merged: List[Any] = []
    if isinstance(ca, list):
        merged.extend(ca)
    elif isinstance(ca, str) and ca:
        merged.append({"type": "text", "text": ca})
    if isinstance(cb, list):
        merged.extend(cb)
    elif isinstance(cb, str) and cb:
        merged.append({"type": "text", "text": cb})
    return {**a, "content": merged}


def merge_consecutive_user_messages(messages: List[Dict]) -> List[Dict]:
    """合并相邻 user 消息(LLM API 要求 user/assistant 交替,防 400)。

    注入按位插入独立 user 消息后统一规范化:相邻 user 合并为一条
    (文本拼接;list content 混合 text 块与 tool_result 块)。
    """
    out: List[Dict] = []
    for m in messages:
        if out and out[-1].get("role") == "user" and m.get("role") == "user":
            out[-1] = _merge_user_contents(out[-1], m)
        else:
            out.append(m)
    return out


def assemble_injections(
    injections: List[Injection],
    system_text: str,
    messages: List[Dict],
    budgets: Optional[Dict[str, int]] = None,
) -> Tuple[str, List[Dict]]:
    """多层级组装:返回 (effective_system, effective_messages)。

    - STABLE_SYSTEM → system 稳定区(基础 system 之后最先)
    - SYSTEM → system 末位
    - PREFIX → 一条 user 消息置于 messages 最前
    - MID → 一条 user 消息置于历史中间(messages[:-1] 的中点按位插入)
    - BEFORE_INPUT → 一条 user 消息置于当前输入前
    未知层:warning 并忽略(单条隔离,不影响对话)。
    """
    budgets = budgets or DEFAULT_LAYER_BUDGETS
    injections = _dedupe_by_key(injections)

    by_layer: Dict[str, List[Injection]] = {L: [] for L in _ALL_LAYERS}
    for item in injections:
        if item.layer in by_layer:
            by_layer[item.layer].append(item)
        else:
            logger.warning("忽略未知注入层 %r(内容前 %d 字符)", item.layer, len(item.content))

    # ---- system 组装:基础 + 稳定区 + system 末位 ----
    parts = [system_text] if system_text else []
    stable = _join_contents(_select_by_budget(by_layer[L_STABLE_SYSTEM], budgets[L_STABLE_SYSTEM]))
    if stable:
        parts.append(stable)
    system_layer = _join_contents(_select_by_budget(by_layer[L_SYSTEM], budgets[L_SYSTEM]))
    if system_layer:
        parts.append(system_layer)
    effective_system = "\n\n".join(parts)

    # ---- messages 组装:MID 中间 → PREFIX 最前 → BEFORE_INPUT 输入前 ----
    effective_messages = list(messages)

    mid = _join_contents(_select_by_budget(by_layer[L_MID], budgets[L_MID]))
    if mid:
        history_part = effective_messages[:-1]
        mid_pos = len(history_part) // 2  # 历史中点按位插入
        effective_messages = (
            history_part[:mid_pos]
            + [{"role": "user", "content": mid}]
            + history_part[mid_pos:]
            + effective_messages[-1:]
        )

    prefix = _join_contents(_select_by_budget(by_layer[L_PREFIX], budgets[L_PREFIX]))
    if prefix:
        effective_messages = [{"role": "user", "content": prefix}, *effective_messages]

    before_input = _join_contents(
        _select_by_budget(by_layer[L_BEFORE_INPUT], budgets[L_BEFORE_INPUT])
    )
    if before_input and effective_messages:
        effective_messages = (
            effective_messages[:-1]
            + [{"role": "user", "content": before_input}]
            + effective_messages[-1:]
        )

    # 合并相邻 user 消息(API 交替要求,防 400;C1 校验前置保证)
    effective_messages = merge_consecutive_user_messages(effective_messages)

    return effective_system, effective_messages
