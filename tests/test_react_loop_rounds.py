"""ReactLoop 多轮独立渲染与 todo 事件透传测试（T5）。

验证 run_stream 在多轮循环中：
1. 每轮循环开始时 yield round_start 事件，loop_idx 从 0 递增；
2. round_start 事件在 text 事件之前；
3. tool 事件附带 session_id 字段（供前端关联 todo 卡片）；
4. done 事件 response 字段为最后一轮文本；
5. 多轮文本通过 round_start 隔离到独立 streamMsg；
6. 单轮（无工具调用）也正常 yield round_start 后接 done；
7. run 方法接受可选 session_id 参数（向后兼容）。

运行方式:
    python -m unittest tests.test_react_loop_rounds -v
    python tests/test_react_loop_rounds.py

mock 策略:
- LLM 调用全部用 MockStreamLLMClient 替代，chat_main_stream 为同步生成器
  （与 ReactLoop.run_stream 中的 `for event in self.llm_client.chat_main_stream(...)`
  调用方式一致）。
- ToolRegistry 用 unittest.mock.MagicMock 替代。
- 不依赖网络 / 模型权重。
"""

from __future__ import annotations

import asyncio
import inspect
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
from src.agent.policy import Decision  # noqa: E402


# ---------------------------------------------------------------------------
# mock 构造工具
# ---------------------------------------------------------------------------

def _text_event(text: str) -> dict:
    """构造文本增量事件。"""
    return {"type": "text", "text": text}


def _done_event(
    text: str = "",
    stop_reason: str = "end_turn",
    tool_use_blocks=None,
) -> dict:
    """构造 done 事件，content_blocks 中包含 text 块（可选 tool_use 块）。

    ReactLoop.run_stream 通过 done 事件中的 content_blocks 解析出
    text_parts 与 tool_use_blocks；text_parts 用于更新 last_text，
    tool_use_blocks 用于触发工具执行。
    """
    content_blocks = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    if tool_use_blocks:
        content_blocks.extend(tool_use_blocks)
    return {
        "type": "done",
        "stop_reason": stop_reason,
        "content_blocks": content_blocks,
    }


def _tool_use_block(
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


class MockStreamLLMClient:
    """Mock LLM 客户端，按预设的多轮响应序列返回 chat_main_stream 异步生成器。

    每次 chat_main_stream 调用弹出 responses 队列首部的一项，该项是
    一个事件列表 [{type: "text", ...}, {type: "done", ...}]，按顺序 yield。

    chat_main_stream 在 ReactLoop.run_stream 中被作为异步生成器使用
    （``async for event in self.llm_client.chat_main_stream(...)``，spec Task 4），
    因此本 mock 实现为 async generator（``async def`` + ``yield``）。
    """

    def __init__(self, responses):
        # responses: List[List[dict]]，每个内层 list 是一次调用的完整事件序列
        self._responses = list(responses)
        self._call_count = 0

    async def chat_main_stream(self, messages=None, tools=None, system=None, max_tokens=None, cancel_event=None, stream_manager=None, session_id=None, activity_timeout=None):
        if self._call_count >= len(self._responses):
            raise RuntimeError(
                f"MockStreamLLMClient: responses exhausted at call "
                f"#{self._call_count + 1}"
            )
        events = self._responses[self._call_count]
        self._call_count += 1
        for evt in events:
            yield evt

    @property
    def call_count(self) -> int:
        return self._call_count


def _make_three_round_responses():
    """构造 3 轮响应序列：2 轮 tool_use + 1 轮 end_turn。

    每轮都先发一个 text 增量事件，再发 done 事件；done 事件的
    content_blocks 中包含与 text 一致的文本块（保证 last_text 正确更新）。

    返回:
        List[List[dict]]，可传给 MockStreamLLMClient。
    """
    return [
        # 第 1 轮：text + done(tool_use)
        [
            _text_event("Round 1 text"),
            _done_event(
                text="Round 1 text",
                stop_reason="tool_use",
                tool_use_blocks=[_tool_use_block(
                    name="search",
                    input_data={"q": "first"},
                    block_id="tu_1",
                )],
            ),
        ],
        # 第 2 轮：text + done(tool_use)
        [
            _text_event("Round 2 text"),
            _done_event(
                text="Round 2 text",
                stop_reason="tool_use",
                tool_use_blocks=[_tool_use_block(
                    name="search",
                    input_data={"q": "second"},
                    block_id="tu_2",
                )],
            ),
        ],
        # 第 3 轮：text + done(end_turn)
        [
            _text_event("Round 3 final"),
            _done_event(
                text="Round 3 final",
                stop_reason="end_turn",
            ),
        ],
    ]


def _make_tool_registry_mock() -> MagicMock:
    """构造一个返回非空 schema 列表、execute_tool 返回 "result" 的 mock。"""
    reg = MagicMock()
    reg.get_tools_schema.return_value = [
        {"name": "search", "description": "search tool", "input_schema": {}},
    ]
    reg.execute_tool.return_value = "result"
    return reg


def _make_tool_use_round_events(text: str = "looping"):
    """构造一轮始终返回 tool_use 的流式事件序列（用于 max_loops 测试）。

    每次调用返回全新的事件列表，避免跨轮共享可变对象。
    """
    return [
        _text_event(text),
        _done_event(
            text=text,
            stop_reason="tool_use",
            tool_use_blocks=[_tool_use_block(
                name="search",
                input_data={},
                block_id="tu_1",
            )],
        ),
    ]


def _collect_events(loop: ReactLoop, user_input: str, session_id=None):
    """驱动 run_stream async generator 收集所有 yield 的事件。"""
    events = []

    async def _run():
        async for evt in loop.run_stream(
            user_input=user_input,
            history=None,
            system=None,
            session_id=session_id,
        ):
            events.append(evt)

    asyncio.run(_run())
    return events


def _async_gen(events):
    """将事件列表包装为 async generator（供 chat_main_stream mock）。

    ``chat_main_stream`` 已改为 async generator（spec Task 4），mock 需返回
    async iterable；用本函数把事件列表包装为 ``async for`` 可消费的对象。
    """
    async def _gen():
        for evt in events:
            yield evt
    return _gen()


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

class TestRunStreamRoundStart(unittest.TestCase):
    """验证 run_stream 的 round_start 事件。"""

    def setUp(self):
        self.mock_llm = MockStreamLLMClient(_make_three_round_responses())
        self.mock_tool_registry = _make_tool_registry_mock()
        self.loop = ReactLoop(
            llm_client=self.mock_llm,
            tool_registry=self.mock_tool_registry,
            max_loops=5,
        )

    def test_run_stream_yields_round_start_per_loop(self):
        """每轮循环都 yield round_start 事件，loop_idx 从 0 递增。"""
        events = _collect_events(self.loop, "Hi")

        round_starts = [e for e in events if e.get("type") == "round_start"]
        # 3 轮循环对应 3 个 round_start
        self.assertEqual(len(round_starts), 3)
        # loop_idx 递增 0, 1, 2
        self.assertEqual(
            [e["loop_idx"] for e in round_starts], [0, 1, 2]
        )

    def test_run_stream_round_start_before_text(self):
        """round_start 事件出现在同轮的 text 事件之前。"""
        events = _collect_events(self.loop, "Hi")

        # 找到第一个 round_start 与第一个 text 的索引
        idx_round = next(
            i for i, e in enumerate(events) if e.get("type") == "round_start"
        )
        idx_text = next(
            i for i, e in enumerate(events) if e.get("type") == "text"
        )
        self.assertLess(idx_round, idx_text)

    def test_run_stream_first_round_loop_idx_zero(self):
        """第 1 轮 round_start 的 loop_idx=0，且为事件流第一个事件。"""
        events = _collect_events(self.loop, "Hi")

        # 第一个事件应为 round_start 且 loop_idx=0
        self.assertEqual(events[0].get("type"), "round_start")
        self.assertEqual(events[0].get("loop_idx"), 0)


class TestRunStreamSessionIdInToolEvents(unittest.TestCase):
    """验证 tool 事件附带 session_id 字段。"""

    def test_run_stream_session_id_in_tool_events(self):
        """tool 事件包含 session_id 字段，值等于传入的 session_id。"""
        mock_llm = MockStreamLLMClient(_make_three_round_responses())
        mock_tool_registry = _make_tool_registry_mock()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_tool_registry,
            max_loops=5,
        )

        events = _collect_events(loop, "Hi", session_id="sess-abc-123")

        tool_events = [e for e in events if e.get("type") == "tool"]
        # 3 轮中前 2 轮各 1 个 tool_use，共 2 个 tool 事件
        self.assertEqual(len(tool_events), 2)
        for te in tool_events:
            self.assertIn("session_id", te)
            self.assertEqual(te["session_id"], "sess-abc-123")

    def test_run_stream_session_id_none_when_not_passed(self):
        """未传 session_id 时，tool 事件中 session_id 字段为 None（向后兼容）。"""
        mock_llm = MockStreamLLMClient(_make_three_round_responses())
        mock_tool_registry = _make_tool_registry_mock()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_tool_registry,
            max_loops=5,
        )

        events = _collect_events(loop, "Hi")

        tool_events = [e for e in events if e.get("type") == "tool"]
        self.assertEqual(len(tool_events), 2)
        for te in tool_events:
            # session_id 字段存在但为 None
            self.assertIn("session_id", te)
            self.assertIsNone(te["session_id"])


class TestRunStreamDoneResponse(unittest.TestCase):
    """验证 done 事件 response 字段。"""

    def test_run_stream_done_has_response(self):
        """done 事件 response 字段为最后一轮文本。"""
        mock_llm = MockStreamLLMClient(_make_three_round_responses())
        mock_tool_registry = _make_tool_registry_mock()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_tool_registry,
            max_loops=5,
        )

        events = _collect_events(loop, "Hi")

        done_events = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(done_events), 1)
        # 最后一轮文本为 "Round 3 final"
        self.assertEqual(done_events[0]["response"], "Round 3 final")


class TestRunStreamMultipleRoundsTextSeparated(unittest.TestCase):
    """验证多轮循环中文本通过 round_start 隔离。"""

    def test_run_stream_multiple_rounds_text_separated(self):
        """多轮循环中每轮文本通过 round_start 隔离到独立 streamMsg。

        验证事件流中每个 round_start 后都跟随该轮独立的 text 事件，
        前端可据此创建独立 streamMsg，避免跨轮累加与 done 覆盖丢失。
        """
        mock_llm = MockStreamLLMClient(_make_three_round_responses())
        mock_tool_registry = _make_tool_registry_mock()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_tool_registry,
            max_loops=5,
        )

        events = _collect_events(loop, "Hi")

        # 按轮次切分事件流：每个 round_start 开启新一轮
        rounds = []
        current = None
        for e in events:
            if e.get("type") == "round_start":
                if current is not None:
                    rounds.append(current)
                current = {"loop_idx": e["loop_idx"], "texts": [], "tools": []}
            elif current is None:
                # round_start 之前不应有 text/tool/done 事件
                self.fail(f"事件在 round_start 之前: {e}")
            elif e.get("type") == "text":
                current["texts"].append(e["text"])
            elif e.get("type") == "tool":
                current["tools"].append(e)
            # done / approval_* 等事件归属当前轮，无需特别收集

        if current is not None:
            rounds.append(current)

        # 应有 3 轮
        self.assertEqual(len(rounds), 3)
        # 每轮 loop_idx 递增
        self.assertEqual([r["loop_idx"] for r in rounds], [0, 1, 2])
        # 每轮文本独立：Round 1 / Round 2 / Round 3 final
        self.assertEqual("".join(rounds[0]["texts"]), "Round 1 text")
        self.assertEqual("".join(rounds[1]["texts"]), "Round 2 text")
        self.assertEqual("".join(rounds[2]["texts"]), "Round 3 final")
        # 前 2 轮各有 1 个 tool 事件，最后 1 轮无 tool
        self.assertEqual(len(rounds[0]["tools"]), 1)
        self.assertEqual(len(rounds[1]["tools"]), 1)
        self.assertEqual(len(rounds[2]["tools"]), 0)


class TestRunMethodAcceptsSessionId(unittest.IsolatedAsyncioTestCase):
    """验证 run 方法接受可选 session_id 参数（向后兼容）。

    注：``run`` / ``chat_main`` 已 async，本类用 IsolatedAsyncioTestCase + await。
    """

    async def test_run_method_accepts_session_id(self):
        """run 方法签名包含 session_id 参数，默认 None，可正常调用。"""
        sig = inspect.signature(ReactLoop.run)
        self.assertIn("session_id", sig.parameters)
        param = sig.parameters["session_id"]
        self.assertIsNone(param.default)

        # 构造一个返回 end_turn 的 mock 响应
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.content = [{"type": "text", "text": "ok"}]
        mock_resp.stop_reason = "end_turn"
        mock_llm.chat_main.return_value = mock_resp

        loop = ReactLoop(llm_client=mock_llm, tool_registry=None)

        # 传 session_id 不应报错
        final, _, _ = await loop.run("Hi", session_id="sess-xyz")
        self.assertEqual(final, "ok")

        # 不传 session_id 也应正常工作（向后兼容）
        mock_llm.chat_main.return_value = mock_resp
        final2, _, _ = await loop.run("Hi")
        self.assertEqual(final2, "ok")


class TestRunStreamSingleRound(unittest.TestCase):
    """验证单轮（无工具调用）也正常 yield round_start 后接 done。"""

    def test_run_stream_no_tool_use_single_round(self):
        """单轮 end_turn 时事件序列为 round_start -> text -> done。"""
        mock_llm = MockStreamLLMClient([
            [
                _text_event("single round text"),
                _done_event(text="single round text", stop_reason="end_turn"),
            ],
        ])
        mock_tool_registry = _make_tool_registry_mock()
        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_tool_registry,
            max_loops=5,
        )

        events = _collect_events(loop, "Hi")

        # 应有 1 个 round_start
        round_starts = [e for e in events if e.get("type") == "round_start"]
        self.assertEqual(len(round_starts), 1)
        self.assertEqual(round_starts[0]["loop_idx"], 0)

        # 应有 1 个 text
        texts = [e for e in events if e.get("type") == "text"]
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0]["text"], "single round text")

        # 应有 1 个 done
        dones = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0]["response"], "single round text")

        # 顺序：round_start -> status(thinking) -> text -> done
        types = [e["type"] for e in events]
        self.assertEqual(types, ["round_start", "status", "text", "done"])


# ---------------------------------------------------------------------------
# T6: deny 后 tool_result 内容明确性测试
# ---------------------------------------------------------------------------


class TestDenyResultContentRun(unittest.IsolatedAsyncioTestCase):
    """验证非流式 run() deny 后 tool_result 包含明确拒绝提示。

    注：``run`` / ``chat_main`` 已 async，本类用 IsolatedAsyncioTestCase + await。
    """

    async def test_run_deny_result_contains_explicit_message(self):
        """run() deny 后 tool_result 包含 [用户已拒绝] 与 请停止重试。"""
        mock_policy = MagicMock()
        mock_policy.check.return_value = Decision(
            action="deny", reason="危险操作", risk_level="high"
        )

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()
        # 第 1 轮：tool_use（触发 deny）
        resp_tool = MagicMock()
        resp_tool.content = [{
            "type": "tool_use",
            "id": "tu_1",
            "name": "file_write",
            "input": {"path": "/etc/passwd"},
        }]
        resp_tool.stop_reason = "tool_use"
        # 第 2 轮：end_turn（结束）
        resp_end = MagicMock()
        resp_end.content = [{"type": "text", "text": "好的，我不再尝试。"}]
        resp_end.stop_reason = "end_turn"
        mock_llm.chat_main.side_effect = [resp_tool, resp_end]

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "file_write", "description": "write", "input_schema": {}}
        ]

        loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=mock_registry,
            max_loops=5,
            policy_engine=mock_policy,
        )

        final, messages, _ = await loop.run("delete everything")

        # execute_tool 不应被调用（被 deny）
        mock_registry.execute_tool.assert_not_called()

        # 检查 messages 中的 tool_result 的 content
        tool_result_content = ""
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_result_content = block.get("content", "")

        self.assertIn("[用户已拒绝]", tool_result_content)
        self.assertIn("请停止重试", tool_result_content)
        self.assertIn("file_write", tool_result_content)
        self.assertIn("危险操作", tool_result_content)
        # 最终回复为第 2 轮文本
        self.assertEqual(final, "好的，我不再尝试。")

    async def test_run_deny_empty_reason_uses_default(self):
        """reason 为空时使用 '用户未提供原因'。"""
        mock_policy = MagicMock()
        mock_policy.check.return_value = Decision(
            action="deny", reason="", risk_level="high"
        )

        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()
        resp_tool = MagicMock()
        resp_tool.content = [{
            "type": "tool_use", "id": "tu_1",
            "name": "search", "input": {},
        }]
        resp_tool.stop_reason = "tool_use"
        resp_end = MagicMock()
        resp_end.content = [{"type": "text", "text": "done"}]
        resp_end.stop_reason = "end_turn"
        mock_llm.chat_main.side_effect = [resp_tool, resp_end]

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "s", "input_schema": {}}
        ]

        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=mock_registry,
            max_loops=5, policy_engine=mock_policy,
        )
        _, messages, _ = await loop.run("Hi")

        tool_result_content = ""
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_result_content = block.get("content", "")

        self.assertIn("用户未提供原因", tool_result_content)


class TestDenyResultContentRunStream(unittest.TestCase):
    """验证流式 run_stream() deny 后 tool 事件 result 包含明确拒绝提示。"""

    def test_run_stream_deny_result_contains_explicit_message(self):
        """run_stream() deny 后 tool 事件 result 包含 [用户已拒绝] 与 请停止重试。"""
        mock_policy = MagicMock()
        mock_policy.check.return_value = Decision(
            action="deny", reason="危险操作", risk_level="high"
        )

        mock_llm = MockStreamLLMClient([
            # 第 1 轮：tool_use（触发 deny）
            [
                _text_event("trying"),
                _done_event(
                    text="trying",
                    stop_reason="tool_use",
                    tool_use_blocks=[_tool_use_block(
                        name="file_write",
                        input_data={"path": "/x"},
                        block_id="tu_1",
                    )],
                ),
            ],
            # 第 2 轮：end_turn
            [
                _text_event("ok"),
                _done_event(text="ok", stop_reason="end_turn"),
            ],
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "file_write", "description": "w", "input_schema": {}}
        ]

        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=mock_registry,
            max_loops=5, policy_engine=mock_policy,
        )

        events = _collect_events(loop, "Hi")

        tool_events = [e for e in events if e.get("type") == "tool"]
        self.assertEqual(len(tool_events), 1)
        result = tool_events[0]["result"]
        self.assertIn("[用户已拒绝]", result)
        self.assertIn("请停止重试", result)
        self.assertIn("file_write", result)
        self.assertIn("危险操作", result)

        # execute_tool 不应被调用
        mock_registry.execute_tool.assert_not_called()


class TestDenyResultConfirmToDenyRunStream(unittest.TestCase):
    """验证流式 confirm→deny（用户拒绝）后 tool 事件 result 包含明确拒绝提示。"""

    def test_run_stream_confirm_then_user_deny(self):
        """confirm 后用户拒绝，tool 事件 result 包含 [用户已拒绝] 与 请停止重试。"""
        mock_policy = MagicMock()
        mock_policy.check.return_value = Decision(
            action="confirm", reason="需要确认", risk_level="high"
        )

        # mock approval_manager
        mock_approval = MagicMock()
        mock_approval.create_request.return_value = "appr-1"

        async def _wait_decision(approval_id):
            return ("deny", "用户拒绝了")
        mock_approval.wait_for_decision = _wait_decision

        mock_llm = MockStreamLLMClient([
            # 第 1 轮：tool_use（触发 confirm 后用户 deny）
            [
                _text_event("trying"),
                _done_event(
                    text="trying",
                    stop_reason="tool_use",
                    tool_use_blocks=[_tool_use_block(
                        name="file_write",
                        input_data={"path": "/x"},
                        block_id="tu_1",
                    )],
                ),
            ],
            # 第 2 轮：end_turn
            [
                _text_event("ok"),
                _done_event(text="ok", stop_reason="end_turn"),
            ],
        ])

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "file_write", "description": "w", "input_schema": {}}
        ]

        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=mock_registry,
            max_loops=5, policy_engine=mock_policy,
            approval_manager=mock_approval,
        )

        events = _collect_events(loop, "Hi")

        tool_events = [e for e in events if e.get("type") == "tool"]
        self.assertEqual(len(tool_events), 1)
        result = tool_events[0]["result"]
        self.assertIn("[用户已拒绝]", result)
        self.assertIn("请停止重试", result)
        self.assertIn("file_write", result)
        self.assertIn("用户拒绝了", result)

        # 审批事件
        appr_req = [e for e in events if e.get("type") == "approval_request"]
        self.assertEqual(len(appr_req), 1)
        appr_res = [e for e in events if e.get("type") == "approval_resolved"]
        self.assertEqual(len(appr_res), 1)
        self.assertEqual(appr_res[0]["decision"], "deny")

        # execute_tool 不应被调用
        mock_registry.execute_tool.assert_not_called()


# ---------------------------------------------------------------------------
# T8: max_loops 达到后总结调用 测试
# ---------------------------------------------------------------------------


class TestMaxLoopsSummaryRun(unittest.IsolatedAsyncioTestCase):
    """验证 run() 达到 max_loops 后调用 chat_main 做总结。

    注：``run`` / ``chat_main`` 已 async，本类用 IsolatedAsyncioTestCase + await。
    """

    async def test_run_max_loops_triggers_summary(self):
        """run() 达到 max_loops 后调用 chat_main 一次（不传 tools）做总结。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()

        # 循环中每次返回 tool_use（永不结束）
        resp_tool = MagicMock()
        resp_tool.content = [{
            "type": "tool_use", "id": "tu_1",
            "name": "search", "input": {},
        }]
        resp_tool.stop_reason = "tool_use"

        # 总结调用返回
        resp_summary = MagicMock()
        resp_summary.content = [{"type": "text", "text": "这是总结回复。"}]
        resp_summary.stop_reason = "end_turn"

        # max_loops=2：2 次循环 + 1 次总结
        mock_llm.chat_main.side_effect = [resp_tool, resp_tool, resp_summary]

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "s", "input_schema": {}}
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=mock_registry,
            max_loops=2,
        )

        final, _, _ = await loop.run("Hi", session_id="sess-1")

        # chat_main 调用 3 次：2 次循环 + 1 次总结
        self.assertEqual(mock_llm.chat_main.call_count, 3)

        # 最后一次调用不传 tools
        _, last_kwargs = mock_llm.chat_main.call_args
        self.assertIsNone(last_kwargs.get("tools"))

        # 返回的是总结文本
        self.assertEqual(final, "这是总结回复。")

    async def test_run_max_loops_summary_failure_fallback(self):
        """总结调用失败时降级返回 last_text。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()

        # 循环中返回 tool_use + 文本（更新 last_text）
        resp_tool = MagicMock()
        resp_tool.content = [
            {"type": "text", "text": "looping..."},
            {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}},
        ]
        resp_tool.stop_reason = "tool_use"

        # 总结调用抛异常
        mock_llm.chat_main.side_effect = [
            resp_tool, resp_tool, RuntimeError("API error")
        ]

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "s", "input_schema": {}}
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=mock_registry,
            max_loops=2,
        )

        final, _, _ = await loop.run("Hi")

        # 降级返回 last_text
        self.assertEqual(final, "looping...")


class TestMaxLoopsSummaryRunStream(unittest.TestCase):
    """验证 run_stream() 达到 max_loops 后调用 chat_main 做总结。

    注：``run_stream`` / ``chat_main_stream`` / ``chat_main`` 已 async，
    ``chat_main_stream`` mock 用 :func:`_async_gen` 包装为 async iterable，
    ``chat_main`` mock 用 :class:`AsyncMock`；事件收集走 ``_collect_events``
    （内部 ``asyncio.run`` + ``async for``）。
    """

    def test_run_stream_max_loops_triggers_summary(self):
        """run_stream() 达到 max_loops 后调用 chat_main 一次做总结。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()
        mock_llm.chat_main_stream.side_effect = [
            _async_gen(_make_tool_use_round_events()),
            _async_gen(_make_tool_use_round_events()),
        ]

        # 总结调用返回
        resp_summary = MagicMock()
        resp_summary.content = [{"type": "text", "text": "流式总结回复"}]
        resp_summary.stop_reason = "end_turn"
        mock_llm.chat_main.return_value = resp_summary

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "s", "input_schema": {}}
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=mock_registry,
            max_loops=2,
        )

        events = _collect_events(loop, "Hi", session_id="sess-1")

        # chat_main 调用 1 次（总结）
        self.assertEqual(mock_llm.chat_main.call_count, 1)

        # 最后一次调用不传 tools
        _, last_kwargs = mock_llm.chat_main.call_args
        self.assertIsNone(last_kwargs.get("tools"))

        # done 事件 response 为总结文本
        done_events = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0]["response"], "流式总结回复")

    def test_run_stream_max_loops_summary_failure_fallback(self):
        """run_stream() 总结调用失败时降级返回 last_text。"""
        mock_llm = MagicMock()
        mock_llm.chat_main = AsyncMock()
        mock_llm.chat_main_stream.side_effect = [
            _async_gen(_make_tool_use_round_events()),
            _async_gen(_make_tool_use_round_events()),
        ]

        # 总结调用抛异常
        mock_llm.chat_main.side_effect = RuntimeError("API error")

        mock_registry = MagicMock()
        mock_registry.get_tools_schema.return_value = [
            {"name": "search", "description": "s", "input_schema": {}}
        ]
        mock_registry.execute_tool.return_value = "result"

        loop = ReactLoop(
            llm_client=mock_llm, tool_registry=mock_registry,
            max_loops=2,
        )

        events = _collect_events(loop, "Hi")

        done_events = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(done_events), 1)
        # 降级返回 last_text（每轮文本为 "looping"）
        self.assertEqual(done_events[0]["response"], "looping")


if __name__ == "__main__":
    unittest.main(verbosity=2)
