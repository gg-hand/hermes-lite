"""Plan 模式工具：plan_task, update_todo。

Re-export 自 builtin_tools.py，后续将迁移函数体到此文件。

注：``_plan_task`` / ``_update_todo`` 是 ``register_plan_tools`` 内部的
closure（嵌套函数），无法在模块级导入。如需访问，请通过
``register_plan_tools`` 注入后由 registry 取出 handler。
"""
from __future__ import annotations

try:
    from ..builtin_tools import register_plan_tools
except ImportError:  # pragma: no cover
    from agent.builtin_tools import register_plan_tools  # type: ignore

__all__ = [
    "register_plan_tools",
]
