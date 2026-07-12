"""Phase 8 Task 1.4 + 1.5: Cron 隔离层上下文路由测试。

验证：
- CronIsolation.from_session_id 正确解析 cron: 前缀
- Orchestrator 检测 cron: 前缀并走隔离路径
- cron 路径不注入用户画像（system_text = SYSTEM_PROMPT）
- cron 路径不注入 TaskManager 进度
- cron 路径检索记忆按 namespace=cron + cron_id 过滤
- user session 行为完全不变（向后兼容）
- ContextManager.build_cron_context 构建隔离 prompt
- build_cron_context 的 system_text 不含 memory.md 画像
- build_cron_context 的 messages[0] 不含 todo

运行方式:
    python -m unittest tests.test_cron_isolation -v
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
from src.memory.cron_isolation import CronIsolation  # noqa: E402
from src.agent.cron_isolator import CronIsolator  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.orchestrator.enhanced_context import EnhancedContextBuilder  # noqa: E402


# ---------------------------------------------------------------------------
# mock 组件
# ---------------------------------------------------------------------------


class MockMemoryMdManager:
    """Mock MemoryMdManager，read 返回固定的用户画像文本。"""

    def read(self):
        return "# 用户画像\n\n## 背景\n- 测试用户画像内容"

    def read_system_profile(self):
        return self.read()

    def read_section_body(self, section_title):
        return ""


class MockMemoryRetriever:
    """Mock MemoryRetriever，记录 namespace/cron_id 调用参数。

    按 namespace 区分返回内容，便于断言隔离性：
    - namespace="user" 返回 user 记忆文本
    - namespace="cron" + cron_id 返回 cron 记忆文本
    """

    def __init__(self):
        self.calls = []  # 记录所有调用参数

    def get_injection_text(self, user_input, namespace="user", cron_id=None, exclude_types=None):
        self.calls.append(
            {"user_input": user_input, "namespace": namespace, "cron_id": cron_id,
             "exclude_types": exclude_types}
        )
        if namespace == "user":
            return "## 相关记忆\n1. user memory fact (相关度: 0.90)"
        if namespace == "cron":
            return f"## 相关记忆\n1. cron memory for {cron_id} (相关度: 0.85)"
        return ""


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
# CronIsolation 数据结构测试
# ---------------------------------------------------------------------------


class TestCronIsolationParsing(unittest.TestCase):
    """验证 CronIsolation.from_session_id 解析逻辑。"""

    def test_cron_prefix_returns_isolation(self):
        """cron: 前缀的 session_id 返回 CronIsolation 实例。"""
        iso = CronIsolation.from_session_id("cron:sched_abc")
        self.assertIsNotNone(iso)
        self.assertEqual(iso.cron_id, "sched_abc")
        self.assertEqual(iso.namespace, "cron")
        self.assertFalse(iso.inject_profile)
        self.assertFalse(iso.inject_todo)
        self.assertTrue(iso.consolidate)

    def test_user_session_returns_none(self):
        """普通 user session_id 返回 None（不走隔离路径）。"""
        self.assertIsNone(CronIsolation.from_session_id("user_session_123"))
        self.assertIsNone(CronIsolation.from_session_id("abc"))

    def test_none_session_returns_none(self):
        """None session_id 返回 None。"""
        self.assertIsNone(CronIsolation.from_session_id(None))

    def test_empty_cron_id_returns_none(self):
        """cron: 前缀但 cron_id 为空时返回 None（降级到用户会话）。"""
        self.assertIsNone(CronIsolation.from_session_id("cron:"))

    def test_non_string_session_returns_none(self):
        """非字符串 session_id 返回 None。"""
        self.assertIsNone(CronIsolation.from_session_id(123))
        self.assertIsNone(CronIsolation.from_session_id(["cron:abc"]))


# ---------------------------------------------------------------------------
# Orchestrator cron 路径路由测试（SubTask 1.4）
# ---------------------------------------------------------------------------


class TestOrchestratorCronRouting(unittest.IsolatedAsyncioTestCase):
    """验证 Orchestrator 检测 cron: 前缀并走隔离路径。

    注：``_build_enhanced_context`` 已 async（Phase 10 异步化改造），
    本类用 IsolatedAsyncioTestCase + await。
    """

    def _make_orchestrator(self, with_task_manager=True):
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
        # Phase 9 Task 5: _build_enhanced_context 现访问 todo_registry，
        # 测试不验证 plan 模式注入，置 None 走降级路径。
        orch.todo_registry = None
        # CronIsolator 委托（方法对象模式，持有 orch 引用）
        orch.cron_isolator = CronIsolator(orchestrator=orch)
        orch.enhanced_context_builder = EnhancedContextBuilder(orch)
        return orch

    async def test_cron_session_uses_cron_namespace_for_retrieval(self):
        """cron session 检索记忆时传 namespace=cron + cron_id。"""
        orch = self._make_orchestrator()
        system_text, enhanced_history, _ = await orch.enhanced_context_builder.build(
            "cron:sched_X", "python question", []
        )
        # 检查 retriever 被调用时传了 cron namespace
        calls = orch.memory_retriever.calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["namespace"], "cron")
        self.assertEqual(calls[0]["cron_id"], "sched_X")

    async def test_cron_session_system_text_equals_prompt(self):
        """cron session 的 system_text 等于 SYSTEM_PROMPT（含工具选择指南）。"""
        orch = self._make_orchestrator()
        system_text, _, _ = await orch.enhanced_context_builder.build(
            "cron:sched_X", "question", []
        )
        self.assertEqual(system_text, SYSTEM_PROMPT)
        # 工具选择指南中列出了 profile_update，但 cron 会话不注入 memory.md 画像内容
        self.assertIn("用户画像", system_text)
        self.assertNotIn("测试用户画像内容", system_text)

    async def test_cron_session_does_not_inject_todo(self):
        """cron session 不注入 TaskManager 进度。"""
        orch = self._make_orchestrator(with_task_manager=True)
        _, enhanced_history, _ = await orch.enhanced_context_builder.build(
            "cron:sched_X", "question", []
        )
        # 找到注入的 user 消息（history 前置的 injection_text）
        injection_msg = enhanced_history[0]
        content = injection_msg["content"]
        self.assertNotIn("任务进度", content)
        self.assertNotIn("当前任务状态", content)

    async def test_cron_session_injects_cron_memory(self):
        """cron session 注入自己命名空间的记忆。"""
        orch = self._make_orchestrator()
        _, enhanced_history, _ = await orch.enhanced_context_builder.build(
            "cron:sched_X", "question", []
        )
        injection_msg = enhanced_history[0]
        self.assertIn("cron memory for sched_X", injection_msg["content"])

    async def test_user_session_uses_user_namespace(self):
        """user session 检索记忆时传 namespace=user（默认）。"""
        orch = self._make_orchestrator()
        await orch.enhanced_context_builder.build("user_session", "question", [])
        calls = orch.memory_retriever.calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["namespace"], "user")
        self.assertIsNone(calls[0]["cron_id"])

    async def test_user_session_includes_profile(self):
        """user session 的 system_text 含 memory.md 用户画像。"""
        orch = self._make_orchestrator()
        system_text, _, _ = await orch.enhanced_context_builder.build(
            "user_session", "question", []
        )
        self.assertIn("测试用户画像内容", system_text)

    async def test_user_session_includes_todo(self):
        """user session 注入 TaskManager 进度。"""
        orch = self._make_orchestrator(with_task_manager=True)
        _, enhanced_history, _ = await orch.enhanced_context_builder.build(
            "user_session", "question", []
        )
        injection_msg = enhanced_history[0]
        self.assertIn("任务进度", injection_msg["content"])

    async def test_user_session_behavior_unchanged(self):
        """user session 行为完全不变（向后兼容关键场景）。"""
        orch = self._make_orchestrator(with_task_manager=False)
        # 无 task_manager 时 user session 也不注入 todo（原有行为）
        system_text, enhanced_history, _ = await orch.enhanced_context_builder.build(
            "user_session", "question", []
        )
        self.assertIn("测试用户画像内容", system_text)
        # 注入 user 记忆
        self.assertIn("user memory fact", enhanced_history[0]["content"])
        # 不含 todo
        self.assertNotIn("任务进度", enhanced_history[0]["content"])


# ---------------------------------------------------------------------------
# ContextManager.build_cron_context 测试（SubTask 1.5）
# ---------------------------------------------------------------------------


class TestContextManagerBuildCronContext(unittest.TestCase):
    """验证 ContextManager.build_cron_context 构建隔离 prompt。"""

    def _make_context_manager(self):
        """构造带 mock 依赖的 ContextManager。"""
        retriever = MockMemoryRetriever()
        return (
            ContextManager(
                tool_registry=MockToolRegistry(),
                memory_md_manager=MockMemoryMdManager(),
                memory_retriever=retriever,
                history_buffer=MockHistoryBuffer(),
            ),
            retriever,
        )

    def test_cron_system_text_excludes_profile(self):
        """build_cron_context 的 system 含工具选择指南（含 profile），不注 memory.md 画像。"""
        cm, _ = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso
        )
        self.assertEqual(prompt["system"], SYSTEM_PROMPT)
        # 工具选择指南列出了 profile_update，但 cron 会话不注入 memory.md 画像内容
        self.assertIn("用户画像", prompt["system"])
        self.assertNotIn("测试用户画像内容", prompt["system"])

    def test_cron_messages_exclude_todo(self):
        """build_cron_context 的 messages[0] 不含 TaskManager 进度。"""
        cm, _ = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso
        )
        # messages[0] 是检索记忆注入，不应含任务进度
        messages = prompt["messages"]
        # 找到第一条 user 消息（注入内容）
        injection_content = messages[0]["content"]
        self.assertNotIn("任务进度", injection_content)
        self.assertNotIn("当前任务状态", injection_content)

    def test_cron_messages_include_cron_memory(self):
        """build_cron_context 注入 cron namespace 检索记忆。"""
        cm, retriever = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso
        )
        # 验证 retriever 被调用时传了 cron namespace
        self.assertEqual(len(retriever.calls), 1)
        self.assertEqual(retriever.calls[0]["namespace"], "cron")
        self.assertEqual(retriever.calls[0]["cron_id"], "sched_A")
        # messages[0] 含 cron 记忆
        self.assertIn("cron memory for sched_A", prompt["messages"][0]["content"])

    def test_cron_messages_last_is_user_input(self):
        """build_cron_context 最后一条消息是当前用户输入。"""
        cm, _ = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        prompt = cm.build_cron_context(
            "cron:sched_A", "my question", cron_iso
        )
        self.assertEqual(
            prompt["messages"][-1], {"role": "user", "content": "my question"}
        )

    def test_cron_tools_from_registry(self):
        """build_cron_context 默认从 tool_registry 取工具 schema。"""
        cm, _ = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso
        )
        self.assertEqual(len(prompt["tools"]), 1)
        self.assertEqual(prompt["tools"][0]["name"], "test_tool")

    def test_cron_tools_override(self):
        """build_cron_context 支持 tools_override 请求级隔离。"""
        cm, _ = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        override = [{"name": "filtered_tool", "description": "过滤后", "input_schema": {}}]
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso, tools_override=override
        )
        self.assertEqual(len(prompt["tools"]), 1)
        self.assertEqual(prompt["tools"][0]["name"], "filtered_tool")

    def test_cron_extra_injection_appended(self):
        """build_cron_context 支持 extra_injection 工作流数据拼接。"""
        cm, _ = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        extra = "## 工作流数据\n- 扫描结果: 3 个文件变更"
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso, extra_injection=extra
        )
        # messages[0] 应同时含 cron 记忆 + 工作流数据
        content = prompt["messages"][0]["content"]
        self.assertIn("cron memory for sched_A", content)
        self.assertIn("工作流数据", content)
        self.assertIn("3 个文件变更", content)

    def test_cron_cache_stability(self):
        """build_cron_context 的 system + tools 字节级稳定（缓存约束验证）。"""
        cm, _ = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        p1 = cm.build_cron_context("cron:sched_A", "q1", cron_iso)
        p2 = cm.build_cron_context("cron:sched_A", "q2", cron_iso)
        self.assertEqual(p1["system"], p2["system"])
        self.assertEqual(p1["tools"], p2["tools"])

    def test_cron_no_memory_retriever_returns_empty_injection(self):
        """无 memory_retriever 时跳过记忆注入，仍正常构建 messages。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=None,
            history_buffer=MockHistoryBuffer(),
        )
        cron_iso = CronIsolation(cron_id="sched_A")
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso
        )
        # 无注入时 messages[0] 应为历史第一条
        messages = prompt["messages"]
        self.assertEqual(messages[0]["content"], "历史用户")


if __name__ == "__main__":
    unittest.main()
