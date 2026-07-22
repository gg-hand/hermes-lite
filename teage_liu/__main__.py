"""python -m hermes 入口：启动 uvicorn 服务。

等价于 `python -m uvicorn teage_liu.app:app --host 0.0.0.0 --port 8000`，
但通过 __main__ 提供统一入口，方便部署脚本和桌面端 sidecar 调用。
"""
from __future__ import annotations

import os

import uvicorn

from teage_liu.app import app  # noqa: F401  确保 app 模块加载（含静态文件挂载）
from teage_liu.config import load_config

CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")


def main() -> None:
    cfg = load_config(CONFIG_PATH)
    server_cfg = cfg.get("server", {}) or {}
    host = server_cfg.get("host", "0.0.0.0")
    port = int(server_cfg.get("port", 8000))

    log_path = os.environ.get("HERMES_SERVER_LOG", "data/server.log")
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    config = uvicorn.Config(
        app="teage_liu.app:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
