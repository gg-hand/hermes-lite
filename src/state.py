"""共享运行时状态。

lifespan 启动时写入，路由 handler 运行时读取。
避免 server.py 与 routes/ 之间的循环依赖和模块标识问题。

兼容性机制（PEP 562）：代理属性未在 state 模块 ``__dict__`` 中时，
``__getattr__`` 自动回退到 server 模块的对应全局变量。这确保：
- 生产环境：lifespan 执行 ``state.xxx = value`` 后，属性进入 ``__dict__``，
  后续读取直接命中，不触发 ``__getattr__``。
- 测试环境：lifespan 未运行，state ``__dict__`` 无该属性，``__getattr__``
  回退到 ``server.xxx``（即测试 patch 的值）。
"""
from __future__ import annotations

import sys
from typing import Any

_PROXY_ATTRS = frozenset({
    "orchestrator", "session_logger", "metrics_collector", "metrics_store",
    "audit_logger", "approval_manager", "task_manager", "cron_scheduler",
    "proposal_store", "health_checker", "stream_manager", "skill_loader",
    "mcp_manager", "upload_manager", "etl_engine", "file_context_injector",
    "soft_restart_in_progress", "metrics_baseline_reset",
})


def __getattr__(name: str) -> Any:
    if name not in _PROXY_ATTRS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    server_mod = sys.modules.get("src.server") or sys.modules.get("server")
    if server_mod is not None:
        return getattr(server_mod, name, None)
    return None
