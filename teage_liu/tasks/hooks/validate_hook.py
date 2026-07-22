"""ValidateHook（L1 校验层，4.1）。

Q14 决策：validate.enabled=False 只跳过执行前校验，
创建时与启动时仍校验（数据完整性不受运行时开关影响）。
"""
from __future__ import annotations

from typing import Any

from .base import ScheduleHookBase


class ValidateHook(ScheduleHookBase):
    """L1: 执行前校验 workflow spec。"""

    name = "validate"

    def __init__(self, config: dict):
        self.config = config

    async def before_execute(self, ctx: Any) -> Any:
        schedule = getattr(ctx, "schedule", None)
        if not schedule or not getattr(schedule, "workflow", None):
            return ctx  # legacy 路径不校验

        from teage_liu.tasks.workflow.validator import validate_workflow_spec
        from teage_liu.tasks.workflow.spec import WorkflowSpec

        # schedule.workflow 可能是 dict（YAML 加载）或 WorkflowSpec 实例
        wf = schedule.workflow
        if isinstance(wf, dict):
            try:
                wf = WorkflowSpec.from_dict(wf)
            except ValueError as e:
                ctx.validation_errors = [f"workflow 配置解析失败: {e}"]
                return ctx

        errors = validate_workflow_spec(
            spec=wf,
            cron_tool_registry=getattr(ctx, "cron_tool_registry", None),
            tool_registry=getattr(ctx, "tool_registry", None),
        )
        if errors:
            ctx.validation_errors = errors
        return ctx
