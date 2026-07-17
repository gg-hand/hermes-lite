"""CronScheduler archive_callback cron 分支迁移测试
（ops-reliability-uplift Task 6.6）。

验证：
- ``_build_cron_archive_callback`` 返回的闭包归档上次 run 的 ``llm_summary``
  而非 FIFO 淘汰的完整 ``conversation_turn`` 原文（避免上下文污染）
- ``llm_summary`` 为空时跳过归档（不写空记录）
- cron 归档 metadata ``type=summary``，与 user session 的
  ``type=conversation_turn`` 隔离
- user session 侧（sid 不以 ``cron:`` 开头）由 orchestrator 默认闭包处理，
  scheduler 闭包直接 return 不干预
- ``_trigger`` 临时覆盖 ``history_buffer.archive_callback`` 后用 try/finally
  恢复原值，即使 chat 抛异常也能恢复

mock 策略：
- ``CronScheduler.__new__`` 绕过 ``__init__``，仅设置 ``runs_store``
- ``orchestrator`` / ``chroma_store`` / ``history_buffer`` 用 ``MagicMock``
- ``RunSummary`` 直接构造实例（dataclass）

运行方式:
    python -m pytest tests/test_archive_callback.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.tasks.run_summary import RunSummary  # noqa: E402
from hermes.tasks.scheduler import CronScheduler, Schedule  # noqa: E402


def _make_scheduler(runs_store: MagicMock) -> CronScheduler:
    """构造最小化 CronScheduler，仅设置 runs_store 属性。"""
    sched = CronScheduler.__new__(CronScheduler)
    sched.runs_store = runs_store
    return sched


def _make_orchestrator_with_chroma(chroma_store: MagicMock) -> MagicMock:
    """构造 mock orchestrator，含 chroma_store / history_buffer 属性。"""
    orch = MagicMock()
    orch.chroma_store = chroma_store
    orch.history_buffer = MagicMock()
    orch.history_buffer.archive_callback = MagicMock(name="orig_archive_cb")
    return orch


class TestCronArchiveCallbackLlmSummary(unittest.TestCase):
    """验证 cron 闭包归档 llm_summary 而非原文。"""

    def setUp(self):
        self.runs_store = MagicMock()
        self.chroma_store = MagicMock()
        self.scheduler = _make_scheduler(self.runs_store)
        self.orchestrator = _make_orchestrator_with_chroma(self.chroma_store)

    def test_cron_session_archives_llm_summary_not_raw_content(self):
        """cron session 归档 llm_summary 而非 FIFO 淘汰的原文。"""
        last_run = RunSummary(
            schedule_id="sched_A",
            run_id="run_001",
            llm_summary="上一次执行的精炼摘要：监控到 3 条新博客",
            assistant_response="这是完整的 assistant_response（应被忽略）",
        )
        self.runs_store.read_last.return_value = last_run

        cb = self.scheduler._build_cron_archive_callback(
            self.orchestrator, "sched_A"
        )
        self.assertIsNotNone(cb, "应返回闭包（runs_store + chroma_store 均可用）")

        # 模拟 FIFO 淘汰调用：sid=cron:sched_A, msg 含完整 conversation_turn
        msg = {
            "role": "assistant",
            "content": "完整的 assistant_response（应被忽略，不应进入向量库）",
            "timestamp": "2026-07-07T10:00:00",
        }
        cb("cron:sched_A", msg)

        # 验证 chroma_store.add_memory 被调用且 content 是 llm_summary
        self.chroma_store.add_memory.assert_called_once()
        call_kwargs = self.chroma_store.add_memory.call_args
        content_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("content")
        self.assertEqual(content_arg, "上一次执行的精炼摘要：监控到 3 条新博客")
        self.assertNotIn("完整的 assistant_response", content_arg)

    def test_cron_session_empty_llm_summary_skips_archive(self):
        """llm_summary 为空时跳过归档（不写空记录）。"""
        last_run = RunSummary(
            schedule_id="sched_A",
            run_id="run_002",
            llm_summary="",  # 空 summary
            assistant_response="有 response 但无 summary",
        )
        self.runs_store.read_last.return_value = last_run

        cb = self.scheduler._build_cron_archive_callback(
            self.orchestrator, "sched_A"
        )
        cb("cron:sched_A", {"role": "assistant", "content": "msg"})

        # chroma_store.add_memory 不应被调用
        self.chroma_store.add_memory.assert_not_called()

    def test_cron_session_metadata_type_is_summary(self):
        """cron 归档 metadata type=summary（与 user session conversation_turn 隔离）。"""
        last_run = RunSummary(
            schedule_id="sched_A",
            run_id="run_003",
            llm_summary="摘要内容",
        )
        self.runs_store.read_last.return_value = last_run

        cb = self.scheduler._build_cron_archive_callback(
            self.orchestrator, "sched_A"
        )
        cb("cron:sched_A", {"role": "assistant", "content": "ignored"})

        self.chroma_store.add_memory.assert_called_once()
        call_kwargs = self.chroma_store.add_memory.call_args
        metadata = call_kwargs.kwargs.get("metadata", {})
        self.assertEqual(metadata.get("type"), "summary")
        # namespace + cron_id 隔离
        self.assertEqual(call_kwargs.kwargs.get("namespace"), "cron")
        self.assertEqual(call_kwargs.kwargs.get("cron_id"), "sched_A")

    def test_user_session_sid_not_cron_returns_silently(self):
        """user session（sid 不以 cron: 开头）由 orchestrator 闭包处理，scheduler 闭包直接 return。"""
        last_run = RunSummary(
            schedule_id="sched_A",
            run_id="run_004",
            llm_summary="user session 不应触发 scheduler 闭包",
        )
        self.runs_store.read_last.return_value = last_run

        cb = self.scheduler._build_cron_archive_callback(
            self.orchestrator, "sched_A"
        )
        # user session sid
        cb("user_session_abc", {"role": "user", "content": "user msg"})

        # 即使 runs_store.read_last 已 mock 返回值，也不应调用 chroma_store
        self.runs_store.read_last.assert_not_called()
        self.chroma_store.add_memory.assert_not_called()


class TestTriggerArchiveCallbackOverrideAndRestore(unittest.IsolatedAsyncioTestCase):
    """验证 _trigger 临时覆盖 + try/finally 恢复 archive_callback。"""

    async def test_trigger_overrides_and_restores_archive_callback(self):
        """_trigger 期间覆盖 archive_callback，结束后恢复原值。"""
        # 构造 scheduler
        runs_store = MagicMock()
        last_run = RunSummary(
            schedule_id="sched_X",
            run_id="run_005",
            llm_summary="上次摘要",
        )
        runs_store.read_last.return_value = last_run

        # 构造 orchestrator：history_buffer.archive_callback 可被覆盖
        chroma_store = MagicMock()
        orig_cb = MagicMock(name="orig_archive_cb")
        history_buffer = MagicMock()
        history_buffer.archive_callback = orig_cb

        orchestrator = MagicMock()
        orchestrator.chroma_store = chroma_store
        orchestrator.history_buffer = history_buffer
        orchestrator.chat = AsyncMock(side_effect=RuntimeError("mock chat failure"))
        orchestrator.session_logger = None  # 跳过 title 设置

        scheduler = CronScheduler.__new__(CronScheduler)
        scheduler.runs_store = runs_store
        scheduler._cron_exprs = {}
        scheduler._persist = MagicMock()
        scheduler._parse_last_run_time = MagicMock(return_value=None)
        scheduler._append_run_summary = MagicMock()
        scheduler._clear_cron_history = MagicMock()
        # Q1+3.6：_run_schedule 需要的额外属性
        scheduler.hooks = MagicMock()
        scheduler.hooks.before_execute = AsyncMock(return_value=MagicMock(validation_errors=[]))
        scheduler.hooks.after_execute = AsyncMock()
        scheduler.hooks.get_retry_max = MagicMock(return_value=3)
        scheduler._failure_counts = {}
        scheduler._orchestrator = orchestrator
        scheduler._schedules = []
        scheduler.workflow_context_factory = None
        scheduler._build_cron_archive_callback = MagicMock(return_value=None)

        # 构造 schedule（无 workflow 走 legacy 路径）
        schedule = Schedule(
            id="sched_X",
            name="测试调度",
            cron="*/5 * * * *",
            task="测试任务",
        )

        # 执行 _run_schedule（chat 会抛异常，但内部 try/except 捕获）
        from datetime import datetime
        await scheduler._run_schedule(orchestrator, schedule, datetime.now())

        # 验证 archive_callback 被恢复为原值
        self.assertIs(
            history_buffer.archive_callback, orig_cb,
            "无论 chat 成功或异常，archive_callback 必须恢复原值",
        )

    async def test_trigger_overwrites_callback_during_chat(self):
        """_trigger 期间 archive_callback 被替换为新闭包（chat 执行期间）。"""
        runs_store = MagicMock()
        last_run = RunSummary(
            schedule_id="sched_Y",
            run_id="run_006",
            llm_summary="上次摘要 Y",
        )
        runs_store.read_last.return_value = last_run

        chroma_store = MagicMock()
        orig_cb = MagicMock(name="orig_archive_cb")
        history_buffer = MagicMock()
        history_buffer.archive_callback = orig_cb

        # 记录 chat 执行瞬间 archive_callback 的值
        captured_cb_during_chat = []

        async def mock_chat(sid, task, is_cron=False):
            captured_cb_during_chat.append(history_buffer.archive_callback)
            return "mock response"

        orchestrator = MagicMock()
        orchestrator.chroma_store = chroma_store
        orchestrator.history_buffer = history_buffer
        orchestrator.chat = AsyncMock(side_effect=mock_chat)
        orchestrator.session_logger = None

        scheduler = CronScheduler.__new__(CronScheduler)
        scheduler.runs_store = runs_store
        scheduler._cron_exprs = {}
        scheduler._persist = MagicMock()
        scheduler._parse_last_run_time = MagicMock(return_value=None)
        scheduler._append_run_summary = MagicMock()
        scheduler._clear_cron_history = MagicMock()
        # Q1+3.6：_run_schedule 需要的额外属性
        scheduler.hooks = MagicMock()
        scheduler.hooks.before_execute = AsyncMock(return_value=MagicMock(validation_errors=[]))
        scheduler.hooks.after_execute = AsyncMock()
        scheduler.hooks.get_retry_max = MagicMock(return_value=3)
        scheduler._failure_counts = {}
        scheduler._orchestrator = orchestrator
        scheduler._schedules = []
        scheduler.workflow_context_factory = None
        # 注意：返回非 None 闭包以触发 archive_callback 覆盖逻辑
        _fake_cron_cb = MagicMock(name="cron_archive_cb")
        scheduler._build_cron_archive_callback = MagicMock(return_value=_fake_cron_cb)

        schedule = Schedule(
            id="sched_Y",
            name="测试调度 Y",
            cron="*/5 * * * *",
            task="测试任务 Y",
        )

        from datetime import datetime
        await scheduler._run_schedule(orchestrator, schedule, datetime.now())

        # chat 执行期间 archive_callback 应被替换为新闭包（非 orig_cb）
        self.assertEqual(len(captured_cb_during_chat), 1)
        self.assertIsNot(
            captured_cb_during_chat[0], orig_cb,
            "chat 执行期间 archive_callback 应被替换为 cron 专用闭包",
        )

        # 结束后恢复
        self.assertIs(
            history_buffer.archive_callback, orig_cb,
            "结束后必须恢复原 archive_callback",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
