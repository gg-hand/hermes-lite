"""统一异常处理测试（Task 6）。

验证全局异常处理器将 ToolError/ConfigError 子类映射为正确的 HTTP 状态码和 JSON 响应。
"""
from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from teage_liu.errors import (  # noqa: E402
    ToolNotFoundError,
    ToolExecutionError,
    ToolPermissionDenied,
    ToolTimeoutError,
    ConfigValidationError,
    ConfigReloadError,
    ContainerConfigError,
)


def _make_test_app() -> FastAPI:
    """创建注册了全局异常处理器的测试 app。"""
    from teage_liu.app import register_exception_handlers
    app = FastAPI()

    @app.get("/raise/tool_not_found")
    def _raise_tool_not_found():
        raise ToolNotFoundError("工具 'foo' 未加载")

    @app.get("/raise/permission_denied")
    def _raise_permission_denied():
        raise ToolPermissionDenied("高危操作被拦截")

    @app.get("/raise/timeout")
    def _raise_timeout():
        raise ToolTimeoutError("工具执行超时")

    @app.get("/raise/tool_execution")
    def _raise_tool_execution():
        raise ToolExecutionError("工具执行失败: 内部错误")

    @app.get("/raise/config_validation")
    def _raise_config_validation():
        raise ConfigValidationError("llm.model 字段缺失")

    @app.get("/raise/config_reload")
    def _raise_config_reload():
        raise ConfigReloadError("热重载重建失败")

    @app.get("/raise/container_config")
    def _raise_container_config():
        raise ContainerConfigError("检测到循环依赖: a → b → a")

    register_exception_handlers(app)
    return app


class TestToolErrorHandling:
    def test_tool_not_found_returns_404(self):
        client = TestClient(_make_test_app())
        resp = client.get("/raise/tool_not_found")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "tool_not_found"
        assert "foo" in body["message"]

    def test_permission_denied_returns_403(self):
        client = TestClient(_make_test_app())
        resp = client.get("/raise/permission_denied")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "permission_denied"

    def test_timeout_returns_504(self):
        client = TestClient(_make_test_app())
        resp = client.get("/raise/timeout")
        assert resp.status_code == 504
        body = resp.json()
        assert body["error"] == "tool_timeout"

    def test_tool_execution_returns_500(self):
        client = TestClient(_make_test_app())
        resp = client.get("/raise/tool_execution")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "tool_execution_error"


class TestConfigErrorHandling:
    def test_config_validation_returns_400(self):
        client = TestClient(_make_test_app())
        resp = client.get("/raise/config_validation")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "config_validation_error"

    def test_config_reload_returns_500(self):
        client = TestClient(_make_test_app())
        resp = client.get("/raise/config_reload")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "config_reload_error"

    def test_container_config_returns_500(self):
        client = TestClient(_make_test_app())
        resp = client.get("/raise/container_config")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "container_config_error"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
