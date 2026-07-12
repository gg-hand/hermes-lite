"""Task 14: 验证 SyncRunner 类可独立导入。

运行方式:
    python -m pytest tests/test_sync_runner.py -v
"""

from __future__ import annotations

import inspect
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()


def test_sync_runner_class_exists():
    """SyncRunner 类存在。"""
    from src.agent.sync_runner import SyncRunner
    assert SyncRunner is not None


def test_run_is_coroutine():
    """run 方法是 async 函数。"""
    from src.agent.sync_runner import SyncRunner
    assert inspect.iscoroutinefunction(SyncRunner.run)


def test_react_loop_delegates_run():
    """ReactLoop.run 委托到 SyncRunner（保持向后兼容）。"""
    from src.agent.react_loop import ReactLoop
    assert hasattr(ReactLoop, "run")
    assert inspect.iscoroutinefunction(ReactLoop.run)
