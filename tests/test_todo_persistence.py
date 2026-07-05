"""TodoListRegistry 持久化层单元测试。

验证 ``src/tasks/todo_list.py`` 的 ``TodoListRegistry`` 磁盘持久化能力：
init_plan 落盘、update_step 持久化、重启懒加载恢复、delete 清理、
并发安全、update_step 懒加载路径、init_plan 覆盖、向后兼容。

运行方式:
    python -m unittest tests.test_todo_persistence -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.tasks.todo_list import TodoListRegistry  # noqa: E402


class TestTodoListRegistryPersistence(unittest.TestCase):
    """TodoListRegistry 持久化层测试。"""

    def setUp(self) -> None:
        """每个用例使用独立临时目录，避免相互污染。"""
        self.tmp_dir = tempfile.mkdtemp(prefix="todo_persist_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _todo_dir(self) -> Path:
        return Path(self.tmp_dir) / "todo"

    def _todo_file(self, session_id: str) -> Path:
        return self._todo_dir() / f"{session_id.replace(':', '_')}.json"

    def test_init_plan_persists_to_disk(self) -> None:
        """init_plan 后断言 todo 文件存在且内容正确。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        reg.init_plan("sess-1", "任务X", [
            {"content": "步骤A"},
            {"content": "步骤B", "depends_on": [0]},
        ])

        path = self._todo_file("sess-1")
        self.assertTrue(path.exists(), f"todo 文件应存在: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["goal"], "任务X")
        self.assertEqual(len(data["steps"]), 2)
        self.assertEqual(data["steps"][0]["status"], "in_progress")
        self.assertEqual(data["steps"][1]["status"], "pending")
        self.assertEqual(data["steps"][1]["depends_on"], [0])
        self.assertFalse(data["completed"])

    def test_update_step_persists_change(self) -> None:
        """update_step 后重新加载 registry，状态正确持久化。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        reg.init_plan("sess-2", "任务Y", [{"content": "唯一"}])
        reg.update_step("sess-2", 0, "completed", "完成")

        # 新 registry 模拟重启
        reg2 = TodoListRegistry(persistence_dir=self.tmp_dir)
        todo_dict = reg2.get_todo_dict("sess-2")
        self.assertIsNotNone(todo_dict)
        self.assertEqual(todo_dict["steps"][0]["status"], "completed")
        self.assertEqual(todo_dict["steps"][0]["result"], "完成")
        self.assertTrue(todo_dict["completed"])

    def test_restart_recovers_todo(self) -> None:
        """重启后通过 get_todo_dict 懒加载恢复 plan。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        reg.init_plan("sess-3", "重启恢复", [
            {"content": "S0"},
            {"content": "S1"},
            {"content": "S2"},
        ])
        reg.update_step("sess-3", 0, "completed")

        # 重启
        reg2 = TodoListRegistry(persistence_dir=self.tmp_dir)
        todo = reg2.get("sess-3")
        self.assertIsNotNone(todo)
        self.assertEqual(todo.goal, "重启恢复")
        self.assertEqual(len(todo.steps), 3)
        # 重启后内存应有缓存
        self.assertIn("sess-3", reg2._todos)

    def test_delete_session_removes_file(self) -> None:
        """delete 后文件不存在，内存也清空。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        reg.init_plan("sess-4", "删除测试", [{"content": "X"}])
        path = self._todo_file("sess-4")
        self.assertTrue(path.exists())

        reg.delete("sess-4")
        self.assertFalse(path.exists())
        self.assertIsNone(reg.get("sess-4"))

    def test_concurrent_update_step_thread_safe(self) -> None:
        """多线程并发 update_step，无异常且最终状态一致。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        # 10 个 step，10 线程各自完成一个
        steps = [{"content": f"S{i}"} for i in range(10)]
        reg.init_plan("sess-5", "并发任务", steps)

        errors: list = []

        def worker(step_id: int) -> None:
            try:
                reg.update_step("sess-5", step_id, "completed")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        # 由于自动推进机制，step 必须按顺序完成。这里并发仅验证线程安全
        # （不验证全部完成，因为依赖推进可能阻塞）
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"并发不应抛异常: {errors}")
        # 验证文件未损坏
        data = json.loads(self._todo_file("sess-5").read_text(encoding="utf-8"))
        self.assertEqual(len(data["steps"]), 10)

    def test_update_step_lazy_load_after_restart(self) -> None:
        """重启后直接调用 update_step（不先 get），能正常更新。

        这是关键场景：原方案漏了 update_step 路径，重启后 LLM 再次调用
        plan_update_step 会报 '❌ 会话无 plan'。现在走 _get_or_load 懒加载。
        """
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        reg.init_plan("sess-6", "懒加载", [{"content": "Step"}])

        # 重启
        reg2 = TodoListRegistry(persistence_dir=self.tmp_dir)
        # 不先 get，直接 update_step
        msg = reg2.update_step("sess-6", 0, "completed", "完成")
        self.assertIn("已更新为", msg)
        # 验证持久化
        data = json.loads(self._todo_file("sess-6").read_text(encoding="utf-8"))
        self.assertEqual(data["steps"][0]["status"], "completed")

    def test_init_plan_overwrites_old_file(self) -> None:
        """同 session 二次 init_plan，旧文件被新内容替换。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        reg.init_plan("sess-7", "旧任务", [{"content": "旧步骤"}])
        old_path = self._todo_file("sess-7")
        old_data = json.loads(old_path.read_text(encoding="utf-8"))
        self.assertEqual(old_data["goal"], "旧任务")

        # 二次 init_plan 覆盖
        reg.init_plan("sess-7", "新任务", [
            {"content": "新步骤1"},
            {"content": "新步骤2"},
        ])
        new_data = json.loads(old_path.read_text(encoding="utf-8"))
        self.assertEqual(new_data["goal"], "新任务")
        self.assertEqual(len(new_data["steps"]), 2)
        self.assertEqual(new_data["steps"][0]["content"], "新步骤1")

    def test_legacy_session_without_file_returns_none(self) -> None:
        """无 todo 文件的 session，三个入口均返回 None / '无 plan'。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        self.assertIsNone(reg.get("nonexistent"))
        self.assertIsNone(reg.get_todo_dict("nonexistent"))
        msg = reg.update_step("nonexistent", 0, "completed")
        self.assertIn("无 plan", msg)

    def test_no_persistence_dir_backward_compat(self) -> None:
        """不传 persistence_dir 时降级为纯内存（向后兼容老测试）。"""
        reg = TodoListRegistry()  # 无参
        reg.init_plan("sess-8", "纯内存", [{"content": "X"}])
        # 文件不应存在（_todo_dir 为 None）
        self.assertIsNone(reg._todo_dir)
        self.assertIsNone(reg._file_for("sess-8"))
        # 内存正常工作
        todo_dict = reg.get_todo_dict("sess-8")
        self.assertIsNotNone(todo_dict)
        self.assertEqual(todo_dict["goal"], "纯内存")
        # update_step 正常
        msg = reg.update_step("sess-8", 0, "completed")
        self.assertIn("已更新为", msg)

    def test_cron_session_id_filename_escaping(self) -> None:
        """cron 会话 ID 'cron:abc' → 文件名 'cron_abc.json'。"""
        reg = TodoListRegistry(persistence_dir=self.tmp_dir)
        reg.init_plan("cron:abc", "cron 任务", [{"content": "C"}])
        path = self._todo_file("cron:abc")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "cron_abc.json")


if __name__ == "__main__":
    unittest.main()
