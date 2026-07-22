"""CatchUpHook（L2 补偿层，5.1，Q9 决策串行 await）。

启动时扫描过期调度项，按 catch_up_policy 处理：
- skip：跳过（默认）
- execute_once：执行一次补偿

Q9 决策：串行 await 执行补偿任务，避免与 run_loop 触发的执行竞争
cron_tool_registry / archive_callback 覆盖。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List

from .base import ScheduleHookBase

logger = logging.getLogger(__name__)


class CatchUpHook(ScheduleHookBase):
    """L2: 启动时扫描过期调度项，按 catch_up_policy 处理。"""

    name = "catchup"

    def __init__(self, config: dict):
        self.default_policy = config.get("default_policy", "skip")

    async def before_execute(self, ctx: Any) -> Any:
        # 运行时不触发，仅启动扫描调用 scan_and_compensate
        return ctx

    async def scan_and_compensate(self, scheduler) -> List[str]:
        """启动时扫描所有过期调度项，按策略处理。

        返回已补偿执行的 schedule_id 列表（skip 策略的不计入）。
        """
        now = datetime.now()
        compensated: List[str] = []
        next_run_updated = False
        for schedule in scheduler._schedules:
            if not schedule.enabled or not schedule.next_run:
                continue
            try:
                next_dt = datetime.fromisoformat(schedule.next_run)
            except (ValueError, TypeError):
                continue
            if next_dt >= now:
                continue  # 未过期

            policy = getattr(schedule, "catch_up_policy", None) or self.default_policy
            if policy == "execute_once":
                logger.info(
                    "补偿执行过期调度 %s (next_run=%s)",
                    schedule.id, schedule.next_run,
                )
                # Q9: 串行 await，非 create_task
                await scheduler._run_schedule_direct(schedule)
                compensated.append(schedule.id)
            # 无论 skip / execute_once，都更新 next_run 到下个周期
            cron_expr = scheduler._cron_exprs.get(schedule.id)
            if cron_expr:
                schedule.next_run = cron_expr.next_run(now).isoformat()
                next_run_updated = True
        # next_run 更新过就持久化（即便没有补偿执行）
        if compensated or next_run_updated:
            scheduler._persist()
        return compensated
