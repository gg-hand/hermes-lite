"""记忆管理工具：search_memory, profile_update, delete_memory, update_memory。

Re-export 自 builtin_tools.py，后续将迁移函数体到此文件。

注：``_search_memory`` / ``_delete_memory`` / ``_update_memory`` /
``_update_profile`` 是 ``register_memory_tools`` / ``_register_update_profile``
内部的 closure（嵌套函数），无法在模块级导入。如需访问，请通过
``register_memory_tools`` 注入后由 registry 取出 handler。
"""
from __future__ import annotations

try:
    from ..builtin_tools import (
        register_memory_tools,
        _register_update_profile,
    )
except ImportError:  # pragma: no cover
    from agent.builtin_tools import (  # type: ignore
        register_memory_tools,
        _register_update_profile,
    )

__all__ = [
    "register_memory_tools",
    "_register_update_profile",
]
