"""Orchestrator._build_cron_enhanced_context exclude_types 透传测试
（ops-reliability-uplift Task 5.7）。

验证：
- ``_build_cron_enhanced_context`` 调用 ``memory_retriever.get_injection_text``
  时传入了 ``exclude_types={"conversation_turn"}``，避免 cron 会话注入上次
  完整 assistant_response 造成"上下文污染偷懒"问题。
- 检索为空（返回空字符串）时 ``injection_text`` 仅含运行环境段（不报错）。

mock 策略：
- 通过 ``Orchestrator.__new__`` 绕过 ``__init__``，仅设置测试所需属性
- ``memory_retriever`` 用 ``MagicMock``，``get_injection_text`` 用 ``AsyncMock``
- ``_apply_condenser`` 用 ``AsyncMock`` 直接返回原 history（隔离 condenser 副作用）
- ``_build_cron_tools`` 用 ``MagicMock`` 返回 None（隔离工具过滤）

运行方式:
    python -m pytest tests/test_orchestrator_cron_context.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.orchestrator import Orchestrator  # noqa: E402
from src.memory.cron_isolation import CronIsolation  # noqa: E402
from src.agent.context_builder import ContextBuilder  # noqa: E402
from src.agent.cron_isolator import CronIsolator  # noqa: E402


def _make_orchestrator(
    memory_text: str = "",
    cron_inject_history_enabled: bool = True,
) -> Orchestrator:
    """构造最小化 Orchestrator，仅设置 cron 路径所需属性。

    参数:
        memory_text: mock 的 get_injection_text 返回值。空字符串模拟
            "检索为空"场景；非空字符串模拟"检索命中"场景。
        cron_inject_history_enabled: ops-reliability-uplift Task 7 的开关，
            True 时走过滤检索，False 时跳过检索。

    注意:
        orchestrator 内部用 ``asyncio.to_thread`` 包装 ``get_injection_text``
        调用（to_thread 期望 sync 函数），故 mock 必须用 sync MagicMock
        而非 AsyncMock——AsyncMock 返回 coroutine 但 to_thread 不会 await，
        导致 ``await_count=0`` 且 ``memory_text`` 实际为 coroutine 对象。
    """
    orch = Orchestrator.__new__(Orchestrator)

    # memory_retriever: MagicMock，get_injection_text 是 sync（被 to_thread 包装）
    retriever = MagicMock()
    retriever.get_injection_text = MagicMock(return_value=memory_text)
    orch.memory_retriever = retriever

    # metrics: MagicMock，observe_memory_retrieval 是同步方法
    orch.metrics = MagicMock()

    # ops-reliability-uplift Task 7: cron_inject_history_enabled 开关
    orch.cron_inject_history_enabled = cron_inject_history_enabled

    # _apply_condenser: 直接返回原 history（隔离 condenser 副作用）
    orch._apply_condenser = AsyncMock(side_effect=lambda h: h)

    # _build_cron_tools: 返回 None（隔离工具过滤逻辑）
    orch._build_cron_tools = MagicMock(return_value=None)

    # 委托管理器（方法对象模式，持有 orch 引用）
    orch.context_builder = ContextBuilder()
    orch.cron_isolator = CronIsolator(orchestrator=orch)

    return orch


class TestBuildCronEnhancedContextExcludeTypes(unittest.IsolatedAsyncioTestCase):
    """验证 _build_cron_enhanced_context 透传 exclude_types 给 retriever。"""

    async def test_exclude_types_passed_to_retriever(self):
        """cron 路径调用 get_injection_text 时应传 exclude_types={"conversation_turn"}。"""
        orch = _make_orchestrator(memory_text="[mock memory]")

        cron_isolation = CronIsolation(cron_id="sched_A")
        history = [{"role": "user", "content": "上次结果是什么"}]

        system_text, enhanced_history, tools_override = (
            await orch.cron_isolator.build_enhanced_context(
                session_id="cron:sched_A",
                user_input="本次问题",
                history=history,
                cron_isolation=cron_isolation,
            )
        )

        # 验证 get_injection_text 被调用且传入了 exclude_types
        # 注意：orchestrator 通过 asyncio.to_thread 包装 sync 调用，
        # 因此用 assert_called_once_with 而非 assert_awaited_once
        retriever = orch.memory_retriever
        retriever.get_injection_text.assert_called_once()
        call_args = retriever.get_injection_text.call_args
        # call_args.kwargs 兼容 kwargs 传参风格
        self.assertEqual(
            call_args.kwargs.get("namespace"), "cron",
            "应传 namespace='cron' 实现 cron 命名空间隔离",
        )
        self.assertEqual(
            call_args.kwargs.get("cron_id"), "sched_A",
            "应传 cron_id 实现调度项隔离",
        )
        self.assertEqual(
            call_args.kwargs.get("exclude_types"), {"conversation_turn"},
            "应传 exclude_types={'conversation_turn'} 过滤上次 assistant_response",
        )

        # system_text 不含用户画像（仅 SYSTEM_PROMPT）
        # enhanced_history 应含 mock memory（前置 user 消息）
        self.assertIn("[mock memory]", enhanced_history[0]["content"])

        # tools_override 为 None（mock 返回值）
        self.assertIsNone(tools_override)

        # metrics.observe_memory_retrieval 被调用（hit=True）
        orch.metrics.observe_memory_retrieval.assert_called_once_with(hit=True)

    async def test_empty_memory_injection_only_env_section(self):
        """检索为空（返回空字符串）时 injection_text 仅含运行环境段，不报错。"""
        # mock memory_retriever 返回空字符串（无相关记忆）
        orch = _make_orchestrator(memory_text="")

        cron_isolation = CronIsolation(cron_id="sched_B")
        history = [{"role": "user", "content": "新任务"}]

        system_text, enhanced_history, tools_override = (
            await orch.cron_isolator.build_enhanced_context(
                session_id="cron:sched_B",
                user_input="新任务输入",
                history=history,
                cron_isolation=cron_isolation,
            )
        )

        # get_injection_text 仍被调用（传 exclude_types）
        # 注意：通过 asyncio.to_thread 包装，sync 调用断言
        retriever = orch.memory_retriever
        retriever.get_injection_text.assert_called_once()
        self.assertEqual(
            retriever.get_injection_text.call_args.kwargs.get("exclude_types"),
            {"conversation_turn"},
            "即使检索为空也应传 exclude_types（保持调用一致性）",
        )

        # metrics 上报 hit=False（memory_text 为空）
        orch.metrics.observe_memory_retrieval.assert_called_once_with(hit=False)

        # enhanced_history[0] 应含运行环境段（## 运行环境），不含 [mock memory]
        first_msg = enhanced_history[0]["content"]
        self.assertIn("## 运行环境", first_msg)
        self.assertNotIn("[mock memory]", first_msg)

        # 原始 history 被保留（condenser mock 直接返回原 history）
        self.assertEqual(len(enhanced_history), 2)
        self.assertEqual(enhanced_history[1], history[0])


class TestBuildCronEnhancedContextInjectHistorySwitch(unittest.IsolatedAsyncioTestCase):
    """验证 _build_cron_enhanced_context 的 cron_inject_history_enabled 开关行为
    （ops-reliability-uplift Task 7.5）。"""

    async def test_inject_history_true_goes_through_filtered_retrieval(self):
        """inject_history=true 时走 Task 5 的过滤检索逻辑（exclude_types 生效）。"""
        # memory_text 非空，模拟检索命中
        orch = _make_orchestrator(
            memory_text="[mock summary]",
            cron_inject_history_enabled=True,
        )

        cron_isolation = CronIsolation(cron_id="sched_C")
        history = [{"role": "user", "content": "新任务"}]

        await orch.cron_isolator.build_enhanced_context(
            session_id="cron:sched_C",
            user_input="任务输入",
            history=history,
            cron_isolation=cron_isolation,
        )

        # get_injection_text 被调用（走过滤检索路径）
        retriever = orch.memory_retriever
        retriever.get_injection_text.assert_called_once()
        # exclude_types 仍传入（Task 5 的过滤逻辑保留）
        self.assertEqual(
            retriever.get_injection_text.call_args.kwargs.get("exclude_types"),
            {"conversation_turn"},
            "inject_history=true 时应走 exclude_types 过滤检索",
        )
        # metrics 上报 hit=True（memory_text 非空）
        orch.metrics.observe_memory_retrieval.assert_called_once_with(hit=True)

    async def test_inject_history_false_skips_retrieval(self):
        """inject_history=false 时跳过检索，messages[0] 不含任何上次结果。"""
        # 即使 memory_text 非空，inject_history=false 也不应调用 get_injection_text
        orch = _make_orchestrator(
            memory_text="[should not be called]",
            cron_inject_history_enabled=False,
        )

        cron_isolation = CronIsolation(cron_id="sched_D")
        history = [{"role": "user", "content": "新任务"}]

        system_text, enhanced_history, tools_override = (
            await orch.cron_isolator.build_enhanced_context(
                session_id="cron:sched_D",
                user_input="任务输入",
                history=history,
                cron_isolation=cron_isolation,
            )
        )

        # get_injection_text 不应被调用（跳过检索）
        retriever = orch.memory_retriever
        retriever.get_injection_text.assert_not_called()

        # metrics 仍上报 hit=False（便于监控区分"无相关记忆"与"配置关闭"）
        orch.metrics.observe_memory_retrieval.assert_called_once_with(hit=False)

        # enhanced_history[0] 仅含运行环境段（## 运行环境），不含 [should not be called]
        first_msg = enhanced_history[0]["content"]
        self.assertIn("## 运行环境", first_msg)
        self.assertNotIn("[should not be called]", first_msg)
        self.assertNotIn("mock summary", first_msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
