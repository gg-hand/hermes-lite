"""SSE 事件透传测试（Phase 6 Task 7）。

验证 ``src/server.py`` 的 ``POST /chat/stream`` 端点正确透传 round_start /
todo_init / todo_update / todo_complete / tool 等事件，并对 done 事件补充
timestamp 字段。同时验证旧 /tasks 端点已删除（返回 404）、/schedules 端点
保留（返回 200）。

Mock 策略：
- patch ``src.server.orchestrator`` 为 mock，其 ``chat_stream`` 返回预设事件
  序列的 async generator。
- patch ``src.server.session_logger`` 为 mock，``create_session`` 返回固定
  session_id（避免依赖真实 SQLite）。
- 使用 FastAPI TestClient 发起 ``POST /chat/stream``，解析 SSE 响应验证事件
  类型与字段。

运行方式:
    python -m unittest tests.test_sse_todo_events -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.server import app  # noqa: E402
# Task 11: server.py 全局变量已删除，通过 app.dependency_overrides 注入 mock
from app import (  # noqa: E402
    get_orchestrator,
    get_session_logger,
    get_stream_manager,
    get_cron_scheduler,
)
from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Mock 构造工具
# ---------------------------------------------------------------------------


class _MockOrchestrator:
    """Mock Orchestrator，``chat_stream`` 按预设事件序列 yield（async generator）。

    模拟真实 ``Orchestrator.chat_stream`` 的行为：依次 yield 预设的事件 dict。
    """

    def __init__(self, events):
        self._events = list(events)

    async def chat_stream(self, session_id, message, cancel_event=None, **kwargs):
        for evt in self._events:
            yield evt


def _make_orchestrator(events):
    """创建 mock orchestrator 实例。"""
    return _MockOrchestrator(events)


def _make_session_logger(session_id="test-session-001"):
    """创建 mock session_logger，``create_session`` 返回固定 session_id。"""
    logger = MagicMock()
    logger.create_session.return_value = session_id
    return logger


def _parse_sse(text):
    """解析 SSE 响应文本为事件 dict 列表。

    SSE 格式：每条事件为 ``data: <json>\\n\\n``。
    跳过空块与非 ``data:`` 前缀的块。
    """
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or not block.startswith("data: "):
            continue
        json_str = block[len("data: "):]
        try:
            events.append(json.loads(json_str))
        except json.JSONDecodeError:
            pass
    return events


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestSseTodoEvents(unittest.TestCase):
    """验证 /chat/stream 端点的 SSE 事件透传逻辑。"""

    def setUp(self):
        self._patches = []
        self._override_keys = []

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        # 清理 DI overrides
        for key in self._override_keys:
            app.dependency_overrides.pop(key, None)
        self._override_keys = []

    def _setup(self, events, extra_patches=None, session_id="test-session-001",
               extra_overrides=None):
        """通过 DI overrides 注入 orchestrator/session_logger 并创建 TestClient。

        参数:
            events: orchestrator.chat_stream yield 的预设事件 dict 列表。
            extra_patches: 额外需要启动的 patch 列表（保留兼容）。
            session_id: mock session_logger.create_session 返回的 session_id。
            extra_overrides: 额外的 (getter_func, mock_instance) 列表。
        """
        orch = _make_orchestrator(events)
        sess_logger = _make_session_logger(session_id)
        # Task 11: 通过 DI overrides 注入（不再 patch src.server 全局变量）
        app.dependency_overrides[get_orchestrator] = lambda: orch
        app.dependency_overrides[get_session_logger] = lambda: sess_logger
        # stream_manager 默认 None（chat_stream 路由依赖此参数）
        app.dependency_overrides[get_stream_manager] = lambda: None
        self._override_keys = [get_orchestrator, get_session_logger, get_stream_manager]
        if extra_overrides:
            for getter, instance in extra_overrides:
                app.dependency_overrides[getter] = lambda i=instance: i
                self._override_keys.append(getter)
        if extra_patches:
            self._patches = list(extra_patches)
            for p in self._patches:
                p.start()
        return TestClient(app)

    def _post_stream(self, client, message="hello", session_id=None):
        """发起 POST /chat/stream 请求，返回完整 SSE 响应文本。"""
        payload = {"message": message}
        if session_id is not None:
            payload["session_id"] = session_id
        resp = client.post("/chat/stream", json=payload)
        assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}: {resp.text}"
        return resp.text

    # ---------- 事件透传测试 ----------

    def test_chat_stream_yields_session_event(self):
        """第一个事件是 session 事件，含 session_id 与 timestamp。"""
        client = self._setup([{"type": "done", "response": "ok"}])
        text = self._post_stream(client)
        events = _parse_sse(text)
        self.assertGreaterEqual(len(events), 2)
        first = events[0]
        self.assertEqual(first["type"], "session")
        self.assertEqual(first["session_id"], "test-session-001")
        self.assertIn("timestamp", first)

    def test_chat_stream_passes_through_round_start(self):
        """round_start 事件被原样透传，保留 loop_idx 字段。"""
        client = self._setup([
            {"type": "round_start", "loop_idx": 0},
            {"type": "text", "text": "R1"},
            {"type": "done", "response": "R1"},
        ])
        text = self._post_stream(client)
        events = _parse_sse(text)
        round_starts = [e for e in events if e.get("type") == "round_start"]
        self.assertEqual(len(round_starts), 1)
        self.assertEqual(round_starts[0]["loop_idx"], 0)

    def test_chat_stream_passes_through_todo_init(self):
        """todo_init 事件被原样透传，含 session_id 与 todo 字段。"""
        todo = {"goal": "完成 T7", "steps": ["改 server.py", "加测试"], "completed": []}
        client = self._setup([
            {"type": "todo_init", "session_id": "test-session-001", "todo": todo},
            {"type": "done", "response": "ok"},
        ])
        text = self._post_stream(client)
        events = _parse_sse(text)
        todo_inits = [e for e in events if e.get("type") == "todo_init"]
        self.assertEqual(len(todo_inits), 1)
        self.assertEqual(todo_inits[0]["session_id"], "test-session-001")
        self.assertEqual(todo_inits[0]["todo"], todo)

    def test_chat_stream_passes_through_todo_update(self):
        """todo_update 事件被原样透传，todo 字段为变更后的完整列表。"""
        todo = {
            "goal": "完成 T7",
            "steps": ["改 server.py", "加测试"],
            "completed": ["改 server.py"],
        }
        client = self._setup([
            {"type": "todo_update", "session_id": "test-session-001", "todo": todo},
            {"type": "done", "response": "ok"},
        ])
        text = self._post_stream(client)
        events = _parse_sse(text)
        updates = [e for e in events if e.get("type") == "todo_update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["todo"], todo)
        self.assertEqual(updates[0]["session_id"], "test-session-001")

    def test_chat_stream_passes_through_todo_complete(self):
        """todo_complete 事件被原样透传，标记 plan 整体完成。"""
        todo = {
            "goal": "完成 T7",
            "steps": ["改 server.py", "加测试"],
            "completed": ["改 server.py", "加测试"],
        }
        client = self._setup([
            {"type": "todo_complete", "session_id": "test-session-001", "todo": todo},
            {"type": "done", "response": "ok"},
        ])
        text = self._post_stream(client)
        events = _parse_sse(text)
        completes = [e for e in events if e.get("type") == "todo_complete"]
        self.assertEqual(len(completes), 1)
        self.assertEqual(completes[0]["todo"], todo)
        self.assertEqual(completes[0]["session_id"], "test-session-001")

    def test_chat_stream_passes_through_tool_with_session_id(self):
        """tool 事件透传并保留 session_id 字段。"""
        client = self._setup([
            {
                "type": "tool",
                "name": "search",
                "input": {"q": "x"},
                "result": "r1",
                "is_error": False,
                "session_id": "test-session-001",
            },
            {"type": "done", "response": "ok"},
        ])
        text = self._post_stream(client)
        events = _parse_sse(text)
        tools = [e for e in events if e.get("type") == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "search")
        self.assertEqual(tools[0]["input"], {"q": "x"})
        self.assertEqual(tools[0]["result"], "r1")
        self.assertFalse(tools[0]["is_error"])
        # session_id 字段应被保留（plan 模式下 tool 事件附带 session_id）
        self.assertEqual(tools[0]["session_id"], "test-session-001")

    def test_chat_stream_done_event_has_timestamp(self):
        """done 事件被补充了 timestamp 字段（源事件无 timestamp）。"""
        # 源 done 事件仅含 response，server 应补充 timestamp
        client = self._setup([
            {"type": "text", "text": "hi"},
            {"type": "done", "response": "hi"},
        ])
        text = self._post_stream(client)
        events = _parse_sse(text)
        dones = [e for e in events if e.get("type") == "done"]
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0]["response"], "hi")
        self.assertIn("timestamp", dones[0])
        # session 事件不应被误判为 done
        self.assertEqual(events[0]["type"], "session")

    # ---------- 中断事件测试（新增） ----------

    def test_chat_stream_interrupt_event_emitted_when_cancelled(self):
        """cancel_event 被设置时，SSE 流发出 interrupt 事件并结束。"""
        import threading
        pre_set_event = threading.Event()
        pre_set_event.set()
        mock_sm = MagicMock()
        mock_sm.register.return_value = pre_set_event
        mock_sm.is_graceful_pending.return_value = False

        client = self._setup(
            [{"type": "done", "response": "不会被触发"}],
            extra_overrides=[(get_stream_manager, mock_sm)],
            session_id="test-session-001",
        )
        text = self._post_stream(client, session_id="test-session-001")
        events = _parse_sse(text)
        types = [e["type"] for e in events]
        self.assertIn("session", types)
        self.assertIn("interrupt", types)
        # interrupt 之后不应有 done
        interrupt_idx = types.index("interrupt")
        self.assertNotIn("done", types[interrupt_idx:])

    def test_chat_stream_no_interrupt_when_not_cancelled(self):
        """cancel_event 未设置时，流正常走完发出 done。"""
        client = self._setup([
            {"type": "text", "text": "hi"},
            {"type": "done", "response": "hi"},
        ])
        text = self._post_stream(client)
        events = _parse_sse(text)
        types = [e["type"] for e in events]
        self.assertIn("done", types)
        self.assertNotIn("interrupt", types)

    # ---------- 端点存续性测试 ----------

    def test_tasks_endpoints_removed(self):
        """GET /tasks 返回 404（端点已删除）。"""
        client = self._setup([{"type": "done", "response": "ok"}])
        resp = client.get("/tasks")
        self.assertEqual(resp.status_code, 404)

    def test_schedules_endpoints_still_work(self):
        """GET /schedules 返回 200（端点保留）。"""
        mock_cs = MagicMock()
        mock_cs.list_schedules.return_value = []
        client = self._setup(
            [{"type": "done", "response": "ok"}],
            extra_overrides=[(get_cron_scheduler, mock_cs)],
        )
        resp = client.get("/schedules")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["schedules"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
