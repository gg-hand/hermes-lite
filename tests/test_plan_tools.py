"""plan_task / update_todo 工具注册与 handler 单元测试。

验证 ``src/agent/builtin_tools.py`` 的 ``register_plan_tools``：
- 注册 2 个 Core Tier 工具（plan_task / update_todo）
- handler 正确调用 ``TodoListRegistry`` 的 init_plan / update_step
- session_id 为 None 时返回错误
- 异常时返回错误字符串
- input_schema 的必填字段与 enum 约束
- 旧任务工具 register_task_tools / enter_plan_mode / exit_plan_mode 已删除

运行方式:
    python -m unittest tests.test_plan_tools -v
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

from src.agent import builtin_tools  # noqa: E402
from src.agent.builtin_tools import BUILTIN_TOOLS, register_plan_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Mock 对象
# ---------------------------------------------------------------------------


class _MockToolRegistry:
    """记录 register_core 调用的 mock ToolRegistry。

    捕获每次 register_core 的 name / description / input_schema / handler，
    便于测试断言注册数量与字段。
    """

    def __init__(self) -> None:
        self.tools: dict = {}  # name -> {"description", "input_schema", "handler"}

    def register_core(self, name, description, input_schema, handler) -> None:
        self.tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }


class _MockTodoRegistry:
    """记录 init_plan / update_step 调用的 mock TodoListRegistry。

    可注入异常以测试 handler 的异常处理分支。
    """

    def __init__(self) -> None:
        self.init_plan_calls: list = []
        self.update_step_calls: list = []
        self.update_step_result: str = "✅ 步骤 0 状态已更新为: completed"
        self.init_plan_exception: Exception = None
        self.update_step_exception: Exception = None

    def init_plan(self, session_id, goal, steps):
        self.init_plan_calls.append((session_id, goal, steps))
        if self.init_plan_exception is not None:
            raise self.init_plan_exception

    def update_step(self, session_id, step_id, status, result=""):
        self.update_step_calls.append((session_id, step_id, status, result))
        if self.update_step_exception is not None:
            raise self.update_step_exception
        return self.update_step_result


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestRegisterPlanTools(unittest.TestCase):
    """register_plan_tools 注册与 handler 行为测试。"""

    def setUp(self) -> None:
        """每个用例使用独立的 mock registry / todo_registry / get_session_id。"""
        self.registry = _MockToolRegistry()
        self.todo_registry = _MockTodoRegistry()
        self._session_id = "test-session-001"
        register_plan_tools(
            self.registry,
            self.todo_registry,
            get_session_id=lambda: self._session_id,
        )

    # 1. 注册了 2 个工具
    def test_register_plan_tools_registers_two_tools(self) -> None:
        """register_plan_tools 应注册 plan_task 与 update_todo 两个工具。"""
        self.assertIn("plan_create", self.registry.tools)
        self.assertIn("plan_update_step", self.registry.tools)
        self.assertEqual(len(self.registry.tools), 2)

    # 2. plan_task handler 成功
    def test_plan_task_handler_success(self) -> None:
        """plan_task handler 成功调用 init_plan 并返回正确字符串。"""
        handler = self.registry.tools["plan_create"]["handler"]
        steps = [
            {"content": "分析需求"},
            {"content": "编写代码", "depends_on": [0]},
            {"content": "运行测试"},
        ]
        result = handler(goal="完成 T3 任务", steps=steps)

        # 验证 init_plan 被调用一次，参数正确
        self.assertEqual(len(self.todo_registry.init_plan_calls), 1)
        session_id, goal, passed_steps = self.todo_registry.init_plan_calls[0]
        self.assertEqual(session_id, "test-session-001")
        self.assertEqual(goal, "完成 T3 任务")
        self.assertEqual(passed_steps, steps)

        # 验证返回字符串
        self.assertIn("已规划 3 个步骤", result)
        self.assertIn("开始执行 step 0: 分析需求", result)

    # 3. plan_task handler session_id 为 None
    def test_plan_task_handler_no_session_id(self) -> None:
        """session_id 为 None 时返回错误字符串。"""
        register_plan_tools(
            self.registry,
            self.todo_registry,
            get_session_id=lambda: None,
        )
        handler = self.registry.tools["plan_create"]["handler"]
        result = handler(goal="任意目标", steps=[{"content": "步骤一"}])
        self.assertEqual(result, "❌ 无法获取 session_id")
        # 不应调用 init_plan
        self.assertEqual(len(self.todo_registry.init_plan_calls), 0)

    # 4. plan_task handler 异常
    def test_plan_task_handler_exception(self) -> None:
        """init_plan 抛异常时返回错误字符串。"""
        self.todo_registry.init_plan_exception = ValueError("steps 不能为空")
        handler = self.registry.tools["plan_create"]["handler"]
        result = handler(goal="任意目标", steps=[])
        self.assertIn("plan_task 执行失败", result)
        self.assertIn("steps 不能为空", result)

    # 5. plan_task input_schema 必填字段
    def test_plan_task_input_schema_required_fields(self) -> None:
        """plan_task input_schema 含 goal / steps 必填字段。"""
        schema = self.registry.tools["plan_create"]["input_schema"]
        self.assertEqual(set(schema["required"]), {"goal", "steps"})
        props = schema["properties"]
        self.assertIn("goal", props)
        self.assertEqual(props["goal"]["type"], "string")
        self.assertIn("steps", props)
        self.assertEqual(props["steps"]["type"], "array")
        # steps.items 含 content 必填
        item_props = props["steps"]["items"]["properties"]
        self.assertIn("content", item_props)
        self.assertEqual(set(props["steps"]["items"]["required"]), {"content"})
        self.assertIn("depends_on", item_props)

    # 6. update_todo handler 成功
    def test_update_todo_handler_success(self) -> None:
        """update_todo handler 成功调用 update_step 并透传返回值。"""
        self.todo_registry.update_step_result = "✅ 步骤 0 状态已更新为: completed"
        handler = self.registry.tools["plan_update_step"]["handler"]
        result = handler(step_id=0, status="completed", result="完成分析")

        # 验证 update_step 被调用，参数正确
        self.assertEqual(len(self.todo_registry.update_step_calls), 1)
        session_id, step_id, status, passed_result = (
            self.todo_registry.update_step_calls[0]
        )
        self.assertEqual(session_id, "test-session-001")
        self.assertEqual(step_id, 0)
        self.assertEqual(status, "completed")
        self.assertEqual(passed_result, "完成分析")

        # 验证透传返回值
        self.assertEqual(result, "✅ 步骤 0 状态已更新为: completed")

    # 7. update_todo handler session_id 为 None
    def test_update_todo_handler_no_session_id(self) -> None:
        """session_id 为 None 时返回错误字符串。"""
        register_plan_tools(
            self.registry,
            self.todo_registry,
            get_session_id=lambda: None,
        )
        handler = self.registry.tools["plan_update_step"]["handler"]
        result = handler(step_id=0, status="completed")
        self.assertEqual(result, "❌ 无法获取 session_id")
        # 不应调用 update_step
        self.assertEqual(len(self.todo_registry.update_step_calls), 0)

    # 8. update_todo handler 异常
    def test_update_todo_handler_exception(self) -> None:
        """update_step 抛异常时返回错误字符串。"""
        self.todo_registry.update_step_exception = RuntimeError("boom")
        handler = self.registry.tools["plan_update_step"]["handler"]
        result = handler(step_id=1, status="failed", result="失败原因")
        self.assertIn("update_todo 执行失败", result)
        self.assertIn("boom", result)

    # 9. update_todo input_schema 必填字段与 enum
    def test_update_todo_input_schema_required_fields_and_enum(self) -> None:
        """update_todo input_schema 含 step_id / status 必填，status 为枚举。"""
        schema = self.registry.tools["plan_update_step"]["input_schema"]
        self.assertEqual(set(schema["required"]), {"step_id", "status"})
        props = schema["properties"]
        self.assertEqual(props["step_id"]["type"], "integer")
        self.assertEqual(props["status"]["type"], "string")
        self.assertEqual(
            set(props["status"]["enum"]), {"completed", "failed"}
        )
        # result 可选
        self.assertIn("result", props)
        self.assertNotIn("result", schema.get("required", []))


# ---------------------------------------------------------------------------
# 旧工具删除验证
# ---------------------------------------------------------------------------


class TestOldToolsRemoved(unittest.TestCase):
    """验证旧任务工具与 plan 模式软约束工具已删除。"""

    # 10. register_task_tools 已删除
    def test_old_task_tools_removed(self) -> None:
        """register_task_tools 应已从模块删除（import 失败）。"""
        with self.assertRaises(ImportError):
            from src.agent.builtin_tools import register_task_tools  # noqa: F401

    # 11. enter_plan_mode / exit_plan_mode 已从模块删除
    def test_enter_plan_mode_removed(self) -> None:
        """enter_plan_mode / exit_plan_mode 应不再是模块属性。"""
        self.assertFalse(hasattr(builtin_tools, "enter_plan_mode"))
        self.assertFalse(hasattr(builtin_tools, "exit_plan_mode"))

    # 12. BUILTIN_TOOLS 列表不再含 plan 模式条目
    def test_builtin_tools_no_plan_mode_entries(self) -> None:
        """BUILTIN_TOOLS 列表中不应含 enter_plan_mode / exit_plan_mode。"""
        tool_names = [t[0] for t in BUILTIN_TOOLS]
        self.assertNotIn("enter_plan_mode", tool_names)
        self.assertNotIn("exit_plan_mode", tool_names)
        # 同时验证 register_plan_tools 注册的工具不在此列表中
        self.assertNotIn("plan_create", tool_names)
        self.assertNotIn("plan_update_step", tool_names)
        # 基础工具仍在
        self.assertIn("file_read", tool_names)
        self.assertIn("file_write", tool_names)
        # bash_exec 已从 BUILTIN_TOOLS 拆分出去，通过 register_bash_tool 单独注册
        self.assertIn("web_fetch", tool_names)


if __name__ == "__main__":
    unittest.main()
