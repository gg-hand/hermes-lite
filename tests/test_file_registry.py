"""FileOperationRegistry 会话级文件操作记录单元测试。

验证 ``src/agent/file_registry.py`` 的 ``FileOperationRegistry``：记录 /
查询 / 会话隔离 / symlink 防御 / 路径规范化 / remove / 不持久化。

FileOperationRegistry 为纯内存对象，不涉及文件 IO；symlink 防御测试通过
``unittest.mock.patch.object(Path, "is_symlink", ...)`` 模拟符号链接，
确保在任意环境（含 Windows 未开启开发者模式 / 非管理员）下均可运行。

运行方式:
    python -m unittest tests.test_file_registry -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock  # noqa: F401 — 启用 unittest.mock.patch
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.agent.file_registry import FileOperationRegistry  # noqa: E402


class TestFileOperationRegistry(unittest.TestCase):
    """FileOperationRegistry 核心逻辑测试。"""

    def test_record_new_file_added_to_created(self):
        """record_write(is_new=True) 加入 created 集合，is_created 返回 True。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/example_new.txt", is_new=True)
        self.assertTrue(reg.is_created("sess", "/tmp/example_new.txt"))
        # 新建文件不应出现在 modified 集合
        self.assertFalse(reg.is_modified("sess", "/tmp/example_new.txt"))

    def test_record_modified_file_added_to_modified(self):
        """record_write(is_new=False) 加入 modified 集合，is_modified 返回 True。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/example_mod.txt", is_new=False)
        self.assertTrue(reg.is_modified("sess", "/tmp/example_mod.txt"))
        # 修改文件不应出现在 created 集合
        self.assertFalse(reg.is_created("sess", "/tmp/example_mod.txt"))

    def test_record_write_creates_session_implicitly(self):
        """对未出现的 session_id 调用 record_write 会隐式创建集合。"""
        reg = FileOperationRegistry()
        # 初始查询不存在 session 返回 False
        self.assertFalse(reg.is_created("new-sess", "/tmp/x.txt"))
        # 记录后隐式创建
        reg.record_write("new-sess", "/tmp/x.txt", is_new=True)
        self.assertTrue(reg.is_created("new-sess", "/tmp/x.txt"))

    def test_record_write_idempotent(self):
        """同一 path 多次 record_write 仅记录一次（集合语义）。"""
        reg = FileOperationRegistry()
        path = "/tmp/dup.txt"
        reg.record_write("sess", path, is_new=True)
        reg.record_write("sess", path, is_new=True)
        # 集合去重，仍只命中一次
        self.assertTrue(reg.is_created("sess", path))

    def test_is_created_nonexistent_session_returns_false(self):
        """is_created 对不存在的 session_id 返回 False。"""
        reg = FileOperationRegistry()
        self.assertFalse(reg.is_created("no-such-sess", "/tmp/x.txt"))

    def test_is_modified_nonexistent_session_returns_false(self):
        """is_modified 对不存在的 session_id 返回 False。"""
        reg = FileOperationRegistry()
        self.assertFalse(reg.is_modified("no-such-sess", "/tmp/x.txt"))

    def test_query_unrecorded_path_returns_false(self):
        """未记录的 path 查询返回 False。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/recorded.txt", is_new=True)
        self.assertFalse(reg.is_created("sess", "/tmp/other.txt"))
        self.assertFalse(reg.is_modified("sess", "/tmp/other.txt"))

    def test_cross_set_isolation_within_session(self):
        """同一 session 内 created 与 modified 集合互不干扰。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/new.txt", is_new=True)
        reg.record_write("sess", "/tmp/mod.txt", is_new=False)
        # new 仅在 created，mod 仅在 modified
        self.assertTrue(reg.is_created("sess", "/tmp/new.txt"))
        self.assertFalse(reg.is_modified("sess", "/tmp/new.txt"))
        self.assertTrue(reg.is_modified("sess", "/tmp/mod.txt"))
        self.assertFalse(reg.is_created("sess", "/tmp/mod.txt"))

    def test_session_isolation(self):
        """不同 session_id 各自维护独立集合，互不干扰。"""
        reg = FileOperationRegistry()
        reg.record_write("sess-a", "/tmp/shared.txt", is_new=True)
        reg.record_write("sess-b", "/tmp/shared.txt", is_new=False)
        # sess-a 视为 created，sess-b 视为 modified
        self.assertTrue(reg.is_created("sess-a", "/tmp/shared.txt"))
        self.assertFalse(reg.is_modified("sess-a", "/tmp/shared.txt"))
        self.assertTrue(reg.is_modified("sess-b", "/tmp/shared.txt"))
        self.assertFalse(reg.is_created("sess-b", "/tmp/shared.txt"))

    def test_session_isolation_no_crosstalk(self):
        """一个 session 的记录不影响另一个 session 的查询。"""
        reg = FileOperationRegistry()
        reg.record_write("sess-a", "/tmp/a.txt", is_new=True)
        # sess-b 完全没有记录
        self.assertFalse(reg.is_created("sess-b", "/tmp/a.txt"))
        self.assertFalse(reg.is_modified("sess-b", "/tmp/a.txt"))

    def test_accepts_str_and_path(self):
        """record_write 与查询方法同时接受 str 与 Path 参数。"""
        reg = FileOperationRegistry()
        # str 录入，Path 查询
        reg.record_write("sess", "/tmp/str_in.txt", is_new=True)
        self.assertTrue(reg.is_created("sess", Path("/tmp/str_in.txt")))
        # Path 录入，str 查询
        reg.record_write("sess", Path("/tmp/path_in.txt"), is_new=False)
        self.assertTrue(reg.is_modified("sess", "/tmp/path_in.txt"))

    def test_path_normalization_resolve(self):
        """不同字符串表示的同一路径经 resolve 后命中（含 .. 规范化）。"""
        tmpdir = tempfile.mkdtemp()
        base = Path(tmpdir)
        # 创建子目录用于构造 .. 路径
        subdir = base / "subdir"
        subdir.mkdir()
        target = base / "file.txt"
        # 用绝对路径记录
        reg = FileOperationRegistry()
        reg.record_write("sess", str(target), is_new=True)
        # 用含 .. 的相对路径查询，resolve 后应与绝对路径相同
        dotted = subdir / ".." / "file.txt"
        self.assertTrue(reg.is_created("sess", str(dotted)))

    def test_remove_from_created(self):
        """remove 从 created 集合移除路径。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/rm.txt", is_new=True)
        self.assertTrue(reg.is_created("sess", "/tmp/rm.txt"))
        reg.remove("sess", "/tmp/rm.txt")
        self.assertFalse(reg.is_created("sess", "/tmp/rm.txt"))

    def test_remove_from_modified(self):
        """remove 从 modified 集合移除路径。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/rm.txt", is_new=False)
        self.assertTrue(reg.is_modified("sess", "/tmp/rm.txt"))
        reg.remove("sess", "/tmp/rm.txt")
        self.assertFalse(reg.is_modified("sess", "/tmp/rm.txt"))

    def test_remove_clears_both_sets(self):
        """remove 同时从 created 与 modified 移除（文件可能在任一集合）。"""
        reg = FileOperationRegistry()
        path = "/tmp/both.txt"
        # 同时加入两个集合（理论场景，验证 remove 清理彻底）
        reg.record_write("sess", path, is_new=True)
        reg.record_write("sess", path, is_new=False)
        self.assertTrue(reg.is_created("sess", path))
        self.assertTrue(reg.is_modified("sess", path))
        reg.remove("sess", path)
        self.assertFalse(reg.is_created("sess", path))
        self.assertFalse(reg.is_modified("sess", path))

    def test_remove_nonexistent_session_noop(self):
        """remove 对不存在的 session_id 为空操作，不抛异常。"""
        reg = FileOperationRegistry()
        # 不应抛异常
        reg.remove("no-such-sess", "/tmp/x.txt")

    def test_remove_nonexistent_path_noop(self):
        """remove 对未记录的 path 为空操作，不抛异常。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/exists.txt", is_new=True)
        # 移除未记录的路径
        reg.remove("sess", "/tmp/not_recorded.txt")
        # 已记录的不受影响
        self.assertTrue(reg.is_created("sess", "/tmp/exists.txt"))

    def test_symlink_defense_is_created_returns_false(self):
        """is_created 对 symlink 路径返回 False（视为用户文件，不享受豁免）。

        通过 mock ``Path.is_symlink`` 返回 ``True`` 模拟符号链接，确保在
        任意环境（含 Windows 未开启开发者模式）下均可验证 symlink 防御逻辑。
        """
        reg = FileOperationRegistry()
        # 先记录路径（record_write 不做 symlink 检查，会正常记录）
        reg.record_write("sess", "/tmp/link.txt", is_new=True)
        # 查询时 is_symlink 返回 True → 防御生效，返回 False
        with unittest.mock.patch.object(
            Path, "is_symlink", return_value=True
        ):
            self.assertFalse(reg.is_created("sess", "/tmp/link.txt"))

    def test_symlink_defense_is_modified_returns_false(self):
        """is_modified 对 symlink 路径返回 False（视为用户文件，不享受豁免）。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/link.txt", is_new=False)
        with unittest.mock.patch.object(
            Path, "is_symlink", return_value=True
        ):
            self.assertFalse(reg.is_modified("sess", "/tmp/link.txt"))

    def test_symlink_defense_only_affects_symlink_paths(self):
        """symlink 防御仅对 is_symlink=True 的路径生效，正常路径查询不受影响。"""
        reg = FileOperationRegistry()
        reg.record_write("sess", "/tmp/real.txt", is_new=True)
        # 默认 is_symlink 返回 False，正常查询命中
        self.assertTrue(reg.is_created("sess", "/tmp/real.txt"))
        # mock 后同一路径视为 symlink，返回 False
        with unittest.mock.patch.object(
            Path, "is_symlink", return_value=True
        ):
            self.assertFalse(reg.is_created("sess", "/tmp/real.txt"))
        # mock 撤销后恢复正常查询
        self.assertTrue(reg.is_created("sess", "/tmp/real.txt"))

    def test_not_persisted_instance_isolation(self):
        """纯内存对象：两个独立实例不共享状态（模拟服务重启后数据丢失）。"""
        reg_a = FileOperationRegistry()
        reg_a.record_write("sess", "/tmp/persist.txt", is_new=True)
        self.assertTrue(reg_a.is_created("sess", "/tmp/persist.txt"))

        # 新实例（模拟重启）应无任何记录
        reg_b = FileOperationRegistry()
        self.assertFalse(reg_b.is_created("sess", "/tmp/persist.txt"))
        self.assertFalse(reg_b.is_modified("sess", "/tmp/persist.txt"))

    def test_not_persisted_no_disk_artifacts(self):
        """不持久化：实例无 save/load 等持久化方法，不写入磁盘文件。"""
        reg = FileOperationRegistry()
        # 不应暴露持久化相关方法
        for attr in ("save", "load", "persist", "dump", "to_file", "flush"):
            self.assertFalse(
                hasattr(reg, attr),
                f"FileOperationRegistry 不应暴露持久化方法 {attr!r}",
            )
        # 内部数据结构为实例属性（内存）
        self.assertIsInstance(reg._sessions, dict)


if __name__ == "__main__":
    unittest.main()
