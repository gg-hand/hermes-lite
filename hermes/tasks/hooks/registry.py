"""HookRegistry（3.7）。

管理所有 hook 实例，按 config 开关启用/禁用。
Q8 决策：_failure_counts 不放在 HookRegistry（避免重建时迁移），
        而是放在 CronScheduler 实例上（长生命周期）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ScheduleHookBase, RetryDecision


class HookRegistry:
    """管理所有 hook 实例，按 config 开关启用/禁用。"""

    def __init__(self, config: dict, scheduler_ref: Optional[Any] = None):
        self.hooks: Dict[str, ScheduleHookBase] = {}
        self._scheduler_ref = scheduler_ref
        self._config = config

        # 延迟导入 hook 实现类，避免循环依赖
        # 各 hook 类在 Task 8/12/11/14 中实现，此处用 try/except 兼容渐进实现
        try:
            from .validate_hook import ValidateHook
            if config.get("validate", {}).get("enabled", True):
                self.hooks["validate"] = ValidateHook(config.get("validate", {}))
        except ImportError:
            pass

        try:
            from .catchup_hook import CatchUpHook
            if config.get("catchup", {}).get("enabled", True):
                self.hooks["catchup"] = CatchUpHook(config.get("catchup", {}))
        except ImportError:
            pass

        try:
            from .retry_hook import RetryHook
            if config.get("retry", {}).get("enabled", True):
                self.hooks["retry"] = RetryHook(config.get("retry", {}))
        except ImportError:
            pass

        try:
            from .notify_hook import NotifyHook
            if config.get("notify", {}).get("enabled", True):
                self.hooks["notify"] = NotifyHook(
                    config.get("notify", {}), scheduler_ref=scheduler_ref
                )
        except ImportError:
            pass

    async def before_execute(self, ctx: Any) -> Any:
        for hook in self.hooks.values():
            ctx = await hook.before_execute(ctx)
        return ctx

    async def on_failure(self, ctx: Any, error: Exception) -> RetryDecision:
        if "retry" in self.hooks:
            return await self.hooks["retry"].on_failure(ctx, error)
        return RetryDecision.give_up()

    async def after_execute(self, ctx: Any, result: Any) -> None:
        for hook in self.hooks.values():
            await hook.after_execute(ctx, result)

    def get_retry_max(self) -> int:
        """Q11: 供 ctx.retry_max 填充，NotifyHook 通知模板读取。"""
        retry_hook = self.hooks.get("retry")
        return getattr(retry_hook, "max_retries", 0) if retry_hook else 0
