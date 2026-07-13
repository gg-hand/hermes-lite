"""Task 12: 验证 orchestrator.factories 模块可独立导入。

运行方式:
    python -m pytest tests/test_orchestrator_factories.py -v
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


def test_factories_module_exists():
    """orchestrator.factories 模块存在。"""
    from hermes.orchestrator import factories
    assert factories is not None


def test_create_llm_client_exists():
    """create_llm_client 工厂函数存在。"""
    from hermes.orchestrator.factories import create_llm_client
    assert callable(create_llm_client)


def test_create_tool_registry_exists():
    """create_tool_registry 工厂函数存在。"""
    from hermes.orchestrator.factories import create_tool_registry
    assert callable(create_tool_registry)


def test_create_memory_subsystem_exists():
    """create_memory_subsystem 工厂函数存在。"""
    from hermes.orchestrator.factories import create_memory_subsystem
    assert callable(create_memory_subsystem)


def test_create_security_subsystem_exists():
    """create_security_subsystem 工厂函数存在。"""
    from hermes.orchestrator.factories import create_security_subsystem
    assert callable(create_security_subsystem)


def test_create_react_loop_exists():
    """create_react_loop 工厂函数存在。"""
    from hermes.orchestrator.factories import create_react_loop
    assert callable(create_react_loop)


def test_orchestrator_init_under_250_lines():
    """Orchestrator.__init__ 应小于 250 行（从 515 行简化）。"""
    from hermes.orchestrator import Orchestrator
    source = inspect.getsource(Orchestrator.__init__)
    line_count = source.count("\n")
    assert line_count < 250, f"__init__ 仍有 {line_count} 行，目标 <250"
