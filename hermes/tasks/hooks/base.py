"""ScheduleHook 协议 + ScheduleHookBase 基类 + RetryDecision（3.3）。

设计原则：
- 签名通用（before/on_failure/after），不绑定 schedule 特有概念
- ctx 类型为 WorkflowContext（复用现有）
- result 类型为 WorkflowResult（复用现有）
- 所有 hook 实现必须继承 ScheduleHookBase，避免 HookRegistry 循环调用时 AttributeError
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class RetryDecision:
    """重试决策。"""

    def __init__(self, retry: bool, delay_seconds: int = 0, reason: str = ""):
        self.retry = retry
        self.delay_seconds = delay_seconds
        self.reason = reason

    @classmethod
    def give_up(cls, reason: str = "") -> "RetryDecision":
        return cls(retry=False, reason=reason)

    @classmethod
    def retry_after(cls, seconds: int, reason: str = "") -> "RetryDecision":
        return cls(retry=True, delay_seconds=seconds, reason=reason)


@runtime_checkable
class ScheduleHook(Protocol):
    """调度执行钩子协议（类型声明，不可直接继承）。"""

    name: str

    async def before_execute(self, ctx: Any) -> Any: ...

    async def on_failure(self, ctx: Any, error: Exception) -> RetryDecision: ...

    async def after_execute(self, ctx: Any, result: Any) -> None: ...


class ScheduleHookBase:
    """ScheduleHook 协议的 no-op 默认实现基类。

    所有 hook 实现必须继承此类，按需 override 对应方法。
    未 override 的方法为 no-op，避免 HookRegistry 循环调用时 AttributeError。
    """

    name: str = ""

    async def before_execute(self, ctx: Any) -> Any:
        return ctx

    async def on_failure(self, ctx: Any, error: Exception) -> RetryDecision:
        return RetryDecision.give_up()

    async def after_execute(self, ctx: Any, result: Any) -> None:
        pass
