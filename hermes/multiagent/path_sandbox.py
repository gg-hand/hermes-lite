"""路径沙箱：所有跨设备请求中的路径字段必须为相对路径，禁止绝对路径和路径穿越。

设计原则：
- 字符串层面快速检查（不访问文件系统）
- 配合 PolicyEngine 的 symlink 检测（运行时）
- 递归处理 dict 中的路径字段
"""
from __future__ import annotations

import re
from pathlib import Path


class PathSandboxError(Exception):
    """路径沙箱违规。"""


# 路径字段名集合（递归 sanitize 时识别）
PATH_FIELDS = frozenset({
    "path", "file_path", "target", "src", "dst",
    "lock_name", "source", "destination",
})

# 绝对路径模式：Unix / 开头，或 Windows X:\ 开头
_ABSOLUTE_UNIX = re.compile(r"^/")
_ABSOLUTE_WINDOWS = re.compile(r"^[a-zA-Z]:[\\/]")


def sanitize_path(path_str: str, bb_root: Path) -> str:
    """检查路径字符串，确保是相对路径且无穿越。

    Args:
        path_str: 待检查的路径字符串。
        bb_root: Blackboard 根目录（用于错误消息，不实际访问）。

    Returns:
        规范化后的相对路径字符串。

    Raises:
        PathSandboxError: 路径违规（绝对路径或穿越）。
    """
    if not isinstance(path_str, str) or not path_str:
        raise PathSandboxError(f"Empty or non-string path: {path_str!r}")

    # 检查绝对路径
    if _ABSOLUTE_UNIX.match(path_str):
        raise PathSandboxError(f"Absolute path not allowed: {path_str}")
    if _ABSOLUTE_WINDOWS.match(path_str):
        raise PathSandboxError(f"Absolute path not allowed: {path_str}")

    # 检查路径穿越（..）
    # 将反斜杠统一为正斜杠
    normalized = path_str.replace("\\", "/")
    parts = normalized.split("/")
    if ".." in parts:
        raise PathSandboxError(f"Path traversal not allowed: {path_str}")

    return path_str


def to_absolute(relative_path: str, bb_root: Path) -> Path:
    """将相对路径解析为绝对路径（在 bb_root 下）。

    Args:
        relative_path: 已通过 sanitize_path 检查的相对路径。
        bb_root: Blackboard 根目录。

    Returns:
        解析后的绝对路径。

    Raises:
        PathSandboxError: 路径违规。
    """
    safe = sanitize_path(relative_path, bb_root)
    return (bb_root / safe).resolve()


def sanitize_dict_paths(data: dict, bb_root: Path) -> None:
    """递归处理 dict 中的路径字段。

    原地修改 data，对每个路径字段调用 sanitize_path 检查。

    Args:
        data: 待处理的 dict。
        bb_root: Blackboard 根目录。

    Raises:
        PathSandboxError: 任一路径字段违规。
    """
    for key, value in list(data.items()):
        if key in PATH_FIELDS and isinstance(value, str):
            data[key] = sanitize_path(value, bb_root)
        elif isinstance(value, dict):
            sanitize_dict_paths(value, bb_root)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    sanitize_dict_paths(item, bb_root)
