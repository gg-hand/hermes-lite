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


# ---------------------------------------------------------------------------
# 全局异常处理器（Task 6）
# ---------------------------------------------------------------------------

def register_exception_handlers(app) -> None:
    """注册统一异常处理器，将 ToolError/ConfigError 子类映射为 HTTP 响应。

    在 server.py 的 lifespan 中调用（或测试中手动调用）。

    映射表:
    - ToolNotFoundError → 404 {"error": "tool_not_found"}
    - ToolPermissionDenied → 403 {"error": "permission_denied"}
    - ToolTimeoutError → 504 {"error": "tool_timeout"}
    - ToolExecutionError → 500 {"error": "tool_execution_error"}
    - ConfigValidationError → 400 {"error": "config_validation_error"}
    - ConfigReloadError → 500 {"error": "config_reload_error"}
    - ContainerConfigError → 500 {"error": "container_config_error"}
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from errors import (
        ToolNotFoundError, ToolExecutionError, ToolPermissionDenied,
        ToolTimeoutError, ConfigValidationError, ConfigReloadError,
        ContainerConfigError,
    )

    @app.exception_handler(ToolNotFoundError)
    async def _handle_tool_not_found(request: Request, exc: ToolNotFoundError):
        return JSONResponse(status_code=404, content={
            "error": "tool_not_found", "message": str(exc),
        })

    @app.exception_handler(ToolPermissionDenied)
    async def _handle_permission_denied(request: Request, exc: ToolPermissionDenied):
        return JSONResponse(status_code=403, content={
            "error": "permission_denied", "message": str(exc),
        })

    @app.exception_handler(ToolTimeoutError)
    async def _handle_timeout(request: Request, exc: ToolTimeoutError):
        return JSONResponse(status_code=504, content={
            "error": "tool_timeout", "message": str(exc),
        })

    @app.exception_handler(ToolExecutionError)
    async def _handle_tool_execution(request: Request, exc: ToolExecutionError):
        return JSONResponse(status_code=500, content={
            "error": "tool_execution_error", "message": str(exc),
        })

    @app.exception_handler(ConfigValidationError)
    async def _handle_config_validation(request: Request, exc: ConfigValidationError):
        return JSONResponse(status_code=400, content={
            "error": "config_validation_error", "message": str(exc),
        })

    @app.exception_handler(ConfigReloadError)
    async def _handle_config_reload(request: Request, exc: ConfigReloadError):
        return JSONResponse(status_code=500, content={
            "error": "config_reload_error", "message": str(exc),
        })

    @app.exception_handler(ContainerConfigError)
    async def _handle_container_config(request: Request, exc: ContainerConfigError):
        return JSONResponse(status_code=500, content={
            "error": "container_config_error", "message": str(exc),
        })


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
