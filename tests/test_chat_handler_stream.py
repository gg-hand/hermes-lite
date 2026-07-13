"""Task 11: 验证 ChatHandler.chat_stream 存在且为 async generator。

运行方式:
    python -m pytest tests/test_chat_handler_stream.py -v
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


def test_chat_stream_method_exists():
    """ChatHandler.chat_stream 方法存在。"""
    from hermes.orchestrator.chat_handler import ChatHandler
    assert hasattr(ChatHandler, "chat_stream")


def test_chat_stream_is_async_generator():
    """chat_stream 是 async generator 函数。"""
    from hermes.orchestrator.chat_handler import ChatHandler
    assert inspect.isasyncgenfunction(ChatHandler.chat_stream)


def test_orchestrator_delegates_chat_stream():
    """Orchestrator.chat_stream 委托到 ChatHandler（保持向后兼容）。"""
    from hermes.orchestrator import Orchestrator
    assert hasattr(Orchestrator, "chat_stream")
    assert inspect.isasyncgenfunction(Orchestrator.chat_stream)
