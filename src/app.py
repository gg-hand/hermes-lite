"""FastAPI 应用入口。

过渡期:app 从 server.py 导入,后续阶段将逐步迁移路由到 routes/ 包。
"""
from __future__ import annotations

# 过渡期:app 和 lifespan 仍在 server.py 中
# 后续 Task 3-5 完成后,将迁移 lifespan 和路由注册到此文件
from server import app  # noqa: F401

# 当作为主模块运行时
if __name__ == "__main__":
    import uvicorn

    config_path = __import__("os").environ.get("HERMES_CONFIG", "config.yaml")
    try:
        from config import load_config
        cfg = load_config(config_path)
        server_cfg = cfg.get("server", {})
        host = server_cfg.get("host", "0.0.0.0")
        port = server_cfg.get("port", 8000)
    except Exception:
        host, port = "0.0.0.0", 8000

    uvicorn.run(app, host=host, port=port)
