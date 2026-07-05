"""RetryBudget 重试预算与 backoff 计算（Task 5.1）。

由 ``WorkflowEngine._execute_with_policy`` 消费，控制 step 失败后的
重试次数与 backoff 间隔。

支持三种 backoff 策略：
- ``fixed``：固定间隔 ``base_delay_ms``
- ``linear``：线性增长 ``base_delay_ms * attempt``
- ``exponential``：指数增长 ``base_delay_ms * (2 ** (attempt-1))``

所有策略都受 ``max_delay_ms`` 上限约束。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Set

from .spec import RetryPolicy

logger = logging.getLogger(__name__)


# 默认允许重试的 ErrorClass 集合（与 ErrorClassifier.ErrorClass.value 对应）
DEFAULT_RETRYABLE_ERRORS: Set[str] = {"transient", "timeout"}

# 永不重试的错误分类（PERMANENT / AUTH_REQUIRED / NotImplementedError）
NON_RETRYABLE_ERRORS: Set[str] = {
    "permanent",
    "auth_required",
    "permission",
    "not_found",
    "param_error",
    "internal_error",
}


@dataclass
class RetryBudget:
    """重试预算管理器。

    跟踪当前 attempt 计数，计算下一次重试的 backoff 间隔。

    属性:
        policy: RetryPolicy 配置。
        attempt: 当前尝试次数（首次执行后为 1，每次 retry +1）。
    """

    policy: RetryPolicy
    attempt: int = 0

    def increment(self) -> int:
        """尝试次数 +1，返回新的 attempt 值。"""
        self.attempt += 1
        return self.attempt

    def has_budget(self) -> bool:
        """是否还有重试预算（attempt < max_attempts）。"""
        return self.attempt < self.policy.max_attempts

    def should_retry(self, error_class: str = "") -> bool:
        """是否应该重试当前错误。

        判定条件：
        1. 有剩余预算（attempt < max_attempts）
        2. 错误分类在 retry_on 集合中（空 error_class 默认 retryable）
        3. 错误分类不在 NON_RETRYABLE_ERRORS 中

        NotImplementedError 永不重试（PERMANENT 语义）。
        """
        if not self.has_budget():
            return False
        if not error_class:
            # 未知错误分类默认 retryable（保守策略，让 retry 兜底）
            return True
        if error_class in NON_RETRYABLE_ERRORS:
            return False
        if error_class == "notimplemented":
            # NotImplementedError 映射为 notimplemented，永不重试
            return False
        retry_on = set(self.policy.retry_on) or DEFAULT_RETRYABLE_ERRORS
        return error_class in retry_on

    def compute_backoff_ms(self) -> int:
        """计算下一次重试的 backoff 间隔（毫秒）。

        基于 ``self.attempt``（已尝试次数）计算下一次（attempt+1）的延迟。
        """
        if self.attempt <= 0:
            return 0
        base = self.policy.base_delay_ms
        strategy = self.policy.backoff_strategy
        if strategy == "fixed":
            delay = base
        elif strategy == "linear":
            delay = base * self.attempt
        elif strategy == "exponential":
            delay = base * (2 ** (self.attempt - 1))
        else:
            logger.warning("未知 backoff_strategy '%s'，降级为 fixed", strategy)
            delay = base
        # 上限约束
        return min(delay, self.policy.max_delay_ms)

    def sleep_backoff(self) -> None:
        """同步 sleep 当前 backoff 间隔（毫秒转秒）。

        用于同步执行路径（WorkflowEngine 在 asyncio.to_thread 中调用）。
        """
        delay_ms = self.compute_backoff_ms()
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)


__all__ = [
    "DEFAULT_RETRYABLE_ERRORS",
    "NON_RETRYABLE_ERRORS",
    "RetryBudget",
]
