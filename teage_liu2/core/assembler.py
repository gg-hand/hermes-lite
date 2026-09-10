"""Assembler(收口四连 + 增量收口不变量,§4.4 阶段 2 落地)。

收口四连(一次对话完整时序):
    ① 注入声明收集(build_injections,注册序)
    ② before(注册序,action 立即应用)
    ③ 组装收口(本模块:注入分层叠加 → merge 相邻 user → 语义级校验)
    ④ 送 LLM

增量收口不变量(v1.7 定案):**任何进入 LLM 的消息序列,必过 merge 相邻 user
+ 语义级校验**——首轮全量收口四连;后续轮 inject_round 全收集 → 增量收口;
轮中钩子(post_tool_call/after_step)的 AppendMessage 应用后,同样在
下一次送 LLM 前过增量收口。
历史沿革:阶段 1 的 validate_messages 仅在对话开头执行一次、每轮注入/追加
不过校验;阶段 2 引入本模块后,任何进入 LLM 的消息序列均过 merge + 语义校验
(见 incremental_finalize),该缺陷已闭环。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .injection import Injection, assemble_injections, merge_consecutive_user_messages
from .types import Message, Snapshot, validate_messages

logger = logging.getLogger(__name__)

#: inject_round 强制注入层(§4.2:layer 强制 BEFORE_INPUT)
_INJECT_ROUND_LAYER = "BEFORE_INPUT"


def finalize_conversation(
    injections: List[Injection],
    snapshot: Snapshot,
    budgets: Optional[Dict[str, int]] = None,
) -> Tuple[str, List[Message], Optional[str]]:
    """收口四连的 ③:注入分层叠加 → merge 相邻 user → 语义级校验。

    返回 (effective_system, effective_messages, problem or None)。

    - 注入的 STABLE_SYSTEM/SYSTEM 层叠加于 before 的 SetSystem 之上
      (注入永远盖过 before 裸 system,§4.4)
    - assemble_injections 内部已 merge 相邻 user;此处再跑语义级校验
      (角色白名单 + 交替,§4.3 合法性双保险)
    """
    effective_system, effective_messages = assemble_injections(
        injections,
        snapshot.system_text,
        snapshot.messages,
        budgets=budgets,
    )
    problem = validate_messages(effective_messages)
    if problem:
        logger.error("收口语义校验失败(会话 %s): %s", snapshot.session_id, problem)
    return effective_system, effective_messages, problem


def _join_round_injections(items: List[Injection]) -> str:
    """轮级注入拼接:priority 降序 + 注册序稳定,内容以换行连接。"""
    ordered = sorted(items, key=lambda i: i.priority, reverse=True)
    return "\n\n".join(i.content for i in ordered if i.content)


def incremental_finalize(
    messages: List[Message],
    round_injections: Optional[List[Injection]] = None,
) -> Tuple[List[Message], Optional[str]]:
    """增量收口不变量(§4.4):任何进入 LLM 的消息序列必过 merge + 语义校验。

    用于:
    - 后续轮:inject_round 全收集 → 增量收口 → 送 LLM
    - 轮中钩子(post_tool_call/after_step)追加消息后,下一次送 LLM 前

    round_injections 仅接受 BEFORE_INPUT 层(§4.2 强制;其他层 warning 忽略),
    置于当前输入前,与输入/工具结果 merge 成一条保持交替。

    返回 (effective_messages, problem or None);problem 非 None = 校验失败,
    调用方不发 LLM(§8 HOOK_INVALID_ACTION → error 事件)。
    """
    msgs = list(messages)
    round_items: List[Injection] = []
    for item in round_injections or []:
        if item.layer != _INJECT_ROUND_LAYER:
            logger.warning(
                "忽略 inject_round 注入层 %r(layer 强制 %s)",
                item.layer, _INJECT_ROUND_LAYER,
            )
            continue
        round_items.append(item)

    before = _join_round_injections(round_items)
    if before:
        if msgs:
            msgs = (
                msgs[:-1]
                + [{"role": "user", "content": before}]
                + msgs[-1:]
            )
        else:
            msgs = [{"role": "user", "content": before}]

    # merge 相邻 user(注入与输入/工具结果合成一条,保持交替)
    msgs = merge_consecutive_user_messages(msgs)
    problem = validate_messages(msgs)
    if problem:
        logger.error("增量收口语义校验失败: %s", problem)
    return msgs, problem
