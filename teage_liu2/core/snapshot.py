"""Snapshot 推进(§4.4 立即应用 + 原子批次;§18.2 COW 快照;§15-A6 资源上限)。

- 每个扩展返回的合法 action 批次原子推进快照,revision +1
- 结构共享:O(n) 浅拷贝(messages 尾部追加新列表 + 共享元素引用),禁 deepcopy
- 两层校验分离:
  * schema 级(validate_action 逐条):应用前校验,非法条拒绝 + error 日志 + 跳过
  * 语义级(组装后统一校验):见 assembler
- 资源上限(§15-A6):单条消息体积 / 消息总条数 / 快照体积,超限拒绝 + error 日志
"""

from __future__ import annotations

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
from .types import (
    RESOURCE_LIMITS,
    Snapshot,
    _SizeCache,
    _json_len,
    validate_message_shape,
)

logger = logging.getLogger(__name__)


def snapshot_bytes(snapshot: Snapshot) -> int:
    """快照序列化体积(增量记账):恒等于 len(json.dumps(snapshot.to_dict()))。

    稳态下只做常数级算术;仅当缓存失效(未建立/messages 列表被换)时才做一次
    精确计算(见 _measure)。
    """
    return _measure(snapshot)[0]


def _measure(snapshot: Snapshot) -> Tuple[int, Snapshot]:
    """返回 (体积, 带缓存的新快照)。

    体积 = base_bytes - 2 + msg_bytes:``to_dict()`` 的 JSON 中,``messages`` 以外的
    部分是固定模板,把 ``[]``(2 字符)换成真实数组即改变 ``msg_bytes - 2`` 字节 ——
    与全量 ``json.dumps`` 精确恒等。
    """
    cache = snapshot._size_cache
    valid = cache is not None and cache.messages_ref is snapshot.messages
    msg_bytes = cache.msg_bytes if valid else -1
    base_bytes = cache.base_bytes if valid else -1
    if msg_bytes < 0:
        msg_bytes = _json_len(snapshot.messages)
    if base_bytes < 0:
        base_bytes = _json_len(replace(snapshot, messages=[]).to_dict())
        if base_bytes == 0:
            # 不可序列化(如 SetExtra 写入非 JSON 值:validate_action 不校验可序列化性)
            # → 与历史 _estimate_bytes 语义一致:体积**恒**视为 0(永不拒绝)。
            # 缓存 base 分量必须存 -1 而非 0:0 会被下一次判定为"已知的 0 字节",
            # 从而使 base-2+msg_bytes 变成可能超限的正数 → 误拒。
            return 0, replace(
                snapshot, _size_cache=_SizeCache(snapshot.messages, msg_bytes, -1)
            )
    cached = replace(
        snapshot, _size_cache=_SizeCache(snapshot.messages, msg_bytes, base_bytes)
    )
    return base_bytes - 2 + msg_bytes, cached


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
    size = _json_len(message)
    if size > limit_msg:
        return (
            f"单条消息体积 {size} 字节超上限 {limit_msg}"
            "(HOOK_INVALID_ACTION)"
        )
    return None


def _check_snapshot_budget(snapshot: Snapshot) -> Tuple[Optional[str], Snapshot]:
    """快照总体积上限检查(§15-A6)。返回 (问题描述, 带体积缓存的新快照)。"""
    limit = RESOURCE_LIMITS["max_snapshot_bytes"]
    size, snapshot = _measure(snapshot)
    if size > limit:
        return (
            f"快照体积 {size} 字节超上限 {limit}(HOOK_INVALID_ACTION)"
        ), snapshot
    return None, snapshot


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

    # 体积记账(§15-A6 增量):缓存失效时分量留 -1,由 _check_snapshot_budget 精确重算
    cache = snapshot._size_cache
    cache_ok = cache is not None and cache.messages_ref is snapshot.messages
    msg_bytes = cache.msg_bytes if cache_ok else -1
    base_bytes = cache.base_bytes if cache_ok else -1

    for a in valid:
        if isinstance(a, AppendMessage):
            budget = _check_message_budget(messages, a.message)
            if budget:
                logger.error("HOOK_INVALID_ACTION: %s", budget)
                invalid.append(budget)
                continue
            was_empty = not messages
            encoded = _json_len(a.message)
            messages = messages + [dict(a.message)]
            if msg_bytes >= 0:
                msg_bytes += encoded + (0 if was_empty else 2)
        elif isinstance(a, SetTools):
            tools = list(a.tools)
            base_bytes = -1
        elif isinstance(a, SetExtra):
            extra[a.key] = a.value
            base_bytes = -1
        elif isinstance(a, SetStop):
            stop = True
            stop_reason = a.reason
            base_bytes = -1
        elif isinstance(a, SetSystem):
            system_text = a.text
            base_bytes = -1
        elif isinstance(a, ModifyToolSchema):
            tools = _modify_tool_schema(tools, a)
            base_bytes = -1

    # revision +1 会让 base 中的整数位数变化(9→10 / 99→100),必须精确差分
    if base_bytes >= 0:
        base_bytes += len(str(snapshot.revision + 1)) - len(str(snapshot.revision))

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
        _size_cache=_SizeCache(messages, msg_bytes, base_bytes),
    )

    budget, new_snapshot = _check_snapshot_budget(new_snapshot)
    if budget:
        # §15-A6 T-8 × hooks H-8 一致性(2026-09-10 评审 FIX-1):
        # 体积上限只拒绝**本批次的 append 类 action**(消息增长的主因,与消息级上限
        # 的"逐条拒绝 + 其余照常应用"语义一致);set 类(SetStop / SetExtra /
        # SetSystem / SetTools / ModifyToolSchema)一律保留 —— 其中 SetStop 承载
        # 安全拦截语义,且 set 类不随轮次累积增长,不接受体积拒绝。
        # 此前"整批回滚"的两处后果均已修复:①同批 [AppendMessage 超限, SetStop]
        # 把拦截一起丢掉;②入参已超限时该会话此后所有钩子批次被永久拒绝。
        set_names = [type(a).__name__ for a in valid if not isinstance(a, AppendMessage)]
        rolled = list(snapshot.messages)
        safe = replace(
            new_snapshot,
            messages=rolled,
            # 消息回滚 → 消息分量回到入参;set 类保留 → base 分量必须失效
            _size_cache=(
                None if snapshot._size_cache is None
                # 消息分量按回滚后的列表重新度量(不沿用入参缓存值,防"非 None 但陈旧")
                else _SizeCache(rolled, _json_len(rolled), -1)
            ),
        )
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
    return snapshot.with_messages(messages + [user_msg])


def validate_snapshot_messages(snapshot: Snapshot) -> Optional[str]:
    """快照消息逐条结构校验(角色白名单 + content 形态)。

    合法返回 None;非法返回错误描述。交替约束由 assembler 收口统一校验。
    """
    for i, m in enumerate(snapshot.messages):
        problem = validate_message_shape(m)
        if problem:
            return f"快照消息[{i}]: {problem}"
    return None
