"""Orchestrator.chat_stream 流式过程消息持久化测试（T6）。

验证 chat_stream 在流式过程中实时收集事件，并在 finally 块中按事件顺序
批量写入 session_logger：
1. 单轮对话：记录 user + final assistant
2. 多轮带工具调用：记录 tool_use + tool_result 4 条消息
3. 多轮纯文本：每轮 assistant 文本独立记录
4. 流中断：finally 块仍写入已收集的消息
5. tool 消息携带 tool_call_id 字段

运行方式：
    python -m unittest tests.test_orchestrator_persistence -v
    python tests/test_orchestrator_persistence.py

mock 策略：
- ReactLoop：直接 yield 预设事件序列的 async generator
- session_logger：记录所有 log_message 调用，便于断言
- Orchestrator：通过 __new__ 绕过 __init__，仅设置测试需要的属性
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.orchestrator import Orchestrator  # noqa: E402
from teage_liu.orchestrator.chat_handler import ChatHandler  # noqa: E402
from teage_liu.orchestrator.enhanced_context import EnhancedContextBuilder  # noqa: E402
from teage_liu.tasks.todo_list import TodoListRegistry  # noqa: E402
from teage_liu.agent.context_builder import ContextBuilder  # noqa: E402
from teage_liu.agent.cron_isolator import CronIsolator  # noqa: E402
from teage_liu.agent.msg_persistence import MessagePersistence  # noqa: E402
from teage_liu.agent.session_manager import SessionManager  # noqa: E402
from teage_liu.agent.skill_manager import SkillManager  # noqa: E402


# ---------------------------------------------------------------------------
# mock 构造工具
# ---------------------------------------------------------------------------


class MockReactLoop:
    """Mock ReactLoop，按预设事件序列 yield（async generator）。

    模拟 ReactLoop.run_stream 的行为：依次 yield 预设的事件 dict。
    """

    def __init__(self, events):
        self._events = list(events)

    async def run_stream(self, user_input=None, history=None, system=None,
                        session_id=None, **kwargs):
        for evt in self._events:
            yield evt


class InterruptingReactLoop:
    """Mock ReactLoop，yield 完预设事件后抛出指定异常。

    用于测试流被中断时 finally 块是否仍能写入已收集的消息。
    """

    def __init__(self, events, exc):
        self._events = list(events)
        self._exc = exc

    async def run_stream(self, user_input=None, history=None, system=None,
                        session_id=None, **kwargs):
        for evt in self._events:
            yield evt
        raise self._exc


class SideEffectReactLoop:
    """Mock ReactLoop，yield 事件前先执行关联的副作用函数。

    用于模拟工具执行的副作用（如 plan_task 调用 todo_registry.init_plan、
    update_todo 调用 todo_registry.update_step）。在真实流程中，工具先执行
    （产生副作用）再由 ReactLoop yield tool 事件，本 mock 复现该时序。

    events 参数为 ``(event_dict, side_effect)`` 元组列表，``side_effect``
    为 ``None`` 时不执行任何副作用。
    """

    def __init__(self, events):
        # events: List[Tuple[dict, Optional[Callable]]]
        self._events = list(events)

    async def run_stream(self, user_input=None, history=None, system=None,
                        session_id=None, **kwargs):
        for evt, side_effect in self._events:
            if side_effect:
                side_effect()
            yield evt


class MockSessionLogger:
    """Mock session_logger，记录所有 log_message 调用便于断言。

    同时维护一个内存版 sessions 列表，支持 list_sessions / create_session。
    """

    def __init__(self):
        # 每条记录：{session_id, role, content, tool_name, tool_call_id, token_count, is_error}
        self.logged = []
        self._sessions = []

    def list_sessions(self):
        return [{"id": sid} for sid in self._sessions]

    def create_session(self, session_id):
        if session_id not in self._sessions:
            self._sessions.append(session_id)

    def log_message(self, session_id, role, content, tool_name=None,
                    tool_call_id=None, token_count=0, is_error=False):
        self.logged.append({
            "session_id": session_id,
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "token_count": token_count,
            "is_error": is_error,
        })


def _make_orchestrator(react_loop, session_logger, todo_registry=None):
    """创建 Orchestrator 实例但绕过 __init__。

    __init__ 需要 config.yaml 与大量真实依赖，这里通过 __new__ 绕过，
    仅设置 chat_stream 路径上需要用到的属性：
    - react_loop / session_logger：注入 mock
    - history_buffer / consolidation_engine：设为 None 跳过对应逻辑
    - context_manager / memory_retriever / task_manager：设为 None，
      使 _build_enhanced_context 降级返回 (SYSTEM_PROMPT, history)
    - todo_registry：T13 新增，注入 TodoListRegistry（默认 None 跳过 todo 事件）
    - guardrail_engine：Phase 9 Task 6 新增，设为 None 跳过护栏逻辑
      （chat_stream 中所有 self.guardrail_engine 检查都先判 None）
    - audit_logger：Phase 9 Task 6 新增审计路径需要，设为 None 跳过审计记录
    - _last_session_id / _current_session_id：会话状态初值
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch.react_loop = react_loop
    orch.session_logger = session_logger
    orch.history_buffer = None
    orch.consolidation_engine = None
    orch.context_manager = None
    orch.memory_retriever = None
    orch.task_manager = None
    orch.todo_registry = todo_registry
    # Phase 9 Task 6: 新增属性（None 跳过护栏与审计逻辑，向后兼容）
    orch.guardrail_engine = None
    orch.audit_logger = None
    orch._last_session_id = None
    orch._current_session_id = None
    orch._pending_interrupt_notices = {}
    orch.llm_client = None
    orch._consecutive_empty_runs = {}
    orch.metrics = None
    # 委托管理器（方法对象模式，持有 orch 引用）
    orch.context_builder = ContextBuilder()
    orch.cron_isolator = CronIsolator(orchestrator=orch)
    orch.msg_persistence = MessagePersistence(orchestrator=orch)
    orch.session_mgr = SessionManager(
        llm_client=None, session_logger=session_logger
    )
    orch.skill_mgr = SkillManager()
    orch.enhanced_context_builder = EnhancedContextBuilder(orch)
    orch.chat_handler = ChatHandler(orch)
    return orch


def _consume(orch, session_id, user_input):
    """驱动 chat_stream async generator，返回 yield 的事件列表。"""

    events = []

    async def _run():
        async for evt in orch.chat_stream(session_id, user_input):
            events.append(evt)

    asyncio.run(_run())
    return events


def _consume_expect_raise(orch, session_id, user_input, exc_type):
    """驱动 chat_stream 预期会抛异常，返回 (yield 的事件, 抛出的异常)。"""

    events = []
    raised = None

    async def _run():
        nonlocal raised
        try:
            async for evt in orch.chat_stream(session_id, user_input):
                events.append(evt)
        except Exception as e:  # noqa: BLE001
            raised = e

    asyncio.run(_run())
    return events, raised


def _text_event(text):
    """构造文本增量事件。"""
    return {"type": "text", "text": text}


def _round_start_event(loop_idx):
    """构造 round_start 事件。"""
    return {"type": "round_start", "loop_idx": loop_idx}


def _tool_event(name, input_data, result, is_error=False, session_id=None):
    """构造工具调用事件。

    字段与 src/agent/react_loop.py 中 run_stream yield 的 tool 事件一致：
    {type, name, input, result, is_error, session_id}（无 tool_use_id 字段）。
    """
    return {
        "type": "tool",
        "name": name,
        "input": input_data,
        "result": result,
        "is_error": is_error,
        "session_id": session_id,
    }


def _done_event(response):
    """构造 done 事件。"""
    return {"type": "done", "response": response}


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestChatStreamPersistsUserAndFinalAssistant(unittest.TestCase):
    """1. 基础场景：单轮对话，验证记录 user + final assistant。"""

    def test_chat_stream_persists_user_and_final_assistant(self):
        """单轮 round_start -> text -> done，应记录 user + assistant 两条消息。"""
        events = [
            _round_start_event(0),
            _text_event("Hello!"),
            _done_event("Hello!"),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(MockReactLoop(events), logger)

        consumed = _consume(orch, "sess-1", "user input")

        # 事件应被透传：status（chat_stream 自身）+ round_start + text + done = 4
        self.assertEqual(len(consumed), 4)

        # 应记录 2 条消息：user + assistant
        logged = logger.logged
        self.assertEqual(len(logged), 2)

        # 第 1 条：user 输入
        self.assertEqual(logged[0]["session_id"], "sess-1")
        self.assertEqual(logged[0]["role"], "user")
        self.assertEqual(logged[0]["content"], "user input")
        self.assertIsNone(logged[0]["tool_name"])

        # 第 2 条：assistant 最终回复
        self.assertEqual(logged[1]["role"], "assistant")
        self.assertEqual(logged[1]["content"], "Hello!")
        self.assertIsNone(logged[1]["tool_name"])


class TestChatStreamPersistsToolMessages(unittest.TestCase):
    """2. 多轮带工具调用，验证记录 tool_use + tool_result 4 条消息。"""

    def test_chat_stream_persists_tool_messages(self):
        """2 轮工具调用：每轮 tool 事件记录 tool_use + tool_result 2 条消息，共 4 条工具消息。"""
        events = [
            _round_start_event(0),
            _text_event("R1"),
            _tool_event("search", {"q": "x"}, "r1", session_id="sess-1"),
            _round_start_event(1),
            _text_event("R2"),
            _tool_event("search", {"q": "y"}, "r2", session_id="sess-1"),
            _round_start_event(2),
            _text_event("Final"),
            _done_event("Final"),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(MockReactLoop(events), logger)

        _consume(orch, "sess-1", "user input")

        logged = logger.logged

        # 工具消息（tool_name 非空）：2 次 tool 调用 × 2 条 = 4 条
        tool_msgs = [m for m in logged if m["tool_name"]]
        self.assertEqual(
            len(tool_msgs), 4,
            f"应有 4 条工具消息，实际 {len(tool_msgs)}: {tool_msgs}",
        )

        # 其中 2 条 tool_use（role=assistant，记录调用）
        tool_use_msgs = [m for m in tool_msgs if m["role"] == "assistant"]
        self.assertEqual(len(tool_use_msgs), 2)
        for m in tool_use_msgs:
            self.assertEqual(m["tool_name"], "search")
            self.assertIn("调用工具 search", m["content"])

        # 其中 2 条 tool_result（role=user，记录结果）
        tool_result_msgs = [m for m in tool_msgs if m["role"] == "user"]
        self.assertEqual(len(tool_result_msgs), 2)
        result_contents = sorted(m["content"] for m in tool_result_msgs)
        self.assertEqual(result_contents, ["r1", "r2"])

        # tool_use 在 tool_result 之前（每个 tool 事件先记 use 后记 result）
        # 验证顺序：第 1 个 tool_use 在第 1 个 tool_result 之前
        first_use_idx = logged.index(tool_use_msgs[0])
        first_result_idx = logged.index(tool_result_msgs[0])
        self.assertLess(first_use_idx, first_result_idx)


class TestChatStreamPersistsPerRoundText(unittest.TestCase):
    """3. 多轮纯文本（无工具），验证每轮 assistant 文本独立记录。"""

    def test_chat_stream_persists_per_round_text(self):
        """3 轮纯文本：每轮 assistant 文本在下一轮 round_start 时独立提交。"""
        events = [
            _round_start_event(0),
            _text_event("R1"),
            _round_start_event(1),
            _text_event("R2"),
            _round_start_event(2),
            _text_event("R3"),
            _done_event("R3"),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(MockReactLoop(events), logger)

        _consume(orch, "sess-1", "user input")

        logged = logger.logged

        # 应记录：1 user + 3 assistant = 4 条
        self.assertEqual(len(logged), 4)

        # 第 1 条 user 输入
        self.assertEqual(logged[0]["role"], "user")
        self.assertEqual(logged[0]["content"], "user input")

        # 后 3 条为各轮 assistant 文本，按时间顺序 R1/R2/R3
        assistant_msgs = [m for m in logged if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 3)
        self.assertEqual(
            [m["content"] for m in assistant_msgs],
            ["R1", "R2", "R3"],
        )

        # 所有 assistant 消息均无 tool_name（纯文本）
        for m in assistant_msgs:
            self.assertIsNone(m["tool_name"])


class TestChatStreamPersistsOnInterrupt(unittest.TestCase):
    """4. 模拟流中断（generator 抛异常），验证 finally 仍写入。"""

    def test_chat_stream_persists_on_interrupt(self):
        """流被中断时，finally 块仍写入已收集的 user + tool 消息。"""
        events = [
            _round_start_event(0),
            _text_event("R1"),
            _tool_event("search", {"q": "x"}, "r1", session_id="sess-1"),
            # 抛异常前的最后一轮累加了文本，但未触发 round_start/done 提交
            _round_start_event(1),
            _text_event("partial-text"),
            # 这里 InterruptingReactLoop 会抛 RuntimeError
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            InterruptingReactLoop(events, RuntimeError("stream interrupted")),
            logger,
        )

        consumed, raised = _consume_expect_raise(
            orch, "sess-1", "user input", RuntimeError
        )

        # 异常应被抛出
        self.assertIsInstance(raised, RuntimeError)
        self.assertEqual(str(raised), "stream interrupted")

        # 已透传的事件（在抛异常前 yield 的）
        self.assertGreaterEqual(len(consumed), 4)

        # finally 块应仍写入：user + R1(在 round_start(1) 时提交) + tool_use + tool_result
        logged = logger.logged
        self.assertGreaterEqual(
            len(logged), 4,
            f"finally 应至少写入 4 条已收集消息，实际 {len(logged)}: {logged}",
        )

        # 第 1 条：user 输入
        self.assertEqual(logged[0]["role"], "user")
        self.assertEqual(logged[0]["content"], "user input")

        # 第 2 条：R1 文本（在 round_start(1) 时作为上一轮文本提交）
        self.assertEqual(logged[1]["role"], "assistant")
        self.assertEqual(logged[1]["content"], "R1")

        # 第 3、4 条：tool_use + tool_result
        self.assertEqual(logged[2]["role"], "assistant")
        self.assertIsNotNone(logged[2]["tool_name"])
        self.assertEqual(logged[2]["tool_name"], "search")

        self.assertEqual(logged[3]["role"], "user")
        self.assertEqual(logged[3]["content"], "r1")
        self.assertEqual(logged[3]["tool_name"], "search")


class TestChatStreamToolCallIdPresent(unittest.TestCase):
    """5. 验证 tool 消息带 tool_call_id 字段。"""

    def test_chat_stream_tool_call_id_present(self):
        """tool_use 与 tool_result 消息都应有非空 tool_call_id，且同次调用配对一致。"""
        events = [
            _round_start_event(0),
            _text_event("R1"),
            _tool_event("search", {"q": "x"}, "r1", session_id="sess-1"),
            _round_start_event(1),
            _text_event("R2"),
            _tool_event("calculator", {"expr": "1+1"}, "2", session_id="sess-1"),
            _round_start_event(2),
            _text_event("Final"),
            _done_event("Final"),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(MockReactLoop(events), logger)

        _consume(orch, "sess-1", "user input")

        logged = logger.logged
        tool_msgs = [m for m in logged if m["tool_name"]]

        # 2 次工具调用 × 2 条 = 4 条
        self.assertEqual(len(tool_msgs), 4)

        # 所有 tool 消息都应有 tool_call_id
        for m in tool_msgs:
            self.assertIsNotNone(m["tool_call_id"])
            self.assertNotEqual(m["tool_call_id"], "")
            self.assertIsInstance(m["tool_call_id"], str)

        # 第 1 次调用：tool_use 与 tool_result 的 tool_call_id 应一致
        first_use = tool_msgs[0]
        first_result = tool_msgs[1]
        self.assertEqual(first_use["tool_call_id"], first_result["tool_call_id"])
        self.assertEqual(first_use["tool_name"], "search")

        # 第 2 次调用：tool_use 与 tool_result 的 tool_call_id 应一致
        second_use = tool_msgs[2]
        second_result = tool_msgs[3]
        self.assertEqual(
            second_use["tool_call_id"], second_result["tool_call_id"]
        )
        self.assertEqual(second_use["tool_name"], "calculator")

        # 两次调用的 tool_call_id 应不同（自增计数器）
        self.assertNotEqual(
            first_use["tool_call_id"], second_use["tool_call_id"]
        )

        # 验证 tool_call_id 格式为 "tool_{N}"（ReactLoop 未透传 tool_use_id 时的 fallback）
        self.assertEqual(first_use["tool_call_id"], "tool_0")
        self.assertEqual(second_use["tool_call_id"], "tool_1")


# ---------------------------------------------------------------------------
# T13: todo 事件发射测试
# ---------------------------------------------------------------------------


class TestTodoInitEventEmitted(unittest.TestCase):
    """T13: plan_task 工具调用后发射 todo_init 事件。"""

    def test_plan_task_emits_todo_init_event(self):
        """plan_task tool 事件后应发射 todo_init 事件，含完整 todo dict。"""
        todo_registry = TodoListRegistry()
        session_id = "sess-todo-1"

        # 模拟 plan_task 工具执行副作用：调用 init_plan 初始化 todo
        def plan_side_effect():
            todo_registry.init_plan(session_id, "完成示例任务", [
                {"content": "步骤一"},
                {"content": "步骤二", "depends_on": [0]},
            ])

        events = [
            (_round_start_event(0), None),
            (_text_event("开始规划"), None),
            (_tool_event(
                "plan_create",
                {"goal": "完成示例任务", "steps": [
                    {"content": "步骤一"},
                    {"content": "步骤二", "depends_on": [0]},
                ]},
                "已规划 2 个步骤，开始执行 step 0: 步骤一",
                session_id=session_id,
            ), plan_side_effect),
            (_round_start_event(1), None),
            (_text_event("开始执行"), None),
            (_done_event("开始执行"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        consumed = _consume(orch, session_id, "帮我完成任务")

        # 应发射 1 个 todo_init 事件
        todo_init_events = [e for e in consumed if e.get("type") == "todo_init"]
        self.assertEqual(
            len(todo_init_events), 1,
            f"应发射 1 个 todo_init 事件，实际 {len(todo_init_events)}",
        )

        evt = todo_init_events[0]
        self.assertEqual(evt["session_id"], session_id)
        todo = evt["todo"]
        self.assertEqual(todo["goal"], "完成示例任务")
        self.assertEqual(len(todo["steps"]), 2)
        self.assertEqual(todo["steps"][0]["content"], "步骤一")
        self.assertEqual(todo["steps"][0]["status"], "in_progress")
        self.assertEqual(todo["steps"][1]["status"], "pending")
        self.assertEqual(todo["steps"][1]["depends_on"], [0])
        self.assertFalse(todo["completed"])

        # 事件顺序：tool(plan_task) 紧接 todo_init
        tool_idx = next(
            i for i, e in enumerate(consumed)
            if e.get("type") == "tool" and e.get("name") == "plan_create"
        )
        todo_init_idx = next(
            i for i, e in enumerate(consumed) if e.get("type") == "todo_init"
        )
        self.assertEqual(todo_init_idx, tool_idx + 1)

    def test_plan_task_todo_init_before_next_text(self):
        """todo_init 事件应在下一轮 text 之前发射。"""
        todo_registry = TodoListRegistry()
        session_id = "sess-todo-2"

        def plan_side_effect():
            todo_registry.init_plan(session_id, "任务", [{"content": "一步"}])

        events = [
            (_round_start_event(0), None),
            (_tool_event("plan_create", {"goal": "任务", "steps": []},
                         "已规划 1 个步骤", session_id=session_id), plan_side_effect),
            (_text_event("下一步文本"), None),
            (_done_event("下一步文本"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        consumed = _consume(orch, session_id, "做任务")

        todo_init_idx = next(
            i for i, e in enumerate(consumed) if e.get("type") == "todo_init"
        )
        text_idx = next(
            i for i, e in enumerate(consumed)
            if e.get("type") == "text" and e.get("text") == "下一步文本"
        )
        self.assertLess(todo_init_idx, text_idx)


class TestTodoUpdateEventEmitted(unittest.TestCase):
    """T13: update_todo 工具调用后发射 todo_update 事件。"""

    def test_update_todo_emits_todo_update_event(self):
        """update_todo tool 事件后应发射 todo_update 事件，todo 为变更后的完整列表。"""
        todo_registry = TodoListRegistry()
        session_id = "sess-todo-3"
        # 预初始化 plan（模拟上一轮 plan_task 已执行）
        todo_registry.init_plan(session_id, "示例任务", [
            {"content": "步骤一"},
            {"content": "步骤二", "depends_on": [0]},
        ])

        # 模拟 update_todo 工具执行副作用：完成 step 0
        def update_side_effect():
            todo_registry.update_step(session_id, 0, "completed", "步骤一完成")

        events = [
            (_round_start_event(0), None),
            (_tool_event("plan_update_step", {"step_id": 0, "status": "completed"},
                         "✅ 步骤 0 状态已更新为: completed",
                         session_id=session_id), update_side_effect),
            (_text_event("继续执行"), None),
            (_done_event("继续执行"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        consumed = _consume(orch, session_id, "更新步骤")

        # 应发射 todo_update（completed=False，因 step 1 仍 pending→in_progress）
        todo_update_events = [e for e in consumed if e.get("type") == "todo_update"]
        self.assertEqual(len(todo_update_events), 1)

        evt = todo_update_events[0]
        self.assertEqual(evt["session_id"], session_id)
        todo = evt["todo"]
        self.assertEqual(todo["steps"][0]["status"], "completed")
        # step 0 完成后自动推进 step 1 为 in_progress
        self.assertEqual(todo["steps"][1]["status"], "in_progress")
        self.assertFalse(todo["completed"])

        # 不应发射 todo_complete（仍有未完成步骤）
        todo_complete_events = [
            e for e in consumed if e.get("type") == "todo_complete"
        ]
        self.assertEqual(len(todo_complete_events), 0)


class TestTodoCompleteEventEmitted(unittest.TestCase):
    """T13: update_todo 后所有 step 完成时发射 todo_complete 事件。"""

    def test_update_todo_all_completed_emits_todo_complete(self):
        """update_todo 使所有 step 均为 completed 时，应额外发射 todo_complete 事件。"""
        todo_registry = TodoListRegistry()
        session_id = "sess-todo-4"
        # 单步骤 plan，完成即全部完成
        todo_registry.init_plan(session_id, "单步任务", [{"content": "唯一步骤"}])

        def update_side_effect():
            todo_registry.update_step(session_id, 0, "completed", "完成")

        events = [
            (_round_start_event(0), None),
            (_tool_event("plan_update_step", {"step_id": 0, "status": "completed"},
                         "✅ 步骤 0 状态已更新为: completed",
                         session_id=session_id), update_side_effect),
            (_done_event("完成"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        consumed = _consume(orch, session_id, "完成最后步骤")

        # 应发射 todo_update
        todo_update_events = [e for e in consumed if e.get("type") == "todo_update"]
        self.assertEqual(len(todo_update_events), 1)

        # 应额外发射 todo_complete
        todo_complete_events = [
            e for e in consumed if e.get("type") == "todo_complete"
        ]
        self.assertEqual(
            len(todo_complete_events), 1,
            f"应发射 1 个 todo_complete 事件，实际 {len(todo_complete_events)}",
        )

        complete_evt = todo_complete_events[0]
        self.assertEqual(complete_evt["session_id"], session_id)
        self.assertTrue(complete_evt["todo"]["completed"])
        self.assertEqual(
            complete_evt["todo"]["steps"][0]["status"], "completed"
        )

        # 事件顺序：tool → todo_update → todo_complete
        tool_idx = next(
            i for i, e in enumerate(consumed)
            if e.get("type") == "tool" and e.get("name") == "plan_update_step"
        )
        update_idx = next(
            i for i, e in enumerate(consumed) if e.get("type") == "todo_update"
        )
        complete_idx = next(
            i for i, e in enumerate(consumed) if e.get("type") == "todo_complete"
        )
        self.assertEqual(update_idx, tool_idx + 1)
        self.assertEqual(complete_idx, update_idx + 1)


class TestTodoPersistenceContentReplaced(unittest.TestCase):
    """T13: plan_task / update_todo 持久化的 tool_result content 替换为 todo_dict JSON。"""

    def test_plan_task_persistence_content_is_todo_json(self):
        """plan_task 的 tool_result 持久化 content 应为 todo_dict 的 JSON 字符串。"""
        import json as _json

        todo_registry = TodoListRegistry()
        session_id = "sess-todo-5"

        def plan_side_effect():
            todo_registry.init_plan(session_id, "任务", [
                {"content": "步骤A"},
            ])

        events = [
            (_round_start_event(0), None),
            (_tool_event("plan_create", {"goal": "任务", "steps": []},
                         "已规划 1 个步骤", session_id=session_id), plan_side_effect),
            (_done_event("done"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        _consume(orch, session_id, "规划任务")

        # 找到 plan_task 的 tool_result 消息（role=user, tool_name=plan_task）
        tool_result_msgs = [
            m for m in logger.logged
            if m["tool_name"] == "plan_create" and m["role"] == "user"
        ]
        self.assertEqual(len(tool_result_msgs), 1)

        content = tool_result_msgs[0]["content"]
        # content 应为合法 JSON，解析后含 goal / steps / completed
        parsed = _json.loads(content)
        self.assertEqual(parsed["goal"], "任务")
        self.assertEqual(len(parsed["steps"]), 1)
        self.assertEqual(parsed["steps"][0]["content"], "步骤A")
        self.assertFalse(parsed["completed"])

    def test_update_todo_persistence_content_is_todo_json(self):
        """update_todo 的 tool_result 持久化 content 应为 todo_dict 的 JSON 字符串。"""
        import json as _json

        todo_registry = TodoListRegistry()
        session_id = "sess-todo-6"
        todo_registry.init_plan(session_id, "任务", [{"content": "步骤A"}])

        def update_side_effect():
            todo_registry.update_step(session_id, 0, "completed", "完成")

        events = [
            (_round_start_event(0), None),
            (_tool_event("plan_update_step", {"step_id": 0, "status": "completed"},
                         "✅ 步骤 0 状态已更新为: completed",
                         session_id=session_id), update_side_effect),
            (_done_event("done"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        _consume(orch, session_id, "更新步骤")

        tool_result_msgs = [
            m for m in logger.logged
            if m["tool_name"] == "plan_update_step" and m["role"] == "user"
        ]
        self.assertEqual(len(tool_result_msgs), 1)

        content = tool_result_msgs[0]["content"]
        parsed = _json.loads(content)
        self.assertTrue(parsed["completed"])
        self.assertEqual(parsed["steps"][0]["status"], "completed")


class TestTodoEventsDegradation(unittest.TestCase):
    """T13: todo_registry 为 None 或无 plan 时降级跳过，不抛异常。"""

    def test_no_todo_events_when_registry_none(self):
        """todo_registry 为 None 时，plan_task/update_todo tool 事件后不发射 todo 事件。"""
        events = [
            (_round_start_event(0), None),
            (_tool_event("plan_create", {"goal": "x", "steps": []},
                         "已规划", session_id="s"), None),
            (_tool_event("plan_update_step", {"step_id": 0, "status": "completed"},
                         "✅", session_id="s"), None),
            (_done_event("done"), None),
        ]
        logger = MockSessionLogger()
        # todo_registry 默认 None
        orch = _make_orchestrator(SideEffectReactLoop(events), logger)

        consumed = _consume(orch, "s", "input")

        # 不应有任何 todo 事件
        todo_events = [
            e for e in consumed
            if e.get("type") in ("todo_init", "todo_update", "todo_complete")
        ]
        self.assertEqual(
            len(todo_events), 0,
            f"todo_registry 为 None 时不应发射 todo 事件，实际 {todo_events}",
        )

    def test_no_todo_events_when_session_has_no_plan(self):
        """session 无 plan 时（get_todo_dict 返回 None），降级跳过不抛异常。"""
        todo_registry = TodoListRegistry()
        session_id = "sess-no-plan"
        # 不调用 init_plan，get_todo_dict 将返回 None

        events = [
            (_round_start_event(0), None),
            (_tool_event("plan_create", {"goal": "x", "steps": []},
                         "已规划", session_id=session_id), None),
            (_tool_event("plan_update_step", {"step_id": 0, "status": "completed"},
                         "✅", session_id=session_id), None),
            (_done_event("done"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        consumed = _consume(orch, session_id, "input")

        # get_todo_dict 返回 None，不应发射 todo 事件，也不应抛异常
        todo_events = [
            e for e in consumed
            if e.get("type") in ("todo_init", "todo_update", "todo_complete")
        ]
        self.assertEqual(len(todo_events), 0)

    def test_non_todo_tools_unaffected(self):
        """非 plan_task/update_todo 的普通工具不应发射 todo 事件。"""
        todo_registry = TodoListRegistry()
        session_id = "sess-normal"
        todo_registry.init_plan(session_id, "已有计划", [{"content": "步"}])

        events = [
            (_round_start_event(0), None),
            (_tool_event("search", {"q": "x"}, "result",
                         session_id=session_id), None),
            (_done_event("done"), None),
        ]
        logger = MockSessionLogger()
        orch = _make_orchestrator(
            SideEffectReactLoop(events), logger, todo_registry=todo_registry
        )

        consumed = _consume(orch, session_id, "搜索")

        todo_events = [
            e for e in consumed
            if e.get("type") in ("todo_init", "todo_update", "todo_complete")
        ]
        self.assertEqual(len(todo_events), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
