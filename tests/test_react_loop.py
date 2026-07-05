"""ReactLoop 单元测试 — 验证 end_turn / tool_use / 异常三分支覆盖。

运行方式:
    python -m unittest tests.test_react_loop -v
    python tests/test_react_loop.py

mock 策略:
- LLM 调用全部用 unittest.mock.MagicMock 替代，无网络请求
- ToolRegistry 用 MagicMock 替代
- ContextManager 不参与（ReactLoop 实际构造函数无此参数）

ReactLoop 实际接口（与骨架假设的差异）:
- 构造函数: ReactLoop(llm_client, tool_registry=None, max_loops=50)
  无 context_manager 参数。
- run() 签名: run(user_input, history=None, system=None, session_id=None,
  tools_override=None) -> Tuple[str, List[Dict], bool]
  （Phase 9 Task 7.4: 第三项 is_complete）
- LLM 调用: self.llm_client.chat_main(messages=, tools=, system=)
- 响应解析: response.content (block 列表) + response.stop_reason
- 工具执行: self.tool_registry.execute_tool(tool_name, tool_input) -> str
- 异常处理: LLM 抛异常时，若已有 last_text 则降级返回，否则向上抛出。
- 最大迭代: for loop_idx in range(max_loops)，耗尽时返回 (last_text, messages, False)。
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


def _make_llm_response(
    text: str = "",
    stop_reason: str = "end_turn",
    tool_use_blocks=None,
) -> MagicMock:
    """构造 mock LLM 响应对象，兼容 ReactLoop 的解析方式。

    ReactLoop 通过 getattr(response, "content", []) 与
    getattr(response, "stop_reason", None) 读取字段。
    content 是 block 列表，每个 block 可以是 dict 或对象
    （_block_to_dict 兼容两者），这里直接用 dict block 简化构造。

    参数:
        text: 文本 block 的内容，为空则不附加 text block。
        stop_reason: "end_turn" / "tool_use" / "max_tokens" 等。
        tool_use_blocks: tool_use block dict 列表，附加到 content 末尾。
    """
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
) -> dict:
    """构造单个 tool_use block dict。"""
    return {
        "type": "tool_use",
        "id": block_id,
        "name": name,
        "input": input_data or {},
    }


class TestReactLoopRun(unittest.IsolatedAsyncioTestCase):
    """验证 run() 方法的三分支逻辑（end_turn / tool_use / 异常）。

    注：``run`` 与 ``chat_main`` 已改为 async（spec Task 4 / Task 3），
    本类用 :class:`unittest.IsolatedAsyncioTestCase` + ``await`` 调用，
    ``chat_main`` mock 用 :class:`AsyncMock` 以支持 ``await``。
    """

    def setUp(self):
        """构造 ReactLoop 实例，注入 mock LLM 与 mock ToolRegistry。"""
        self.mock_llm = MagicMock()
        # chat_main 现为 async def，用 AsyncMock 使 `await mock_llm.chat_main(...)` 可工作
        self.mock_llm.chat_main = AsyncMock()
        self.mock_tool_registry = MagicMock()
        # get_tools_schema 默认返回一个非空 schema 列表
        self.mock_tool_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "search tool", "input_schema": {}},
        ]
        self.loop = ReactLoop(
            llm_client=self.mock_llm,
            tool_registry=self.mock_tool_registry,
            max_loops=5,
        )

    # ------------------------------------------------------------------
    # end_turn 分支
    # ------------------------------------------------------------------

    async def test_run_end_turn_branch(self):
        """LLM 直接返回 end_turn 时，run() 返回最终答案不调用工具。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="Hello!",
            stop_reason="end_turn",
        )

        final_response, messages, _, _ = await self.loop.run("Hi")

        self.assertEqual(final_response, "Hello!")
        self.mock_llm.chat_main.assert_called_once()
        self.mock_tool_registry.execute_tool.assert_not_called()
        # messages 至少包含 user_input 与 assistant 响应
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "Hi")
        self.assertEqual(messages[1]["role"], "assistant")

    async def test_run_end_turn_with_empty_text(self):
        """LLM 返回 end_turn 但无文本 block 时，final_response 为空串。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="",
            stop_reason="end_turn",
        )

        final_response, _, _, _ = await self.loop.run("Hi")

        self.assertEqual(final_response, "")

    # ------------------------------------------------------------------
    # tool_use 分支
    # ------------------------------------------------------------------

    async def test_run_tool_use_branch(self):
        """LLM 请求 tool_use 时，执行工具并继续循环到 end_turn。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="Let me search",
                stop_reason="tool_use",
                tool_use_blocks=[
                    _make_tool_use_block(
                        name="search",
                        input_data={"q": "test"},
                        block_id="tu_1",
                    )
                ],
            ),
            _make_llm_response(
                text="Final answer",
                stop_reason="end_turn",
            ),
        ]
        self.mock_tool_registry.execute_tool.return_value = "search result"

        final_response, messages, _, _ = await self.loop.run("Search for test")

        self.assertEqual(final_response, "Final answer")
        self.assertEqual(self.mock_llm.chat_main.call_count, 2)
        # execute_tool 以位置参数 (tool_name, tool_input) 调用
        self.mock_tool_registry.execute_tool.assert_called_once_with(
            "search", {"q": "test"}
        )
        # messages 顺序: user, assistant(tool_use), user(tool_result), assistant(end_turn)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[2]["role"], "user")
        tool_result = messages[2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "tu_1")
        self.assertEqual(tool_result["content"], "search result")

    async def test_run_multiple_tool_use_in_one_response(self):
        """单次响应包含多个 tool_use block 时，全部执行后继续循环。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="running two tools",
                stop_reason="tool_use",
                tool_use_blocks=[
                    _make_tool_use_block(name="search", input_data={"q": "a"}, block_id="t1"),
                    _make_tool_use_block(name="calc", input_data={"x": 1}, block_id="t2"),
                ],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.side_effect = ["r1", "r2"]

        final_response, messages, _, _ = await self.loop.run("Hi")

        self.assertEqual(final_response, "done")
        self.assertEqual(self.mock_tool_registry.execute_tool.call_count, 2)
        # 验证两个工具都被调用
        calls = self.mock_tool_registry.execute_tool.call_args_list
        self.assertEqual(calls[0].args, ("search", {"q": "a"}))
        self.assertEqual(calls[1].args, ("calc", {"x": 1}))
        # tool_result 消息包含两条结果
        tool_result_msg = messages[2]
        self.assertEqual(len(tool_result_msg["content"]), 2)

    async def test_run_tool_exception_handled(self):
        """工具执行抛异常时，错误结果回传并继续循环到 end_turn。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="let me search",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(name="search")],
            ),
            _make_llm_response(text="after tool error", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.side_effect = RuntimeError("tool broken")

        final_response, messages, _, _ = await self.loop.run("Hi")

        self.assertEqual(final_response, "after tool error")
        self.mock_tool_registry.execute_tool.assert_called_once()
        # 验证 messages 中包含 is_error 的 tool_result
        tool_result_msg = messages[2]
        tool_result = tool_result_msg["content"][0]
        self.assertTrue(tool_result.get("is_error"))
        # Phase A: 异常通过 from_exception 归一化为 InternalError，receipt 格式
        # 为 "[失败] 内部错误\n原因：RuntimeError: tool broken\n建议：..."
        self.assertIn("[失败]", tool_result["content"])
        self.assertIn("RuntimeError", tool_result["content"])

    # ------------------------------------------------------------------
    # 异常分支
    # ------------------------------------------------------------------

    async def test_run_exception_branch_no_text(self):
        """LLM 首次调用即抛异常且无先前文本时，异常向上传播。"""
        self.mock_llm.chat_main.side_effect = RuntimeError("LLM down")

        with self.assertRaises(RuntimeError) as ctx:
            await self.loop.run("Hi")
        self.assertIn("LLM down", str(ctx.exception))
        # 工具未被调用
        self.mock_tool_registry.execute_tool.assert_not_called()

    async def test_run_exception_branch_with_text_degrades(self):
        """LLM 第二次调用抛异常时，已有 last_text 则降级返回。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="partial answer",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block()],
            ),
            RuntimeError("LLM down on second call"),
        ]
        self.mock_tool_registry.execute_tool.return_value = "ok"

        final_response, messages, _, _ = await self.loop.run("Hi")

        # 降级返回先前已得到的文本
        self.assertEqual(final_response, "partial answer")
        self.assertEqual(self.mock_llm.chat_main.call_count, 2)

    # ------------------------------------------------------------------
    # max_iterations 分支
    # ------------------------------------------------------------------

    async def test_run_max_iterations(self):
        """LLM 始终返回 tool_use 时，达到 max_loops 后触发总结调用。

        注意：Phase 9 Task 7.3 引入卡死检测后，需使用不同参数避免触发卡死，
        从而真正测试 max_loops 耗尽后的总结调用路径。
        """
        self.loop.max_loops = 3
        # 使用不同参数避免触发 Phase 9 Task 7.3 卡死检测
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="looping",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(input_data={"q": "0"})],
            ),
            _make_llm_response(
                text="looping",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(input_data={"q": "1"})],
            ),
            _make_llm_response(
                text="looping",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(input_data={"q": "2"})],
            ),
            # 总结调用返回
            _make_llm_response(text="looping", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.return_value = "result"

        final_response, messages, _, _ = await self.loop.run("Hi")

        # 达到 max_loops 后触发 T8 总结调用（3 次循环 + 1 次总结 = 4 次）
        self.assertEqual(self.mock_llm.chat_main.call_count, 4)
        self.assertEqual(final_response, "looping")

    def test_run_max_iterations_default_is_50(self):
        """默认 max_loops 为 50（Phase 9 Task 7.1 调整）。"""
        loop = ReactLoop(llm_client=MagicMock(), tool_registry=None)
        self.assertEqual(loop.max_loops, 50)

    # ------------------------------------------------------------------
    # 纯对话模式（tool_registry=None）
    # ------------------------------------------------------------------

    async def test_run_pure_chat_mode(self):
        """tool_registry=None 时纯对话模式，tools=None 传给 LLM。"""
        loop = ReactLoop(llm_client=self.mock_llm, tool_registry=None)
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="hi",
            stop_reason="end_turn",
        )

        final_response, _, _, _ = await loop.run("Hi")

        self.assertEqual(final_response, "hi")
        _, kwargs = self.mock_llm.chat_main.call_args
        self.assertIsNone(kwargs["tools"])

    async def test_run_tool_use_without_registry(self):
        """tool_registry=None 时 LLM 返回 tool_use，直接返回当前文本不执行工具。"""
        loop = ReactLoop(llm_client=self.mock_llm, tool_registry=None)
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="need tool but no registry",
            stop_reason="tool_use",
            tool_use_blocks=[_make_tool_use_block()],
        )

        final_response, _, _, _ = await loop.run("Hi")

        self.assertEqual(final_response, "need tool but no registry")
        self.mock_llm.chat_main.assert_called_once()

    # ------------------------------------------------------------------
    # history / system 透传
    # ------------------------------------------------------------------

    async def test_run_history_passed_through(self):
        """history 被正确拼接到 messages 前部（浅拷贝，不影响原 list）。"""
        history = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )

        final_response, messages, _, _ = await self.loop.run("new question", history=history)

        self.assertEqual(final_response, "ok")
        # messages[0:2] 是 history 浅拷贝
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "previous question")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["content"], "previous answer")
        # messages[2] 是新的 user_input
        self.assertEqual(messages[2]["role"], "user")
        self.assertEqual(messages[2]["content"], "new question")
        # 原 history 不被修改
        self.assertEqual(len(history), 2)

    async def test_run_system_passed_through(self):
        """system 提示词被透传给 chat_main。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )

        await self.loop.run("Hi", system="You are helpful")

        _, kwargs = self.mock_llm.chat_main.call_args
        self.assertEqual(kwargs["system"], "You are helpful")

    async def test_run_system_none_by_default(self):
        """未传 system 时，system 参数为 None。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )

        await self.loop.run("Hi")

        _, kwargs = self.mock_llm.chat_main.call_args
        self.assertIsNone(kwargs["system"])

    # ------------------------------------------------------------------
    # get_tools_schema 异常降级
    # ------------------------------------------------------------------

    async def test_get_tools_schema_failure_degrades(self):
        """get_tools_schema 抛异常时降级为纯对话模式（tools=None）。"""
        self.mock_tool_registry.get_tools_schema.side_effect = RuntimeError(
            "schema error"
        )
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )

        final_response, _, _, _ = await self.loop.run("Hi")

        self.assertEqual(final_response, "ok")
        _, kwargs = self.mock_llm.chat_main.call_args
        self.assertIsNone(kwargs["tools"])

    async def test_get_tools_schema_called_each_run(self):
        """每次 run() 都调用 get_tools_schema 获取最新工具列表。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )

        await self.loop.run("Hi")
        await self.loop.run("Hi again")

        self.assertEqual(self.mock_tool_registry.get_tools_schema.call_count, 2)

    # ------------------------------------------------------------------
    # messages 完整性
    # ------------------------------------------------------------------

    async def test_run_returns_messages_with_history_and_new(self):
        """返回的 messages_used 包含 history 与循环中新增的所有消息。"""
        history = [{"role": "user", "content": "old"}]
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="searching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(block_id="tu_x")],
            ),
            _make_llm_response(text="final", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.return_value = "r"

        _, messages, _, _ = await self.loop.run("new", history=history)

        # 期望: history(1) + user_input(1) + assistant(1) + tool_result(1) + assistant(1) = 5
        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0]["content"], "old")
        self.assertEqual(messages[1]["content"], "new")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[3]["role"], "user")
        self.assertEqual(messages[4]["role"], "assistant")


class TestReactLoopInfoCounter(unittest.IsolatedAsyncioTestCase):
    """验证信息计数器（_info_count）的累加与重置逻辑。

    注：``run`` / ``chat_main`` 已 async，本类用 IsolatedAsyncioTestCase + await。
    """

    def setUp(self):
        self.mock_llm = MagicMock()
        self.mock_llm.chat_main = AsyncMock()
        self.mock_tool_registry = MagicMock()
        self.mock_tool_registry.get_tools_schema.return_value = []
        self.loop = ReactLoop(
            llm_client=self.mock_llm,
            tool_registry=self.mock_tool_registry,
            max_loops=5,
        )

    def test_info_counter_starts_zero(self):
        """初始计数器为 0。"""
        self.assertEqual(self.loop.get_info_count(), 0)

    async def test_info_counter_increments_on_end_turn(self):
        """end_turn 分支: user + assistant = 2 次计数。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )

        await self.loop.run("Hi")

        # 1 (user) + 1 (assistant) = 2
        self.assertEqual(self.loop.get_info_count(), 2)

    async def test_info_counter_increments_on_tool_use(self):
        """tool_use 分支: user + assistant + tool_result + assistant = 4 次计数。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="searching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block()],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.return_value = "result"

        await self.loop.run("Hi")

        # 1 (user) + 1 (assistant) + 1 (tool_result) + 1 (assistant) = 4
        self.assertEqual(self.loop.get_info_count(), 4)

    async def test_info_counter_multiple_tools(self):
        """单次响应多个 tool_use: 每条 tool_result 各计 1 次。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="searching",
                stop_reason="tool_use",
                tool_use_blocks=[
                    _make_tool_use_block(block_id="t1"),
                    _make_tool_use_block(block_id="t2"),
                ],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.side_effect = ["r1", "r2"]

        await self.loop.run("Hi")

        # 1 (user) + 1 (assistant) + 2 (tool_results) + 1 (assistant) = 5
        self.assertEqual(self.loop.get_info_count(), 5)

    async def test_history_not_counted_in_info_counter(self):
        """history 中的消息不计入信息计数器。"""
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )

        await self.loop.run("new", history=history)

        # 1 (new user) + 1 (assistant) = 2，history 2 条不计入
        self.assertEqual(self.loop.get_info_count(), 2)

    async def test_reset_info_count(self):
        """reset_info_count 将计数器归零。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )
        await self.loop.run("Hi")
        self.assertEqual(self.loop.get_info_count(), 2)

        self.loop.reset_info_count()
        self.assertEqual(self.loop.get_info_count(), 0)

    async def test_info_counter_accumulates_across_runs(self):
        """多次 run() 调用之间计数器累加（需手动 reset）。"""
        self.mock_llm.chat_main.return_value = _make_llm_response(
            text="ok",
            stop_reason="end_turn",
        )
        await self.loop.run("Hi")
        await self.loop.run("Hi again")

        # (1+1) + (1+1) = 4
        self.assertEqual(self.loop.get_info_count(), 4)


class TestBlockToDict(unittest.TestCase):
    """验证 _block_to_dict 静态方法的 block 转换逻辑。"""

    def test_dict_passthrough(self):
        """dict 类型 block 直接返回（不复制）。"""
        block = {"type": "text", "text": "hello"}
        result = ReactLoop._block_to_dict(block)
        self.assertIs(result, block)

    def test_text_block_object(self):
        """对象类型的 text block 正确转换为 dict。"""
        block = MagicMock()
        block.type = "text"
        block.text = "hello world"

        result = ReactLoop._block_to_dict(block)

        self.assertEqual(result, {"type": "text", "text": "hello world"})

    def test_tool_use_block_object(self):
        """对象类型的 tool_use block 正确转换为 dict。"""
        block = MagicMock()
        block.type = "tool_use"
        block.id = "tool_123"
        block.name = "search"
        block.input = {"q": "test"}

        result = ReactLoop._block_to_dict(block)

        self.assertEqual(
            result,
            {
                "type": "tool_use",
                "id": "tool_123",
                "name": "search",
                "input": {"q": "test"},
            },
        )

    def test_tool_use_block_with_none_input(self):
        """tool_use block 的 input 为 None 时转为空 dict。"""
        block = MagicMock()
        block.type = "tool_use"
        block.id = "tool_1"
        block.name = "search"
        block.input = None

        result = ReactLoop._block_to_dict(block)

        self.assertEqual(result["input"], {})

    def test_unknown_block_type(self):
        """未知 block 类型返回 {type: <type>}。"""
        block = MagicMock()
        block.type = "unsupported"

        result = ReactLoop._block_to_dict(block)

        self.assertEqual(result, {"type": "unsupported"})

    def test_block_with_no_type_attribute(self):
        """无 type 字段的 block 返回 {type: "unknown"}。"""

        class NoTypeBlock:
            pass

        result = ReactLoop._block_to_dict(NoTypeBlock())

        self.assertEqual(result, {"type": "unknown"})


# ---------------------------------------------------------------------------
# Phase 9+ 错误分类增强：_detect_tool_stuck 含 error_class
# ---------------------------------------------------------------------------

class TestStuckDetectionWithErrorClass(unittest.TestCase):
    """验证 _detect_tool_stuck 对 error_class 的语义感知。"""

    def test_anti_crawler_immediate_stuck(self):
        """recent_calls 含 anti_crawler → 立即卡死，不等阈值。"""
        recent: list = [
            ("web_fetch", "hash_a", None),
            ("web_fetch", "hash_b", None),
            ("web_fetch", "hash_a", "anti_crawler"),
        ]
        is_stuck, reason = ReactLoop._detect_tool_stuck(
            "web_fetch", "hash_a", recent, window_size=5, threshold=3,
        )
        self.assertTrue(is_stuck)
        self.assertEqual(reason, "anti_crawler")

    def test_permanent_immediate_stuck(self):
        """recent_calls 含 permanent → 立即卡死。"""
        recent: list = [
            ("search", "params_1", None),
            ("search", "params_1", "permanent"),
        ]
        is_stuck, reason = ReactLoop._detect_tool_stuck(
            "search", "params_1", recent, window_size=5, threshold=3,
        )
        self.assertTrue(is_stuck)
        self.assertEqual(reason, "permanent")

    def test_transient_not_immediate_stuck(self):
        """transient 不触发立即卡死，且单条匹配达不到阈值时不卡死。"""
        # 只有 1 条同参数记录（transient），threshold=3 → 达不到 2 条匹配
        recent: list = [
            ("search", "hash_a", "transient"),
        ]
        is_stuck, _ = ReactLoop._detect_tool_stuck(
            "search", "hash_a", recent, window_size=5, threshold=3,
        )
        self.assertFalse(is_stuck)

    def test_anti_crawler_other_param_ok(self):
        """ANTI_CRAWLER 记录但不同 params_hash → 不卡死。"""
        recent: list = [
            ("web_fetch", "hash_a", "ANTI_CRAWLER"),
        ]
        is_stuck, _ = ReactLoop._detect_tool_stuck(
            "web_fetch", "hash_b", recent, window_size=5, threshold=3,
        )
        self.assertFalse(is_stuck)

    def test_no_error_class_backward(self):
        """error_class 为 None（老记录）走原有阈值逻辑。"""
        recent: list = [
            ("search", "hash_a", None),
            ("search", "hash_a", None),
        ]
        is_stuck, _ = ReactLoop._detect_tool_stuck(
            "search", "hash_a", recent, window_size=5, threshold=3,
        )
        # 2 条匹配，threshold=3 → matches(2) >= 2 → 卡死
        self.assertTrue(is_stuck)

    def test_mixed_error_classes(self):
        """多条混杂记录，正确识别同参数 anti_crawler。"""
        recent: list = [
            ("web_fetch", "hash_x", None),
            ("web_fetch", "hash_a", "anti_crawler"),
            ("bash_exec", "hash_y", "transient"),
            ("web_fetch", "hash_a", "anti_crawler"),
        ]
        is_stuck, reason = ReactLoop._detect_tool_stuck(
            "web_fetch", "hash_a", recent, window_size=5, threshold=3,
        )
        self.assertTrue(is_stuck)
        self.assertEqual(reason, "anti_crawler")

    def test_threshold_still_works(self):
        """无 ANTI_CRAWLER/PERMANENT 时，原有阈值逻辑正常。"""
        recent: list = [
            ("search", "hash_a", None),
            ("search", "hash_a", None),
            ("search", "hash_a", None),
        ]
        is_stuck, _ = ReactLoop._detect_tool_stuck(
            "search", "hash_a", recent, window_size=5, threshold=3,
        )
        self.assertTrue(is_stuck)


# ---------------------------------------------------------------------------
# _build_stuck_message 原因区分
# ---------------------------------------------------------------------------

class TestStuckMessageBuiltin(unittest.TestCase):
    """验证 _build_stuck_message 区分不同终止原因。"""

    def test_anti_crawler_message(self):
        msg = ReactLoop._build_stuck_message("web_fetch", "anti_crawler")
        self.assertIn("web_fetch", msg)
        self.assertIn("反爬虫", msg)

    def test_permanent_message(self):
        msg = ReactLoop._build_stuck_message("search", "permanent")
        self.assertIn("search", msg)
        self.assertIn("永久", msg)

    def test_default_message(self):
        msg = ReactLoop._build_stuck_message("bash_exec", "")
        self.assertIn("bash_exec", msg)
        self.assertIn("卡死", msg)


# ---------------------------------------------------------------------------
# run() 含 anti_crawler 提示注入（集成测试）
# ---------------------------------------------------------------------------

class TestAntiCrawlerHintInjection(unittest.IsolatedAsyncioTestCase):
    """验证 run() 中工具返回 ANTI_CRAWLER 后的策略提示注入。

    注：``run`` / ``chat_main`` 已 async，本类用 IsolatedAsyncioTestCase + await。
    """

    def setUp(self):
        self.mock_llm = MagicMock()
        self.mock_llm.chat_main = AsyncMock()
        self.mock_tool_registry = MagicMock()
        self.mock_tool_registry.get_tools_schema.return_value = [
            {"name": "web_fetch", "description": "fetch", "input_schema": {}},
        ]
        self.loop = ReactLoop(
            llm_client=self.mock_llm,
            tool_registry=self.mock_tool_registry,
            max_loops=5,
        )

    async def test_anti_crawler_hint_injected(self):
        """web_fetch 返回 [HTTP 403] 时结果尾部应含 [系统提示]。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[
                    _make_tool_use_block(
                        name="web_fetch",
                        input_data={"url": "https://bilibili.com/up"},
                        block_id="t1",
                    )
                ],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.return_value = (
            "[HTTP 403]\n\nAccess Denied by WAF"
        )

        _, messages, _, _ = await self.loop.run("fetch bilibili")

        # 检查 tool_result 内容是否含 [系统提示]
        tool_result_msg = messages[2]
        tool_result = tool_result_msg["content"][0]
        self.assertIn("[系统提示", tool_result["content"])
        self.assertIn("反爬虫", tool_result["content"])

    async def test_permanent_hint_injected(self):
        """web_fetch 返回 [HTTP 404] 时结果尾部应含永久错误提示。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[
                    _make_tool_use_block(
                        name="web_fetch",
                        input_data={"url": "https://example.com/missing"},
                        block_id="t1",
                    )
                ],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.return_value = (
            "[HTTP 404]\n\nNot Found"
        )

        _, messages, _, _ = await self.loop.run("fetch")

        tool_result = messages[2]["content"][0]
        self.assertIn("[系统提示", tool_result["content"])
        self.assertIn("永久", tool_result["content"])

    async def test_success_no_hint(self):
        """正常返回不含 [系统提示]。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[
                    _make_tool_use_block(
                        name="web_fetch",
                        input_data={"url": "https://example.com"},
                        block_id="t1",
                    )
                ],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.return_value = (
            "[HTTP 200]\n\n<html>...</html>"
        )

        _, messages, _, _ = await self.loop.run("fetch")

        tool_result = messages[2]["content"][0]
        self.assertNotIn("[系统提示", tool_result["content"])

    async def test_transient_hint_injected(self):
        """临时错误返回含提示。"""
        self.mock_llm.chat_main.side_effect = [
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[
                    _make_tool_use_block(
                        name="web_fetch",
                        input_data={"url": "https://example.com"},
                        block_id="t1",
                    )
                ],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ]
        self.mock_tool_registry.execute_tool.return_value = (
            "[HTTP 503]\n\nService Unavailable"
        )

        _, messages, _, _ = await self.loop.run("fetch")

        tool_result = messages[2]["content"][0]
        self.assertIn("[系统提示", tool_result["content"])


class TestErrorClassifierMetricsReporting(unittest.IsolatedAsyncioTestCase):
    """Phase 1 反馈监控：验证 ErrorClassifier 错误识别会同步 is_error 并上报 error_class。

    覆盖 react_loop.run() :810-813 和 run_stream() :1366-1369 两处盲区修复。
    """

    async def test_permanent_error_sets_is_error_and_reports_class(self):
        """web_fetch 返回 404 时 metrics.observe_tool_error_class 被调用且 is_error=True。"""
        from src.monitoring.metrics import MetricsCollector

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=[
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(
                    name="web_fetch",
                    input_data={"url": "https://example.com/missing"},
                    block_id="t1",
                )],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "web_fetch", "description": "fetch", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "[HTTP 404]\n\nNot Found"

        metrics = MetricsCollector()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=5,
            metrics=metrics,
        )
        _, messages, _, _ = await loop.run("fetch")

        # 验证 error_class 已上报为 permanent
        snap = metrics.snapshot()
        self.assertIn("web_fetch", snap["tool_error_classes_total"])
        self.assertEqual(
            snap["tool_error_classes_total"]["web_fetch"].get("permanent", 0), 1
        )
        # 验证 tool_calls_errors_total 也累加（is_error=True 透传到 observe_tool_call）
        self.assertEqual(snap["tool_calls_errors_total"].get("web_fetch", 0), 1)
        # 验证 tool_calls_total 累加
        self.assertEqual(snap["tool_calls_total"].get("web_fetch", 0), 1)

    async def test_anti_crawler_error_reports_anti_crawler_class(self):
        """web_fetch 返回 403 时 metrics 上报 anti_crawler 分类。"""
        from src.monitoring.metrics import MetricsCollector

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=[
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(
                    name="web_fetch",
                    input_data={"url": "https://bilibili.com/up"},
                    block_id="t1",
                )],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "web_fetch", "description": "fetch", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = (
            "[HTTP 403]\n\nAccess Denied by WAF"
        )

        metrics = MetricsCollector()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=5,
            metrics=metrics,
        )
        await loop.run("fetch bilibili")

        snap = metrics.snapshot()
        self.assertEqual(
            snap["tool_error_classes_total"]["web_fetch"].get("anti_crawler", 0), 1
        )
        self.assertEqual(snap["tool_calls_errors_total"].get("web_fetch", 0), 1)

    async def test_success_does_not_report_error_class(self):
        """正常返回不触发 error_class 上报，也不累加 errors。"""
        from src.monitoring.metrics import MetricsCollector

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=[
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(
                    name="web_fetch",
                    input_data={"url": "https://example.com"},
                    block_id="t1",
                )],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "web_fetch", "description": "fetch", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "[HTTP 200]\n\n<html>...</html>"

        metrics = MetricsCollector()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=5,
            metrics=metrics,
        )
        await loop.run("fetch")

        snap = metrics.snapshot()
        # 成功路径不应有任何 error_class 上报
        self.assertEqual(snap["tool_error_classes_total"], {})
        self.assertEqual(snap["tool_calls_errors_total"], {})
        # 但 tool_calls_total 仍累加
        self.assertEqual(snap["tool_calls_total"].get("web_fetch", 0), 1)

    async def test_metrics_none_does_not_crash_on_error(self):
        """metrics=None 时 ErrorClassifier 错误路径不崩溃（向后兼容）。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=[
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(
                    name="web_fetch",
                    input_data={"url": "https://example.com/missing"},
                    block_id="t1",
                )],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "web_fetch", "description": "fetch", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "[HTTP 404]\n\nNot Found"

        # 不传 metrics，应为 None
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=5,
        )
        # 不应抛异常
        response, _, _, _ = await loop.run("fetch")
        self.assertEqual(response, "done")

    async def test_error_class_also_reports_retry(self):
        """Phase 2 反馈监控：error_class 识别时同步上报 observe_tool_retry。"""
        from src.monitoring.metrics import MetricsCollector

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=[
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(
                    name="web_fetch",
                    input_data={"url": "https://example.com/missing"},
                    block_id="t1",
                )],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "web_fetch", "description": "fetch", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "[HTTP 404]\n\nNot Found"

        metrics = MetricsCollector()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=5,
            metrics=metrics,
        )
        await loop.run("fetch")

        snap = metrics.snapshot()
        # error_class 识别一次 → retry 计数也累加一次
        self.assertEqual(snap["tool_retries_total"].get("web_fetch", 0), 1)
        # 同时 error_class 也累加
        self.assertEqual(
            snap["tool_error_classes_total"]["web_fetch"].get("permanent", 0), 1
        )

    async def test_success_does_not_report_retry(self):
        """Phase 2 反馈监控：成功路径不触发 retry 计数。"""
        from src.monitoring.metrics import MetricsCollector

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock(side_effect=[
            _make_llm_response(
                text="fetching",
                stop_reason="tool_use",
                tool_use_blocks=[_make_tool_use_block(
                    name="web_fetch",
                    input_data={"url": "https://example.com"},
                    block_id="t1",
                )],
            ),
            _make_llm_response(text="done", stop_reason="end_turn"),
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "web_fetch", "description": "fetch", "input_schema": {}},
        ]
        mock_registry.execute_tool.return_value = "[HTTP 200]\n\n<html>...</html>"

        metrics = MetricsCollector()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=5,
            metrics=metrics,
        )
        await loop.run("fetch")

        snap = metrics.snapshot()
        # 成功路径不应触发 retry
        self.assertEqual(snap["tool_retries_total"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
