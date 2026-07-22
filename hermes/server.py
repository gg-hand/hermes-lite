"""向后兼容入口：静态文件挂载已移至 hermes.app。

保留此文件仅为兼容旧脚本 `python src/server.py`（现已迁移到 hermes/）。
新入口请使用 `python -m hermes` 或 `uvicorn hermes.app:app`。
"""
from __future__ import annotations

from hermes.app import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn
    from hermes.config import load_config

    cfg = load_config("config.yaml")
    server_cfg = cfg.get("server", {}) or {}
    uvicorn.run(
        "hermes.app:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8000)),
        reload=False,
    )
