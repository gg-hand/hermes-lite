"""黑板目录读写：原子写入 / 路径沙箱 / YAML safe_load / JSONL append。

设计原则：
- atomic_write：先写 .tmp 再 os.replace（同目录内原子 rename）
- append_jsonl：直接 append 模式打开（无 .tmp，因为 rename 会破坏 append-only 语义）
- validate_path_safety：拒绝绝对路径 / .. 穿越 / symlink 逃逸
- read_yaml_frontmatter：必须 safe_load，禁用 yaml.load
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Tuple

import aiofiles
import yaml

from hermes.multiagent.exceptions import PathSafetyError


async def atomic_write(path: Path, content: str) -> None:
    """原子写入：先写 .tmp 再 os.replace（同目录内原子 rename）。

    Args:
        path: 目标文件路径（必须已通过 validate_path_safety）
        content: 写入内容
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
        await f.write(content)
        await f.flush()
        os.fsync(f.fileno())
    # 同目录内 rename 原子（POSIX rename / Windows MoveFileExWithProgress）
    os.replace(tmp_path, path)


def validate_path_safety(bb_root: Path, target: Path) -> Path:
    """路径沙箱校验。

    规则：
    - 拒绝绝对路径（target 必须是相对路径或在 bb_root 内）
    - 拒绝 .. 穿越（resolved path 必须在 bb_root 内）
    - 拒绝 symlink 逃逸（target 及其父目录链不得含 symlink）

    Returns:
        规范化后的绝对路径（在 bb_root 内）

    Raises:
        PathSafetyError: 路径违规
    """
    bb_root_resolved = bb_root.resolve()
    target_str = str(target)

    # 1. 检查 symlink 逃逸（在 resolve 之前检查，保留 symlink 检测能力）
    if target.is_symlink():
        raise PathSafetyError(f"symlink escape: {target} is symlink")
    _check_symlink_in_path_chain(bb_root_resolved, target)

    # 2. 判断路径特征
    # Windows 上 Path("/etc/passwd").is_absolute() 返回 False（无盘符），
    # 但这种路径仍会逃逸 bb_root，所以需要额外检测 POSIX 风格根路径
    is_absolute_like = (
        target.is_absolute()
        or target_str.startswith("/")
        or target_str.startswith("\\")
        or (len(target_str) >= 2 and target_str[1] == ":" and target_str[0].isalpha())
    )
    has_traversal = ".." in target.parts

    # 3. 计算解析后的路径
    if target.is_absolute():
        target_resolved = target.resolve()
    else:
        target_resolved = (bb_root_resolved / target).resolve()

    # 4. 检查是否在 bb_root 内
    try:
        target_resolved.relative_to(bb_root_resolved)
    except ValueError:
        if has_traversal:
            raise PathSafetyError(f"path traversal outside bb_root: {target}")
        if is_absolute_like:
            raise PathSafetyError(f"absolute path outside bb_root: {target}")
        raise PathSafetyError(f"path outside bb_root: {target}")

    return target_resolved


def _check_symlink_in_path_chain(bb_root: Path, target: Path) -> None:
    """检查 target 路径链上是否含 symlink（防 symlink 逃逸）。

    检查 bb_root 到 target 之间的所有中间目录组件。
    """
    if target.is_absolute():
        target_abs = target
    else:
        target_abs = bb_root / target

    try:
        rel = target_abs.relative_to(bb_root)
    except ValueError:
        # target 不在 bb_root 下（可能是绝对路径或 .. 穿越），
        # 由后续 absolute/traversal 检查处理
        return

    current = bb_root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise PathSafetyError(f"symlink in path chain: {current}")


def read_json(path: Path) -> dict:
    """读取 JSON 文件。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_yaml_frontmatter(path: Path) -> Tuple[dict, str]:
    """读取 Markdown 文件的 YAML frontmatter + body。

    Returns:
        (frontmatter_dict, body_str)

    Note:
        必须用 yaml.safe_load，禁用 yaml.load（防任意代码执行）
    """
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return {}, content

    # 分割 frontmatter 和 body
    parts = content.split("---\n", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter_str = parts[1]
    body = parts[2]
    frontmatter = yaml.safe_load(frontmatter_str) or {}
    return frontmatter, body


async def append_jsonl(path: Path, record: dict) -> None:
    """直接 append 模式写入 JSONL 文件（无 .tmp）。

    注意：append-only 语义，不能写 .tmp 再 rename（rename 会覆盖已有内容）。
    串行化由调用方保证（如 audit_logger 通过 portalocker.Lock）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "a", encoding="utf-8") as f:
        await f.write(json.dumps(record, ensure_ascii=False) + "\n")
        await f.flush()
        os.fsync(f.fileno())
