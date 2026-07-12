"""Task 1: 验证 src.agent.builtin_tools.py shim 已删除。

删除后：
- `import src.agent.builtin_tools` 应失败（ModuleNotFoundError，ImportError 子类）
- `src.agent.tools` 仍提供 register_builtin_tools / BUILTIN_TOOLS
- test_plan_tools.py 中 `from src.agent import builtin_tools` 改为别名 `tools as builtin_tools`
"""
from __future__ import annotations

import importlib

import pytest


def test_builtin_tools_module_not_importable():
    """src.agent.builtin_tools 模块应已删除，import 失败。"""
    with pytest.raises(ImportError):
        importlib.import_module("src.agent.builtin_tools")


def test_tools_module_still_provides_register_builtin_tools():
    """src.agent.tools 仍提供 register_builtin_tools。"""
    from src.agent.tools import register_builtin_tools, BUILTIN_TOOLS
    assert callable(register_builtin_tools)
    assert isinstance(BUILTIN_TOOLS, list)


def test_tools_module_still_provides_plan_tools_imports():
    """src.agent.tools.plan_tools 仍可正常导入 register_plan_tools。"""
    from src.agent.tools.plan_tools import register_plan_tools
    assert callable(register_plan_tools)
