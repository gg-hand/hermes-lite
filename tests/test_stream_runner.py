"""Task 13: 验证 StreamRunner 类可独立导入。

运行方式:
    python -m pytest tests/test_stream_runner.py -v
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


def test_stream_runner_class_exists():
    """StreamRunner 类存在。"""
    from hermes.agent.stream_runner import StreamRunner
    assert StreamRunner is not None


def test_run_stream_is_async_generator():
    """run_stream 是 async generator 函数。"""
    from hermes.agent.stream_runner import StreamRunner
    assert inspect.isasyncgenfunction(StreamRunner.run_stream)


def test_react_loop_delegates_run_stream():
    """ReactLoop.run_stream 委托到 StreamRunner（保持向后兼容）。"""
    from hermes.agent.react_loop import ReactLoop
    assert hasattr(ReactLoop, "run_stream")
    assert inspect.isasyncgenfunction(ReactLoop.run_stream)
