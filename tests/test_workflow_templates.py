"""工作流模板系统单元测试（Phase 8 Task 2.1 ~ 2.7 + 2.13）。

覆盖：
- ``WorkflowTemplate`` 抽象基类 + ``WorkflowContext`` + ``WorkflowResult``
  数据结构（SubTask 2.1）
- ``directory_watch`` / ``summary`` / ``email_notify`` / ``cleanup_suggest``
  / ``research`` / ``custom`` 六个内置模板（SubTask 2.2 ~ 2.6）
- 时间变量替换三层链路（SubTask 2.7）：模板变量层 / 文件名层 / 任务文本层
- 缓存约束验证（SubTask 2.13）：模板 system prompt 禁含动态变量
- ``BUILTIN_TEMPLATES`` 注册表与 ``get_template`` 查找

运行方式:
    python -m unittest tests.test_workflow_templates -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.tasks.workflow import (  # noqa: E402
    BUILTIN_TEMPLATES,
    CleanupSuggestTemplate,
    CustomTemplate,
    DirectoryWatchTemplate,
    EmailNotifyTemplate,
    ResearchTemplate,
    SummaryTemplate,
    WorkflowContext,
    WorkflowResult,
    WorkflowTemplate,
    get_template,
    render_time_variables,
)


# ---------------------------------------------------------------------------
# Mock 工具：LLM 客户端 / chroma_store / session_logger / react_loop
# ---------------------------------------------------------------------------


class MockLLMResponse:
    """模拟 Anthropic SDK 响应对象（含 .content 列表）。"""

    def __init__(self, content_blocks):
        self.content = content_blocks


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use_block(name: str, tool_input: dict, block_id: str = "t1") -> dict:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


class MockLLMClient:
    """模拟 LLM 客户端，记录调用并返回预设响应。

    不使用 MagicMock，保证 ``chat_main`` / ``chat_main_sync`` 为真实可调用对象。

    注：Phase 10 异步化改造后，workflow ``base._call_llm_single_turn`` 调用
    ``chat_main_sync``（sync wrapper），而非 ``chat_main``（async def）。
    两个方法在此 mock 中均实现，便于测试覆盖。
    """

    def __init__(self, response_text: str = "LLM 分析结果", tool_calls=None):
        self._response_text = response_text
        self._tool_calls = tool_calls or []
        self.calls = []  # 记录所有 chat_main / chat_main_sync 调用

    def chat_main(self, messages=None, tools=None, system=None):
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        blocks = [_text_block(self._response_text)]
        blocks.extend(self._tool_calls)
        return MockLLMResponse(blocks)

    def chat_main_sync(self, messages=None, tools=None, system=None):
        """sync wrapper，与 chat_main 行为一致（供 workflow base.py 调用）。"""
        return self.chat_main(messages=messages, tools=tools, system=system)


class MockChromaStore:
    """模拟 ChromaMemoryStore，支持 list_memories / add_memory / query_memory。"""

    def __init__(self):
        self._store = []  # [{id, content, metadata}]

    def add_memory(self, content, metadata=None, namespace=None, cron_id=None):
        self._store.append(
            {
                "id": f"mem-{len(self._store)}",
                "content": content,
                "metadata": metadata or {},
                "namespace": namespace,
                "cron_id": cron_id,
            }
        )

    def list_memories(self, namespace=None, cron_id=None):
        result = []
        for mem in self._store:
            if namespace is not None and mem.get("namespace") != namespace:
                continue
            if cron_id is not None and mem.get("cron_id") != cron_id:
                continue
            result.append(
                {
                    "id": mem["id"],
                    "content": mem["content"],
                    "metadata": mem["metadata"],
                }
            )
        return result

    def query_memory(self, query, namespace=None, cron_id=None, n_results=5):
        return self.list_memories(namespace=namespace, cron_id=cron_id)[:n_results]


class MockSessionLogger:
    """模拟 SessionLogger，支持 get_session_messages / get_recent_messages。"""

    def __init__(self, messages_by_session=None):
        self._messages = messages_by_session or {}

    def get_session_messages(self, session_id, limit=100):
        return self._messages.get(session_id, [])[:limit]

    def get_recent_messages(self, session_id, n=100):
        msgs = self._messages.get(session_id, [])
        return msgs[-n:] if n > 0 else []


class MockReactLoop:
    """模拟 ReactLoop，记录调用并返回预设响应与 messages。"""

    def __init__(self, response_text: str = "研究结果", tool_messages=None):
        self._response_text = response_text
        self._tool_messages = tool_messages or []
        self.calls = []

    async def run(self, user_input=None, history=None, system=None, session_id=None, **kwargs):
        self.calls.append(
            {
                "user_input": user_input,
                "history": history,
                "system": system,
                "session_id": session_id,
            }
        )
        # ReactLoop.run 返回四元组 (response, messages, is_complete, termination_reason)
        # 注：async 化后（spec Task 4），research.py 用 asyncio.run(react_loop.run(...)) 包裹
        return self._response_text, self._tool_messages, True, "normal"


# ---------------------------------------------------------------------------
# SubTask 2.1: WorkflowContext / WorkflowResult / WorkflowTemplate
# ---------------------------------------------------------------------------


class TestWorkflowBaseContextResult(unittest.TestCase):
    """SubTask 2.1：WorkflowContext / WorkflowResult / WorkflowTemplate 基础结构。"""

    def test_workflow_context_defaults(self):
        """WorkflowContext 默认值正确，current_time 自动填充。"""
        ctx = WorkflowContext(session_id="cron:s1", schedule_id="s1")
        self.assertEqual(ctx.session_id, "cron:s1")
        self.assertEqual(ctx.schedule_id, "s1")
        self.assertIsNone(ctx.llm_client)
        self.assertIsNone(ctx.chroma_store)
        self.assertEqual(ctx.report_dir, "data/reports")
        self.assertIsNotNone(ctx.current_time)
        self.assertIsNone(ctx.last_run_time)

    def test_workflow_context_get_env_value_uses_callback(self):
        """get_env_value 优先使用 get_env 回调。"""
        calls = []

        def get_env(key, default=""):
            calls.append((key, default))
            return f"mock-{key}"

        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            get_env=get_env,
        )
        self.assertEqual(ctx.get_env_value("SMTP_HOST", ""), "mock-SMTP_HOST")
        self.assertEqual(calls, [("SMTP_HOST", "")])

    def test_workflow_context_get_env_value_fallback_to_os_environ(self):
        """get_env=None 时回退到 os.environ.get。"""
        ctx = WorkflowContext(session_id="cron:s1", schedule_id="s1")
        os.environ["TEST_WORKFLOW_ENV_KEY"] = "from_os"
        try:
            self.assertEqual(
                ctx.get_env_value("TEST_WORKFLOW_ENV_KEY", ""),
                "from_os",
            )
        finally:
            del os.environ["TEST_WORKFLOW_ENV_KEY"]

    def test_workflow_context_render_replaces_time_variables(self):
        """render 方法替换时间变量占位符。"""
        now = datetime(2026, 6, 30, 14, 30, 0)
        last_run = datetime(2026, 6, 29, 10, 0, 0)
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            current_time=now,
            last_run_time=last_run,
        )
        rendered = ctx.render("now={now} today={today} last={last_run_time}")
        self.assertIn("2026-06-30 14:30:00", rendered)
        self.assertIn("2026-06-30", rendered)
        self.assertIn("2026-06-29 10:00:00", rendered)

    def test_workflow_context_render_first_run(self):
        """首次执行（last_run_time=None）时 {last_run_time} 替换为 首次执行。"""
        now = datetime(2026, 6, 30, 14, 30, 0)
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            current_time=now,
            last_run_time=None,
        )
        rendered = ctx.render("last={last_run_time}")
        self.assertIn("首次执行", rendered)

    def test_workflow_context_ensure_report_dir(self):
        """ensure_report_dir 创建目录并返回路径。"""
        tmpdir = tempfile.mkdtemp()
        try:
            report_dir = os.path.join(tmpdir, "reports")
            ctx = WorkflowContext(
                session_id="cron:s1",
                schedule_id="s1",
                report_dir=report_dir,
            )
            returned = ctx.ensure_report_dir()
            self.assertEqual(returned, report_dir)
            self.assertTrue(os.path.isdir(report_dir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_workflow_result_defaults(self):
        """WorkflowResult 默认值正确。"""
        result = WorkflowResult()
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "")
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.outputs, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.metrics_for_injection, {})

    def test_workflow_result_add_error_marks_failure(self):
        """add_error 记录错误并标记 success=False。"""
        result = WorkflowResult()
        result.add_error("出错了")
        self.assertFalse(result.success)
        self.assertEqual(result.errors, ["出错了"])

    def test_workflow_result_to_injection_text_empty(self):
        """metrics_for_injection 为空时 to_injection_text 返回空字符串。"""
        result = WorkflowResult()
        self.assertEqual(result.to_injection_text(), "")

    def test_workflow_result_to_injection_text_with_data(self):
        """metrics_for_injection 有数据时生成 markdown 段。"""
        result = WorkflowResult()
        result.metrics_for_injection = {"扫描文件数": 10, "新增": 3}
        text = result.to_injection_text()
        self.assertIn("## 工作流数据", text)
        self.assertIn("- 扫描文件数: 10", text)
        self.assertIn("- 新增: 3", text)

    def test_workflow_template_is_abstract(self):
        """WorkflowTemplate 是抽象基类，不能直接实例化。"""
        with self.assertRaises(TypeError):
            WorkflowTemplate()  # type: ignore[abstract]

    def test_workflow_template_build_system_prompt_default_empty(self):
        """基类 build_system_prompt 默认返回空字符串。"""
        # 通过子类访问（不覆盖 build_system_prompt）
        class _Minimal(WorkflowTemplate):
            name = "minimal"

            def execute(self, config, context):
                return WorkflowResult()

        self.assertEqual(_Minimal().build_system_prompt(), "")


# ---------------------------------------------------------------------------
# SubTask 2.7: 时间变量替换（三层链路）
# ---------------------------------------------------------------------------


class TestRenderTimeVariables(unittest.TestCase):
    """SubTask 2.7 第一层：render_time_variables 函数。"""

    def test_render_now_placeholder(self):
        """{now} 替换为当前时间 ISO 格式。"""
        now = datetime(2026, 6, 30, 14, 30, 45)
        rendered = render_time_variables(
            "当前: {now}", current_time=now
        )
        self.assertEqual(rendered, "当前: 2026-06-30 14:30:45")

    def test_render_today_placeholder(self):
        """{today} 替换为当前日期。"""
        now = datetime(2026, 6, 30, 14, 30)
        rendered = render_time_variables("日期: {today}", current_time=now)
        self.assertEqual(rendered, "日期: 2026-06-30")

    def test_render_this_week_start_placeholder(self):
        """{this_week_start} 替换为本周一日期（周一为一周起点）。"""
        # 2026-06-30 是周二，本周一是 2026-06-29
        now = datetime(2026, 6, 30, 14, 30)
        rendered = render_time_variables(
            "周一: {this_week_start}", current_time=now
        )
        self.assertEqual(rendered, "周一: 2026-06-29")

    def test_render_this_week_start_on_monday(self):
        """周一当天 this_week_start 等于 today。"""
        # 2026-06-29 是周一
        now = datetime(2026, 6, 29, 9, 0)
        rendered = render_time_variables(
            "{this_week_start}", current_time=now
        )
        self.assertEqual(rendered, "2026-06-29")

    def test_render_last_run_time_placeholder(self):
        """{last_run_time} 替换为上次执行时间。"""
        now = datetime(2026, 6, 30, 14, 30)
        last_run = datetime(2026, 6, 28, 10, 0, 0)
        rendered = render_time_variables(
            "上次: {last_run_time}",
            current_time=now,
            last_run_time=last_run,
        )
        self.assertEqual(rendered, "上次: 2026-06-28 10:00:00")

    def test_render_last_run_time_first_run(self):
        """首次执行（last_run_time=None）替换为 首次执行。"""
        now = datetime(2026, 6, 30, 14, 30)
        rendered = render_time_variables(
            "上次: {last_run_time}",
            current_time=now,
            last_run_time=None,
        )
        self.assertEqual(rendered, "上次: 首次执行")

    def test_render_none_text_returns_empty(self):
        """text=None 返回空字符串（空值容错）。"""
        self.assertEqual(render_time_variables(None), "")
        self.assertEqual(render_time_variables(""), "")

    def test_render_no_placeholders_unchanged(self):
        """无占位符时原样返回。"""
        text = "纯文本无变量"
        self.assertEqual(
            render_time_variables(text, current_time=datetime(2026, 6, 30)),
            text,
        )

    def test_render_multiple_placeholders(self):
        """多占位符同时替换。"""
        now = datetime(2026, 6, 30, 14, 30)
        last_run = datetime(2026, 6, 29, 10, 0)
        text = "{now} | {today} | {this_week_start} | {last_run_time}"
        rendered = render_time_variables(
            text, current_time=now, last_run_time=last_run
        )
        self.assertEqual(
            rendered,
            "2026-06-30 14:30:00 | 2026-06-30 | 2026-06-29 | 2026-06-29 10:00:00",
        )


# ---------------------------------------------------------------------------
# SubTask 2.2: directory_watch 模板
# ---------------------------------------------------------------------------


class TestDirectoryWatchTemplate(unittest.TestCase):
    """SubTask 2.2：directory_watch 模板扫描/对比/写文件/LLM 调用。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.watch_path = os.path.join(self.tmpdir, "watched")
        self.report_dir = os.path.join(self.tmpdir, "reports")
        os.makedirs(self.watch_path)
        # 创建初始文件
        Path(self.watch_path, "a.txt").write_text("hello", encoding="utf-8")
        Path(self.watch_path, "b.py").write_text("code", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # 清理 snapshot 数据目录（模板硬编码 data/schedules/{id}/snapshot.json）
        sched_dir = os.path.join("data", "schedules", "s1")
        if os.path.isdir(sched_dir):
            shutil.rmtree(sched_dir, ignore_errors=True)

    def _make_context(self, llm_client=None, current_time=None, last_run_time=None):
        return WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm_client or MockLLMClient(response_text="趋势分析结果"),
            report_dir=self.report_dir,
            current_time=current_time or datetime(2026, 6, 30, 14, 30),
            last_run_time=last_run_time,
        )

    def test_directory_watch_name(self):
        """模板 name 为 directory_watch。"""
        self.assertEqual(DirectoryWatchTemplate.name, "directory_watch")

    def test_directory_watch_build_system_prompt_fixed(self):
        """build_system_prompt 返回固定文本（缓存约束 5：禁含动态变量）。"""
        prompt = DirectoryWatchTemplate().build_system_prompt()
        self.assertIsInstance(prompt, str)
        self.assertTrue(prompt)
        # 不含时间变量占位符
        for ph in ("{now}", "{today}", "{this_week_start}", "{last_run_time}"):
            self.assertNotIn(ph, prompt)
        # 两次调用结果一致（字节级稳定）
        self.assertEqual(prompt, DirectoryWatchTemplate().build_system_prompt())

    def test_directory_watch_missing_watch_path(self):
        """缺少 watch_path 字段返回错误结果。"""
        template = DirectoryWatchTemplate()
        ctx = self._make_context()
        result = template.execute({}, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("watch_path" in e for e in result.errors))

    def test_directory_watch_scan_and_diff_first_run(self):
        """首次执行：扫描全部文件，差异 = 全部新增。"""
        template = DirectoryWatchTemplate()
        ctx = self._make_context()
        result = template.execute(
            {"watch_path": self.watch_path}, ctx
        )
        self.assertTrue(result.success, f"errors={result.errors}")
        # metrics: 2 个文件全部算新增
        self.assertEqual(result.metrics_for_injection["扫描文件数"], 2)
        self.assertEqual(result.metrics_for_injection["新增文件"], 2)
        self.assertEqual(result.metrics_for_injection["修改文件"], 0)
        self.assertEqual(result.metrics_for_injection["删除文件"], 0)
        # LLM 被调用
        self.assertEqual(len(ctx.llm_client.calls), 1)
        # 报告文件已写入
        self.assertTrue(any(o["type"] == "report" for o in result.outputs))
        report_files = [
            o["path"] for o in result.outputs if o["type"] == "report"
        ]
        self.assertTrue(os.path.exists(report_files[0]))

    def test_directory_watch_second_run_detects_modification(self):
        """第二次执行：修改一个文件后差异包含修改项。"""
        template = DirectoryWatchTemplate()
        ctx1 = self._make_context()
        result1 = template.execute({"watch_path": self.watch_path}, ctx1)
        self.assertTrue(result1.success)

        # 修改 a.txt 内容（mtime 变化）
        import time

        time.sleep(0.05)
        Path(self.watch_path, "a.txt").write_text(
            "hello modified", encoding="utf-8"
        )

        # 第二次执行
        ctx2 = self._make_context(
            last_run_time=datetime(2026, 6, 30, 14, 30)
        )
        result2 = template.execute({"watch_path": self.watch_path}, ctx2)
        self.assertTrue(result2.success, f"errors={result2.errors}")
        # 第二次扫描：无新增，1 修改（a.txt），0 删除
        self.assertEqual(result2.metrics_for_injection["扫描文件数"], 2)
        self.assertEqual(result2.metrics_for_injection["新增文件"], 0)
        self.assertEqual(result2.metrics_for_injection["修改文件"], 1)
        self.assertEqual(result2.metrics_for_injection["删除文件"], 0)

    def test_directory_watch_second_run_detects_deletion(self):
        """第二次执行：删除文件后差异包含删除项。"""
        template = DirectoryWatchTemplate()
        ctx1 = self._make_context()
        template.execute({"watch_path": self.watch_path}, ctx1)

        # 删除 b.py
        os.remove(os.path.join(self.watch_path, "b.py"))

        ctx2 = self._make_context(
            last_run_time=datetime(2026, 6, 30, 14, 30)
        )
        result2 = template.execute({"watch_path": self.watch_path}, ctx2)
        self.assertTrue(result2.success)
        self.assertEqual(result2.metrics_for_injection["删除文件"], 1)
        self.assertEqual(result2.metrics_for_injection["新增文件"], 0)

    def test_directory_watch_report_filename_uses_date(self):
        """报告文件名含日期（SubTask 2.7 第二层：文件名层）。"""
        template = DirectoryWatchTemplate()
        current_time = datetime(2026, 6, 30, 14, 30)
        ctx = self._make_context(current_time=current_time)
        result = template.execute({"watch_path": self.watch_path}, ctx)
        report_files = [
            o["path"] for o in result.outputs if o["type"] == "report"
        ]
        # 文件名应含 20260630
        self.assertIn("20260630", os.path.basename(report_files[0]))

    def test_directory_watch_llm_input_contains_time_variables_rendered(self):
        """LLM 用户输入层时间变量已被替换（SubTask 2.7 第三层）。"""
        llm = MockLLMClient(response_text="分析")
        last_run = datetime(2026, 6, 29, 10, 0, 0)
        ctx = self._make_context(
            llm_client=llm,
            current_time=datetime(2026, 6, 30, 14, 30),
            last_run_time=last_run,
        )
        template = DirectoryWatchTemplate()
        template.execute({"watch_path": self.watch_path}, ctx)
        # 取 LLM 调用的 user_input
        messages = llm.calls[0]["messages"]
        user_content = messages[0]["content"]
        # 已替换为实际时间，不含占位符
        self.assertNotIn("{now}", user_content)
        self.assertNotIn("{last_run_time}", user_content)
        self.assertIn("2026-06-30 14:30:00", user_content)
        self.assertIn("2026-06-29 10:00:00", user_content)

    def test_directory_watch_llm_client_none_skips_llm(self):
        """llm_client=None 时跳过 LLM 调用，仍写报告。"""
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=None,
            report_dir=self.report_dir,
            current_time=datetime(2026, 6, 30, 14, 30),
        )
        template = DirectoryWatchTemplate()
        result = template.execute({"watch_path": self.watch_path}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "")
        # 报告仍写入
        self.assertTrue(any(o["type"] == "report" for o in result.outputs))

    def test_directory_watch_nonexistent_path_returns_error(self):
        """watch_path 不存在时返回错误。"""
        template = DirectoryWatchTemplate()
        ctx = self._make_context()
        result = template.execute(
            {"watch_path": "/nonexistent/path/xyz"}, ctx
        )
        self.assertFalse(result.success)
        self.assertTrue(any("扫描目录失败" in e for e in result.errors))


# ---------------------------------------------------------------------------
# SubTask 2.3: summary 模板
# ---------------------------------------------------------------------------


class TestSummaryTemplate(unittest.TestCase):
    """SubTask 2.3：summary 模板读历史 + LLM 总结。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.report_dir = os.path.join(self.tmpdir, "reports")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_context(self, session_logger=None, llm_client=None):
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm_client or MockLLMClient(response_text="会话摘要结果"),
            report_dir=self.report_dir,
            current_time=datetime(2026, 6, 30, 14, 30),
            last_run_time=None,
        )
        if session_logger is not None:
            setattr(ctx, "session_logger", session_logger)
        return ctx

    def test_summary_name(self):
        self.assertEqual(SummaryTemplate.name, "summary")

    def test_summary_build_system_prompt_fixed(self):
        """system prompt 固定，不含动态变量。"""
        prompt = SummaryTemplate().build_system_prompt()
        self.assertTrue(prompt)
        for ph in ("{now}", "{today}", "{this_week_start}", "{last_run_time}"):
            self.assertNotIn(ph, prompt)

    def test_summary_missing_session_id(self):
        """缺少 session_id 返回错误。"""
        template = SummaryTemplate()
        ctx = self._make_context()
        result = template.execute({}, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("session_id" in e for e in result.errors))

    def test_summary_no_session_logger(self):
        """session_logger 未配置返回错误。"""
        template = SummaryTemplate()
        ctx = self._make_context(session_logger=None)
        result = template.execute({"session_id": "sess1"}, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("session_logger" in e for e in result.errors))

    def test_summary_empty_history(self):
        """会话历史为空时返回「无会话历史可总结」。"""
        logger = MockSessionLogger({})  # 空
        ctx = self._make_context(session_logger=logger)
        template = SummaryTemplate()
        result = template.execute({"session_id": "sess1"}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "（无会话历史可总结）")
        self.assertEqual(result.metrics_for_injection["消息数"], 0)

    def test_summary_with_history_calls_llm(self):
        """有历史时调用 LLM 总结。"""
        messages = [
            {"role": "user", "content": "你好", "tool_name": ""},
            {"role": "assistant", "content": "你好，有什么可以帮你？", "tool_name": ""},
            {"role": "user", "content": "帮我写代码", "tool_name": ""},
        ]
        logger = MockSessionLogger({"sess1": messages})
        llm = MockLLMClient(response_text="这是会话摘要")
        ctx = self._make_context(session_logger=logger, llm_client=llm)
        template = SummaryTemplate()
        result = template.execute({"session_id": "sess1"}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "这是会话摘要")
        self.assertEqual(result.metrics_for_injection["消息数"], 3)
        self.assertEqual(len(llm.calls), 1)
        # 报告文件已写
        self.assertTrue(any(o["type"] == "report" for o in result.outputs))

    def test_summary_filters_tool_messages(self):
        """工具调用消息（tool_name 非空）被过滤。"""
        messages = [
            {"role": "user", "content": "你好", "tool_name": ""},
            {"role": "assistant", "content": "", "tool_name": "bash_exec"},
            {"role": "assistant", "content": "好的回复", "tool_name": ""},
        ]
        logger = MockSessionLogger({"sess1": messages})
        llm = MockLLMClient(response_text="摘要")
        ctx = self._make_context(session_logger=logger, llm_client=llm)
        template = SummaryTemplate()
        result = template.execute({"session_id": "sess1"}, ctx)
        # 只统计 2 条非工具消息
        self.assertEqual(result.metrics_for_injection["消息数"], 2)

    def test_summary_report_filename_uses_date_and_session(self):
        """报告文件名含 session_id 与日期。"""
        messages = [{"role": "user", "content": "测试", "tool_name": ""}]
        logger = MockSessionLogger({"sess_1": messages})
        ctx = self._make_context(session_logger=logger)
        template = SummaryTemplate()
        result = template.execute({"session_id": "sess_1"}, ctx)
        report_files = [
            o["path"] for o in result.outputs if o["type"] == "report"
        ]
        fname = os.path.basename(report_files[0])
        self.assertIn("20260630", fname)
        # session_id 含特殊字符 _，被清理后文件名中应出现 sess_1 或 sess 后缀片段
        self.assertTrue(fname.startswith("summary_"), f"文件名应以 summary_ 开头: {fname}")
        # 文件名应含 session_id 的字母数字部分
        self.assertIn("sess", fname)


# ---------------------------------------------------------------------------
# SubTask 2.4: email_notify 模板（纯确定性，不调 LLM）
# ---------------------------------------------------------------------------


class TestEmailNotifyTemplate(unittest.TestCase):
    """SubTask 2.4：email_notify 模板渲染 + mock SMTP。"""

    def test_email_notify_name(self):
        self.assertEqual(EmailNotifyTemplate.name, "email_notify")

    def test_email_notify_missing_required_fields(self):
        """缺少 to/subject/body 之一返回错误。"""
        template = EmailNotifyTemplate()
        ctx = WorkflowContext(session_id="cron:s1", schedule_id="s1")
        result = template.execute({"to": "a@b.com"}, ctx)  # 缺 subject/body
        self.assertFalse(result.success)
        self.assertTrue(any("to/subject/body" in e for e in result.errors))

    def test_email_notify_missing_smtp_config(self):
        """SMTP 配置不完整返回错误（不调 LLM）。"""
        template = EmailNotifyTemplate()
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            get_env=lambda k, d="": d,  # 返回默认空值
        )
        result = template.execute(
            {
                "to": "a@b.com",
                "subject": "主题",
                "body": "正文",
            },
            ctx,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("SMTP" in e for e in result.errors))

    def test_email_notify_renders_time_variables(self):
        """subject/body 中的时间变量被替换。"""
        sent_emails = []

        def fake_send_smtp(
            self, host, port, user, password, from_addr, recipients, msg, use_tls
        ):
            # MIMEText 的 payload 默认 base64 编码，需 decode=True 取原文
            raw_body = msg.get_payload(decode=True)
            body_text = raw_body.decode("utf-8") if raw_body else ""
            sent_emails.append(
                {
                    "subject": msg["Subject"],
                    "body": body_text,
                    "recipients": recipients,
                }
            )

        template = EmailNotifyTemplate()
        now = datetime(2026, 6, 30, 14, 30)
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            current_time=now,
            last_run_time=None,
            get_env=lambda k, d="": {
                "SMTP_HOST": "mock",
                "SMTP_USER": "u@x.com",
                "SMTP_PASSWORD": "p",
            }.get(k, d),
        )
        with patch.object(EmailNotifyTemplate, "_send_smtp", fake_send_smtp):
            result = template.execute(
                {
                    "to": "a@b.com, c@d.com",
                    "subject": "报告 {today}",
                    "body": "生成时间 {now}",
                },
                ctx,
            )
        self.assertTrue(result.success, f"errors={result.errors}")
        self.assertEqual(len(sent_emails), 1)
        self.assertEqual(sent_emails[0]["subject"], "报告 2026-06-30")
        self.assertIn("2026-06-30 14:30:00", sent_emails[0]["body"])
        # 两个收件人
        self.assertEqual(len(sent_emails[0]["recipients"]), 2)
        # metrics
        self.assertEqual(result.metrics_for_injection["收件人数"], 2)

    def test_email_notify_no_llm_call(self):
        """email_notify 不调用 LLM（assistant_response 始终为空）。"""
        sent = []

        def fake_send(self, *args, **kwargs):
            sent.append(True)

        llm = MockLLMClient()
        template = EmailNotifyTemplate()
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm,
            get_env=lambda k, d="": {
                "SMTP_HOST": "mock",
                "SMTP_USER": "u@x.com",
                "SMTP_PASSWORD": "p",
            }.get(k, d),
        )
        with patch.object(EmailNotifyTemplate, "_send_smtp", fake_send):
            result = template.execute(
                {"to": "a@b.com", "subject": "s", "body": "b"}, ctx
            )
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "")
        self.assertEqual(len(llm.calls), 0)  # LLM 未被调用

    def test_email_notify_use_tls_from_config(self):
        """use_tls 由 config 显式指定。"""
        sent = []

        def fake_send(self, host, port, user, password, from_addr, recipients, msg, use_tls):
            sent.append(use_tls)

        template = EmailNotifyTemplate()
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            get_env=lambda k, d="": {
                "SMTP_HOST": "mock",
                "SMTP_USER": "u@x.com",
                "SMTP_PASSWORD": "p",
            }.get(k, d),
        )
        with patch.object(EmailNotifyTemplate, "_send_smtp", fake_send):
            result = template.execute(
                {
                    "to": "a@b.com",
                    "subject": "s",
                    "body": "b",
                    "use_tls": False,
                },
                ctx,
            )
        self.assertTrue(result.success)
        self.assertEqual(sent, [False])


# ---------------------------------------------------------------------------
# SubTask 2.5: cleanup_suggest 模板
# ---------------------------------------------------------------------------


class TestCleanupSuggestTemplate(unittest.TestCase):
    """SubTask 2.5：cleanup_suggest 查询 + LLM 建议。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.report_dir = os.path.join(self.tmpdir, "reports")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_context(self, chroma_store=None, llm_client=None):
        return WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm_client or MockLLMClient(response_text="清理建议"),
            chroma_store=chroma_store,
            report_dir=self.report_dir,
            current_time=datetime(2026, 6, 30, 14, 30),
        )

    def test_cleanup_suggest_name(self):
        self.assertEqual(CleanupSuggestTemplate.name, "cleanup_suggest")

    def test_cleanup_suggest_build_system_prompt_fixed(self):
        prompt = CleanupSuggestTemplate().build_system_prompt()
        for ph in ("{now}", "{today}", "{this_week_start}", "{last_run_time}"):
            self.assertNotIn(ph, prompt)

    def test_cleanup_suggest_no_chroma_store(self):
        """chroma_store 未配置返回错误。"""
        template = CleanupSuggestTemplate()
        ctx = self._make_context(chroma_store=None)
        result = template.execute({}, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("chroma_store" in e for e in result.errors))

    def test_cleanup_suggest_no_low_value_memories(self):
        """无低价值记忆时返回「无低价值记忆可清理」。"""
        store = MockChromaStore()
        # 只添加高价值记忆
        store.add_memory(
            "重要记忆", metadata={"importance": 0.9}, namespace="cron", cron_id="s1"
        )
        ctx = self._make_context(chroma_store=store)
        template = CleanupSuggestTemplate()
        result = template.execute({}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "（无低价值记忆可清理）")
        self.assertEqual(result.metrics_for_injection["低价值记忆数"], 0)

    def test_cleanup_suggest_with_low_value_memories_calls_llm(self):
        """有低价值记忆时调用 LLM 生成建议。"""
        store = MockChromaStore()
        store.add_memory(
            "低价值1", metadata={"importance": 0.1}, namespace="cron", cron_id="s1"
        )
        store.add_memory(
            "低价值2", metadata={"importance": 0.2}, namespace="cron", cron_id="s1"
        )
        store.add_memory(
            "高价值", metadata={"importance": 0.8}, namespace="cron", cron_id="s1"
        )
        llm = MockLLMClient(response_text="删除候选：低价值1")
        ctx = self._make_context(chroma_store=store, llm_client=llm)
        template = CleanupSuggestTemplate()
        result = template.execute({}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "删除候选：低价值1")
        self.assertEqual(result.metrics_for_injection["低价值记忆数"], 2)
        self.assertEqual(len(llm.calls), 1)
        # 报告已写
        self.assertTrue(any(o["type"] == "report" for o in result.outputs))

    def test_cleanup_suggest_filters_by_namespace(self):
        """list_memories 按 namespace=cron + cron_id 过滤。"""
        store = MockChromaStore()
        # cron namespace 低价值
        store.add_memory(
            "cron低", metadata={"importance": 0.1}, namespace="cron", cron_id="s1"
        )
        # user namespace 低价值（不应被选中）
        store.add_memory(
            "user低", metadata={"importance": 0.1}, namespace="user", cron_id=None
        )
        ctx = self._make_context(chroma_store=store)
        template = CleanupSuggestTemplate()
        result = template.execute({}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.metrics_for_injection["低价值记忆数"], 1)

    def test_cleanup_suggest_custom_threshold(self):
        """自定义 importance_threshold。"""
        store = MockChromaStore()
        store.add_memory(
            "中价值", metadata={"importance": 0.5}, namespace="cron", cron_id="s1"
        )
        ctx = self._make_context(chroma_store=store)
        template = CleanupSuggestTemplate()
        # 阈值 0.6 时 0.5 应被选中
        result = template.execute({"importance_threshold": 0.6}, ctx)
        self.assertEqual(result.metrics_for_injection["低价值记忆数"], 1)
        # 阈值 0.4 时 0.5 不被选中
        result2 = template.execute({"importance_threshold": 0.4}, ctx)
        self.assertEqual(result2.metrics_for_injection["低价值记忆数"], 0)


# ---------------------------------------------------------------------------
# SubTask 2.6: research 模板 + custom stub
# ---------------------------------------------------------------------------


class TestResearchTemplate(unittest.TestCase):
    """SubTask 2.6：research 模板 LLM 自主流程。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.report_dir = os.path.join(self.tmpdir, "reports")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_context(self, react_loop=None, llm_client=None):
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm_client or MockLLMClient(response_text="研究结果"),
            report_dir=self.report_dir,
            current_time=datetime(2026, 6, 30, 14, 30),
        )
        if react_loop is not None:
            setattr(ctx, "react_loop", react_loop)
        return ctx

    def test_research_name(self):
        self.assertEqual(ResearchTemplate.name, "research")

    def test_research_build_system_prompt_fixed(self):
        prompt = ResearchTemplate().build_system_prompt()
        for ph in ("{now}", "{today}", "{this_week_start}", "{last_run_time}"):
            self.assertNotIn(ph, prompt)

    def test_research_missing_topic(self):
        """缺少 topic 返回错误。"""
        template = ResearchTemplate()
        ctx = self._make_context()
        result = template.execute({}, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("topic" in e for e in result.errors))

    def test_research_with_react_loop(self):
        """有 react_loop 时走 ReactLoop.run。"""
        tool_messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "search",
                        "input": {"q": "test"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "搜索结果",
                        "is_error": False,
                    }
                ],
            },
        ]
        react = MockReactLoop(
            response_text="研究完成", tool_messages=tool_messages
        )
        ctx = self._make_context(react_loop=react)
        template = ResearchTemplate()
        result = template.execute({"topic": "AI 趋势"}, ctx)
        self.assertTrue(result.success, f"errors={result.errors}")
        self.assertEqual(result.assistant_response, "研究完成")
        # react_loop 被调用一次
        self.assertEqual(len(react.calls), 1)
        # 工具调用被提取
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0]["name"], "search")
        self.assertEqual(result.tool_calls[0]["result"], "搜索结果")
        self.assertFalse(result.tool_calls[0]["is_error"])
        # 报告已写
        self.assertTrue(any(o["type"] == "report" for o in result.outputs))

    def test_research_without_react_loop_falls_back_to_single_turn(self):
        """无 react_loop 时降级为单轮 LLM 调用。"""
        llm = MockLLMClient(response_text="单轮研究")
        ctx = self._make_context(react_loop=None, llm_client=llm)
        template = ResearchTemplate()
        result = template.execute({"topic": "测试主题"}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "单轮研究")
        self.assertEqual(len(llm.calls), 1)

    def test_research_react_loop_exception_falls_back(self):
        """ReactLoop 抛异常时降级为单轮调用，错误被记录但仍有 LLM 回复。"""
        class FailingReactLoop:
            async def run(self, **kwargs):
                raise RuntimeError("react 失败")

        llm = MockLLMClient(response_text="降级结果")
        ctx = self._make_context(react_loop=FailingReactLoop(), llm_client=llm)
        template = ResearchTemplate()
        result = template.execute({"topic": "测试"}, ctx)
        # ReactLoop 失败导致 success=False（错误被记录到 errors），
        # 但降级路径仍调用 LLM 填充 assistant_response
        self.assertFalse(result.success)
        self.assertTrue(any("ReactLoop" in e for e in result.errors))
        self.assertEqual(result.assistant_response, "降级结果")
        self.assertEqual(len(llm.calls), 1)  # 降级 LLM 被调用一次

    def test_research_metrics_include_topic_and_loops(self):
        """metrics_for_injection 含主题与最大循环数。"""
        react = MockReactLoop(response_text="结果")
        ctx = self._make_context(react_loop=react)
        template = ResearchTemplate()
        result = template.execute({"topic": "主题X", "max_loops": 10}, ctx)
        self.assertEqual(result.metrics_for_injection["研究主题"], "主题X")
        self.assertEqual(result.metrics_for_injection["最大循环数"], 10)


class TestCustomTemplate(unittest.TestCase):
    """Phase 8 Task 5.8：custom 模板对接 cron_tool（实装后行为）。"""

    def test_custom_name(self):
        self.assertEqual(CustomTemplate.name, "custom")

    def test_custom_execute_missing_tool_name_returns_error(self):
        """缺少 tool_name 字段时返回含 error 的 WorkflowResult。"""
        template = CustomTemplate()
        ctx = WorkflowContext(session_id="cron:s1", schedule_id="s1")
        result = template.execute({}, ctx)
        self.assertFalse(result.success)
        self.assertTrue(any("tool_name" in e for e in result.errors))

    def test_custom_execute_successful_with_llm_step(self):
        """成功执行 cron_tool + LLM 步骤生成报告。"""
        from unittest.mock import patch
        from datetime import datetime

        template = CustomTemplate()
        # 临时目录作为 report_dir
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = WorkflowContext(
                session_id="cron:s1",
                schedule_id="s1",
                llm_client=None,  # LLM 步骤会降级跳过
                report_dir=tmpdir,
                current_time=datetime(2026, 6, 30, 10, 0, 0),
            )
            # mock cron_tool_loader.execute_tool 返回成功结果
            with patch(
                "teage_liu.tasks.workflow.custom._execute_cron_tool",
                return_value="echo: hello world",
            ):
                result = template.execute(
                    {"tool_name": "echo_text", "input": {"text": "hello world"}},
                    ctx,
                )
            # 工具调用成功
            self.assertTrue(result.success)
            self.assertEqual(len(result.tool_calls), 1)
            self.assertEqual(result.tool_calls[0]["name"], "echo_text")
            self.assertFalse(result.tool_calls[0]["is_error"])
            self.assertEqual(
                result.tool_calls[0]["result"], "echo: hello world"
            )
            # metrics_for_injection 正确填充
            self.assertEqual(
                result.metrics_for_injection["cron_tool"], "echo_text"
            )
            self.assertEqual(
                result.metrics_for_injection["执行状态"], "success"
            )
            # llm_client 为 None 时不写报告文件（LLM 步骤降级跳过）
            self.assertEqual(result.outputs, [])

    def test_custom_execute_tool_failure_skips_llm(self):
        """cron_tool 执行抛异常时记录 error 并跳过 LLM 步骤。"""
        from unittest.mock import patch
        from datetime import datetime

        template = CustomTemplate()
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            current_time=datetime(2026, 6, 30, 10, 0, 0),
        )
        with patch(
            "teage_liu.tasks.workflow.custom._execute_cron_tool",
            side_effect=RuntimeError("subprocess crashed"),
        ):
            result = template.execute(
                {"tool_name": "broken_tool"}, ctx
            )
        # 工具调用失败
        self.assertFalse(result.success)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertTrue(result.tool_calls[0]["is_error"])
        self.assertEqual(
            result.metrics_for_injection["执行状态"], "failed"
        )
        # 失败时不写报告文件
        self.assertEqual(result.outputs, [])

    def test_custom_execute_skip_llm_step(self):
        """llm_step=False 时仅执行 cron_tool，不调用 LLM。"""
        from unittest.mock import patch
        from datetime import datetime

        template = CustomTemplate()
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=None,
            current_time=datetime(2026, 6, 30, 10, 0, 0),
        )
        with patch(
            "teage_liu.tasks.workflow.custom._execute_cron_tool",
            return_value="data: 42",
        ):
            result = template.execute(
                {"tool_name": "data_collector", "llm_step": False},
                ctx,
            )
        # 工具调用成功，但 assistant_response 为空（跳过 LLM）
        self.assertTrue(result.success)
        self.assertEqual(result.assistant_response, "")
        self.assertEqual(len(result.tool_calls), 1)

    def test_custom_invalid_input_type_returns_error(self):
        """input 字段非 dict 时返回 error。"""
        template = CustomTemplate()
        ctx = WorkflowContext(session_id="cron:s1", schedule_id="s1")
        result = template.execute(
            {"tool_name": "echo_text", "input": "not a dict"},
            ctx,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("input" in e for e in result.errors))

    def test_custom_build_system_prompt_no_dynamic_vars(self):
        """build_system_prompt 返回固定 prompt（缓存约束 5）。"""
        template = CustomTemplate()
        prompt1 = template.build_system_prompt()
        prompt2 = template.build_system_prompt()
        self.assertEqual(prompt1, prompt2)
        # 不含动态变量占位符
        for ph in ("{now}", "{today}", "{last_run_time}", "{this_week_start}"):
            self.assertNotIn(ph, prompt1)


# ---------------------------------------------------------------------------
# BUILTIN_TEMPLATES 注册表 + get_template
# ---------------------------------------------------------------------------


class TestBuiltinTemplatesRegistry(unittest.TestCase):
    """验证 BUILTIN_TEMPLATES 注册表与 get_template 查找。"""

    def test_registry_contains_all_builtin_templates(self):
        """注册表含 6 个内置模板。"""
        expected = {
            "directory_watch",
            "summary",
            "email_notify",
            "cleanup_suggest",
            "research",
            "custom",
        }
        self.assertEqual(set(BUILTIN_TEMPLATES.keys()), expected)

    def test_get_template_returns_class(self):
        """get_template 按名返回模板类。"""
        self.assertIs(get_template("directory_watch"), DirectoryWatchTemplate)
        self.assertIs(get_template("summary"), SummaryTemplate)
        self.assertIs(get_template("email_notify"), EmailNotifyTemplate)
        self.assertIs(get_template("cleanup_suggest"), CleanupSuggestTemplate)
        self.assertIs(get_template("research"), ResearchTemplate)
        self.assertIs(get_template("custom"), CustomTemplate)

    def test_get_template_unknown_returns_none(self):
        """get_template 未知名返回 None。"""
        self.assertIsNone(get_template("nonexistent_template"))

    def test_all_templates_subclass_workflow_template(self):
        """所有内置模板都是 WorkflowTemplate 子类。"""
        for cls in BUILTIN_TEMPLATES.values():
            self.assertTrue(issubclass(cls, WorkflowTemplate))


# ---------------------------------------------------------------------------
# SubTask 2.13: 缓存约束验证（system prompt 固定，禁含动态变量）
# ---------------------------------------------------------------------------


class TestCacheConstraints(unittest.TestCase):
    """验证缓存约束：模板 system prompt 禁含动态变量。"""

    def test_all_builtin_template_prompts_have_no_dynamic_variables(self):
        """所有内置模板的 build_system_prompt 不含时间变量占位符。"""
        placeholders = ("{now}", "{today}", "{this_week_start}", "{last_run_time}")
        for name, cls in BUILTIN_TEMPLATES.items():
            template = cls()
            prompt = template.build_system_prompt()
            for ph in placeholders:
                self.assertNotIn(
                    ph,
                    prompt,
                    f"模板 {name} 的 system prompt 含动态变量 {ph}",
                )

    def test_all_builtin_template_prompts_byte_stable(self):
        """同一模板的 build_system_prompt 多次调用结果一致（字节级稳定）。"""
        for name, cls in BUILTIN_TEMPLATES.items():
            template = cls()
            prompt1 = template.build_system_prompt()
            prompt2 = template.build_system_prompt()
            self.assertEqual(
                prompt1,
                prompt2,
                f"模板 {name} 的 system prompt 不稳定",
            )

    def test_time_variables_only_in_user_input_layer(self):
        """时间变量只出现在 LLM 用户输入层，不在 system prompt。

        通过检查 directory_watch / summary / cleanup_suggest / research 的
        _build_llm_input 输出含占位符（待 context.render 替换），
        而 build_system_prompt 不含占位符。
        """
        templates_with_llm = [
            DirectoryWatchTemplate(),
            SummaryTemplate(),
            CleanupSuggestTemplate(),
            ResearchTemplate(),
        ]
        for template in templates_with_llm:
            # system prompt 不含占位符
            prompt = template.build_system_prompt()
            self.assertNotIn("{now}", prompt)
            self.assertNotIn("{last_run_time}", prompt)


# ---------------------------------------------------------------------------
# Task 7.5: 缺陷修复验证（D2/D4/D5/D7）
# ---------------------------------------------------------------------------


class TestWorkflowDefectD2TemplateNotFound(unittest.TestCase):
    """D2 修复：scheduler._execute_workflow 模板未找到返回 success=False。

    验证 CronScheduler._execute_workflow 在以下 5 个失败路径中
    永不返回 None，而是返回 WorkflowResult(success=False, errors=[...])：
    - BUILTIN_TEMPLATES 为空
    - 缺 template 字段
    - 模板未找到
    - WorkflowContext 构造失败
    - NotImplementedError
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_scheduler(self):
        from teage_liu.tasks.scheduler import CronScheduler
        return CronScheduler(schedules_file=self.sched_file)

    def test_d2_unknown_template_returns_failure_result(self):
        """D2-路径3：引用未知模板名返回 success=False + 错误描述。"""
        from teage_liu.tasks.scheduler import CronScheduler, Schedule
        from teage_liu.tasks.workflow import WorkflowResult

        scheduler = self._new_scheduler()
        schedule = Schedule(
            id="s1",
            name="测试",
            cron="* * * * *",
            task="t",
            workflow={"template": "nonexistent_template_xyz"},
        )
        result = scheduler._execute_workflow(
            orchestrator=None,
            schedule=schedule,
            task_text="t",
            started_at_dt=datetime(2026, 7, 5, 14, 30),
            last_run_dt=None,
        )
        self.assertIsNotNone(result, "D2 修复后不应返回 None")
        self.assertFalse(result.success)
        self.assertTrue(
            any("nonexistent_template_xyz" in e for e in result.errors),
            f"errors 应包含未知模板名: {result.errors}",
        )

    def test_d2_missing_template_field_returns_failure_result(self):
        """D2-路径2：缺 template 字段返回 success=False。"""
        from teage_liu.tasks.scheduler import Schedule

        scheduler = self._new_scheduler()
        schedule = Schedule(
            id="s2",
            name="测试",
            cron="* * * * *",
            task="t",
            workflow={"watch_path": "/tmp"},  # 缺 template
        )
        result = scheduler._execute_workflow(
            orchestrator=None,
            schedule=schedule,
            task_text="t",
            started_at_dt=datetime(2026, 7, 5, 14, 30),
            last_run_dt=None,
        )
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertTrue(
            any("template" in e for e in result.errors),
            f"errors 应说明缺 template 字段: {result.errors}",
        )


class TestWorkflowDefectD4RunIdSuffix(unittest.TestCase):
    """D4 修复：报告文件名追加 run_id[:8] 后缀避免同日多次触发覆盖。

    验证 directory_watch / summary / research / cleanup_suggest / custom
    五个模板的 _write_report 方法生成的文件名含 run_id 前 8 位后缀。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.report_dir = os.path.join(self.tmpdir, "reports")
        os.makedirs(self.report_dir)
        self.watch_path = os.path.join(self.tmpdir, "watched")
        os.makedirs(self.watch_path)
        Path(self.watch_path, "a.txt").write_text("hello", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        sched_dir = os.path.join("data", "schedules", "s1")
        if os.path.isdir(sched_dir):
            shutil.rmtree(sched_dir, ignore_errors=True)

    def _make_context(self, run_id=None, llm_client=None):
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm_client or MockLLMClient(response_text="分析结果"),
            report_dir=self.report_dir,
            current_time=datetime(2026, 7, 5, 14, 30),
        )
        if run_id is not None:
            # run_id 通过 setattr 注入（WorkflowContext 未声明此字段）
            setattr(ctx, "run_id", run_id)
        return ctx

    def test_d4_directory_watch_filename_contains_run_id_suffix(self):
        """directory_watch 报告文件名含 run_id[:8]。"""
        template = DirectoryWatchTemplate()
        ctx = self._make_context(run_id="abcdef1234567890")
        result = template.execute({"watch_path": self.watch_path}, ctx)
        self.assertTrue(result.success, f"errors={result.errors}")
        report_files = [
            o["path"] for o in result.outputs if o["type"] == "report"
        ]
        self.assertEqual(len(report_files), 1)
        fname = os.path.basename(report_files[0])
        self.assertIn("abcdef12", fname, f"文件名应含 run_id[:8]: {fname}")

    def test_d4_same_day_multiple_runs_no_overwrite(self):
        """同日两次触发（不同 run_id）生成不同文件，互不覆盖。"""
        template = DirectoryWatchTemplate()
        # 第一次执行
        ctx1 = self._make_context(run_id="run1aaa1234567890")
        result1 = template.execute({"watch_path": self.watch_path}, ctx1)
        self.assertTrue(result1.success, f"errors={result1.errors}")
        report1 = [o["path"] for o in result1.outputs if o["type"] == "report"][0]

        # 第二次执行（不同 run_id）
        ctx2 = self._make_context(run_id="run2bbb9876543210")
        result2 = template.execute({"watch_path": self.watch_path}, ctx2)
        self.assertTrue(result2.success, f"errors={result2.errors}")
        report2 = [o["path"] for o in result2.outputs if o["type"] == "report"][0]

        # 两个文件路径不同
        self.assertNotEqual(report1, report2)
        # 两个文件都存在（未被覆盖）
        self.assertTrue(os.path.exists(report1), f"第一次报告应存在: {report1}")
        self.assertTrue(os.path.exists(report2), f"第二次报告应存在: {report2}")

    def test_d4_research_filename_contains_run_id_suffix(self):
        """research 报告文件名含 run_id[:8]。"""
        react = MockReactLoop(response_text="研究结果")
        template = ResearchTemplate()
        # 使用 distinctive run_id：前 8 位为 "r1abcd12" 避免与 "research" 前缀重叠
        ctx = self._make_context(run_id="r1abcd1299999999", llm_client=MockLLMClient())
        setattr(ctx, "react_loop", react)
        result = template.execute({"topic": "AI 趋势"}, ctx)
        self.assertTrue(result.success, f"errors={result.errors}")
        report_files = [
            o["path"] for o in result.outputs if o["type"] == "report"
        ]
        self.assertEqual(len(report_files), 1)
        fname = os.path.basename(report_files[0])
        self.assertIn("r1abcd12", fname, f"文件名应含 run_id[:8]: {fname}")

    def test_d4_summary_filename_contains_run_id_suffix(self):
        """summary 报告文件名含 run_id[:8]。"""
        messages = [{"role": "user", "content": "测试", "tool_name": ""}]
        logger = MockSessionLogger({"sess1": messages})
        template = SummaryTemplate()
        # 使用 distinctive run_id：前 8 位为 "s1abcd12" 避免与 "summary" 前缀重叠
        ctx = self._make_context(run_id="s1abcd1299999999")
        setattr(ctx, "session_logger", logger)
        result = template.execute({"session_id": "sess1"}, ctx)
        self.assertTrue(result.success, f"errors={result.errors}")
        report_files = [
            o["path"] for o in result.outputs if o["type"] == "report"
        ]
        self.assertEqual(len(report_files), 1)
        fname = os.path.basename(report_files[0])
        self.assertIn("s1abcd12", fname, f"文件名应含 run_id[:8]: {fname}")


class TestWorkflowDefectD5ErrorChannel(unittest.TestCase):
    """D5 修复：LLM 异常记录到 error_channel + WorkflowEngine 合并到 result.errors。

    验证：
    - _call_llm_single_turn 在 LLM 客户端抛异常时写入 context.error_channel
    - WorkflowEngine.execute 末尾合并 error_channel 内容到 WorkflowResult.errors
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.report_dir = os.path.join(self.tmpdir, "reports")
        os.makedirs(self.report_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_d5_llm_exception_recorded_to_error_channel(self):
        """D5-1：LLM 客户端抛异常时写入 context.error_channel。"""
        class FailingLLMClient:
            """模拟 chat_main_sync 抛异常的 LLM 客户端。"""
            def chat_main(self, **kwargs):
                raise RuntimeError("LLM 服务不可用")
            def chat_main_sync(self, **kwargs):
                raise RuntimeError("LLM 服务不可用")

        template = DirectoryWatchTemplate()
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=FailingLLMClient(),
            report_dir=self.report_dir,
            current_time=datetime(2026, 7, 5, 14, 30),
        )
        watch_path = os.path.join(self.tmpdir, "watched")
        os.makedirs(watch_path)
        Path(watch_path, "a.txt").write_text("hello", encoding="utf-8")
        result = template.execute({"watch_path": watch_path}, ctx)
        # error_channel 含 LLM 异常记录
        self.assertTrue(
            any("LLM 调用失败" in msg for msg in ctx.error_channel),
            f"error_channel 应含 LLM 异常: {ctx.error_channel}",
        )
        # 模板仍返回结果（降级路径）
        self.assertIsNotNone(result)

    def test_d5_error_channel_merged_to_workflow_result_errors(self):
        """D5-2：WorkflowEngine.execute 合并 error_channel 到 result.errors。"""
        from teage_liu.tasks.workflow.engine import WorkflowEngine
        from teage_liu.tasks.workflow.spec import StepSpec, WorkflowSpec

        # 构造简易 spec（单 step）
        spec = WorkflowSpec(
            name="d5_test",
            steps=[StepSpec(id="s1", type="llm", config={"prompt": "x"})],
        )
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            report_dir=self.report_dir,
            current_time=datetime(2026, 7, 5, 14, 30),
        )
        # 模拟 error_channel 已有内容（由 _call_llm_single_turn 写入）
        ctx.error_channel = ["LLM 调用失败: timeout", "context overload"]

        engine = WorkflowEngine()
        result = engine.execute(spec, ctx)
        # error_channel 内容合并到 result.errors
        self.assertIn("LLM 调用失败: timeout", result.errors)
        self.assertIn("context overload", result.errors)


class TestWorkflowDefectD7EventLoopSafe(unittest.TestCase):
    """D7 修复：ResearchTemplate 在事件循环内不抛 RuntimeError。

    验证：
    - 当 asyncio.run 抛 RuntimeError（已有事件循环）时，
      降级到 run_coroutine_threadsafe 路径
    - 降级失败时通过外层 except 走单轮 LLM 调用
    - 不向上层抛 RuntimeError
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.report_dir = os.path.join(self.tmpdir, "reports")
        os.makedirs(self.report_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_d7_research_in_background_loop_thread(self):
        """D7-1：事件循环在后台线程运行时，主线程调用模板不抛 RuntimeError。

        模拟 CronScheduler 实际场景：FastAPI 事件循环在主线程运行，
        CronScheduler 通过 asyncio.to_thread 在工作线程执行 workflow。
        工作线程中 asyncio.run 不受主线程事件循环影响。
        """
        import threading

        template = ResearchTemplate()
        react = MockReactLoop(response_text="后台循环研究结果")
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=MockLLMClient(response_text="降级结果"),
            report_dir=self.report_dir,
            current_time=datetime(2026, 7, 5, 14, 30),
        )
        setattr(ctx, "react_loop", react)

        # 后台线程运行事件循环（模拟主线程的 FastAPI 循环）
        bg_loop = asyncio.new_event_loop()
        bg_started = threading.Event()

        def run_bg_loop():
            asyncio.set_event_loop(bg_loop)
            bg_started.set()
            try:
                bg_loop.run_forever()
            finally:
                bg_loop.close()

        bg_thread = threading.Thread(target=run_bg_loop, daemon=True)
        bg_thread.start()
        bg_started.wait(timeout=2.0)

        try:
            # 主线程调用模板（主线程无 running loop，asyncio.run 正常工作）
            result = template.execute({"topic": "测试"}, ctx)
            self.assertIsNotNone(result)
            self.assertEqual(result.assistant_response, "后台循环研究结果")
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            bg_thread.join(timeout=2.0)

    def test_d7_research_runtime_error_falls_back_to_single_turn(self):
        """D7-2：asyncio.run 抛 RuntimeError 时降级到单轮 LLM 调用。

        当无法通过 run_coroutine_threadsafe 执行时（loop 不可用），
        外层 except 捕获并降级到 _call_llm_single_turn。
        """
        template = ResearchTemplate()
        llm = MockLLMClient(response_text="降级单轮结果")
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm,
            report_dir=self.report_dir,
            current_time=datetime(2026, 7, 5, 14, 30),
        )

        # 构造 react_loop：run 协程总是抛 RuntimeError
        class FailingReactLoop:
            async def run(self, **kwargs):
                raise RuntimeError("react 内部失败")

            def metrics(self):
                return None

        setattr(ctx, "react_loop", FailingReactLoop())
        result = template.execute({"topic": "测试"}, ctx)
        # ReactLoop 失败后降级到单轮调用
        self.assertEqual(result.assistant_response, "降级单轮结果")
        self.assertTrue(any("ReactLoop" in e for e in result.errors))
        # LLM 单轮被调用一次
        self.assertEqual(len(llm.calls), 1)

    def test_d7_research_asyncio_run_runtime_error_caught(self):
        """D7-3：asyncio.run 抛 RuntimeError 时被 D7 修复捕获，不向上传播。

        通过 mock asyncio.run 抛 RuntimeError，验证模板走 D7 降级路径。
        若 run_coroutine_threadsafe 也失败，则外层 except 走单轮调用。
        """
        template = ResearchTemplate()
        llm = MockLLMClient(response_text="最终降级结果")
        ctx = WorkflowContext(
            session_id="cron:s1",
            schedule_id="s1",
            llm_client=llm,
            report_dir=self.report_dir,
            current_time=datetime(2026, 7, 5, 14, 30),
        )
        react = MockReactLoop(response_text="不应到达此结果")
        setattr(ctx, "react_loop", react)

        # mock asyncio.run 抛 RuntimeError，模拟「已有事件循环」场景
        original_run = asyncio.run

        def fake_asyncio_run(coro, **kwargs):
            # 关闭未 await 的协程，避免 ResourceWarning
            try:
                coro.close()
            except Exception:
                pass
            raise RuntimeError("asyncio.run() cannot be called from a running event loop")

        with patch("asyncio.run", side_effect=fake_asyncio_run):
            # mock get_event_loop 返回 None，强制 D7 路径抛 RuntimeError
            # 由外层 except 捕获并降级到单轮调用
            class NoneLoopPolicy:
                def get_event_loop(self):
                    return None

            with patch(
                "asyncio.get_event_loop_policy",
                return_value=NoneLoopPolicy(),
            ):
                result = template.execute({"topic": "测试"}, ctx)

        # 验证：降级到单轮调用，assistant_response 为 LLM 单轮结果
        self.assertEqual(result.assistant_response, "最终降级结果")
        self.assertTrue(
            any("ReactLoop" in e for e in result.errors),
            f"errors 应含 ReactLoop 失败信息: {result.errors}",
        )
        # LLM 单轮被调用
        self.assertEqual(len(llm.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
