# hermes/__main__.py
"""python -m hermes 入口。"""
import os
import uvicorn
from hermes.config import load_config
from hermes.app import app

if __name__ == "__main__":
    config_path = os.environ.get("HERMES_CONFIG", "config.yaml")
    try:
        cfg = load_config(config_path)
        server_cfg = cfg.get("server", {})
    except Exception:
        server_cfg = {}
    uvicorn.run(
        app,
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8000)),
    )
