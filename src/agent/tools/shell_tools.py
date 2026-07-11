"""Shell 工具：execute_command, kill_running_process。

Re-export 自 builtin_tools.py，后续将迁移函数体到此文件。
"""
from __future__ import annotations

try:
    from ..builtin_tools import (
        execute_command,
        kill_running_process,
        _has_shell_metachar,
        _set_running_proc,
        _get_and_clear_running_proc,
        _maybe_rewrite_multiline_python_c,
        _execute_command_inner,
        register_bash_tool,
    )
except ImportError:  # pragma: no cover
    from agent.builtin_tools import (  # type: ignore
        execute_command,
        kill_running_process,
        _has_shell_metachar,
        _set_running_proc,
        _get_and_clear_running_proc,
        _maybe_rewrite_multiline_python_c,
        _execute_command_inner,
        register_bash_tool,
    )

__all__ = [
    "execute_command",
    "kill_running_process",
    "register_bash_tool",
]
