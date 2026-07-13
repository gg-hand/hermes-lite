"""TodoList 会话级内存对象单元测试。

验证 ``src/tasks/todo_list.py`` 的 ``Step`` / ``TodoList`` /
``TodoListRegistry``：初始化 / 状态机 / 自动推进 / 依赖编排 /
序列化 / 会话隔离 / 覆盖重置。

TodoList 为纯内存对象，不涉及文件 IO，无需临时目录。

运行方式:
    python -m unittest tests.test_todo_list -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.tasks.todo_list import Step, TodoList, TodoListRegistry  # noqa: E402


class TestTodoList(unittest.TestCase):
    """TodoList 核心逻辑测试。"""

    def test_init_plan_assigns_ids_and_first_in_progress(self):
        """init_plan 分配自增 ID 0/1/2，首步为 in_progress，其余 pending。"""
        todo = TodoList(goal="完成示例")
        todo.init_plan([
            {"content": "步骤一"},
            {"content": "步骤二", "depends_on": [0]},
            {"content": "步骤三"},
        ])
        self.assertEqual(len(todo.steps), 3)
        self.assertEqual(todo.steps[0].id, 0)
        self.assertEqual(todo.steps[1].id, 1)
        self.assertEqual(todo.steps[2].id, 2)
        self.assertEqual(todo.steps[0].status, "in_progress")
        self.assertEqual(todo.steps[1].status, "pending")
        self.assertEqual(todo.steps[2].status, "pending")
        self.assertEqual(todo.steps[0].depends_on, [])
        self.assertEqual(todo.steps[1].depends_on, [0])
        self.assertEqual(todo.steps[0].result, None)
        self.assertFalse(todo.completed)

    def test_init_plan_empty_steps_raises(self):
        """init_plan 空列表抛 ValueError。"""
        todo = TodoList(goal="空计划")
        with self.assertRaises(ValueError):
            todo.init_plan([])

    def test_update_step_completed_auto_advances_next(self):
        """完成当前 step 后自动推进下一个依赖满足的 pending step。"""
        todo = TodoList(goal="示例")
        todo.init_plan([
            {"content": "A"},
            {"content": "B", "depends_on": [0]},
        ])
        msg = todo.update_step(0, "completed", result="A 完成")
        self.assertIn("✅", msg)
        self.assertEqual(todo.steps[0].status, "completed")
        self.assertEqual(todo.steps[0].result, "A 完成")
        # step1 依赖 step0 已完成，自动推进为 in_progress
        self.assertEqual(todo.steps[1].status, "in_progress")
        self.assertFalse(todo.completed)

    def test_update_step_completed_no_pending_remaining(self):
        """完成最后一步时无 pending 可推进，completed=True。"""
        todo = TodoList(goal="示例")
        todo.init_plan([{"content": "唯一步骤"}])
        msg = todo.update_step(0, "completed")
        self.assertIn("✅", msg)
        self.assertEqual(todo.steps[0].status, "completed")
        # 无其他 in_progress 步骤
        self.assertFalse(any(s.status == "in_progress" for s in todo.steps))
        self.assertTrue(todo.completed)

    def test_update_step_failed_does_not_advance(self):
        """失败不触发自动推进。"""
        todo = TodoList(goal="示例")
        todo.init_plan([
            {"content": "A"},
            {"content": "B", "depends_on": [0]},
        ])
        msg = todo.update_step(0, "failed", result="A 失败")
        self.assertIn("✅", msg)
        self.assertEqual(todo.steps[0].status, "failed")
        self.assertEqual(todo.steps[0].result, "A 失败")
        # 失败不推进，step1 仍 pending
        self.assertEqual(todo.steps[1].status, "pending")
        # 存在 failed，completed 不为 True
        self.assertFalse(todo.completed)

    def test_update_step_invalid_status_returns_error(self):
        """非法状态返回错误字符串，不抛异常，状态不变。"""
        todo = TodoList(goal="示例")
        todo.init_plan([{"content": "A"}])
        msg = todo.update_step(0, "xyz")
        self.assertIn("❌", msg)
        self.assertIn("非法状态", msg)
        # 状态未变更
        self.assertEqual(todo.steps[0].status, "in_progress")

    def test_update_step_nonexistent_id_returns_error(self):
        """不存在的 step_id 返回错误，不抛异常。"""
        todo = TodoList(goal="示例")
        todo.init_plan([{"content": "A"}])
        msg = todo.update_step(99, "completed")
        self.assertIn("❌", msg)
        self.assertIn("不存在", msg)

    def test_update_step_completed_cannot_change_again(self):
        """终态 completed 不可再次变更。"""
        todo = TodoList(goal="示例")
        todo.init_plan([
            {"content": "A"},
            {"content": "B"},
        ])
        todo.update_step(0, "completed")
        self.assertEqual(todo.steps[0].status, "completed")
        # 再次更新已完成步骤
        msg = todo.update_step(0, "failed")
        self.assertIn("❌", msg)
        self.assertIn("不允许变更", msg)
        self.assertEqual(todo.steps[0].status, "completed")

    def test_update_step_pending_depends_unmet_cannot_start(self):
        """pending 且依赖未满足时不能标记 in_progress。"""
        todo = TodoList(goal="示例")
        # step0(in_progress) -> step1(depends_on 0) -> step2(depends_on 1)
        todo.init_plan([
            {"content": "A"},
            {"content": "B", "depends_on": [0]},
            {"content": "C", "depends_on": [1]},
        ])
        # step0 尚未完成，step1 依赖未满足
        msg = todo.update_step(1, "in_progress")
        self.assertIn("❌", msg)
        self.assertIn("依赖未满足", msg)
        self.assertEqual(todo.steps[1].status, "pending")

    def test_update_step_pending_depends_met_can_start(self):
        """pending 且依赖已满足时可手动标记 in_progress（手动启动场景）。"""
        todo = TodoList(goal="示例")
        # 三步：A / B(depends 0) / C(depends 0)
        todo.init_plan([
            {"content": "A"},
            {"content": "B", "depends_on": [0]},
            {"content": "C", "depends_on": [0]},
        ])
        # 完成 step0 -> 自动推进第一个 pending+deps 满足的 step（B）
        todo.update_step(0, "completed")
        self.assertEqual(todo.steps[1].status, "in_progress")
        # step2 依赖 step0 已完成，但仍为 pending（自动推进只取第一个）
        self.assertEqual(todo.steps[2].status, "pending")
        # 手动启动 step2
        msg = todo.update_step(2, "in_progress")
        self.assertIn("✅", msg)
        self.assertEqual(todo.steps[2].status, "in_progress")

    def test_to_dict_serialization(self):
        """to_dict 序列化结构正确。"""
        todo = TodoList(goal="示例")
        todo.init_plan([
            {"content": "A"},
            {"content": "B", "depends_on": [0]},
        ])
        d = todo.to_dict()
        self.assertEqual(d["goal"], "示例")
        self.assertFalse(d["completed"])
        self.assertEqual(len(d["steps"]), 2)
        self.assertEqual(d["steps"][0], {
            "id": 0,
            "content": "A",
            "status": "in_progress",
            "depends_on": [],
            "result": None,
        })
        self.assertEqual(d["steps"][1]["id"], 1)
        self.assertEqual(d["steps"][1]["content"], "B")
        self.assertEqual(d["steps"][1]["status"], "pending")
        self.assertEqual(d["steps"][1]["depends_on"], [0])
        self.assertEqual(d["steps"][1]["result"], None)

    def test_registry_session_isolation(self):
        """不同 session_id 互不影响。"""
        reg = TodoListRegistry()
        reg.init_plan("sess-a", "目标A", [{"content": "A1"}])
        reg.init_plan("sess-b", "目标B", [{"content": "B1"}])
        self.assertIsNotNone(reg.get("sess-a"))
        self.assertIsNotNone(reg.get("sess-b"))
        self.assertEqual(reg.get("sess-a").goal, "目标A")
        self.assertEqual(reg.get("sess-b").goal, "目标B")
        # A 完成不影响 B
        reg.update_step("sess-a", 0, "completed")
        self.assertEqual(reg.get("sess-a").steps[0].status, "completed")
        self.assertEqual(reg.get("sess-b").steps[0].status, "in_progress")

    def test_registry_overwrites_on_reinit(self):
        """同 session_id 多次 init_plan 覆盖前一次，旧 plan 失效。"""
        reg = TodoListRegistry()
        reg.init_plan("sess", "旧目标", [{"content": "A"}, {"content": "B"}])
        reg.update_step("sess", 0, "completed")
        # 重新初始化
        reg.init_plan("sess", "新目标", [{"content": "X"}])
        todo = reg.get("sess")
        self.assertEqual(todo.goal, "新目标")
        self.assertEqual(len(todo.steps), 1)
        self.assertEqual(todo.steps[0].content, "X")
        self.assertEqual(todo.steps[0].status, "in_progress")
        self.assertFalse(todo.completed)

    def test_registry_update_step_no_plan_returns_error(self):
        """session 无 plan 时 update_step 返回错误。"""
        reg = TodoListRegistry()
        msg = reg.update_step("no-such-sess", 0, "completed")
        self.assertIn("❌", msg)
        self.assertIn("无 plan", msg)

    def test_registry_get_todo_dict_nonexistent_returns_none(self):
        """不存在的 session 返回 None。"""
        reg = TodoListRegistry()
        self.assertIsNone(reg.get_todo_dict("no-such-sess"))
        self.assertIsNone(reg.get("no-such-sess"))

    def test_all_completed_sets_completed_flag(self):
        """全部步骤完成时 completed=True，中间状态不为 True。"""
        todo = TodoList(goal="示例")
        todo.init_plan([
            {"content": "A"},
            {"content": "B", "depends_on": [0]},
            {"content": "C", "depends_on": [1]},
        ])
        self.assertFalse(todo.completed)
        todo.update_step(0, "completed")  # 推进 step1
        self.assertFalse(todo.completed)
        todo.update_step(1, "completed")  # 推进 step2
        self.assertFalse(todo.completed)
        todo.update_step(2, "completed")  # 全部完成
        self.assertTrue(todo.completed)


if __name__ == "__main__":
    unittest.main()
