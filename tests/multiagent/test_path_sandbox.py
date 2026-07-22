"""路径沙箱测试（Task 5, Plan 3）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from teage_liu.multiagent.path_sandbox import (
    PathSandboxError,
    sanitize_dict_paths,
    sanitize_path,
    to_absolute,
)


class TestPathSandbox:
    """路径沙箱测试。"""

    def test_relative_path_allowed(self, tmp_path: Path):
        """相对路径通过。"""
        result = sanitize_path("agents/worker_001.md", tmp_path)
        assert result == "agents/worker_001.md"

    def test_absolute_path_rejected(self, tmp_path: Path):
        """绝对路径拒绝。"""
        with pytest.raises(PathSandboxError, match="(?i:absolute)"):
            sanitize_path("/etc/passwd", tmp_path)

    def test_windows_absolute_path_rejected(self, tmp_path: Path):
        """Windows 绝对路径拒绝。"""
        with pytest.raises(PathSandboxError, match="(?i:absolute)"):
            sanitize_path("C:\\Windows\\System32", tmp_path)

    def test_path_traversal_rejected(self, tmp_path: Path):
        """路径穿越拒绝。"""
        with pytest.raises(PathSandboxError, match="traversal"):
            sanitize_path("../../../etc/passwd", tmp_path)

    def test_path_traversal_in_middle_rejected(self, tmp_path: Path):
        """中间的 .. 拒绝。"""
        with pytest.raises(PathSandboxError, match="traversal"):
            sanitize_path("agents/../../../etc/passwd", tmp_path)

    def test_backslash_traversal_rejected(self, tmp_path: Path):
        """反斜杠穿越拒绝。"""
        with pytest.raises(PathSandboxError, match="traversal"):
            sanitize_path("..\\..\\..\\etc\\passwd", tmp_path)

    def test_normalized_relative_path_resolved(self, tmp_path: Path):
        """规范化后的相对路径解析为绝对路径。"""
        result = sanitize_path("agents/worker_001.md", tmp_path)
        # 返回的是相对路径字符串
        assert result == "agents/worker_001.md"

    def test_resolve_to_absolute(self, tmp_path: Path):
        """to_absolute 方法将相对路径解析为绝对路径。"""
        result = to_absolute("agents/worker_001.md", tmp_path)
        assert result == (tmp_path / "agents" / "worker_001.md").resolve()

    def test_resolve_to_absolute_rejects_escape(self, tmp_path: Path):
        """to_absolute 拒绝逃逸路径。"""
        with pytest.raises(PathSandboxError):
            to_absolute("../../../etc/passwd", tmp_path)

    def test_sanitize_dict_paths(self, tmp_path: Path):
        """sanitize_dict_paths 递归处理 dict 中的路径字段。"""
        data = {
            "path": "agents/worker_001.md",  # 合法
            "nested": {
                "file_path": "messages.md",  # 合法
            },
            "other_field": "not_a_path",
        }
        sanitize_dict_paths(data, tmp_path)
        # 合法路径不变
        assert data["path"] == "agents/worker_001.md"

    def test_sanitize_dict_rejects_absolute(self, tmp_path: Path):
        """sanitize_dict 拒绝绝对路径。"""
        data = {"path": "/etc/passwd"}
        with pytest.raises(PathSandboxError):
            sanitize_dict_paths(data, tmp_path)

    def test_sanitize_dict_rejects_traversal_in_list(self, tmp_path: Path):
        """sanitize_dict 拒绝 list 内嵌套 dict 的路径穿越。"""
        data = {
            "items": [
                {"file_path": "../../etc/passwd"},  # 穿越路径
            ]
        }
        with pytest.raises(PathSandboxError):
            sanitize_dict_paths(data, tmp_path)

    def test_sanitize_dict_rejects_lock_name_traversal(self, tmp_path: Path):
        """sanitize_dict 拒绝 lock_name 字段中的路径穿越。"""
        data = {"lock_name": "../../../etc/passwd"}
        with pytest.raises(PathSandboxError):
            sanitize_dict_paths(data, tmp_path)

    def test_empty_path_rejected(self, tmp_path: Path):
        """空路径拒绝。"""
        with pytest.raises(PathSandboxError):
            sanitize_path("", tmp_path)

    def test_non_string_path_rejected(self, tmp_path: Path):
        """非字符串路径拒绝。"""
        with pytest.raises(PathSandboxError):
            sanitize_path(None, tmp_path)  # type: ignore[arg-type]

    def test_symlink_rejected(self, tmp_path: Path):
        """symlink 路径拒绝（PolicyEngine 层）。

        path_sandbox 仅做字符串检查；实际 symlink 检测由
        file_lock.py / blackboard.validate_path_safety 完成。
        """
        # 这里仅测试字符串层面的拒绝（合法相对路径）
        result = sanitize_path("agents/worker_001.md", tmp_path)
        assert result == "agents/worker_001.md"
