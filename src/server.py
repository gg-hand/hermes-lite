"""EC2 长驻 HTTP 服务入口。

所有路由、lifespan、后台循环、配置工具已提取到独立模块（app.py /
lifespan.py / background_loops.py / config_helpers.py / logging_setup.py /
routes/）。server.py 仅保留全局组件变量声明（供测试 patch 与 state.py
代理）+ 配置工具 re-export + 静态文件挂载 + uvicorn 入口。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

# 确保 src/ 在 sys.path 中（直接导入 logging_setup/config_helpers 等模块时需要）
_SRC_DIR = str(Path(__file__).resolve().parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# ---------- 常量 ----------
VERSION = "0.1.0"
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")
SERVER_LOG_PATH = os.environ.get("HERMES_SERVER_LOG", "data/server.log")
SKILL_STATE_PATH = "data/skills_state.json"

# ---------- 日志 ----------
from logging_setup import logger  # noqa: E402, F401

# ---------- 配置模块（routes/config.py 通过 srv.load_config / srv.clear_config_cache 访问） ----------
from config import load_config, clear_config_cache  # noqa: E402, F401

# ---------- 全局组件 ----------
# 在 lifespan.py 中初始化，全局复用。保留在此处供测试 patch 与 state.py 代理。
orchestrator: Optional[Any] = None; session_logger: Optional[Any] = None
metrics_collector: Optional[Any] = None; metrics_store: Optional[Any] = None
metrics_persist_task: Optional[Any] = None; _metrics_baseline_reset: bool = False
audit_logger: Optional[Any] = None; approval_manager: Optional[Any] = None
skill_loader = None; skill_tools_registered = False; mcp_manager = None
task_manager: Optional[Any] = None; cron_scheduler: Optional[Any] = None
proposal_store: Optional[Any] = None; cron_tool_registry: Optional[Any] = None
health_checker: Optional[Any] = None; stream_manager: Optional[Any] = None
upload_manager: Optional[Any] = None; etl_engine: Optional[Any] = None
file_context_injector: Optional[Any] = None

# ---------- 配置工具函数（供 tests/test_config_update.py 从 server 导入） ----------
from config_helpers import (  # noqa: E402
    _RESTART_REQUIRED_KEYS,
    _MISSING,
    _config_write_lock,
    _RUNTIME_HOTUPDATE_MAP,
    _deep_merge_config,
    _backup_config,
    _atomic_write_config,
    _validate_config_schema,
    _check_needs_restart,
    _apply_runtime_config,
)

# ---------- 后台循环 re-export（供 tests/test_server.py 通过 srv.metrics_persist_loop 访问） ----------
from background_loops import metrics_persist_loop  # noqa: E402, F401

# ---------- FastAPI 应用 ----------
from app import app  # noqa: E402, F401

# ---------- 静态文件服务（Web 前端） ----------
from fastapi.staticfiles import StaticFiles  # noqa: E402

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
_STATIC_DIR = os.path.join(_WEB_DIR, "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
elif os.path.isdir(_WEB_DIR):
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")


# ---------- 入口 ----------
if __name__ == "__main__":
    import uvicorn
    from config import load_config

    try:
        cfg = load_config(CONFIG_PATH)
        server_cfg = cfg.get("server", {})
    except Exception:
        server_cfg = {}

    uvicorn.run(
        "src.server:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8000)),
        workers=1,
    )
