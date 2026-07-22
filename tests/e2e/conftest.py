"""tests/e2e 公共 fixtures。

Plan 4 前端 UI 测试需要运行中的 teage-liu 服务。默认情况下，若服务不可达
或 Playwright 浏览器未安装，所有 e2e 测试自动跳过。
"""
from __future__ import annotations

import socket

import pytest


def _server_reachable(url: str) -> bool:
    """检查 teage-liu 服务是否可达。"""
    try:
        # 解析 URL 中的 host:port
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except Exception:
        return False


def pytest_configure(config):
    """注册自定义标记。"""
    config.addinivalue_line(
        "markers", "e2e: end-to-end test (requires running teage-liu server + Playwright browser)"
    )


def pytest_collection_modifyitems(config, items):
    """e2e 测试默认跳过，除非通过 -m e2e 显式选择。"""
    marker_expr = config.getoption("-m") or ""
    if "e2e" not in marker_expr:
        skip_marker = pytest.mark.skip(reason="e2e test, run with -m e2e")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_marker)
        return

    # 显式选择 e2e 时，若服务不可达也跳过
    hermes_url = "http://127.0.0.1:18394"
    if not _server_reachable(hermes_url):
        skip_marker = pytest.mark.skip(reason=f"teage-liu server not reachable at {hermes_url}")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_marker)
        return

    # 检查 Playwright 浏览器是否可用
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        skip_marker = pytest.mark.skip(reason="playwright not installed")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_marker)


@pytest.fixture
def hermes_app_url() -> str:
    """teage-liu 服务 URL。"""
    return "http://127.0.0.1:18394"
