"""ReactLoop 自动续接 + 重试检测 + 总熔断 单元测试（Phase 9 Task 7.9）。

验证四道防线：
1. 单工具重试检测：同工具同参数重复 3 次触发卡死，返回卡死消息，is_complete=False
2. 重试检测：不同参数不触发卡死
3. 自动续接：达到 max_loops 且 TodoList 有未完成步骤时，orchestrator 自动续接
4. 自动续接：TodoList 全部完成时不续接
5. 总熔断：累计 200 轮强制结束
6. 正常完成：is_complete=True 时不续接

运行方式:
    python -m unittest tests.test_react_loop_continuation -v
    python tests/test_react_loop_continuation.py
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.react_loop import ReactLoop  # noqa: E402
from src.agent.context_builder import ContextBuilder  # noqa: E402
from src.agent.cron_isolator import CronIsolator  # noqa: E402
from src.agent.msg_persistence import MessagePersistence  # noqa: E402
from src.agent.session_manager import SessionManager  # noqa: E402
from src.agent.skill_manager import SkillManager  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_llm_response(
    text: str = "",
    stop_reason: str = "end_turn",
    tool_use_blocks=None,
):
    """构造 mock LLM 响应对象。"""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_use_blocks:
        content.extend(tool_use_blocks)
    response = MagicMock()
    response.content = content
    response.stop_reason = stop_reason
    return response


def _make_tool_use_block(
    name: str = "search",
    input_data: dict = None,
    block_id: str = "tool_1",
):
    """构造单个 tool_use block dict。"""
    return {
        "type": "tool_use",
        "id": block_id,
        "name": name,
        "input": input_data or {},
    }


# ---------------------------------------------------------------------------
# SubTask 7.3: 单工具重试检测
# ---------------------------------------------------------------------------

class TestToolStuckDetection(unittest.IsolatedAsyncioTestCase):
    """验证单工具重试检测逻辑。

    注：``run`` / ``chat_main`` 已 async，本类用 IsolatedAsyncioTestCase + await。
    """

    async def test_same_tool_same_params_triggers_stuck(self):
        """同工具同参数重复 3 次触发卡死，返回卡死消息，is_complete=False。"""
        # 构造 LLM 响应：每次都返回相同 tool_use（search, {"q": "test"}）
        tool_block = _make_tool_use_block(
            name="search",
            input_data={"q": "test"},
            block_id="tu_1",
        )
        # 第 1、2 次正常返回 tool_use，第 3 次触发卡死（在执行前检测）
        # 实际上：第 1 次执行后 recent_calls=[(search, hash1)]
        # 第 2 次执行后 recent_calls=[(search, hash1), (search, hash1)]
        # 第 3 次执行前检测：window=[(search, hash1), (search, hash1)]，
        # matches=2 >= threshold-1=2 → 卡死
        responses = [
            _make_llm_response(
                text="thinking", stop_reason="tool_use",
                tool_use_blocks=[tool_block],
            )
        ]
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=responses * 10)  # 重复返回相同响应

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "search", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=10,
        )
        response, messages, is_complete, _ = await loop.run("Hi")

        # 验证卡死消息
        self.assertIn("卡死", response)
        self.assertIn("search", response)
        # is_complete=False（卡死不算完成）
        self.assertFalse(is_complete)
        # execute_tool 最多被调用 2 次（第 3 次在执行前被卡死检测拦截）
        self.assertLessEqual(mock_registry.execute_tool.call_count, 2)

    async def test_warn_then_stop_on_repeated_same_params(self):
        """首次命中重复 → 软警告（不执行工具，循环继续）；二次命中 → 硬终止。

        时间线（threshold=3，window=5）：
        - 第 1 次：recent_calls=[]，is_stuck=False → 执行工具
        - 第 2 次：recent_calls=[(search,h1,None)]，matches=1 < 2，is_stuck=False → 执行工具
        - 第 3 次：recent_calls=[(...),(search,h1,None)]，matches=2 >= 2，is_stuck=True，
                  warned_pairs 空 → warn，不执行工具，recent_calls 追加 (search,h1,"stuck_warned")
        - 第 4 次：matches=3 >= 2，is_stuck=True，warned_pairs 含 (search,h1) → stop，硬终止
        """
        tool_block = _make_tool_use_block(
            name="search",
            input_data={"q": "test"},
            block_id="tu_1",
        )
        responses = [
            _make_llm_response(
                text="thinking", stop_reason="tool_use",
                tool_use_blocks=[tool_block],
            )
        ]
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=responses * 10)

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "search", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=10,
        )
        response, messages, is_complete, _ = await loop.run("Hi")

        # 硬终止：返回卡死消息
        self.assertIn("卡死", response)
        self.assertIn("search", response)
        self.assertFalse(is_complete)
        # 关键验证：execute_tool 仅被调用 2 次（第 1、2 次正常执行）
        # 第 3 次 warn 跳过执行，第 4 次 stop 硬终止
        self.assertEqual(mock_registry.execute_tool.call_count, 2)
        # messages 中应有一条 stuck_warned 的 tool_result（is_error=True）
        warn_results = [
            m for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                and b.get("is_error")
                for b in m["content"]
            )
        ]
        self.assertGreaterEqual(len(warn_results), 1, "应至少有一条 stuck_warned tool_result")

    async def test_different_params_does_not_trigger_stuck(self):
        """不同参数不触发卡死，正常完成循环。"""
        # 构造 3 次不同参数的 tool_use，然后 end_turn
        tool_blocks = [
            _make_tool_use_block(
                name="search",
                input_data={"q": f"query{i}"},
                block_id=f"tu_{i}",
            )
            for i in range(3)
        ]
        responses = [
            _make_llm_response(
                text=f"round {i}", stop_reason="tool_use",
                tool_use_blocks=[tool_blocks[i]],
            )
            for i in range(3)
        ]
        responses.append(
            _make_llm_response(text="done", stop_reason="end_turn")
        )

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=responses)

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "search", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=10,
        )
        response, messages, is_complete, _ = await loop.run("Hi")

        # 正常完成
        self.assertEqual(response, "done")
        self.assertTrue(is_complete)
        # execute_tool 被调用 3 次（不同参数，不触发卡死）
        self.assertEqual(mock_registry.execute_tool.call_count, 3)

    async def test_different_tools_same_params_does_not_trigger_stuck(self):
        """不同工具名相同参数不触发卡死。"""
        # 交替使用 search 和 fetch 工具，相同参数
        responses = []
        for i in range(4):
            tool_name = "search" if i % 2 == 0 else "fetch"
            responses.append(
                _make_llm_response(
                    text=f"round {i}", stop_reason="tool_use",
                    tool_use_blocks=[
                        _make_tool_use_block(
                            name=tool_name,
                            input_data={"q": "same"},
                            block_id=f"tu_{i}",
                        )
                    ],
                )
            )
        responses.append(
            _make_llm_response(text="done", stop_reason="end_turn")
        )

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=responses)

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "search", "input_schema": {}},
            {"name": "fetch", "description": "fetch", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=10,
        )
        response, messages, is_complete, _ = await loop.run("Hi")

        # 正常完成（不同工具名不触发卡死）
        self.assertEqual(response, "done")
        self.assertTrue(is_complete)

    def test_compute_params_hash_consistency(self):
        """_compute_params_hash 对相同 dict（不同 key 顺序）返回相同 hash。"""
        hash1 = ReactLoop._compute_params_hash({"a": 1, "b": 2})
        hash2 = ReactLoop._compute_params_hash({"b": 2, "a": 1})
        self.assertEqual(hash1, hash2)

    def test_detect_tool_stuck_threshold(self):
        """_detect_tool_stuck 在 threshold-1 次匹配时触发。"""
        # 空 recent_calls → 不触发
        is_stuck, reason = ReactLoop._detect_tool_stuck("search", "hash1", [])
        self.assertFalse(is_stuck)
        self.assertEqual(reason, "")
        # 1 次匹配 → 不触发（threshold=3，需 matches >= 2）
        is_stuck, _ = ReactLoop._detect_tool_stuck(
            "search", "hash1",
            [("search", "hash1", None)],
        )
        self.assertFalse(is_stuck)
        # 2 次匹配 → 触发
        is_stuck, _ = ReactLoop._detect_tool_stuck(
            "search", "hash1",
            [("search", "hash1", None), ("search", "hash1", None)],
        )
        self.assertTrue(is_stuck)
        # 2 次匹配但 window 外有更多 → 仅看 window_size=5
        calls = [("search", "hash1", None)] * 10
        is_stuck, _ = ReactLoop._detect_tool_stuck("search", "hash1", calls)
        self.assertTrue(is_stuck)
        # 不同 hash → 不触发
        is_stuck, _ = ReactLoop._detect_tool_stuck(
            "search", "hash2",
            [("search", "hash1", None), ("search", "hash1", None)],
        )
        self.assertFalse(is_stuck)


# ---------------------------------------------------------------------------
# SubTask 7.4: run() 返回值签名（is_complete）
# ---------------------------------------------------------------------------

class TestRunReturnValueSignature(unittest.IsolatedAsyncioTestCase):
    """验证 run() 返回四元组 (response, messages, is_complete, termination_reason)。

    注：``run`` / ``chat_main`` 已 async，本类用 IsolatedAsyncioTestCase + await。
    """

    async def test_end_turn_returns_is_complete_true(self):
        """end_turn 自然结束时 is_complete=True。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(return_value=_make_llm_response(
            text="Hello!", stop_reason="end_turn",
        ))
        loop = ReactLoop(llm_client=mock_llm, tool_registry=None, max_loops=5)
        response, messages, is_complete, _ = await loop.run("Hi")
        self.assertEqual(response, "Hello!")
        self.assertTrue(is_complete)

    async def test_max_loops_returns_is_complete_false(self):
        """达到 max_loops 时 is_complete=False。"""
        # 构造始终返回 tool_use 的响应，确保耗尽 max_loops
        # 使用不同参数避免卡死，确保是 max_loops 耗尽而非卡死
        responses = []
        for i in range(20):
            responses.append(
                _make_llm_response(
                    text=f"round {i}", stop_reason="tool_use",
                    tool_use_blocks=[
                        _make_tool_use_block(
                            name="search",
                            input_data={"q": f"query_{i}"},
                            block_id=f"tu_{i}",
                        )
                    ],
                )
            )

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=responses)

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "search", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=3,
        )
        response, messages, is_complete, _ = await loop.run("Hi")

        # max_loops 耗尽 → is_complete=False
        self.assertFalse(is_complete)

    async def test_tool_registry_none_returns_is_complete_false(self):
        """tool_registry 为 None 且模型请求工具时 is_complete=False。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(return_value=_make_llm_response(
            text="thinking", stop_reason="tool_use",
            tool_use_blocks=[
                _make_tool_use_block(name="search", input_data={})
            ],
        ))
        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=None, max_loops=5,
        )
        response, messages, is_complete, _ = await loop.run("Hi")
        self.assertFalse(is_complete)


# ---------------------------------------------------------------------------
# SubTask 7.6-7.8: orchestrator 自动续接 + 总熔断 + 续接消息
# ---------------------------------------------------------------------------

class TestOrchestratorContinuation(unittest.TestCase):
    """验证 orchestrator 自动续接包装层逻辑（通过辅助方法测试）。"""

    def test_has_unfinished_steps_none_dict(self):
        """todo_dict 为 None 时返回 False（不续接）。"""
        from src.orchestrator import Orchestrator
        self.assertFalse(ContextBuilder.has_unfinished_steps(None))

    def test_has_unfinished_steps_empty_steps(self):
        """todo_dict 无 steps 时返回 False。"""
        from src.orchestrator import Orchestrator
        self.assertFalse(
            ContextBuilder.has_unfinished_steps({"goal": "g", "steps": []})
        )

    def test_has_unfinished_steps_all_completed(self):
        """所有 step 均为 completed 时返回 False。"""
        from src.orchestrator import Orchestrator
        todo_dict = {
            "goal": "g",
            "steps": [
                {"id": 0, "status": "completed", "content": "a"},
                {"id": 1, "status": "completed", "content": "b"},
            ],
            "completed": True,
        }
        self.assertFalse(ContextBuilder.has_unfinished_steps(todo_dict))

    def test_has_unfinished_steps_has_pending(self):
        """有 pending 步骤时返回 True。"""
        from src.orchestrator import Orchestrator
        todo_dict = {
            "goal": "g",
            "steps": [
                {"id": 0, "status": "completed", "content": "a"},
                {"id": 1, "status": "pending", "content": "b"},
            ],
            "completed": False,
        }
        self.assertTrue(ContextBuilder.has_unfinished_steps(todo_dict))

    def test_has_unfinished_steps_has_in_progress(self):
        """有 in_progress 步骤时返回 True。"""
        from src.orchestrator import Orchestrator
        todo_dict = {
            "goal": "g",
            "steps": [
                {"id": 0, "status": "in_progress", "content": "a"},
            ],
            "completed": False,
        }
        self.assertTrue(ContextBuilder.has_unfinished_steps(todo_dict))

    def test_has_unfinished_steps_has_failed(self):
        """有 failed 步骤时返回 True（允许 LLM 重试）。"""
        from src.orchestrator import Orchestrator
        todo_dict = {
            "goal": "g",
            "steps": [
                {"id": 0, "status": "failed", "content": "a"},
            ],
            "completed": False,
        }
        self.assertTrue(ContextBuilder.has_unfinished_steps(todo_dict))

    def test_build_continuation_message_with_todo(self):
        """构造续接消息含 goal + 进度 + 未完成步骤。"""
        from src.orchestrator import Orchestrator
        # 需要一个 Orchestrator 实例来调用实例方法（或用 unbound 调用）
        # _build_continuation_message 委托到 context_builder
        orch = object.__new__(Orchestrator)
        orch.context_builder = ContextBuilder()
        todo_dict = {
            "goal": "完成报告",
            "steps": [
                {"id": 0, "status": "completed", "content": "调研"},
                {"id": 1, "status": "in_progress", "content": "写作"},
                {"id": 2, "status": "pending", "content": "审阅"},
            ],
            "completed": False,
        }
        msg = orch.context_builder.build_continuation_message(todo_dict)
        self.assertIn("上一轮已达循环上限", msg)
        self.assertIn("完成报告", msg)
        self.assertIn("1/3", msg)
        self.assertIn("写作", msg)
        self.assertIn("审阅", msg)
        self.assertIn("无需重复已完成的工作", msg)

    def test_build_continuation_message_none_todo(self):
        """todo_dict 为 None 时降级为通用续接消息。"""
        from src.orchestrator import Orchestrator
        orch = object.__new__(Orchestrator)
        orch.context_builder = ContextBuilder()
        msg = orch.context_builder.build_continuation_message(None)
        self.assertIn("上一轮已达循环上限", msg)
        self.assertIn("无需重复已完成的工作", msg)


class TestOrchestratorContinuationIntegration(unittest.IsolatedAsyncioTestCase):
    """验证 orchestrator chat() 自动续接集成逻辑（mock react_loop）。

    注：``orchestrator.chat`` / ``react_loop.run`` / ``_build_enhanced_context``
    / ``_maybe_flush_on_session_switch`` 已 async（spec Task 5），mock 用
    AsyncMock，测试方法 async def + await。
    """

    def _make_orchestrator_with_mocks(self):
        """构造一个最小化的 Orchestrator 实例（跳过 __init__）。"""
        from src.orchestrator import Orchestrator
        orch = object.__new__(Orchestrator)
        # 注入 mock 依赖
        orch.todo_registry = MagicMock()
        orch.react_loop = MagicMock()
        orch.react_loop.max_loops = 50
        orch.history_buffer = None
        orch.session_logger = None
        orch.consolidation_engine = None
        orch.task_manager = None
        orch.context_manager = None
        orch.memory_retriever = None
        # Phase 9 Task 6: 新增属性（None 跳过护栏与审计逻辑，向后兼容）
        orch.guardrail_engine = None
        orch.audit_logger = None
        orch._current_session_id = None
        # spec Task 5.1：chat() 访问 _pending_interrupt_notices，需手动注入
        orch._pending_interrupt_notices = {}
        # chat() 空回复计数路径访问 _consecutive_empty_runs
        orch._consecutive_empty_runs = {}
        orch.llm_client = None
        # Phase 1 反馈监控：chat() 调用 self.metrics.observe_termination
        orch.metrics = None
        # react_loop.run 已 async（spec Task 4），用 AsyncMock
        orch.react_loop.run = AsyncMock()
        # 委托管理器（方法对象模式，持有 orch 引用）
        orch.context_builder = ContextBuilder()
        orch.cron_isolator = CronIsolator(orchestrator=orch)
        orch.msg_persistence = MessagePersistence(orchestrator=orch)
        orch.session_mgr = SessionManager(llm_client=None, session_logger=None)
        orch.skill_mgr = SkillManager()
        # Phase 3 Task 9: EnhancedContextBuilder（Mock，build 方法由各用例单独 AsyncMock）
        orch.enhanced_context_builder = MagicMock()
        return orch

    async def test_auto_continuation_when_todo_unfinished(self):
        """达到 max_loops 且 TodoList 有未完成步骤时，orchestrator 自动续接。"""
        orch = self._make_orchestrator_with_mocks()

        # mock react_loop.run：第一次返回 is_complete=False，第二次 True
        call_count = [0]

        async def mock_run(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次：达到 max_loops，is_complete=False
                return ("partial", [], False, "normal")
            else:
                # 第二次：自然完成
                return ("final answer", [], True, "normal")

        orch.react_loop.run.side_effect = mock_run

        # mock todo_registry：返回有未完成步骤的 todo
        orch.todo_registry.get_todo_dict.return_value = {
            "goal": "g",
            "steps": [
                {"id": 0, "status": "in_progress", "content": "step1"},
            ],
            "completed": False,
        }

        # mock _build_enhanced_context 返回简单三元组（async，spec Task 5.8）
        orch.enhanced_context_builder.build = AsyncMock(
            return_value=("system", [], None)
        )
        # mock _persist_new_messages 避免历史缓冲逻辑（sync，保持 MagicMock）
        orch._persist_new_messages = MagicMock()
        orch._maybe_flush_on_session_switch = AsyncMock()

        # 调用 chat()（async，spec Task 5.1）
        result = await orch.chat("session-1", "Hi")

        # 验证：调用了 2 次 react_loop.run（续接 1 次）
        self.assertEqual(orch.react_loop.run.call_count, 2)
        # 验证：最终返回第二次的结果
        self.assertEqual(result, "final answer")
        # 验证：第二次调用的 user_input 是续接消息
        second_call_kwargs = orch.react_loop.run.call_args_list[1].kwargs
        self.assertIn("上一轮已达循环上限", second_call_kwargs["user_input"])
        self.assertIn("step1", second_call_kwargs["user_input"])

    async def test_no_continuation_when_todo_all_completed(self):
        """TodoList 全部完成时不续接（is_complete=False 但不续接）。"""
        orch = self._make_orchestrator_with_mocks()

        # mock react_loop.run：返回 is_complete=False
        orch.react_loop.run.return_value = ("partial", [], False, "normal")

        # mock todo_registry：返回所有步骤已完成
        orch.todo_registry.get_todo_dict.return_value = {
            "goal": "g",
            "steps": [
                {"id": 0, "status": "completed", "content": "step1"},
            ],
            "completed": True,
        }

        orch.enhanced_context_builder.build = AsyncMock(
            return_value=("system", [], None)
        )
        orch._persist_new_messages = MagicMock()
        orch._maybe_flush_on_session_switch = AsyncMock()

        result = await orch.chat("session-1", "Hi")

        # 验证：只调用了 1 次 react_loop.run（不续接）
        self.assertEqual(orch.react_loop.run.call_count, 1)
        self.assertEqual(result, "partial")

    async def test_no_continuation_when_no_todo_registry(self):
        """todo_registry 为 None 时不续接。"""
        orch = self._make_orchestrator_with_mocks()
        orch.todo_registry = None

        orch.react_loop.run.return_value = ("partial", [], False, "normal")

        orch.enhanced_context_builder.build = AsyncMock(
            return_value=("system", [], None)
        )
        orch._persist_new_messages = MagicMock()
        orch._maybe_flush_on_session_switch = AsyncMock()

        result = await orch.chat("session-1", "Hi")

        # 验证：只调用了 1 次 react_loop.run（不续接）
        self.assertEqual(orch.react_loop.run.call_count, 1)
        self.assertEqual(result, "partial")

    async def test_no_continuation_when_is_complete_true(self):
        """is_complete=True 时不续接。"""
        orch = self._make_orchestrator_with_mocks()

        orch.react_loop.run.return_value = ("done", [], True, "normal")

        orch.enhanced_context_builder.build = AsyncMock(
            return_value=("system", [], None)
        )
        orch._persist_new_messages = MagicMock()
        orch._maybe_flush_on_session_switch = AsyncMock()

        result = await orch.chat("session-1", "Hi")

        # 验证：只调用了 1 次 react_loop.run
        self.assertEqual(orch.react_loop.run.call_count, 1)
        self.assertEqual(result, "done")
        # todo_registry 不应被查询
        orch.todo_registry.get_todo_dict.assert_not_called()

    async def test_total_circuit_breaker_200_rounds(self):
        """总熔断 200 轮强制结束。"""
        orch = self._make_orchestrator_with_mocks()
        # 设置 max_loops=50，则 200/50=4 次调用后触发熔断
        orch.react_loop.max_loops = 50

        # mock react_loop.run：始终返回 is_complete=False
        orch.react_loop.run.return_value = ("partial", [], False, "normal")

        # mock todo_registry：始终返回有未完成步骤
        orch.todo_registry.get_todo_dict.return_value = {
            "goal": "g",
            "steps": [
                {"id": 0, "status": "in_progress", "content": "step1"},
            ],
            "completed": False,
        }

        orch.enhanced_context_builder.build = AsyncMock(
            return_value=("system", [], None)
        )
        orch._persist_new_messages = MagicMock()
        orch._maybe_flush_on_session_switch = AsyncMock()

        result = await orch.chat("session-1", "Hi")

        # 验证：调用了 4 次 react_loop.run（4*50=200 触发熔断）
        self.assertEqual(orch.react_loop.run.call_count, 4)
        # 验证：返回熔断消息
        self.assertIn("总轮次上限 200", result)
        self.assertIn("终止", result)

    async def test_observe_termination_called_with_react_loop_reason(self):
        """Phase 1 反馈监控：orchestrator.chat() 调用后 metrics.observe_termination 被触发。

        验证非流式路径埋点：orchestrator.py:885-887 在 await react_loop.run() 后
        立即上报 termination_reason。
        """
        from src.monitoring.metrics import MetricsCollector

        orch = self._make_orchestrator_with_mocks()
        # 注入真实 metrics 实例（替换默认的 None）
        metrics = MetricsCollector()
        orch.metrics = metrics

        # 模拟正常完成
        orch.react_loop.run.return_value = ("done", [], True, "normal")
        orch.enhanced_context_builder.build = AsyncMock(
            return_value=("system", [], None)
        )
        orch._persist_new_messages = MagicMock()
        orch._maybe_flush_on_session_switch = AsyncMock()

        await orch.chat("session-1", "Hi")

        # 验证 termination_reason 上报
        snap = metrics.snapshot()
        self.assertEqual(
            snap["termination_reasons_total"].get("normal", 0), 1
        )

    async def test_observe_termination_reports_each_continuation_round(self):
        """Phase 1 反馈监控：自动续接场景下每次 run() 都上报 termination_reason。"""
        from src.monitoring.metrics import MetricsCollector

        orch = self._make_orchestrator_with_mocks()
        metrics = MetricsCollector()
        orch.metrics = metrics

        # mock react_loop.run：第一次 max_loops 耗尽，第二次正常完成
        call_count = [0]

        async def mock_run(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "partial", [], False, "max_loops"
            return "final answer", [], True, "normal"

        orch.react_loop.run = AsyncMock(side_effect=mock_run)
        orch.react_loop.max_loops = 50

        orch.todo_registry.get_todo_dict.return_value = {
            "goal": "g",
            "steps": [{"id": 0, "status": "in_progress", "content": "step1"}],
            "completed": False,
        }
        orch.enhanced_context_builder.build = AsyncMock(
            return_value=("system", [], None)
        )
        orch._persist_new_messages = MagicMock()
        orch._maybe_flush_on_session_switch = AsyncMock()

        await orch.chat("session-1", "Hi")

        # 验证两次 termination_reason 都被上报
        snap = metrics.snapshot()
        self.assertEqual(
            snap["termination_reasons_total"].get("max_loops", 0), 1
        )
        self.assertEqual(
            snap["termination_reasons_total"].get("normal", 0), 1
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
