"""文件操作工具：file_read, file_write, file_delete, file_listdir, file_edit, file_glob, file_grep。

Re-export 自 builtin_tools.py，后续将迁移函数体到此文件。
"""
from __future__ import annotations

# 兼容相对导入与直接运行
try:
    from ..builtin_tools import (
        read_file,
        write_file,
        delete_file,
        list_directory,
        file_edit,
        file_glob,
        file_grep,
        register_file_tools,
    )
except ImportError:  # pragma: no cover
    from agent.builtin_tools import (  # type: ignore
        read_file,
        write_file,
        delete_file,
        list_directory,
        file_edit,
        file_glob,
        file_grep,
        register_file_tools,
    )

__all__ = [
    "read_file",
    "write_file",
    "delete_file",
    "list_directory",
    "file_edit",
    "file_glob",
    "file_grep",
    "register_file_tools",
]
