"""pytest 全局配置：确保 teage_liu 包可导入。

在所有测试模块导入之前，将项目根目录加入 sys.path，
使 ``from teage_liu.xxx import ...`` 绝对导入在测试中可用。

另注册 ``integration`` 标记：默认 skip，需 ``--run-integration`` 显式启用。
用于 N-worker 协作长跑验证（依赖真实双 worker 服务在 8000/8001 启动）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = PROJECT_ROOT / "teage_liu"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if str(PKG_DIR) not in sys.path:
    sys.path.insert(0, str(PKG_DIR))


def pytest_configure(config):
    """注册自定义标记，避免 pytest unknown marker 警告。"""
    config.addinivalue_line(
        "markers",
        "integration: 集成测试（需真实服务，默认 skip，用 --run-integration 启用）",
    )


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行 integration 标记的集成测试（默认跳过）",
    )


def pytest_collection_modifyitems(config, items):
    """未传 --run-integration 时，跳过所有 integration 标记的用例。"""
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(
        reason="集成测试，需 --run-integration 选项运行"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
