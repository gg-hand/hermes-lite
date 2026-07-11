"""FastAPI 应用入口。

过渡期:app 从 server.py 导入,后续阶段将逐步迁移路由到 routes/ 包。
Task 5: 增加 DI 容器初始化 + get_container() 供热重载使用。
"""
from __future__ import annotations

from typing import Optional

# 过渡期:app 和 lifespan 仍在 server.py 中
# 后续 Task 3-5 完成后,将迁移 lifespan 和路由注册到此文件
from server import app  # noqa: F401

# ---------------------------------------------------------------------------
# DI 容器（Task 5）
# ---------------------------------------------------------------------------

_container = None


def init_container(config: dict):
    """初始化全局 DI 容器。

    在 server.py 的 lifespan 中调用。容器注册的组件目前仅用于热重载
    （PUT /config 时检测变更段并重建受影响组件）。Orchestrator 等核心
    组件仍由 server.py 直接管理，容器作为过渡层逐步接入。

    参数:
        config: 已解析的配置字典。
    """
    global _container
    from container import Container
    _container = Container(config)


def get_container():
    """返回全局 DI 容器，未初始化时返回 None。"""
    return _container


def close_container() -> None:
    """关闭容器并释放资源（在 lifespan 的 yield 之后调用）。"""
    global _container
    if _container is not None:
        try:
            _container.close()
        except Exception:
            pass
        _container = None


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
