"""通知模板渲染（6.3，Q11 max_retries 从 ctx.retry_max 读取）。"""
from __future__ import annotations

from typing import Any, Dict


def render_email_html(ctx: Any, result: Any, consecutive_failures: int) -> str:
    """渲染邮件 HTML 模板。"""
    schedule = ctx.schedule
    schedule_name = getattr(schedule, "name", schedule.id)
    started_at = getattr(ctx, "started_at", "") or ""
    duration = getattr(result, "duration_seconds", 0) or 0
    retry_count = getattr(ctx, "retry_count", 0)
    retry_max = getattr(ctx, "retry_max", 0)
    error_summary = "; ".join(result.errors) if result.errors else "未知错误"

    step_rows = []
    for trace in getattr(result, "step_traces", []):
        step_rows.append(
            f"<tr><td>{trace.step_name}</td><td>{trace.status}</td>"
            f"<td>{trace.error_message or ''}</td>"
            f"<td>{trace.retry_reason or ''}</td></tr>"
        )
    step_traces_html = (
        "<table border='1'><tr><th>Step</th><th>状态</th>"
        "<th>错误</th><th>重试原因</th></tr>"
        + "".join(step_rows) + "</table>"
        if step_rows else "<p>无 step 轨迹</p>"
    )

    return f"""
<h2>调度任务失败: {schedule_name}</h2>
<hr>
<table>
  <tr><td>调度ID</td><td>{schedule.id}</td></tr>
  <tr><td>执行时间</td><td>{started_at}</td></tr>
  <tr><td>耗时</td><td>{duration}秒</td></tr>
  <tr><td>重试次数</td><td>{retry_count}/{retry_max}</td></tr>
  <tr><td>连续失败</td><td>{consecutive_failures}次</td></tr>
</table>
<h3>错误详情</h3>
<pre>{error_summary}</pre>
<h3>Step 轨迹</h3>
{step_traces_html}
"""


def render_webhook_payload(ctx: Any, result: Any, consecutive_failures: int) -> Dict[str, Any]:
    """渲染 Webhook JSON payload（兼容企业微信/钉钉/飞书 markdown 格式）。"""
    schedule = ctx.schedule
    schedule_name = getattr(schedule, "name", schedule.id)
    started_at = getattr(ctx, "started_at", "") or ""
    retry_count = getattr(ctx, "retry_count", 0)
    retry_max = getattr(ctx, "retry_max", 0)
    error_summary = "; ".join(result.errors) if result.errors else "未知错误"

    text = (
        f"## 调度任务失败\n"
        f"> **调度**: {schedule_name}\n"
        f"> **时间**: {started_at}\n"
        f"> **重试**: {retry_count}/{retry_max}\n"
        f"> **连续失败**: {consecutive_failures}次\n\n"
        f"**错误**:\n```\n{error_summary}\n```"
    )
    return {
        "msgtype": "markdown",
        "markdown": {"title": f"调度失败: {schedule_name}", "text": text},
    }
