"""EC2 长驻 HTTP 服务入口。

重构后：全局组件变量和 state 代理已删除，Container 成为唯一真相源。
路由通过 Depends(get_xxx) 获取组件。server.py 仅保留常量 + 静态文件挂载。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------- 常量 ----------
VERSION = "0.1.0"
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")
SERVER_LOG_PATH = os.environ.get("HERMES_SERVER_LOG", "data/server.log")
SKILL_STATE_PATH = "data/skills_state.json"

# ---------- 日志 / 应用 / 静态文件 ----------
from hermes.logging_setup import logger  # noqa: E402, F401
from hermes.app import app  # noqa: E402, F401
from fastapi.staticfiles import StaticFiles  # noqa: E402

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
_STATIC_DIR = os.path.join(_WEB_DIR, "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
elif os.path.isdir(_WEB_DIR):
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")
