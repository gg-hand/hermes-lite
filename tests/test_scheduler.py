"""CronExpr 与 CronScheduler 单元测试（Phase 6 Task 9）。

验证：
- ``src/tasks/cron_expr.py`` 的 CronExpr 类：通配符/具体值/步长/范围/列表/
  非法字段/next_run/dom 与 dow 的 OR 语义。
- ``src/tasks/scheduler.py`` 的 CronScheduler 类：配置加载/非法 cron 标记禁用/
  立即触发/同分钟去重/CRUD/YAML 持久化。

CronScheduler 测试使用 mock orchestrator（真实可调用对象，不调用 LLM），
每个测试用例使用独立的临时 schedules.yaml 文件。

运行方式:
    python -m unittest tests.test_scheduler -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.tasks.cron_expr import CronExpr  # noqa: E402
from teage_liu.tasks.scheduler import CronScheduler  # noqa: E402


class MockOrchestrator:
    """真实可调用的 mock orchestrator，记录 chat 调用。

    不使用 unittest.mock.MagicMock，因为 asyncio.to_thread 需要真实可调用对象。
    """

    def __init__(self):
        self.calls = []
        # history_buffer=None 让 _clear_cron_history 静默跳过（不阻塞测试）
        self.history_buffer = None

    async def chat(self, session_id, user_input, is_cron=False):
        self.calls.append((session_id, user_input))
        return "mock response"


# ===========================================================================
# CronExpr 测试
# ===========================================================================

class TestCronExpr(unittest.TestCase):
    """CronExpr 解析与匹配逻辑测试。"""

    def test_cron_expr_wildcard_match(self):
        """`* * * * *` 任意时间命中。"""
        expr = CronExpr("* * * * *")
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 30)))
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 0, 0)))
        self.assertTrue(expr.matches(datetime(2026, 12, 31, 23, 59)))

    def test_cron_expr_specific_minute(self):
        """`30 * * * *` 仅 30 分命中。"""
        expr = CronExpr("30 * * * *")
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 30)))
        self.assertFalse(expr.matches(datetime(2026, 6, 28, 9, 31)))

    def test_cron_expr_step(self):
        """`*/15 * * * *` 0/15/30/45 命中。"""
        expr = CronExpr("*/15 * * * *")
        for m in (0, 15, 30, 45):
            self.assertTrue(
                expr.matches(datetime(2026, 6, 28, 9, m)),
                f"分钟 {m} 应命中",
            )
        for m in (1, 14, 16, 29, 31, 44, 46):
            self.assertFalse(
                expr.matches(datetime(2026, 6, 28, 9, m)),
                f"分钟 {m} 不应命中",
            )

    def test_cron_expr_range(self):
        """`0 9 * * 1-5` 工作日 9 点命中。"""
        expr = CronExpr("0 9 * * 1-5")
        # 2026-06-29 是周一（cron dow=1），9:00 命中
        self.assertTrue(expr.matches(datetime(2026, 6, 29, 9, 0)))
        # 2026-06-28 是周日（cron dow=0），不命中
        self.assertFalse(expr.matches(datetime(2026, 6, 28, 9, 0)))
        # 周一 10:00 不命中（小时不符）
        self.assertFalse(expr.matches(datetime(2026, 6, 29, 10, 0)))

    def test_cron_expr_list(self):
        """`0,30 * * * *` 0 与 30 分命中。"""
        expr = CronExpr("0,30 * * * *")
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 0)))
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 30)))
        self.assertFalse(expr.matches(datetime(2026, 6, 28, 9, 15)))

    def test_cron_expr_invalid_field_raises(self):
        """`99 * * * *` 抛 ValueError。"""
        with self.assertRaises(ValueError):
            CronExpr("99 * * * *")

    def test_cron_expr_next_run(self):
        """next_run 返回正确未来时间。"""
        expr = CronExpr("30 * * * *")
        # 9:00 之后下一次命中是 9:30
        nxt = expr.next_run(datetime(2026, 6, 28, 9, 0, 0))
        self.assertEqual(nxt, datetime(2026, 6, 28, 9, 30, 0))
        # 恰好命中时返回严格大于 after 的下一次（10:30）
        nxt2 = expr.next_run(datetime(2026, 6, 28, 9, 30, 0))
        self.assertEqual(nxt2, datetime(2026, 6, 28, 10, 30, 0))

    def test_cron_expr_dom_and_dow_or_semantics(self):
        """日均非 * 时 OR 关系：任一字段命中即匹配。"""
        # `0 0 15 * 1`：每月 15 号 OR 每周一（cron dow=1）的 0:00
        expr = CronExpr("0 0 15 * 1")
        # 2026-07-15 是周三，day=15 命中 dom 分支
        self.assertTrue(expr.matches(datetime(2026, 7, 15, 0, 0)))
        # 2026-06-22 是周一，day=22 不命中 dom 但 cron dow=1 命中 dow
        self.assertTrue(expr.matches(datetime(2026, 6, 22, 0, 0)))
        # 2026-06-16 是周二，day=16 不命中 dom，dow=2 不命中 dow → 不匹配
        self.assertFalse(expr.matches(datetime(2026, 6, 16, 0, 0)))


# ===========================================================================
# CronScheduler 测试
# ===========================================================================

class TestCronScheduler(unittest.TestCase):
    """CronScheduler 调度器测试，每例使用独立临时 schedules.yaml。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_scheduler(self) -> CronScheduler:
        return CronScheduler(schedules_file=self.sched_file)

    def test_scheduler_load_from_config(self):
        """解析 config 段为 Schedule 列表。"""
        scheduler = self._new_scheduler()
        scheduler.load_from_config([
            {"id": "s1", "name": "每分钟", "cron": "* * * * *", "task": "hello"},
            {"id": "s2", "name": "九点", "cron": "0 9 * * *", "task": "morning", "enabled": False},
        ])
        schedules = scheduler.list_schedules()
        self.assertEqual(len(schedules), 2)
        self.assertEqual(schedules[0]["id"], "s1")
        self.assertTrue(schedules[0]["enabled"])
        self.assertEqual(schedules[1]["id"], "s2")
        self.assertFalse(schedules[1]["enabled"])

    def test_scheduler_load_invalid_cron_marks_disabled(self):
        """非法 cron 项 enabled=False。"""
        scheduler = self._new_scheduler()
        scheduler.load_from_config([
            {"id": "bad", "name": "非法", "cron": "99 * * * *", "task": "x"},
        ])
        schedules = scheduler.list_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertFalse(schedules[0]["enabled"])

    def test_scheduler_trigger_now(self):
        """立即触发调用 orchestrator.chat（验证调用次数与参数）。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "测试", "cron": "* * * * *", "task": "执行任务", "enabled": True,
        })
        orch = MockOrchestrator()
        asyncio.run(scheduler.trigger_now(orch, sched_id))
        self.assertEqual(len(orch.calls), 1)
        session_id, user_input = orch.calls[0]
        self.assertEqual(session_id, f"cron:{sched_id}")
        self.assertEqual(user_input, "执行任务")
        # 触发后 last_run 已更新
        sched = scheduler.get_schedule(sched_id)
        self.assertIsNotNone(sched["last_run"])

    def test_scheduler_dedup_same_minute(self):
        """同一分钟内仅触发一次（运行真实 run_loop，patch wait_for 加速）。"""
        scheduler = self._new_scheduler()
        scheduler.add_schedule({
            "name": "每分钟", "cron": "* * * * *", "task": "hello", "enabled": True,
        })
        orch = MockOrchestrator()
        iteration = {"count": 0}

        async def fake_wait_for(awaitable, timeout):
            iteration["count"] += 1
            # 关闭未 await 的 wait() 协程，避免 ResourceWarning
            if hasattr(awaitable, "close"):
                try:
                    awaitable.close()
                except Exception:
                    pass
            if iteration["count"] >= 2:
                scheduler.stop()
                return
            raise asyncio.TimeoutError()

        async def run():
            with patch("asyncio.wait_for", fake_wait_for):
                await scheduler.run_loop(orch)

        asyncio.run(run())
        # 两次迭代均在同一分钟内，第二次被去重，仅触发一次
        self.assertEqual(len(orch.calls), 1)

    def test_scheduler_crud(self):
        """add/update/delete/list 持久化。"""
        scheduler = self._new_scheduler()
        # add
        sched_id = scheduler.add_schedule({
            "name": "原", "cron": "* * * * *", "task": "t1", "enabled": True,
        })
        self.assertEqual(len(scheduler.list_schedules()), 1)
        # update
        ok = scheduler.update_schedule(sched_id, {"name": "新", "enabled": False})
        self.assertTrue(ok)
        sched = scheduler.get_schedule(sched_id)
        self.assertEqual(sched["name"], "新")
        self.assertFalse(sched["enabled"])
        # update 不存在
        self.assertFalse(scheduler.update_schedule("nope", {"enabled": True}))
        # delete
        self.assertTrue(scheduler.delete_schedule(sched_id))
        self.assertEqual(scheduler.list_schedules(), [])
        # delete 不存在
        self.assertFalse(scheduler.delete_schedule(sched_id))

    def test_scheduler_persistence_yaml(self):
        """YAML 读写 roundtrip。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "持久化", "cron": "0 9 * * *", "task": "morning", "enabled": True,
        })
        # 新实例同路径重新加载
        scheduler2 = CronScheduler(schedules_file=self.sched_file)
        schedules = scheduler2.list_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["id"], sched_id)
        self.assertEqual(schedules[0]["cron"], "0 9 * * *")
        self.assertEqual(schedules[0]["task"], "morning")


# ===========================================================================
# Task 10: _execute_workflow 双轨支持 + run_id 注入 + step_traces 持久化
# ===========================================================================


class WorkflowMockOrchestrator:
    """供 workflow 路径测试用的 mock orchestrator。

    提供最小依赖集合：``llm_client`` / ``chroma_store`` / ``session_logger`` /
    ``react_loop`` / ``tool_registry`` / ``policy_engine`` / ``audit_logger`` /
    ``skill_loader``，覆盖 ``_build_workflow_context`` 的 setattr 注入需求。
    不调用真实 LLM / SMTP / 文件系统。
    """

    def __init__(self):
        self.calls = []  # 记录 chat 调用（legacy 路径）
        self.llm_client = None
        self.chroma_store = None
        self.session_logger = None
        self.react_loop = None
        self.tool_registry = None
        self.policy_engine = None
        self.audit_logger = None
        self.skill_loader = None

    def chat(self, session_id, user_input):
        self.calls.append((session_id, user_input))
        return "mock response"


class TestExecuteWorkflowDualTrack(unittest.TestCase):
    """Task 10.2/10.5：_execute_workflow 双轨支持测试。

    验证：
    - 多步模式（含 steps）走 WorkflowEngine.execute
    - 简易模式（仅 template）走旧 WorkflowTemplate.execute
    - D2 修复：模板未找到返回 success=False
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")
        self.runs_dir = os.path.join(self.tmpdir, "schedules")
        # 使用临时目录避免污染 data/schedules
        from teage_liu.tasks.run_summary import RunsJsonlStore
        self._RunsJsonlStore = RunsJsonlStore

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_scheduler(self) -> CronScheduler:
        scheduler = CronScheduler(schedules_file=self.sched_file)
        # 注入临时 runs_store 避免污染 data/schedules
        scheduler.runs_store = self._RunsJsonlStore(self.runs_dir)
        return scheduler

    def test_multistep_workflow_goes_through_engine(self):
        """多步 workflow（含 steps）走 WorkflowEngine.execute 路径。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "多步",
            "cron": "0 9 * * *",
            "task": "执行多步",
            "enabled": True,
            "workflow": {
                "name": "test_workflow",
                "steps": [
                    {
                        "id": "s1",
                        "type": "deterministic",
                        "config": {"template": "email_notify"},
                    }
                ],
            },
        })
        orch = WorkflowMockOrchestrator()

        # patch WorkflowEngine.execute 验证调用路径
        from teage_liu.tasks import scheduler as sched_mod
        original_execute = sched_mod.WorkflowEngine.execute
        captured = {"called": False, "spec_name": None}

        def fake_execute(self_engine, spec, context):
            captured["called"] = True
            captured["spec_name"] = spec.name
            from teage_liu.tasks.workflow import WorkflowResult, StepTrace
            result = WorkflowResult(
                success=True,
                assistant_response="engine result",
                workflow_name=spec.name,
            )
            result.step_traces.append(
                StepTrace(step_id="s1", step_type="deterministic", status="success")
            )
            return result

        try:
            sched_mod.WorkflowEngine.execute = fake_execute
            asyncio.run(scheduler.trigger_now(orch, sched_id))
        finally:
            sched_mod.WorkflowEngine.execute = original_execute

        self.assertTrue(captured["called"], "WorkflowEngine.execute 应被调用")
        self.assertEqual(captured["spec_name"], "test_workflow")
        # 验证 RunSummary 已写入 runs.jsonl
        last_run = scheduler.runs_store.read_last(sched_id)
        self.assertIsNotNone(last_run)
        self.assertTrue(last_run.success)
        self.assertEqual(last_run.assistant_response, "engine result")
        self.assertEqual(last_run.workflow_name, "test_workflow")

    def test_simple_template_workflow_goes_through_template_path(self):
        """简易模式（仅 template）走旧 WorkflowTemplate.execute 路径。

        使用 mock 模板类避免真实 SMTP/LLM 依赖，验证：
        - WorkflowEngine.execute 未被调用
        - 模板 execute 被调用
        - StepTrace 包装补充（Task 10.3）
        """
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "简易",
            "cron": "0 9 * * *",
            "task": "执行简易",
            "enabled": True,
            "workflow": {"template": "test_mock_template"},
        })
        orch = WorkflowMockOrchestrator()

        # 注册 mock 模板到 BUILTIN_TEMPLATES
        from teage_liu.tasks import scheduler as sched_mod
        from teage_liu.tasks.workflow import WorkflowResult, WorkflowTemplate

        class MockTemplate(WorkflowTemplate):
            name = "test_mock_template"

            def execute(self, config, context):
                return WorkflowResult(
                    success=True, assistant_response="template result"
                )

        original_templates = dict(sched_mod.BUILTIN_TEMPLATES)
        sched_mod.BUILTIN_TEMPLATES["test_mock_template"] = MockTemplate

        # patch WorkflowEngine.execute 验证未被调用
        original_execute = sched_mod.WorkflowEngine.execute
        engine_called = {"called": False}

        def fake_execute(self_engine, spec, context):
            engine_called["called"] = True
            return WorkflowResult()

        try:
            sched_mod.WorkflowEngine.execute = fake_execute
            asyncio.run(scheduler.trigger_now(orch, sched_id))
        finally:
            sched_mod.WorkflowEngine.execute = original_execute
            sched_mod.BUILTIN_TEMPLATES.clear()
            sched_mod.BUILTIN_TEMPLATES.update(original_templates)

        self.assertFalse(engine_called["called"], "简易模式不应调 WorkflowEngine")
        last_run = scheduler.runs_store.read_last(sched_id)
        self.assertIsNotNone(last_run)
        self.assertEqual(last_run.assistant_response, "template result")
        # Task 10.3：旧路径也补充 step_traces
        self.assertEqual(len(last_run.step_traces), 1)
        self.assertEqual(last_run.step_traces[0]["status"], "success")
        self.assertEqual(last_run.step_traces[0]["step_id"], "template")

    def test_d2_unknown_template_returns_failure(self):
        """D2 修复：未知模板名返回 success=False（不抛异常）。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "未知模板",
            "cron": "0 9 * * *",
            "task": "执行未知",
            "enabled": True,
            "workflow": {"template": "nonexistent_template_xxx"},
        })
        orch = WorkflowMockOrchestrator()
        asyncio.run(scheduler.trigger_now(orch, sched_id))

        last_run = scheduler.runs_store.read_last(sched_id)
        self.assertIsNotNone(last_run)
        self.assertFalse(last_run.success)
        # 错误信息含模板名
        self.assertTrue(
            any("nonexistent_template_xxx" in e for e in last_run.errors),
            f"errors 应含模板名，实际: {last_run.errors}",
        )


class TestRunIdAndStepTracesPersistence(unittest.TestCase):
    """Task 10.1/10.4：run_id 注入 context + step_traces 持久化测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")
        self.runs_dir = os.path.join(self.tmpdir, "schedules")
        from teage_liu.tasks.run_summary import RunsJsonlStore
        self._RunsJsonlStore = RunsJsonlStore

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_scheduler(self) -> CronScheduler:
        scheduler = CronScheduler(schedules_file=self.sched_file)
        scheduler.runs_store = self._RunsJsonlStore(self.runs_dir)
        return scheduler

    def test_run_id_injected_into_context(self):
        """Task 10.1：run_id 通过 setattr 注入 WorkflowContext。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "run_id 测试",
            "cron": "0 9 * * *",
            "task": "测试 run_id",
            "enabled": True,
            "workflow": {
                "name": "ctx_test",
                "steps": [
                    {
                        "id": "s1",
                        "type": "deterministic",
                        "config": {},
                    }
                ],
            },
        })
        orch = WorkflowMockOrchestrator()

        from teage_liu.tasks import scheduler as sched_mod
        from teage_liu.tasks.workflow import WorkflowResult, StepTrace

        captured = {"context": None, "run_id": None}

        def fake_execute(self_engine, spec, context):
            captured["context"] = context
            captured["run_id"] = getattr(context, "run_id", None)
            # 验证其他 setattr 注入字段
            captured["tool_registry"] = getattr(context, "tool_registry", None)
            captured["policy_engine"] = getattr(context, "policy_engine", None)
            captured["audit_logger"] = getattr(context, "audit_logger", None)
            captured["orchestrator"] = getattr(context, "orchestrator", None)
            captured["session_id"] = getattr(context, "session_id", None)
            result = WorkflowResult(success=True, assistant_response="ok")
            result.step_traces.append(
                StepTrace(step_id="s1", step_type="deterministic", status="success")
            )
            return result

        original_execute = sched_mod.WorkflowEngine.execute
        try:
            sched_mod.WorkflowEngine.execute = fake_execute
            asyncio.run(scheduler.trigger_now(orch, sched_id))
        finally:
            sched_mod.WorkflowEngine.execute = original_execute

        # run_id 注入到 context
        self.assertIsNotNone(captured["run_id"], "context.run_id 应被注入")
        self.assertEqual(len(captured["run_id"]), 12, "run_id 应为 12 字符")
        # 其他 setattr 注入字段
        self.assertEqual(captured["session_id"], f"cron:{sched_id}")
        self.assertIs(captured["orchestrator"], orch)
        # run_id 持久化到 RunSummary
        last_run = scheduler.runs_store.read_last(sched_id)
        self.assertIsNotNone(last_run)
        self.assertEqual(last_run.run_id, captured["run_id"])

    def test_step_traces_persisted_to_runs_jsonl(self):
        """Task 10.4：step_traces 从 WorkflowResult 持久化到 runs.jsonl。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "step_traces 持久化",
            "cron": "0 9 * * *",
            "task": "测试 trace 持久化",
            "enabled": True,
            "workflow": {
                "name": "trace_test",
                "steps": [
                    {"id": "s1", "type": "deterministic", "config": {}},
                    {"id": "s2", "type": "llm", "config": {}, "depends_on": ["s1"]},
                ],
            },
        })
        orch = WorkflowMockOrchestrator()

        from teage_liu.tasks import scheduler as sched_mod
        from teage_liu.tasks.workflow import WorkflowResult, StepTrace

        def fake_execute(self_engine, spec, context):
            result = WorkflowResult(
                success=True,
                assistant_response="trace test ok",
                workflow_name="trace_test",
            )
            result.step_traces.append(
                StepTrace(
                    step_id="s1",
                    step_name="第一步",
                    step_type="deterministic",
                    status="success",
                    duration_ms=10,
                )
            )
            result.step_traces.append(
                StepTrace(
                    step_id="s2",
                    step_name="第二步",
                    step_type="llm",
                    status="success",
                    duration_ms=50,
                )
            )
            return result

        original_execute = sched_mod.WorkflowEngine.execute
        try:
            sched_mod.WorkflowEngine.execute = fake_execute
            asyncio.run(scheduler.trigger_now(orch, sched_id))
        finally:
            sched_mod.WorkflowEngine.execute = original_execute

        last_run = scheduler.runs_store.read_last(sched_id)
        self.assertIsNotNone(last_run)
        # step_traces 持久化为 List[Dict]
        self.assertEqual(len(last_run.step_traces), 2)
        self.assertEqual(last_run.step_traces[0]["step_id"], "s1")
        self.assertEqual(last_run.step_traces[0]["status"], "success")
        self.assertEqual(last_run.step_traces[0]["duration_ms"], 10)
        self.assertEqual(last_run.step_traces[1]["step_id"], "s2")
        self.assertEqual(last_run.step_traces[1]["step_type"], "llm")
        # workflow_name 持久化
        self.assertEqual(last_run.workflow_name, "trace_test")
        # run_id 持久化（12 字符）
        self.assertEqual(len(last_run.run_id), 12)


# ===========================================================================
# ops-reliability-uplift Task 4: _trigger 清空 history_buffer
# ===========================================================================


class MockHistoryBuffer:
    """记录 clear_session 调用的 mock history_buffer。"""

    def __init__(self):
        self.cleared_sessions = []

    def clear_session(self, session_id):
        self.cleared_sessions.append(session_id)


class HistoryAwareMockOrchestrator:
    """带 history_buffer 的 mock orchestrator（用于 Task 4 测试）。

    显式传 history_buffer=None 时保持 None（用于测试 None 兼容场景）。
    """

    _SENTINEL = object()

    def __init__(self, history_buffer=_SENTINEL):
        if history_buffer is self._SENTINEL:
            self.history_buffer = MockHistoryBuffer()
        else:
            self.history_buffer = history_buffer
        self.calls = []

    async def chat(self, session_id, user_input, is_cron=False):
        self.calls.append((session_id, user_input, is_cron))
        return "mock response"


class TestTriggerClearsHistory(unittest.TestCase):
    """验证 _trigger 触发前清空 history_buffer（Task 4.5）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_scheduler(self) -> CronScheduler:
        return CronScheduler(schedules_file=self.sched_file)

    def test_trigger_calls_clear_session_before_chat(self):
        """触发前调用 history_buffer.clear_session。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "测试", "cron": "* * * * *", "task": "执行任务", "enabled": True,
        })
        history_buf = MockHistoryBuffer()
        orch = HistoryAwareMockOrchestrator(history_buffer=history_buf)

        asyncio.run(scheduler.trigger_now(orch, sched_id))

        # clear_session 在 chat 之前被调用
        self.assertEqual(len(history_buf.cleared_sessions), 1)
        self.assertEqual(history_buf.cleared_sessions[0], f"cron:{sched_id}")
        # chat 也被调用
        self.assertEqual(len(orch.calls), 1)

    def test_clear_session_deletes_jsonl_file(self):
        """clear_session 删除磁盘 JSONL 文件（验证调用链路完整性）。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "测试", "cron": "* * * * *", "task": "执行任务", "enabled": True,
        })
        # 使用真实 HistoryBuffer，配置持久化路径
        from teage_liu.storage.history_buffer import HistoryBuffer
        persist_dir = os.path.join(self.tmpdir, "history")
        os.makedirs(persist_dir, exist_ok=True)
        history_buf = HistoryBuffer(max_turns=20, persistence_dir=persist_dir)
        session_id = f"cron:{sched_id}"
        # 模拟上次执行残留的 JSONL 文件（session_id 中 ':' 被替换为 '_'）
        safe_name = session_id.replace(":", "_")
        jsonl_path = os.path.join(persist_dir, f"{safe_name}.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write('{"role": "user", "content": "上次消息"}\n')

        orch = HistoryAwareMockOrchestrator(history_buffer=history_buf)
        asyncio.run(scheduler.trigger_now(orch, sched_id))

        # JSONL 文件应被删除
        self.assertFalse(os.path.exists(jsonl_path))

    def test_history_buffer_none_does_not_raise(self):
        """orchestrator.history_buffer 为 None 时不报错（兼容测试装配）。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "测试", "cron": "* * * * *", "task": "执行任务", "enabled": True,
        })
        # history_buffer 设为 None
        orch = HistoryAwareMockOrchestrator(history_buffer=None)
        # 不应抛异常
        asyncio.run(scheduler.trigger_now(orch, sched_id))
        # chat 仍被调用
        self.assertEqual(len(orch.calls), 1)

    def test_multiple_triggers_do_not_accumulate_history(self):
        """多次连续触发，每次都清空 history，不累积。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "测试", "cron": "* * * * *", "task": "执行任务", "enabled": True,
        })
        history_buf = MockHistoryBuffer()
        orch = HistoryAwareMockOrchestrator(history_buffer=history_buf)

        # 连续触发 3 次
        asyncio.run(scheduler.trigger_now(orch, sched_id))
        asyncio.run(scheduler.trigger_now(orch, sched_id))
        asyncio.run(scheduler.trigger_now(orch, sched_id))

        # clear_session 应被调用 3 次（每次触发前都清空）
        self.assertEqual(len(history_buf.cleared_sessions), 3)
        # 所有调用都是同一个 session_id
        self.assertTrue(all(s == f"cron:{sched_id}" for s in history_buf.cleared_sessions))
        # chat 也被调用 3 次
        self.assertEqual(len(orch.calls), 3)


# ===========================================================================
# 3.5 _collect_components 集中注入
# ===========================================================================


class TestCollectComponents(unittest.TestCase):
    """3.5: _collect_components 集中注入，消除手动 setattr 遗漏。"""

    def test_collect_components_returns_all_injected_attrs(self):
        """_collect_components 返回 _INJECT_COMPONENTS 列出的所有属性。"""
        from unittest.mock import MagicMock

        scheduler = CronScheduler.__new__(CronScheduler)
        mock_orch = MagicMock()
        mock_orch.tool_registry = "tr"
        mock_orch.cron_tool_registry = "ctr"
        mock_orch.policy_engine = "pe"
        mock_orch.audit_logger = "al"
        mock_orch.skill_loader = "sl"

        components = scheduler._collect_components(mock_orch)
        self.assertEqual(components["tool_registry"], "tr")
        self.assertEqual(components["cron_tool_registry"], "ctr")
        self.assertEqual(components["policy_engine"], "pe")
        self.assertEqual(components["audit_logger"], "al")
        self.assertEqual(components["skill_loader"], "sl")

    def test_collect_components_handles_missing_attrs(self):
        """orchestrator 缺少属性时返回 None，不抛 AttributeError。"""
        from unittest.mock import MagicMock

        scheduler = CronScheduler.__new__(CronScheduler)
        mock_orch = MagicMock(spec=[])  # 无任何属性

        components = scheduler._collect_components(mock_orch)
        self.assertIsNone(components["tool_registry"])
        self.assertIsNone(components["cron_tool_registry"])

    def test_INJECT_COMPONENTS_constant_exists(self):
        """_INJECT_COMPONENTS 类常量存在且包含 5 个组件。"""
        self.assertTrue(hasattr(CronScheduler, "_INJECT_COMPONENTS"))
        components = CronScheduler._INJECT_COMPONENTS
        self.assertIn("tool_registry", components)
        self.assertIn("cron_tool_registry", components)
        self.assertIn("policy_engine", components)
        self.assertIn("audit_logger", components)
        self.assertIn("skill_loader", components)


if __name__ == "__main__":
    unittest.main(verbosity=2)
