"""运行环境信息注入功能测试。

验证：
- Orchestrator._build_environment_section 构造含必需字段
  （OS / 工作目录 / Shell / Python 启动命令 / Python 路径）
- 跨平台自适应（Windows 检测 PowerShell，Linux/Mac 检测 bash）
- 用户会话 messages[0] 顶部含 "## 运行环境" 段
- 环境信息注入位置在 TaskManager 进度注入之前
- cron 会话 messages[0] 顶部含 "## 运行环境" 段
- ContextManager.build_cron_context 支持 env_section 参数注入
- SYSTEM_PROMPT 未被污染（环境信息不写入 system_text）

运行方式:
    python -m pytest tests/test_environment_injection.py -v
    或
    python -m unittest tests.test_environment_injection -v
"""

from __future__ import annotations

import os
import platform
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
from src.agent.context_builder import ContextBuilder  # noqa: E402
from src.agent.cron_isolator import CronIsolator  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402


# ---------------------------------------------------------------------------
# mock 组件（与 test_cron_isolation.py 风格保持一致）
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
# Orchestrator._build_environment_section 单元测试
# ---------------------------------------------------------------------------


class TestBuildEnvironmentSection(unittest.TestCase):
    """验证 Orchestrator._build_environment_section 构造的环境信息段。"""

    def _make_orchestrator(self):
        """通过 __new__ 构造 Orchestrator，仅设置测试需要的属性。"""
        orch = Orchestrator.__new__(Orchestrator)
        # _build_environment_section 委托到 context_builder
        orch.context_builder = ContextBuilder()
        return orch

    def test_method_exists(self):
        """Orchestrator 类应包含 _build_environment_section 方法。"""
        self.assertTrue(hasattr(Orchestrator, "_build_environment_section"))
        self.assertTrue(callable(getattr(Orchestrator, "_build_environment_section")))

    def test_returns_non_empty_string(self):
        """方法返回值应为非空字符串。"""
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        self.assertIsInstance(section, str)
        self.assertGreater(len(section), 0)

    def test_section_starts_with_header(self):
        """环境信息段应以 '## 运行环境' markdown 标题开头。"""
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        self.assertTrue(
            section.startswith("## 运行环境"),
            f"环境信息段应以 '## 运行环境' 开头，实际开头: {section[:30]!r}",
        )

    def test_section_contains_required_fields(self):
        """环境信息段应包含 5 个必需字段。"""
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        # 必需字段标题
        self.assertIn("操作系统:", section)
        self.assertIn("工作目录:", section)
        self.assertIn("Shell:", section)
        self.assertIn("Python 启动命令:", section)
        self.assertIn("Python 路径:", section)

    def test_section_contains_real_os_info(self):
        """环境信息段应含真实的 OS 信息（与 platform.system 一致）。"""
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        expected_os = f"{platform.system()} {platform.release()}"
        self.assertIn(expected_os, section)

    def test_section_contains_real_cwd(self):
        """环境信息段应含真实的工作目录（与 os.getcwd() 一致）。"""
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        self.assertIn(os.getcwd(), section)

    def test_section_contains_real_python_path(self):
        """环境信息段应含真实的 Python 解释器路径（与 sys.executable 一致）。"""
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        self.assertIn(sys.executable, section)

    def test_section_shell_adapt_windows(self):
        """Windows 平台下 Shell 字段应为 PowerShell 或 cmd。"""
        if platform.system() != "Windows":
            self.skipTest("仅在 Windows 平台下运行")
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        # 提取 Shell 行
        shell_line = next(
            (line for line in section.splitlines() if line.startswith("- Shell:")),
            None,
        )
        self.assertIsNotNone(shell_line, "环境信息段应含 Shell 行")
        shell_value = shell_line.split(":", 1)[1].strip()
        self.assertIn(shell_value, ("PowerShell", "cmd"))

    def test_section_shell_adapt_non_windows(self):
        """非 Windows 平台下 Shell 字段应为 bash 或 sh。"""
        if platform.system() == "Windows":
            self.skipTest("仅在非 Windows 平台下运行")
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        shell_line = next(
            (line for line in section.splitlines() if line.startswith("- Shell:")),
            None,
        )
        self.assertIsNotNone(shell_line, "环境信息段应含 Shell 行")
        shell_value = shell_line.split(":", 1)[1].strip()
        self.assertIn(shell_value, ("bash", "sh"))

    def test_section_python_cmd_is_python3_or_python(self):
        """Python 启动命令字段应为 python3 或 python。"""
        orch = self._make_orchestrator()
        section = orch._build_environment_section()
        cmd_line = next(
            (
                line
                for line in section.splitlines()
                if line.startswith("- Python 启动命令:")
            ),
            None,
        )
        self.assertIsNotNone(cmd_line, "环境信息段应含 Python 启动命令行")
        cmd_value = cmd_line.split(":", 1)[1].strip()
        self.assertIn(cmd_value, ("python3", "python"))

    def test_section_stable_within_same_process(self):
        """同一进程内多次调用返回结果一致（运行时真实值稳定）。"""
        orch = self._make_orchestrator()
        section1 = orch._build_environment_section()
        section2 = orch._build_environment_section()
        self.assertEqual(section1, section2)


# ---------------------------------------------------------------------------
# Orchestrator 用户会话路径注入测试
# ---------------------------------------------------------------------------


class TestUserSessionEnvironmentInjection(unittest.IsolatedAsyncioTestCase):
    """验证用户会话 _build_enhanced_context 注入环境信息到 messages[0]。

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
        # 此测试不验证 plan 模式注入，置 None 走降级路径。
        orch.todo_registry = None
        # 委托管理器（方法对象模式，持有 orch 引用）
        orch.context_builder = ContextBuilder()
        orch.cron_isolator = CronIsolator(orchestrator=orch)
        return orch

    async def test_user_session_messages_zero_contains_env_section(self):
        """用户会话 messages[0] 顶部应含 '## 运行环境' 段。"""
        orch = self._make_orchestrator()
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "user_session", "question", []
        )
        # messages[0] 应为注入的 user 消息
        self.assertGreater(len(enhanced_history), 0)
        content = enhanced_history[0]["content"]
        self.assertIn("## 运行环境", content)
        # 环境信息应在最前面
        self.assertTrue(
            content.startswith("## 运行环境"),
            f"messages[0] 应以 '## 运行环境' 开头，实际开头: {content[:30]!r}",
        )

    async def test_user_session_env_section_before_task_progress(self):
        """环境信息注入位置应在 TaskManager 进度注入之前。"""
        orch = self._make_orchestrator(with_task_manager=True)
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "user_session", "question", []
        )
        content = enhanced_history[0]["content"]
        env_pos = content.find("## 运行环境")
        task_pos = content.find("## 当前任务状态")
        self.assertGreaterEqual(env_pos, 0, "应注入 '## 运行环境' 段")
        self.assertGreaterEqual(task_pos, 0, "应注入 '## 当前任务状态' 段")
        self.assertLess(
            env_pos,
            task_pos,
            "环境信息应在 TaskManager 进度之前（env_pos < task_pos）",
        )

    async def test_user_session_env_section_before_memory(self):
        """环境信息注入位置应在长期记忆注入之前。"""
        orch = self._make_orchestrator(with_task_manager=False)
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "user_session", "question", []
        )
        content = enhanced_history[0]["content"]
        env_pos = content.find("## 运行环境")
        memory_pos = content.find("## 相关记忆")
        self.assertGreaterEqual(env_pos, 0, "应注入 '## 运行环境' 段")
        self.assertGreaterEqual(memory_pos, 0, "应注入 '## 相关记忆' 段")
        self.assertLess(
            env_pos,
            memory_pos,
            "环境信息应在长期记忆之前（env_pos < memory_pos）",
        )

    async def test_user_session_system_text_not_polluted(self):
        """SYSTEM_PROMPT 不应被环境信息污染（system_text 中不含 '## 运行环境'）。"""
        orch = self._make_orchestrator()
        system_text, _, _ = await orch._build_enhanced_context(
            "user_session", "question", []
        )
        self.assertNotIn("## 运行环境", system_text)
        # system_text 仍含 SYSTEM_PROMPT 基线
        self.assertIn(SYSTEM_PROMPT, system_text)

    async def test_user_session_env_section_contains_required_fields(self):
        """用户会话注入的环境信息段应含 5 个必需字段。"""
        orch = self._make_orchestrator()
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "user_session", "question", []
        )
        content = enhanced_history[0]["content"]
        self.assertIn("操作系统:", content)
        self.assertIn("工作目录:", content)
        self.assertIn("Shell:", content)
        self.assertIn("Python 启动命令:", content)
        self.assertIn("Python 路径:", content)


# ---------------------------------------------------------------------------
# Orchestrator cron 会话路径注入测试
# ---------------------------------------------------------------------------


class TestCronSessionEnvironmentInjection(unittest.IsolatedAsyncioTestCase):
    """验证 cron 会话 _build_enhanced_context 注入环境信息到 messages[0]。

    注：``_build_enhanced_context`` 已 async（Phase 10 异步化改造），
    本类用 IsolatedAsyncioTestCase + await。
    """

    def _make_orchestrator(self):
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
        orch.task_manager = MockTaskManager()  # cron 路径不应注入 todo
        orch.cron_scheduler = None
        orch.cron_tool_registry = None
        orch.tool_registry = MockToolRegistry()
        # Phase 9 Task 5: _build_enhanced_context 现访问 todo_registry，
        # cron 路径不应注入 plan todo，置 None 走降级路径。
        orch.todo_registry = None
        # 委托管理器（方法对象模式，持有 orch 引用）
        orch.context_builder = ContextBuilder()
        orch.cron_isolator = CronIsolator(orchestrator=orch)
        return orch

    async def test_cron_session_messages_zero_contains_env_section(self):
        """cron 会话 messages[0] 顶部应含 '## 运行环境' 段。"""
        orch = self._make_orchestrator()
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "cron:sched_X", "question", []
        )
        self.assertGreater(len(enhanced_history), 0)
        content = enhanced_history[0]["content"]
        self.assertIn("## 运行环境", content)
        # 环境信息应在最前面
        self.assertTrue(
            content.startswith("## 运行环境"),
            f"cron messages[0] 应以 '## 运行环境' 开头，实际开头: {content[:30]!r}",
        )

    async def test_cron_session_env_section_before_memory(self):
        """cron 环境信息注入位置应在长期记忆注入之前。"""
        orch = self._make_orchestrator()
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "cron:sched_X", "question", []
        )
        content = enhanced_history[0]["content"]
        env_pos = content.find("## 运行环境")
        memory_pos = content.find("## 相关记忆")
        self.assertGreaterEqual(env_pos, 0, "应注入 '## 运行环境' 段")
        self.assertGreaterEqual(memory_pos, 0, "应注入 '## 相关记忆' 段")
        self.assertLess(
            env_pos,
            memory_pos,
            "cron 环境信息应在长期记忆之前（env_pos < memory_pos）",
        )

    async def test_cron_session_does_not_inject_todo(self):
        """cron 会话不应注入 TaskManager 进度。"""
        orch = self._make_orchestrator()
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "cron:sched_X", "question", []
        )
        content = enhanced_history[0]["content"]
        self.assertNotIn("## 当前任务状态", content)
        self.assertNotIn("任务进度", content)

    async def test_cron_session_system_text_not_polluted(self):
        """cron 会话 SYSTEM_PROMPT 不应被环境信息污染。"""
        orch = self._make_orchestrator()
        system_text, _, _ = await orch._build_enhanced_context(
            "cron:sched_X", "question", []
        )
        self.assertNotIn("## 运行环境", system_text)
        self.assertEqual(system_text, SYSTEM_PROMPT)

    async def test_cron_session_env_section_contains_required_fields(self):
        """cron 会话注入的环境信息段应含 5 个必需字段。"""
        orch = self._make_orchestrator()
        _, enhanced_history, _ = await orch._build_enhanced_context(
            "cron:sched_X", "question", []
        )
        content = enhanced_history[0]["content"]
        self.assertIn("操作系统:", content)
        self.assertIn("工作目录:", content)
        self.assertIn("Shell:", content)
        self.assertIn("Python 启动命令:", content)
        self.assertIn("Python 路径:", content)


# ---------------------------------------------------------------------------
# ContextManager.build_cron_context env_section 参数测试
# ---------------------------------------------------------------------------


class TestContextManagerEnvSection(unittest.TestCase):
    """验证 ContextManager.build_cron_context 支持 env_section 参数。"""

    def _make_context_manager(self):
        """构造带 mock 依赖的 ContextManager。"""
        return ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=MockMemoryRetriever(),
            history_buffer=MockHistoryBuffer(),
        )

    def test_env_section_injected_at_top_of_messages_zero(self):
        """env_section 注入到 messages[0] 最前面（在 time_context 之前）。"""
        cm = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        env = "## 运行环境\n- 操作系统: TestOS\n- 工作目录: /tmp"
        time_ctx = "## 时间上下文\n- current_time: 2026-06-30"
        prompt = cm.build_cron_context(
            "cron:sched_A",
            "question",
            cron_iso,
            time_context=time_ctx,
            env_section=env,
        )
        content = prompt["messages"][0]["content"]
        env_pos = content.find("## 运行环境")
        time_pos = content.find("## 时间上下文")
        self.assertGreaterEqual(env_pos, 0, "应注入 env_section")
        self.assertGreaterEqual(time_pos, 0, "应注入 time_context")
        self.assertLess(
            env_pos,
            time_pos,
            "env_section 应在 time_context 之前（env_pos < time_pos）",
        )

    def test_env_section_injected_when_only_env(self):
        """仅传 env_section（其他注入为 None）时正常注入。"""
        cm = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        env = "## 运行环境\n- 操作系统: TestOS"
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso, env_section=env
        )
        content = prompt["messages"][0]["content"]
        self.assertIn("## 运行环境", content)
        self.assertIn("TestOS", content)

    def test_env_section_none_skips_injection(self):
        """env_section 为 None 时跳过注入（向后兼容）。"""
        cm = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        # 不传 env_section（默认 None）
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso
        )
        content = prompt["messages"][0]["content"]
        self.assertNotIn("## 运行环境", content)

    def test_env_section_combined_with_other_injections(self):
        """env_section 与 time_context / history_summaries / extra_injection 同时注入。"""
        cm = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        env = "## 运行环境\n- 操作系统: TestOS"
        time_ctx = "## 时间上下文\n- current_time: 2026-06-30"
        summaries = "## 历史执行摘要\n- 上次执行成功"
        extra = "## 工作流数据\n- 扫描结果: 3 个文件变更"
        prompt = cm.build_cron_context(
            "cron:sched_A",
            "question",
            cron_iso,
            extra_injection=extra,
            time_context=time_ctx,
            history_summaries=summaries,
            env_section=env,
        )
        content = prompt["messages"][0]["content"]
        # 所有注入字段均存在
        self.assertIn("## 运行环境", content)
        self.assertIn("## 时间上下文", content)
        self.assertIn("## 历史执行摘要", content)
        self.assertIn("## 工作流数据", content)
        self.assertIn("cron memory for sched_A", content)  # 检索记忆
        # env_section 在最前面
        self.assertTrue(
            content.startswith("## 运行环境"),
            f"messages[0] 应以 env_section 开头，实际开头: {content[:30]!r}",
        )

    def test_env_section_does_not_pollute_system_text(self):
        """env_section 不污染 system_text（缓存命中区不受影响）。"""
        cm = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        env = "## 运行环境\n- 操作系统: TestOS"
        prompt = cm.build_cron_context(
            "cron:sched_A", "question", cron_iso, env_section=env
        )
        self.assertEqual(prompt["system"], SYSTEM_PROMPT)
        self.assertNotIn("## 运行环境", prompt["system"])

    def test_env_section_backward_compatible_signature(self):
        """build_cron_context 签名向后兼容（不传 env_section 仍可调用）。"""
        cm = self._make_context_manager()
        cron_iso = CronIsolation(cron_id="sched_A")
        # 模拟旧调用方式（无 env_section 参数）
        prompt = cm.build_cron_context(
            "cron:sched_A",
            "question",
            cron_iso,
            tools_override=None,
            extra_injection=None,
            time_context=None,
            history_summaries=None,
        )
        # 应正常返回
        self.assertIn("messages", prompt)
        self.assertIn("system", prompt)
        self.assertIn("tools", prompt)


if __name__ == "__main__":
    unittest.main()
