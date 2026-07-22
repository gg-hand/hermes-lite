"""Agent 元认知：失败信号触发与用户反馈检测。

从 ReactLoop 提取，负责 Agent 自画像信号入池逻辑：
- 工具连续失败 ≥2 次或 PERMANENT 错误 → 信号入池
- 用户口头反馈（"又错了"/"上次说过"）→ 信号入池
"""
from __future__ import annotations

import logging
import weakref
from typing import Any, Optional

logger = logging.getLogger(__name__)


def maybe_trigger_agent_failure_signal(
    orchestrator_ref: Optional[weakref.ReferenceType],
    session_id: Optional[str],
    tool_name: str,
    error_class: Optional[str],
    consecutive_failures: int,
) -> bool:
    """触发 Agent 自画像信号入池（连续失败≥2 次 或 PERMANENT 错误类）。

    通过 ``orchestrator_ref`` 弱引用访问 ``signal_pool``，调用
    ``signal_pool.add(target="agent", section="Agent 自画像", content=...)``
    将失败模式作为 Agent 自画像信号入池。

    cron 会话（session_id 以 ``cron:`` 开头）跳过，避免污染用户画像。
    """
    if session_id and isinstance(session_id, str) and session_id.startswith("cron:"):
        return False
    orch = orchestrator_ref() if orchestrator_ref else None
    if orch is None or getattr(orch, "signal_pool", None) is None:
        logger.debug(
            "signal_pool 不可用，Agent 失败信号未入池（tool=%s, failures=%d）",
            tool_name, consecutive_failures,
        )
        return False
    ec_part = f"，错误类型={error_class}" if error_class else ""
    content = (
        f"Agent 在工具 {tool_name} 上连续失败 {consecutive_failures} 次{ec_part}"
    )
    try:
        orch.signal_pool.add(
            content=content,
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        logger.info(
            "Agent 失败信号入池（target=agent）: tool=%s failures=%d ec=%s",
            tool_name, consecutive_failures, error_class,
        )
        return True
    except Exception as e:
        logger.warning("Agent 失败信号入池异常: %s", e)
        return False


def check_user_failure_feedback(
    orchestrator_ref: Optional[weakref.ReferenceType],
    user_input: str,
    session_id: Optional[str],
) -> None:
    """检测用户对失败的口头反馈（"又错了"/"上次说过"等），触发 Agent 信号入池。"""
    if not user_input:
        return
    feedback_keywords = ("又错了", "上次说过", "不是说过", "说过不要", "重复犯")
    for kw in feedback_keywords:
        if kw in user_input:
            maybe_trigger_agent_failure_signal(
                orchestrator_ref=orchestrator_ref,
                session_id=session_id,
                tool_name="unknown",
                error_class="user_feedback",
                consecutive_failures=1,
            )
            return
