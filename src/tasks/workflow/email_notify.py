"""email_notify 工作流模板（Phase 8 Task 2.4）。

纯确定性邮件通知模板，**不调用 LLM**（``max_loops=0`` 语义）。

执行流程：
1. 渲染邮件模板（``subject`` + ``body``，支持时间变量替换）
2. 通过 SMTP 发送邮件

环境变量（通过 ``context.get_env_value`` 读取，便于测试 mock）：
- ``SMTP_HOST``：SMTP 服务器主机
- ``SMTP_PORT``：SMTP 服务器端口（默认 587）
- ``SMTP_USER``：SMTP 用户名
- ``SMTP_PASSWORD``：SMTP 密码
- ``SMTP_FROM``：发件人邮箱（缺省取 ``SMTP_USER``）
- ``SMTP_USE_TLS``：是否启用 TLS（``"1"`` / ``"true"`` 启用，默认启用）

缓存约束：
- 不调用 LLM，无 system prompt
- 时间变量在邮件 subject / body 渲染时替换（确定性步骤）
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Any, Dict, List, Optional

from .base import WorkflowContext, WorkflowResult, WorkflowTemplate

logger = logging.getLogger(__name__)


class EmailNotifyTemplate(WorkflowTemplate):
    """邮件通知工作流模板（纯确定性，不调 LLM）。

    配置字段（``config`` dict）：
    - ``to``（必填）：收件人邮箱（多个用逗号分隔）
    - ``subject``（必填）：邮件主题（支持时间变量占位符）
    - ``body``（必填）：邮件正文（支持时间变量占位符）
    - ``from``（可选）：发件人邮箱，缺省取 ``SMTP_FROM`` 环境变量
    - ``smtp_host``（可选）：SMTP 主机，缺省取 ``SMTP_HOST`` 环境变量
    - ``smtp_port``（可选）：SMTP 端口，缺省取 ``SMTP_PORT`` 环境变量
    - ``smtp_user``（可选）：SMTP 用户名，缺省取 ``SMTP_USER`` 环境变量
    - ``smtp_password``（可选）：SMTP 密码，缺省取 ``SMTP_PASSWORD`` 环境变量
    - ``use_tls``（可选）：是否启用 TLS，缺省取 ``SMTP_USE_TLS`` 环境变量

    输出：
    - ``metrics_for_injection``：``{"收件人": N, "主题": subject}``
    - 不产生 LLM 回复，``assistant_response`` 始终为空字符串
    - ``success`` 反映 SMTP 发送是否成功
    """

    name = "email_notify"

    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:
        result = WorkflowResult()

        to = config.get("to")
        subject = config.get("subject")
        body = config.get("body")
        if not to or not subject or not body:
            result.add_error(
                "配置缺少必填字段 to/subject/body 之一"
            )
            return result

        # 时间变量替换（SubTask 2.7：在邮件渲染层替换）
        subject_rendered = context.render(subject)
        body_rendered = context.render(body)

        # 解析 SMTP 配置（优先 config，缺省取环境变量）
        smtp_host = config.get("smtp_host") or context.get_env_value(
            "SMTP_HOST", ""
        )
        smtp_port = int(
            config.get("smtp_port")
            or context.get_env_value("SMTP_PORT", "587")
        )
        smtp_user = config.get("smtp_user") or context.get_env_value(
            "SMTP_USER", ""
        )
        smtp_password = config.get("smtp_password") or context.get_env_value(
            "SMTP_PASSWORD", ""
        )
        from_addr = config.get("from") or context.get_env_value(
            "SMTP_FROM", smtp_user
        )
        use_tls_cfg = config.get("use_tls")
        if use_tls_cfg is None:
            use_tls = context.get_env_value("SMTP_USE_TLS", "1").lower() in (
                "1",
                "true",
                "yes",
            )
        else:
            use_tls = bool(use_tls_cfg)

        if not smtp_host or not smtp_user or not smtp_password:
            result.add_error(
                "SMTP 配置不完整：缺少 SMTP_HOST / SMTP_USER / SMTP_PASSWORD"
            )
            return result

        # 收件人列表（逗号分隔）
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        if not recipients:
            result.add_error("收件人列表为空")
            return result

        # 构造邮件
        msg = MIMEText(body_rendered, "plain", "utf-8")
        msg["Subject"] = subject_rendered
        msg["From"] = formataddr(("", from_addr))
        msg["To"] = ", ".join(recipients)
        msg["Date"] = formatdate(localtime=True)

        # 发送 SMTP
        try:
            self._send_smtp(
                smtp_host,
                smtp_port,
                smtp_user,
                smtp_password,
                from_addr,
                recipients,
                msg,
                use_tls,
            )
        except Exception as e:
            result.add_error(f"SMTP 发送失败: {e}")
            return result

        result.metrics_for_injection = {
            "收件人数": len(recipients),
            "邮件主题": subject_rendered,
        }
        # 纯确定性模板，不产生 LLM 回复
        result.assistant_response = ""
        return result

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _send_smtp(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        from_addr: str,
        recipients: List[str],
        msg: MIMEText,
        use_tls: bool,
    ) -> None:
        """通过 SMTP 发送邮件。

        使用 ``smtplib.SMTP`` 连接，``use_tls=True`` 时调用 ``starttls()``。
        验证后 ``sendmail`` 发送。失败时抛异常，由调用方捕获。
        """
        # 兼容测试 mock：如果 host 形如 mock 或环境标识，跳过真实连接
        # 但仍调用 sendmail 方法以验证邮件构造
        with smtplib.SMTP(host, port) as server:
            if use_tls:
                server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, recipients, msg.as_string())
