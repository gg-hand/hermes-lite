"""TodoList 状态注入 messages[0] 测试。

Phase 9 Task 5: 验证 ``Orchestrator._format_todo_for_injection`` 与
``_build_enhanced_context`` 的 TodoList 状态注入行为。

覆盖场景：
- ``_format_todo_for_injection``:
  - 有 TodoList 时返回"## 当前计划进度"段（含 goal/总进度/各步骤状态）
  - 未完成步骤标注 ``[ ]``，已完成步骤标注 ``[x]``
  - 末尾含"提醒：每完成一个步骤，必须调用 update_todo 标记为 completed"
  - ``todo_dict`` 为 ``None`` / 空 dict / 无 steps 时返回空串
- ``_build_enhanced_context`` 用户会话路径注入：
  - 有 TodoList 时 messages[0] 含"## 当前计划进度"段
  - TodoList 段位于 TaskManager 进度段之后
  - SYSTEM_PROMPT 不被污染（todo 不写入 system_text）
- ``_build_enhanced_context`` 降级：
  - ``todo_registry`` 为 ``None`` 时不抛异常，messages[0] 不含 todo 段
  - session 无 plan（get_todo_dict 返回 None）时不抛异常

运行方式:
    python -m pytest tests/test_todo_injection.py -v
    或
    python -m unittest tests.test_todo_injection -v
"""

from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.llm.prompts import SYSTEM_PROMPT  # noqa: E402
from src.memory.context_manager import ContextManager  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.tasks.todo_list import TodoListRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# mock 组件（与 test_environment_injection.py 风格保持一致）
# ---------------------------------------------------------------------------


class MockMemoryMdManager:
    """Mock MemoryMdManager，read 返回固定的用户画像文本。"""

    def read(self):
        return "# 用户画像\n\n## 背景\n- 测试用户画像内容"


class MockMemoryRetriever:
    """Mock MemoryRetriever，返回固定的检索记忆文本。"""

    def get_injection_text(self, user_input, namespace="user", cron_id=None):
        return "## 相关记忆\n1. user memory fact (相关度: 0.90)"


class MockHistoryBuffer:
    """Mock HistoryBuffer，返回固定的历史消息。"""

    def get_history(self, session_id):
        return [
            {"role": "user", "content": "历史用户"},
            {"role": "assistant", "content": "历史助手"},
        ]


class MockTaskManager:
    """Mock TaskManager，get_progress_summary 返回固定任务进度。"""

    def get_progress_summary(self):
        return "## 任务进度\n- [x] 已完成步骤"


class MockToolRegistry:
    """Mock ToolRegistry。"""

    def get_tools_schema(self):
        return [{"name": "test_tool", "description": "测试", "input_schema": {}}]


# ---------------------------------------------------------------------------
# _format_todo_for_injection 单元测试
# ---------------------------------------------------------------------------


class TestFormatTodoForInjection(unittest.TestCase):
    """验证 Orchestrator._format_todo_for_injection 的格式化行为。"""

    def _make_orchestrator(self):
        """通过 __new__ 构造 Orchestrator，仅设置测试需要的属性。"""
        orch = Orchestrator.__new__(Orchestrator)
        # _format_todo_for_injection 不依赖任何实例属性
        return orch

    def test_method_exists(self):
        """Orchestrator 类应包含 _format_todo_for_injection 方法。"""
        self.assertTrue(hasattr(Orchestrator, "_format_todo_for_injection"))
        self.assertTrue(callable(getattr(Orchestrator, "_format_todo_for_injection")))

    def test_none_todo_dict_returns_empty(self):
        """todo_dict 为 None 时返回空串。"""
        orch = self._make_orchestrator()
        self.assertEqual(orch._format_todo_for_injection(None), "")

    def test_empty_dict_returns_empty(self):
        """todo_dict 为空 dict 时返回空串。"""
        orch = self._make_orchestrator()
        self.assertEqual(orch._format_todo_for_injection({}), "")

    def test_no_steps_returns_empty(self):
        """todo_dict 无 steps 字段时返回空串。"""
        orch = self._make_orchestrator()
        self.assertEqual(
            orch._format_todo_for_injection({"goal": "g", "steps": []}),
            "",
        )

    def test_returns_section_with_header(self):
        """有 TodoList 时返回 '## 当前计划进度' 段。"""
        orch = self._make_orchestrator()
        todo_dict = {
            "goal": "完成 T5 任务",
            "steps": [
                {"id": 0, "content": "步骤1", "status": "completed"},
                {"id": 1, "content": "步骤2", "status": "in_progress"},
                {"id": 2, "content": "步骤3", "status": "pending"},
            ],
            "completed": False,
        }
        section = orch._format_todo_for_injection(todo_dict)
        self.assertTrue(section.startswith("## 当前计划进度"))
        self.assertIn("**目标**: 完成 T5 任务", section)
        self.assertIn("**总进度**: 1/3", section)

    def test_step_markers_completed_vs_uncompleted(self):
        """已完成步骤标注 [x]，未完成步骤标注 [ ]。"""
        orch = self._make_orchestrator()
        todo_dict = {
            "goal": "g",
            "steps": [
                {"id": 0, "content": "已完成步骤", "status": "completed"},
                {"id": 1, "content": "进行中步骤", "status": "in_progress"},
                {"id": 2, "content": "待办步骤", "status": "pending"},
                {"id": 3, "content": "失败步骤", "status": "failed"},
            ],
            "completed": False,
        }
        section = orch._format_todo_for_injection(todo_dict)
        # completed -> [x]
        self.assertIn("[x] 已完成步骤", section)
        # in_progress / pending / failed -> [ ]
        self.assertIn("[ ] 进行中步骤", section)
        self.assertIn("[ ] 待办步骤", section)
        self.assertIn("[ ] 失败步骤", section)

    def test_progress_count_all_completed(self):
        """全部步骤 completed 时总进度为 N/N。"""
        orch = self._make_orchestrator()
        todo_dict = {
            "goal": "g",
            "steps": [
                {"id": 0, "content": "s1", "status": "completed"},
                {"id": 1, "content": "s2", "status": "completed"},
            ],
            "completed": True,
        }
        section = orch._format_todo_for_injection(todo_dict)
        self.assertIn("**总进度**: 2/2", section)
        # 所有步骤都标 [x]
        self.assertIn("[x] s1", section)
        self.assertIn("[x] s2", section)

    def test_progress_count_none_completed(self):
        """全部步骤 pending/in_progress 时总进度为 0/N。"""
        orch = self._make_orchestrator()
        todo_dict = {
            "goal": "g",
            "steps": [
                {"id": 0, "content": "s1", "status": "in_progress"},
                {"id": 1, "content": "s2", "status": "pending"},
            ],
            "completed": False,
        }
        section = orch._format_todo_for_injection(todo_dict)
        self.assertIn("**总进度**: 0/2", section)

    def test_reminder_footer(self):
        """段末应含 update_todo 提醒文案。"""
        orch = self._make_orchestrator()
        todo_dict = {
            "goal": "g",
            "steps": [{"id": 0, "content": "s1", "status": "in_progress"}],
            "completed": False,
        }
        section = orch._format_todo_for_injection(todo_dict)
        self.assertIn(
            "提醒：每完成一个步骤，必须调用 update_todo 标记为 completed",
            section,
        )
        # 提醒应在最后
        self.assertTrue(
            section.rstrip().endswith(
                "提醒：每完成一个步骤，必须调用 update_todo 标记为 completed"
            ),
            f"段末应为提醒文案，实际末尾: {section[-80:]!r}",
        )

    def test_missing_goal_field_uses_empty(self):
        """goal 字段缺失时降级为空串（不抛异常）。"""
        orch = self._make_orchestrator()
        todo_dict = {
            "steps": [{"id": 0, "content": "s1", "status": "in_progress"}],
        }
        section = orch._format_todo_for_injection(todo_dict)
        self.assertIn("**目标**: ", section)

    def test_missing_status_treated_as_uncompleted(self):
        """step 缺 status 字段时按未完成处理（标 [ ]）。"""
        orch = self._make_orchestrator()
        todo_dict = {
            "goal": "g",
            "steps": [{"id": 0, "content": "s1"}],  # 无 status
        }
        section = orch._format_todo_for_injection(todo_dict)
        self.assertIn("[ ] s1", section)
        self.assertIn("**总进度**: 0/1", section)


# ---------------------------------------------------------------------------
# _build_enhanced_context 用户会话 TodoList 注入测试
# ---------------------------------------------------------------------------


class TestUserSessionTodoInjection(unittest.TestCase):
    """验证用户会话 _build_enhanced_context 注入 TodoList 状态到 messages[0]。"""

    def _make_orchestrator(
        self,
        with_todo_registry: bool = True,
        with_task_manager: bool = True,
    ):
        """通过 __new__ 构造 Orchestrator，仅设置测试需要的属性。"""
        orch = Orchestrator.__new__(Orchestrator)
        orch.memory_retriever = MockMemoryRetriever()
        orch.context_manager = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=orch.memory_retriever,
            history_buffer=MockHistoryBuffer(),
        )
        orch.condenser = None
        orch.metrics = None
        orch.task_manager = MockTaskManager() if with_task_manager else None
        orch.todo_registry = (
            TodoListRegistry() if with_todo_registry else None
        )
        return orch

    def _init_plan(self, orch, session_id: str):
        """为测试 orch 的 todo_registry 初始化一个 3 步 plan，标记 step 0 完成。"""
        assert orch.todo_registry is not None
        orch.todo_registry.init_plan(
            session_id,
            "完成 T5 注入测试",
            [
                {"content": "步骤1：分析需求"},
                {"content": "步骤2：编写代码", "depends_on": [0]},
                {"content": "步骤3：运行测试"},
            ],
        )
        # 标记 step 0 完成（会自动推进 step 1 为 in_progress）
        orch.todo_registry.update_step(session_id, 0, "completed", "分析完成")

    def test_messages_zero_contains_todo_section(self):
        """有 TodoList 时 messages[0] 应含 '## 当前计划进度' 段。"""
        orch = self._make_orchestrator()
        session_id = "user_session_1"
        self._init_plan(orch, session_id)

        _, enhanced_history, _ = orch._build_enhanced_context(
            session_id, "继续下一步", []
        )
        self.assertGreater(len(enhanced_history), 0)
        content = enhanced_history[0]["content"]
        self.assertIn("## 当前计划进度", content)

    def test_todo_section_contains_goal_and_progress(self):
        """messages[0] 的 todo 段应含 goal 与总进度。"""
        orch = self._make_orchestrator()
        session_id = "user_session_2"
        self._init_plan(orch, session_id)

        _, enhanced_history, _ = orch._build_enhanced_context(
            session_id, "继续", []
        )
        content = enhanced_history[0]["content"]
        self.assertIn("**目标**: 完成 T5 注入测试", content)
        # step 0 已完成，总共 3 步
        self.assertIn("**总进度**: 1/3", content)

    def test_todo_section_contains_step_markers(self):
        """messages[0] 的 todo 段应含 [x] / [ ] 步骤标记。"""
        orch = self._make_orchestrator()
        session_id = "user_session_3"
        self._init_plan(orch, session_id)

        _, enhanced_history, _ = orch._build_enhanced_context(
            session_id, "继续", []
        )
        content = enhanced_history[0]["content"]
        # step 0 已完成
        self.assertIn("[x] 步骤1：分析需求", content)
        # step 1 自动推进为 in_progress，step 2 仍 pending
        self.assertIn("[ ] 步骤2：编写代码", content)
        self.assertIn("[ ] 步骤3：运行测试", content)

    def test_todo_section_contains_reminder_footer(self):
        """messages[0] 的 todo 段末尾应含 update_todo 提醒。"""
        orch = self._make_orchestrator()
        session_id = "user_session_4"
        self._init_plan(orch, session_id)

        _, enhanced_history, _ = orch._build_enhanced_context(
            session_id, "继续", []
        )
        content = enhanced_history[0]["content"]
        self.assertIn(
            "提醒：每完成一个步骤，必须调用 update_todo 标记为 completed",
            content,
        )

    def test_todo_section_after_task_progress(self):
        """TodoList 段应位于 TaskManager 进度段之后。"""
        orch = self._make_orchestrator(with_task_manager=True)
        session_id = "user_session_5"
        self._init_plan(orch, session_id)

        _, enhanced_history, _ = orch._build_enhanced_context(
            session_id, "继续", []
        )
        content = enhanced_history[0]["content"]
        todo_pos = content.find("## 当前计划进度")
        task_pos = content.find("## 当前任务状态")
        self.assertGreaterEqual(todo_pos, 0, "应注入 '## 当前计划进度' 段")
        self.assertGreaterEqual(task_pos, 0, "应注入 '## 当前任务状态' 段")
        self.assertLess(
            task_pos,
            todo_pos,
            "TodoList 段应在 TaskManager 进度段之后"
            "（task_pos < todo_pos）",
        )

    def test_todo_section_after_env_and_memory(self):
        """TodoList 段应位于环境信息段和长期记忆段之后。"""
        orch = self._make_orchestrator()
        session_id = "user_session_6"
        self._init_plan(orch, session_id)

        _, enhanced_history, _ = orch._build_enhanced_context(
            session_id, "继续", []
        )
        content = enhanced_history[0]["content"]
        env_pos = content.find("## 运行环境")
        memory_pos = content.find("## 相关记忆")
        todo_pos = content.find("## 当前计划进度")
        self.assertGreaterEqual(env_pos, 0)
        self.assertGreaterEqual(memory_pos, 0)
        self.assertGreaterEqual(todo_pos, 0)
        self.assertLess(env_pos, memory_pos)
        self.assertLess(memory_pos, todo_pos)

    def test_system_text_not_polluted_by_todo(self):
        """SYSTEM_PROMPT 不应被 TodoList 段污染。"""
        orch = self._make_orchestrator()
        session_id = "user_session_7"
        self._init_plan(orch, session_id)

        system_text, _, _ = orch._build_enhanced_context(
            session_id, "继续", []
        )
        self.assertNotIn("## 当前计划进度", system_text)
        self.assertNotIn("当前计划进度", system_text)
        self.assertIn(SYSTEM_PROMPT, system_text)


# ---------------------------------------------------------------------------
# _build_enhanced_context 降级测试（无 TodoList / 无 plan）
# ---------------------------------------------------------------------------


class TestTodoInjectionDegradation(unittest.TestCase):
    """验证无 TodoList 时 _build_enhanced_context 不抛异常且不注入 todo 段。"""

    def _make_orchestrator(self, with_todo_registry: bool):
        """通过 __new__ 构造 Orchestrator，仅设置测试需要的属性。"""
        orch = Orchestrator.__new__(Orchestrator)
        orch.memory_retriever = MockMemoryRetriever()
        orch.context_manager = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=orch.memory_retriever,
            history_buffer=MockHistoryBuffer(),
        )
        orch.condenser = None
        orch.metrics = None
        orch.task_manager = MockTaskManager()
        orch.todo_registry = (
            TodoListRegistry() if with_todo_registry else None
        )
        return orch

    def test_no_todo_registry_no_exception(self):
        """todo_registry 为 None 时 _build_enhanced_context 不抛异常。"""
        orch = self._make_orchestrator(with_todo_registry=False)
        # 不应抛异常
        system_text, enhanced_history, _ = orch._build_enhanced_context(
            "no_registry_session", "question", []
        )
        # messages[0] 不含 todo 段
        if enhanced_history:
            content = enhanced_history[0]["content"]
            self.assertNotIn("## 当前计划进度", content)

    def test_no_plan_no_exception(self):
        """todo_registry 存在但 session 无 plan 时 _build_enhanced_context 不抛异常。"""
        orch = self._make_orchestrator(with_todo_registry=True)
        # 不调用 init_plan，get_todo_dict 返回 None
        system_text, enhanced_history, _ = orch._build_enhanced_context(
            "no_plan_session", "question", []
        )
        # 不应抛异常且 messages[0] 不含 todo 段
        if enhanced_history:
            content = enhanced_history[0]["content"]
            self.assertNotIn("## 当前计划进度", content)
            self.assertNotIn("**总进度**", content)

    def test_no_todo_registry_does_not_pollute_system_text(self):
        """todo_registry 为 None 时 system_text 不被污染。"""
        orch = self._make_orchestrator(with_todo_registry=False)
        system_text, _, _ = orch._build_enhanced_context(
            "no_registry_session", "question", []
        )
        self.assertNotIn("## 当前计划进度", system_text)
        self.assertIn(SYSTEM_PROMPT, system_text)

    def test_empty_plan_skips_injection(self):
        """todo_registry 存在但 plan 已清空（理论上 steps 为空）时跳过注入。

        注：当前 TodoListRegistry 不支持 steps 为空的 plan（init_plan 会
        抛 ValueError），此用例通过手工构造空 steps 的 dict 模拟边界情况，
        验证 _format_todo_for_injection 的降级逻辑。
        """
        orch = self._make_orchestrator(with_todo_registry=True)
        # 直接调用 _format_todo_for_injection 验证空 steps 降级
        section = orch._format_todo_for_injection(
            {"goal": "g", "steps": [], "completed": False}
        )
        self.assertEqual(section, "")


# ---------------------------------------------------------------------------
# 端到端：todo_registry 状态变化反映到 messages[0]
# ---------------------------------------------------------------------------


class TestTodoInjectionStateUpdate(unittest.TestCase):
    """验证 todo_registry 状态变化会反映到 messages[0] 的 todo 段。"""

    def _make_orchestrator(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.memory_retriever = MockMemoryRetriever()
        orch.context_manager = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=orch.memory_retriever,
            history_buffer=MockHistoryBuffer(),
        )
        orch.condenser = None
        orch.metrics = None
        orch.task_manager = None
        orch.todo_registry = TodoListRegistry()
        return orch

    def test_progress_reflects_state_change(self):
        """同一 session 多次 update_step 后，messages[0] 的总进度应同步更新。"""
        orch = self._make_orchestrator()
        session_id = "state_update_session"
        orch.todo_registry.init_plan(
            session_id,
            "三步任务",
            [
                {"content": "s1"},
                {"content": "s2", "depends_on": [0]},
                {"content": "s3", "depends_on": [1]},
            ],
        )

        # 初始：0/3
        _, history, _ = orch._build_enhanced_context(
            session_id, "q", []
        )
        self.assertIn("**总进度**: 0/3", history[0]["content"])

        # 完成 step 0（自动推进 step 1）：1/3
        orch.todo_registry.update_step(session_id, 0, "completed", "")
        _, history, _ = orch._build_enhanced_context(
            session_id, "q", []
        )
        self.assertIn("**总进度**: 1/3", history[0]["content"])

        # 完成 step 1（自动推进 step 2）：2/3
        orch.todo_registry.update_step(session_id, 1, "completed", "")
        _, history, _ = orch._build_enhanced_context(
            session_id, "q", []
        )
        self.assertIn("**总进度**: 2/3", history[0]["content"])

        # 完成 step 2：3/3
        orch.todo_registry.update_step(session_id, 2, "completed", "")
        _, history, _ = orch._build_enhanced_context(
            session_id, "q", []
        )
        self.assertIn("**总进度**: 3/3", history[0]["content"])
        # 所有步骤都标 [x]
        content = history[0]["content"]
        self.assertIn("[x] s1", content)
        self.assertIn("[x] s2", content)
        self.assertIn("[x] s3", content)


if __name__ == "__main__":
    unittest.main()
