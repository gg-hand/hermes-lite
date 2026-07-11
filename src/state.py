"""共享运行时状态。

lifespan 启动时写入，路由 handler 运行时读取。
避免 server.py 与 routes/ 之间的循环依赖和模块标识问题。
"""
from __future__ import annotations

from typing import Any, Optional

orchestrator: Optional[Any] = None
session_logger: Optional[Any] = None
metrics_collector: Optional[Any] = None
metrics_store: Optional[Any] = None
audit_logger: Optional[Any] = None
approval_manager: Optional[Any] = None
task_manager: Optional[Any] = None
cron_scheduler: Optional[Any] = None
proposal_store: Optional[Any] = None
health_checker: Optional[Any] = None
stream_manager: Optional[Any] = None
skill_loader: Optional[Any] = None
mcp_manager: Optional[Any] = None
upload_manager: Optional[Any] = None
etl_engine: Optional[Any] = None
file_context_injector: Optional[Any] = None
soft_restart_in_progress: bool = False
