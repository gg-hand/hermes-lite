"""RetryHook（L3 重试层，5.2）。

Q1 决策 C：RetryHook 是唯一重试层（WorkflowEngine 内的 RetryBudget 已移除）。
Q3 决策：整次重跑（不做 step 级 checkpoint）。
Q10 决策：permanent 错误类型识别 + AuthRequiredError 加入顶层 isinstance。
"""
from __future__ import annotations

import logging
from typing import Any

from .base import ScheduleHookBase, RetryDecision

logger = logging.getLogger(__name__)


def _is_permanent_error(error: Exception) -> bool:
    """判断是否为不可恢复的 permanent 错误。

    permanent 类型（Q10）：
    - ToolNotFoundError / ParamError / ValidationError / HookAbortError / AuthRequiredError
    - WorkflowExecutionError 且 step_traces 含 permanent / auth_required 错误

    transient 类型（可重试）：网络超时 / ConnectionError / SMTP 失败 / 未分类异常（默认 transient）
    """
    from teage_liu.agent.tool_error import (
        ToolNotFoundError, ParamError, HookAbortError, ValidationError,
        AuthRequiredError, WorkflowExecutionError,
    )
    # P1 修复：AuthRequiredError 加入顶层 isinstance 检查
    if isinstance(error, (ToolNotFoundError, ParamError, ValidationError,
                          HookAbortError, AuthRequiredError)):
        return True
    if isinstance(error, WorkflowExecutionError):
        result = error.result
        for trace in getattr(result, "step_traces", []):
            err_class = getattr(trace, "error_class", None)
            if err_class in ("permanent", "auth_required"):
                return True
    return False


class RetryHook(ScheduleHookBase):
    """L3: 失败后固定间隔重试。permanent 错误不重试。"""

    name = "retry"

    def __init__(self, config: dict):
        self.max_retries = config.get("max_retries", 3)
        self.interval_seconds = config.get("interval_seconds", 300)
        self.transient_only = config.get("transient_only", True)

    async def on_failure(self, ctx: Any, error: Exception) -> RetryDecision:
        if self.transient_only and _is_permanent_error(error):
            return RetryDecision.give_up(
                reason=f"permanent 错误，不重试: {type(error).__name__}: {error}"
            )
        if ctx.retry_count >= self.max_retries:
            return RetryDecision.give_up(
                reason=f"已达最大重试次数 {self.max_retries}"
            )
        return RetryDecision.retry_after(
            seconds=self.interval_seconds,
            reason=(f"transient 错误，第 {ctx.retry_count + 1}/{self.max_retries} "
                    f"次重试: {error}")
        )
