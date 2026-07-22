"""向后兼容入口：静态文件挂载已移至 teage_liu.app。

保留此文件仅为兼容旧脚本 `python src/server.py`（现已迁移到 hermes/）。
新入口请使用 `python -m hermes` 或 `uvicorn teage_liu.app:app`。
"""
from __future__ import annotations

from teage_liu.app import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn
    from teage_liu.config import load_config

    cfg = load_config("config.yaml")
    server_cfg = cfg.get("server", {}) or {}
    uvicorn.run(
        "teage_liu.app:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8000)),
        reload=False,
    )
