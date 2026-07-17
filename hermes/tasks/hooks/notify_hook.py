"""NotifyHook（L4 通知层，6.1）。

Q7 决策：result 类型为 WorkflowResult（复用现有，不引入 RunResult）。
Q8 决策：_failure_counts 放在 CronScheduler 上（配置热更新零迁移）。
Q11 决策：max_retries 从 ctx.retry_max 读取（由 HookRegistry.get_retry_max() 填充）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .base import ScheduleHookBase

logger = logging.getLogger(__name__)


class NotifyHook(ScheduleHookBase):
    """L4: 失败后邮件 + Webhook 双通道通知。连续失败自动 disable。"""

    name = "notify"

    def __init__(self, config: dict, scheduler_ref: Optional[Any] = None):
        self.threshold = config.get("consecutive_failures_threshold", 3)
        self.channels = config.get("channels", {}) or {}
        self._scheduler_ref = scheduler_ref

    def _get_failure_count(self, schedule_id: str) -> int:
        scheduler = self._scheduler_ref
        if scheduler is None:
            return 0
        return scheduler._failure_counts.get(schedule_id, 0)

    def _set_failure_count(self, schedule_id: str, count: int) -> None:
        scheduler = self._scheduler_ref
        if scheduler is None:
            return
        if count == 0:
            scheduler._failure_counts.pop(schedule_id, None)
        else:
            scheduler._failure_counts[schedule_id] = count

    async def after_execute(self, ctx: Any, result: Any) -> None:
        if result.success:
            self._set_failure_count(ctx.schedule.id, 0)
            return

        count = self._get_failure_count(ctx.schedule.id) + 1
        self._set_failure_count(ctx.schedule.id, count)

        # 发送通知（邮件 + Webhook 并行）
        sent_channels = []
        tasks = []
        if self.channels.get("email", {}).get("enabled"):
            tasks.append(self._send_email(ctx, result, count))
            sent_channels.append("email")
        if self.channels.get("webhook", {}).get("enabled"):
            tasks.append(self._send_webhook(ctx, result, count))
            sent_channels.append("webhook")
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            successful = []
            for ch, r in zip(sent_channels, results):
                if isinstance(r, Exception):
                    logger.warning("通知渠道 %s 发送失败: %s", ch, r)
                else:
                    successful.append(ch)
            # 写入 RunSummary 字段（Task 13 新增）
            if successful:
                result.notified = True
                result.notification_channels = successful

        # 连续失败达阈值 → 自动 disable
        if count >= self.threshold:
            logger.warning(
                "调度 %s 连续失败 %d 次，自动 disable",
                ctx.schedule.id, count,
            )
            ctx.schedule.enabled = False

    async def _send_email(self, ctx: Any, result: Any, consecutive_failures: int) -> None:
        """发送邮件通知。SMTP 配置从 config 读取（6.2）。"""
        from .notify_templates import render_email_html
        email_cfg = self.channels.get("email", {})
        html = render_email_html(ctx, result, consecutive_failures)
        await self._smtp_send(email_cfg, html)

    async def _send_webhook(self, ctx: Any, result: Any, consecutive_failures: int) -> None:
        """发送 Webhook 通知。"""
        from .notify_templates import render_webhook_payload
        import aiohttp
        webhook_cfg = self.channels.get("webhook", {})
        url = webhook_cfg.get("url", "")
        if not url:
            return
        payload = render_webhook_payload(ctx, result, consecutive_failures)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()

    async def _smtp_send(self, email_cfg: dict, html: str) -> None:
        """使用 aiosmtplib 异步发送邮件（6.2）。"""
        import aiosmtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"调度任务失败通知"
        msg["From"] = email_cfg.get("sender_email", "")
        msg["To"] = email_cfg.get("receiver_email", "")

        msg.attach(MIMEText(html, "html", "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=email_cfg.get("smtp_host", ""),
            port=int(email_cfg.get("smtp_port", 465)),
            username=email_cfg.get("sender_email", ""),
            password=email_cfg.get("sender_password", ""),
            use_tls=email_cfg.get("use_tls", True),
        )
