"""Snapshot 推进(§4.4 立即应用 + 原子批次;§18.2 COW 快照;§15-A6 资源上限)。

- 每个扩展返回的合法 action 批次原子推进快照,revision +1
- 结构共享:O(n) 浅拷贝(messages 尾部追加新列表 + 共享元素引用),禁 deepcopy
- 两层校验分离:
  * schema 级(validate_action 逐条):应用前校验,非法条拒绝 + error 日志 + 跳过
  * 语义级(组装后统一校验):见 assembler
- 资源上限(§15-A6):单条消息体积 / 消息总条数 / 快照体积,超限拒绝 + error 日志
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from .actions import (
    AppendMessage,
    ModifyToolSchema,
    SetExtra,
    SetStop,
    SetSystem,
    SetTools,
    validate_action,
)
from .types import RESOURCE_LIMITS, Snapshot, validate_message_shape

logger = logging.getLogger(__name__)


def _estimate_bytes(obj: Any) -> int:
    """估算对象序列化体积(资源上限检查用,非精确 JSON 长度)。"""
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _check_message_budget(
    messages: List[Dict[str, Any]], message: Dict[str, Any]
) -> Optional[str]:
    """单条消息入快照前的资源上限检查(§15-A6)。"""
    limit_count = RESOURCE_LIMITS["max_messages_per_conversation"]
    if len(messages) >= limit_count:
        return (
            f"消息总条数超上限 {limit_count}(HOOK_INVALID_ACTION)"
        )
    limit_msg = RESOURCE_LIMITS["max_message_bytes"]
    size = _estimate_bytes(message)
    if size > limit_msg:
        return (
            f"单条消息体积 {size} 字节超上限 {limit_msg}"
            "(HOOK_INVALID_ACTION)"
        )
    return None


def _check_snapshot_budget(snapshot: Snapshot) -> Optional[str]:
    """快照总体积上限检查(§15-A6)。"""
    limit = RESOURCE_LIMITS["max_snapshot_bytes"]
    size = _estimate_bytes(snapshot.to_dict())
    if size > limit:
        return (
            f"快照体积 {size} 字节超上限 {limit}(HOOK_INVALID_ACTION)"
        )
    return None


def apply_action_batch(
    snapshot: Snapshot, actions: List[Any]
) -> Tuple[Snapshot, List[str]]:
    """立即应用一个 action 批次(原子推进,revision +1)。

    返回 (新快照, 非法 action 描述列表)。非法条拒绝 + error 日志 +
    跳过该条;合法条批次原子推进——下一个扩展看到完整批次推进后的快照。

    :param snapshot: 当前快照(不可变,只读输入)
    :param actions: 该扩展返回的 action 列表
    """
    if not actions:
        return snapshot, []

    # schema 级校验(§4.4 两层校验分离:逐条先校验)
    invalid: List[str] = []
    valid: List[Any] = []
    for a in actions:
        problem = validate_action(a)
        if problem:
            logger.error("HOOK_INVALID_ACTION: %s", problem)
            invalid.append(problem)
        else:
            valid.append(a)

    if not valid:
        return snapshot, invalid

    # 逐条应用(原子批次:全部合法 action 一次性推进)
    # O(n) 浅拷贝:新列表 + 共享元素引用(§18.2,禁 deepcopy)
    messages = list(snapshot.messages)
    tools = list(snapshot.tools)
    extra = dict(snapshot.extra)
    system_text = snapshot.system_text
    stop = snapshot.stop
    stop_reason = snapshot.stop_reason

    for a in valid:
        if isinstance(a, AppendMessage):
            budget = _check_message_budget(messages, a.message)
            if budget:
                logger.error("HOOK_INVALID_ACTION: %s", budget)
                invalid.append(budget)
                continue
            messages = messages + [dict(a.message)]
        elif isinstance(a, SetTools):
            tools = list(a.tools)
        elif isinstance(a, SetExtra):
            extra[a.key] = a.value
        elif isinstance(a, SetStop):
            stop = True
            stop_reason = a.reason
        elif isinstance(a, SetSystem):
            system_text = a.text
        elif isinstance(a, ModifyToolSchema):
            tools = _modify_tool_schema(tools, a)

    # 语义级检查:追加消息的角色/形状(逐条结构校验,交替由 assembler 收口)
    new_snapshot = replace(
        snapshot,
        messages=messages,
        tools=tools,
        extra=extra,
        system_text=system_text,
        stop=stop,
        stop_reason=stop_reason,
        revision=snapshot.revision + 1,
    )

    budget = _check_snapshot_budget(new_snapshot)
    if budget:
        # §15-A6 T-8 × hooks H-8 一致性(2026-09-10 评审 FIX-1):
        # 体积上限只拒绝**本批次的 append 类 action**(消息增长的主因,与消息级上限
        # 的"逐条拒绝 + 其余照常应用"语义一致);set 类(SetStop / SetExtra /
        # SetSystem / SetTools / ModifyToolSchema)一律保留 —— 其中 SetStop 承载
        # 安全拦截语义,且 set 类不随轮次累积增长,不接受体积拒绝。
        # 此前"整批回滚"的两处后果均已修复:①同批 [AppendMessage 超限, SetStop]
        # 把拦截一起丢掉;②入参已超限时该会话此后所有钩子批次被永久拒绝。
        set_names = [type(a).__name__ for a in valid if not isinstance(a, AppendMessage)]
        safe = replace(new_snapshot, messages=list(snapshot.messages))
        logger.error(
            "HOOK_INVALID_ACTION: %s(本批次 append 类 action 已拒绝,set 类保留:%s)",
            budget, set_names,
        )
        invalid.append(budget)
        return safe, invalid

    return new_snapshot, invalid


def _modify_tool_schema(
    tools: List[Dict[str, Any]], action: ModifyToolSchema
) -> List[Dict[str, Any]]:
    """按工具名定向修改单个工具 schema(§4.1;未命中工具名 → 忽略 + 记录)。"""
    found = False
    out: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            out.append(t)
            continue
        if t.get("name") == action.name:
            modified = dict(t)
            if action.description is not None:
                modified["description"] = action.description
            if action.input_schema is not None:
                modified["input_schema"] = action.input_schema
            out.append(modified)
            found = True
        else:
            out.append(t)
    if not found:
        logger.error(
            "ModifyToolSchema 未命中工具名 %r(忽略该 action)",
            action.name,
        )
    return out


def snapshot_with_user_message(snapshot: Snapshot, content: Any) -> Snapshot:
    """构造含当前 user 输入消息的新快照(初始快照构建用)。"""
    messages = snapshot.messages
    user_msg: Dict[str, Any] = {"role": "user", "content": content}
    budget = _check_message_budget(messages, user_msg)
    if budget:
        logger.error("HOOK_INVALID_ACTION: %s", budget)
        return snapshot
    return replace(snapshot, messages=messages + [user_msg])


def validate_snapshot_messages(snapshot: Snapshot) -> Optional[str]:
    """快照消息逐条结构校验(角色白名单 + content 形态)。

    合法返回 None;非法返回错误描述。交替约束由 assembler 收口统一校验。
    """
    for i, m in enumerate(snapshot.messages):
        problem = validate_message_shape(m)
        if problem:
            return f"快照消息[{i}]: {problem}"
    return None
