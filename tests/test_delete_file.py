"""delete_file 工具单元测试 — 验证基础版与 v2 版本（注入 file_registry）的删除行为。

覆盖场景：
- 基础版 delete_file：删除文件 / 文件不存在 / symlink 拒绝
- v2 版 delete_file（注入 file_registry）：删除后从集合移除
- v2 版 write_file（注入 file_registry）：新建记录到 created / 修改记录到 modified
- register_builtin_tools 向后兼容（无 file_registry 时仍可注册）

运行方式:
    python -m unittest tests.test_delete_file -v
    python tests/test_delete_file.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.builtin_tools import (  # noqa: E402
    delete_file,
    write_file,
    register_builtin_tools,
)
from src.agent.file_registry import FileOperationRegistry  # noqa: E402
from src.agent.tool_registry import ToolRegistry  # noqa: E402


class TestDeleteFileBasic(unittest.TestCase):
    """基础版 delete_file 函数测试（不注入 file_registry）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delete_file_basic_")

    def _path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def _make_file(self, name: str, content: str = "x") -> str:
        p = self._path(name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_delete_existing_file(self):
        """删除存在的文件 → 返回成功信息，文件确实被删除。"""
        p = self._make_file("to_delete.txt")
        result = delete_file(p)
        self.assertIn("已删除文件", result)
        self.assertFalse(os.path.exists(p))

    def test_delete_nonexistent_file(self):
        """删除不存在的文件 → 返回提示，不抛异常。"""
        p = self._path("not_exist.txt")
        result = delete_file(p)
        self.assertIn("文件不存在", result)
        self.assertIn(p, result)

    def test_delete_symlink_rejected(self):
        """删除符号链接 → 返回拒绝信息，不执行删除。"""
        # 创建目标文件与符号链接
        target = self._make_file("target.txt")
        link = self._path("link.txt")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不支持创建符号链接")
        # 沙盒环境可能静默失败（os.symlink 未抛异常但未真正创建 symlink）
        if not Path(link).is_symlink():
            self.skipTest("沙盒环境不支持真正创建符号链接（os.symlink 静默失败）")
        result = delete_file(link)
        self.assertIn("拒绝删除符号链接", result)
        # 目标文件未被删除
        self.assertTrue(os.path.exists(target))

    def test_delete_failure_error(self):
        """删除失败时返回错误信息，不抛异常。"""
        # 传入无效路径触发异常
        result = delete_file("/nonexistent_root/invalid/path/file.txt")
        # 不存在或失败都行，只要不抛异常
        self.assertIsInstance(result, str)


class TestWriteFileV2WithRegistry(unittest.TestCase):
    """v2 版 write_file（注入 file_registry）测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="write_file_v2_")
        self.registry = FileOperationRegistry()
        self.session_id = "test-session-v2"

    def _path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def _make_file(self, name: str, content: str = "x") -> str:
        p = self._path(name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _make_v2_write_file(self):
        """构造一个 v2 版 write_file closure，模拟 register_builtin_tools 注入。"""
        from src.agent.builtin_tools import _register_write_file_v2

        # 用 ToolRegistry 注册 v2 版
        tr = ToolRegistry()
        _register_write_file_v2(tr, self.registry, lambda: self.session_id)
        # 通过 ToolRegistry.execute_tool 调用 v2 write_file
        return tr

    def test_write_new_file_records_created(self):
        """新建文件 → 记录到 created 集合。"""
        tr = self._make_v2_write_file()
        path = self._path("new.py")
        result = tr.execute_tool("file_write", {"path": path, "content": "hello"})
        self.assertIn("已写入文件", result)
        # 验证记录到 created
        self.assertTrue(self.registry.is_created(self.session_id, path))
        self.assertFalse(self.registry.is_modified(self.session_id, path))

    def test_write_existing_file_records_modified(self):
        """修改已有文件 → 记录到 modified 集合。"""
        tr = self._make_v2_write_file()
        path = self._make_file("existing.txt")
        result = tr.execute_tool("file_write", {"path": path, "content": "new"})
        self.assertIn("已写入文件", result)
        # 验证记录到 modified
        self.assertTrue(self.registry.is_modified(self.session_id, path))
        self.assertFalse(self.registry.is_created(self.session_id, path))

    def test_write_file_no_session_id_skip_record(self):
        """session_id 为 None 时跳过记录（向后兼容）。"""
        from src.agent.builtin_tools import _register_write_file_v2

        tr = ToolRegistry()
        # get_session_id 返回 None
        _register_write_file_v2(tr, self.registry, lambda: None)
        path = self._path("no_session.py")
        result = tr.execute_tool("file_write", {"path": path, "content": "x"})
        self.assertIn("已写入文件", result)
        # 没有记录到任何集合
        # 用任意 session_id 查询都应该返回 False
        self.assertFalse(self.registry.is_created("any-session", path))
        self.assertFalse(self.registry.is_modified("any-session", path))

    def test_write_file_v2_session_isolation(self):
        """会话 A 写入的文件不会记录到会话 B 的集合。"""
        tr = self._make_v2_write_file()
        path = self._path("session_a.py")
        tr.execute_tool("file_write", {"path": path, "content": "x"})
        # 会话 A 有记录
        self.assertTrue(self.registry.is_created(self.session_id, path))
        # 会话 B 无记录
        self.assertFalse(self.registry.is_created("session-B", path))


class TestDeleteFileV2WithRegistry(unittest.TestCase):
    """v2 版 delete_file（注入 file_registry）测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="delete_file_v2_")
        self.registry = FileOperationRegistry()
        self.session_id = "test-session-v2"

    def _path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def _make_file(self, name: str, content: str = "x") -> str:
        p = self._path(name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _make_v2_delete_file(self):
        """构造一个 v2 版 delete_file closure。"""
        from src.agent.builtin_tools import _register_delete_file_v2

        tr = ToolRegistry()
        _register_delete_file_v2(tr, self.registry, lambda: self.session_id)
        return tr

    def test_delete_file_removes_from_created(self):
        """删除文件后从 created 集合移除。"""
        tr = self._make_v2_delete_file()
        path = self._make_file("to_remove.py")
        # 先记录到 created
        self.registry.record_write(self.session_id, path, is_new=True)
        self.assertTrue(self.registry.is_created(self.session_id, path))
        # 执行删除
        result = tr.execute_tool("file_delete", {"path": path})
        self.assertIn("已删除文件", result)
        # 验证从 created 移除
        self.assertFalse(self.registry.is_created(self.session_id, path))
        self.assertFalse(self.registry.is_modified(self.session_id, path))
        # 文件确实被删除
        self.assertFalse(os.path.exists(path))

    def test_delete_file_removes_from_modified(self):
        """删除文件后从 modified 集合移除。"""
        tr = self._make_v2_delete_file()
        path = self._make_file("modified.txt")
        # 先记录到 modified
        self.registry.record_write(self.session_id, path, is_new=False)
        self.assertTrue(self.registry.is_modified(self.session_id, path))
        # 执行删除
        result = tr.execute_tool("file_delete", {"path": path})
        self.assertIn("已删除文件", result)
        # 验证从 modified 移除
        self.assertFalse(self.registry.is_modified(self.session_id, path))
        self.assertFalse(self.registry.is_created(self.session_id, path))

    def test_delete_file_nonexistent(self):
        """删除不存在的文件 → 返回提示，不抛异常，不修改 registry。"""
        tr = self._make_v2_delete_file()
        path = self._path("not_exist.txt")
        result = tr.execute_tool("file_delete", {"path": path})
        self.assertIn("文件不存在", result)

    def test_delete_file_symlink_rejected(self):
        """删除符号链接 → 拒绝，不修改 registry。"""
        tr = self._make_v2_delete_file()
        target = self._make_file("target.txt")
        link = self._path("link.txt")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不支持创建符号链接")
        # 沙盒环境可能静默失败（os.symlink 未抛异常但未真正创建 symlink）
        if not Path(link).is_symlink():
            self.skipTest("沙盒环境不支持真正创建符号链接（os.symlink 静默失败）")
        result = tr.execute_tool("file_delete", {"path": link})
        self.assertIn("拒绝删除符号链接", result)
        # 目标文件未被删除
        self.assertTrue(os.path.exists(target))

    def test_delete_file_no_session_id_skip_remove(self):
        """session_id 为 None 时跳过从 registry 移除（但文件仍被删除）。"""
        from src.agent.builtin_tools import _register_delete_file_v2

        tr = ToolRegistry()
        _register_delete_file_v2(tr, self.registry, lambda: None)
        path = self._make_file("no_session_del.txt")
        result = tr.execute_tool("file_delete", {"path": path})
        self.assertIn("已删除文件", result)
        # 文件被删除
        self.assertFalse(os.path.exists(path))


class TestRegisterBuiltinToolsCompat(unittest.TestCase):
    """验证 register_builtin_tools 向后兼容性。"""

    def test_register_without_file_registry(self):
        """无 file_registry 注入时仍可注册所有工具（基础版）。"""
        tr = ToolRegistry()
        # 不注入 file_registry，应使用默认值 None
        register_builtin_tools(tr)
        tools_schema = tr.get_tools_schema()
        tool_names = {t["name"] for t in tools_schema}
        # 基础版仍包含 write_file / delete_file
        self.assertIn("file_write", tool_names)
        self.assertIn("file_delete", tool_names)
        self.assertIn("file_read", tool_names)
        # bash_exec split out to register_bash_tool
        # self.assertIn("bash_exec", tool_names)
        self.assertIn("tool_list", tool_names)
        self.assertIn("tool_call", tool_names)

    def test_register_with_file_registry_overrides(self):
        """注入 file_registry 后 write_file / delete_file 被覆盖为 v2 版本。"""
        tr = ToolRegistry()
        registry = FileOperationRegistry()
        register_builtin_tools(
            tr,
            file_registry=registry,
            get_session_id=lambda: "test-session",
        )
        tools_schema = tr.get_tools_schema()
        tool_names = {t["name"] for t in tools_schema}
        # v2 版仍包含 write_file / delete_file
        self.assertIn("file_write", tool_names)
        self.assertIn("file_delete", tool_names)

    def test_register_v2_write_file_actually_records(self):
        """通过 register_builtin_tools 注册的 v2 write_file 实际记录到 registry。"""
        tr = ToolRegistry()
        registry = FileOperationRegistry()
        register_builtin_tools(
            tr,
            file_registry=registry,
            get_session_id=lambda: "integration-test",
        )
        # 在临时目录写入新文件
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "integration.py")
            result = tr.execute_tool("file_write", {"path": path, "content": "x"})
            self.assertIn("已写入文件", result)
            # 验证记录到 created
            self.assertTrue(registry.is_created("integration-test", path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
