"""Orchestrator 会话标题生成单元测试 — 验证 asyncio task 引用持有。

运行方式:
    python -m unittest tests.test_orchestrator_title -v
    python tests/test_orchestrator_title.py

mock 策略:
- 通过 Orchestrator.__new__ 绕过 __init__，仅设置测试所需属性
- llm_client / session_logger 用 MagicMock 替代
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.orchestrator import Orchestrator  # noqa: E402


def _make_llm_response(text: str) -> MagicMock:
    """构造 mock LLM 响应对象，.content 为含单个 text block 的列表。"""
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


class TestTitleTaskReference(unittest.IsolatedAsyncioTestCase):
    """验证 _maybe_generate_title_async 创建的 asyncio task 被强引用持有。

    bug 背景：asyncio.create_task 返回的 Task 仅被事件循环持弱引用，
    若调用方不保存强引用，task 会在被调度执行前被 GC 回收，导致
    会话标题永远不被生成（sessions.title 永远为 NULL）。
    """

    def _make_orchestrator(self) -> Orchestrator:
        """构造最小化 Orchestrator，仅设置标题生成所需属性。"""
        orch = Orchestrator.__new__(Orchestrator)
        orch._titled_sessions = set()
        orch._pending_title_tasks = set()
        orch.session_logger = MagicMock()
        orch.session_logger.get_session_title.return_value = None  # DB 中无标题

        llm_client = MagicMock()
        # chat_consolidation 是 async 方法，用 AsyncMock
        llm_client.chat_consolidation = AsyncMock(
            return_value=_make_llm_response("测试标题")
        )
        orch.llm_client = llm_client
        return orch

    async def test_title_task_reference_held_during_execution(self):
        """调用 _maybe_generate_title_async 后 _pending_title_tasks 应非空。"""
        orch = self._make_orchestrator()

        orch._maybe_generate_title_async("sess-123", "帮我查一下雷电将军的周边价格")

        # task 应已被加入 _pending_title_tasks（强引用持有）
        self.assertEqual(len(orch._pending_title_tasks), 1)

        # 等待 task 完成执行
        await asyncio.gather(*orch._pending_title_tasks)

        # task 完成后由 add_done_callback 自动从 set 中移除
        self.assertEqual(len(orch._pending_title_tasks), 0)

        # session_logger.update_session_title 被调用，标题被写入
        orch.session_logger.update_session_title.assert_called_once_with(
            "sess-123", "测试标题"
        )

        # _titled_sessions 缓存命中
        self.assertIn("sess-123", orch._titled_sessions)

    async def test_title_task_not_created_for_cron_session(self):
        """cron 会话跳过标题生成。"""
        orch = self._make_orchestrator()

        orch._maybe_generate_title_async("cron:sched_X", "some input")

        # cron session 不应创建 task
        self.assertEqual(len(orch._pending_title_tasks), 0)
        orch.llm_client.chat_consolidation.assert_not_called()

    async def test_title_task_skipped_when_already_titled(self):
        """已有标题的会话跳过生成。"""
        orch = self._make_orchestrator()
        # 模拟已有标题：_titled_sessions 缓存命中
        orch._titled_sessions.add("sess-already")

        orch._maybe_generate_title_async("sess-already", "some input")

        self.assertEqual(len(orch._pending_title_tasks), 0)
        orch.llm_client.chat_consolidation.assert_not_called()

    async def test_title_task_skipped_when_db_has_title(self):
        """DB 中已有标题的会话跳过生成（缓存回填路径）。"""
        orch = self._make_orchestrator()
        # 模拟 DB 查询返回非空标题
        orch.session_logger.get_session_title.return_value = "已有标题"

        orch._maybe_generate_title_async("sess-in-db", "some input")

        self.assertEqual(len(orch._pending_title_tasks), 0)
        # 缓存回填
        self.assertIn("sess-in-db", orch._titled_sessions)
        orch.llm_client.chat_consolidation.assert_not_called()

    async def test_title_task_failure_does_not_leak_reference(self):
        """LLM 调用失败时 task 也应被 add_done_callback 清理。"""
        orch = self._make_orchestrator()
        # 让 LLM 调用抛异常
        orch.llm_client.chat_consolidation = AsyncMock(
            side_effect=RuntimeError("LLM down")
        )

        orch._maybe_generate_title_async("sess-fail", "some input")

        self.assertEqual(len(orch._pending_title_tasks), 1)

        # 等待 task 完成（异常会被 _generate_title_task 内部 try/except 捕获）
        await asyncio.gather(*orch._pending_title_tasks)

        # task 完成后引用被清理
        self.assertEqual(len(orch._pending_title_tasks), 0)
        # 标题未被写入
        orch.session_logger.update_session_title.assert_not_called()

    async def test_runtime_error_in_create_task_logs_warning(self):
        """asyncio.create_task 抛 RuntimeError 时记录 warning 日志（不静默吞掉）。

        bug 背景：原实现 `except RuntimeError: pass` 静默吞掉 task 创建失败，
        导致客户端断连时标题永远不生成且无任何日志可追踪。
        """
        orch = self._make_orchestrator()
        # 模拟 asyncio.create_task 抛 RuntimeError（如无运行事件循环场景）
        with patch(
            "asyncio.create_task", side_effect=RuntimeError("no running loop")
        ):
            with self.assertLogs("src.orchestrator", level="WARNING") as cm:
                orch._maybe_generate_title_async("sess-runtime", "some input")

        # task 未创建
        self.assertEqual(len(orch._pending_title_tasks), 0)
        # 日志含 RuntimeError 信息
        self.assertTrue(
            any("创建标题生成任务失败" in msg for msg in cm.output),
            f"日志应含「创建标题生成任务失败」，实际: {cm.output}",
        )

    async def test_empty_title_logs_info(self):
        """LLM 返回空标题时记录 info 日志（便于排查为何标题未写入）。

        bug 背景：原实现空标题路径无日志，难以排查 LLM 返回空响应的 case。
        """
        orch = self._make_orchestrator()
        # LLM 返回空字符串
        orch.llm_client.chat_consolidation = AsyncMock(
            return_value=_make_llm_response("")
        )

        with self.assertLogs("src.orchestrator", level="INFO") as cm:
            orch._maybe_generate_title_async("sess-empty", "some input")
            await asyncio.gather(*orch._pending_title_tasks)

        # 标题未写入
        orch.session_logger.update_session_title.assert_not_called()
        # 日志含「标题生成返回空响应」
        self.assertTrue(
            any("标题生成返回空响应" in msg for msg in cm.output),
            f"日志应含「标题生成返回空响应」，实际: {cm.output}",
        )

    async def test_title_task_timeout_skips(self):
        """LLM 调用超时（asyncio.TimeoutError）时跳过标题写入，记录 warning。

        验证 asyncio.wait_for 的 15s 超时保护：LLM 卡死时不挂起 task。
        """
        orch = self._make_orchestrator()
        # 模拟 chat_consolidation 抛出 TimeoutError
        # （asyncio.wait_for 超时后内部会抛 asyncio.TimeoutError）
        orch.llm_client.chat_consolidation = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        with self.assertLogs("src.orchestrator", level="WARNING") as cm:
            orch._maybe_generate_title_async("sess-timeout", "some input")
            await asyncio.gather(*orch._pending_title_tasks)

        # 标题未写入
        orch.session_logger.update_session_title.assert_not_called()
        # 日志含超时信息
        self.assertTrue(
            any("标题生成超时" in msg for msg in cm.output),
            f"日志应含「标题生成超时」，实际: {cm.output}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
