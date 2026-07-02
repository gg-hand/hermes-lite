"""HistoryBuffer 归档回调单元测试 — 验证 FIFO 淘汰时归档到向量库的行为。

覆盖以下测试用例：
1. test_fifo_invokes_archive_callback      - FIFO 淘汰时 callback 被调用，传入正确 session_id 与 message
2. test_archived_message_contains_metadata - callback 收到的 message 含 role/content/timestamp
3. test_no_callback_falls_back_to_pure_fifo - archive_callback=None 时纯 FIFO 丢弃（向后兼容）
4. test_callback_exception_does_not_break_add - callback 抛异常时 add_message 仍正常完成
5. test_multi_session_independent_archive   - 两个会话各自 FIFO 独立归档
6. test_multiple_evictions_call_callback_each_time - 连续淘汰多条时 callback 被多次调用

运行方式：
    python -m unittest tests.test_history_buffer_archive -v
    python tests/test_history_buffer_archive.py
"""

from __future__ import annotations

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

# 将项目根目录加入 sys.path，使 from src.xxx import yyy 可用
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.storage.history_buffer import HistoryBuffer  # noqa: E402


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestHistoryBufferArchiveCallback(unittest.TestCase):
    """验证 HistoryBuffer 的 FIFO 淘汰归档行为。"""

    def test_fifo_invokes_archive_callback(self):
        """max_turns=3 添加 4 条消息，第 1 条被淘汰时 callback 被调用。

        验证：
        - callback 被调用 1 次（4 条触发 1 次 FIFO 淘汰）；
        - callback 收到的 session_id 与 add_message 传入的 session_id 一致；
        - callback 收到的 message content 等于最早被淘汰的那条。
        """
        archived: list = []  # [(session_id, message), ...]

        def archive_cb(sid: str, msg: dict) -> None:
            archived.append((sid, msg))

        buf = HistoryBuffer(max_turns=3, archive_callback=archive_cb)
        sid = "session-1"
        buf.add_message(sid, "user", "消息 A")
        buf.add_message(sid, "assistant", "消息 B")
        buf.add_message(sid, "user", "消息 C")
        # 第 4 条触发 FIFO：最早一条（消息 A）被淘汰并归档
        buf.add_message(sid, "assistant", "消息 D")

        # callback 被调用 1 次
        self.assertEqual(len(archived), 1, "应只归档 1 条消息")
        # session_id 正确
        self.assertEqual(archived[0][0], sid)
        # 被归档的是最早的消息 A
        self.assertEqual(archived[0][1]["content"], "消息 A")
        # 当前 history 保留最新的 3 条（B, C, D），不含 A
        history = buf.get_history(sid)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["content"], "消息 B")
        self.assertEqual(history[-1]["content"], "消息 D")

    def test_archived_message_contains_metadata(self):
        """验证 callback 收到的 message 包含 role / content / timestamp 字段。"""
        archived: list = []

        def archive_cb(sid: str, msg: dict) -> None:
            archived.append((sid, msg))

        buf = HistoryBuffer(max_turns=1, archive_callback=archive_cb)
        sid = "session-meta"
        buf.add_message(
            sid,
            "user",
            "你好世界",
            # 通过 kwargs 附加额外字段，验证归档时 message 完整保留
            tool_name="dummy_tool",
        )
        # 第 2 条触发 FIFO，淘汰第 1 条
        buf.add_message(sid, "assistant", "回复")

        self.assertEqual(len(archived), 1)
        msg = archived[0][1]
        # role / content / timestamp 三个核心字段都存在
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["content"], "你好世界")
        self.assertIn("timestamp", msg)
        self.assertIsInstance(msg["timestamp"], str)
        # timestamp 为 ISO 格式（粗略校验：包含 'T'）
        self.assertIn("T", msg["timestamp"])
        # kwargs 附加字段也被保留
        self.assertEqual(msg.get("tool_name"), "dummy_tool")
        # session_id 透传正确
        self.assertEqual(archived[0][0], sid)

    def test_no_callback_falls_back_to_pure_fifo(self):
        """archive_callback=None 时纯 FIFO 丢弃，不抛异常（向后兼容）。"""
        # 不传入 archive_callback，默认为 None
        buf = HistoryBuffer(max_turns=2)
        sid = "session-no-cb"
        buf.add_message(sid, "user", "A")
        buf.add_message(sid, "user", "B")
        # 第 3 条触发 FIFO：A 被直接丢弃，无回调调用
        buf.add_message(sid, "user", "C")

        history = buf.get_history(sid)
        self.assertEqual(len(history), 2)
        # 保留最新的 B, C
        self.assertEqual(history[0]["content"], "B")
        self.assertEqual(history[1]["content"], "C")
        # archive_callback 属性为 None
        self.assertIsNone(buf.archive_callback)

    def test_callback_exception_does_not_break_add(self):
        """callback 抛异常时 add_message 仍正常完成，history 状态正确。"""
        call_count = [0]

        def faulty_cb(sid: str, msg: dict) -> None:
            call_count[0] += 1
            raise RuntimeError("故意抛错")

        buf = HistoryBuffer(max_turns=2, archive_callback=faulty_cb)
        sid = "session-fault"
        # 添加 3 条，第 3 条触发 FIFO，callback 抛异常但被捕获
        buf.add_message(sid, "user", "A")
        buf.add_message(sid, "user", "B")
        # 不应抛异常
        buf.add_message(sid, "user", "C")

        # callback 被调用了 1 次（虽然抛了异常）
        self.assertEqual(call_count[0], 1)
        # history 仍然正确保留最新的 2 条（B, C）
        history = buf.get_history(sid)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "B")
        self.assertEqual(history[1]["content"], "C")

    def test_multi_session_independent_archive(self):
        """两个会话各自的 FIFO 独立归档，互不干扰。"""
        archived: dict = {}  # {session_id: [msg, ...]}

        def archive_cb(sid: str, msg: dict) -> None:
            archived.setdefault(sid, []).append(msg)

        # max_turns=2，便于快速触发淘汰
        buf = HistoryBuffer(max_turns=2, archive_callback=archive_cb)
        sid_a = "session-a"
        sid_b = "session-b"

        # session-a 添加 3 条 → 淘汰 1 条
        buf.add_message(sid_a, "user", "A1")
        buf.add_message(sid_a, "user", "A2")
        buf.add_message(sid_a, "user", "A3")

        # session-b 添加 3 条 → 淘汰 1 条
        buf.add_message(sid_b, "user", "B1")
        buf.add_message(sid_b, "user", "B2")
        buf.add_message(sid_b, "user", "B3")

        # 两个会话各自归档 1 条，且内容互不混淆
        self.assertEqual(len(archived.get(sid_a, [])), 1)
        self.assertEqual(len(archived.get(sid_b, [])), 1)
        self.assertEqual(archived[sid_a][0]["content"], "A1")
        self.assertEqual(archived[sid_b][0]["content"], "B1")

        # 各自 history 保留最新 2 条
        hist_a = buf.get_history(sid_a)
        hist_b = buf.get_history(sid_b)
        self.assertEqual([m["content"] for m in hist_a], ["A2", "A3"])
        self.assertEqual([m["content"] for m in hist_b], ["B2", "B3"])

    def test_multiple_evictions_call_callback_each_time(self):
        """一次 add_message 不会触发多次淘汰，但批量 add 多次会每次都回调。

        场景：max_turns=2，连续 add 5 条，应淘汰 3 条（A/B/C），
        callback 被调用 3 次，顺序与淘汰顺序一致。
        """
        archived: list = []

        def archive_cb(sid: str, msg: dict) -> None:
            archived.append(msg["content"])

        buf = HistoryBuffer(max_turns=2, archive_callback=archive_cb)
        sid = "session-multi"
        for c in ["A", "B", "C", "D", "E"]:
            buf.add_message(sid, "user", c)

        # 淘汰顺序：A, B, C（D, E 保留）
        self.assertEqual(archived, ["A", "B", "C"])
        # 当前 history 保留 D, E
        history = buf.get_history(sid)
        self.assertEqual([m["content"] for m in history], ["D", "E"])


class TestHistoryBufferArchiveIntegrationWithChroma(unittest.TestCase):
    """验证归档回调与 ChromaMemoryStore 的端到端集成（基于 mock chromadb）。"""

    def test_archive_callback_writes_to_chroma_store(self):
        """归档回调将淘汰消息以 type=conversation_turn 写入 ChromaMemoryStore。"""
        # 使用 mock 的 chromadb，避免依赖真实模型权重
        from src.storage.chroma_store import ChromaMemoryStore

        store = ChromaMemoryStore(persist_path="data/chroma_test_archive")

        buf = HistoryBuffer(
            max_turns=2,
            archive_callback=lambda sid, msg: store.add_memory(
                str(msg.get("content", "")),
                metadata={
                    "type": "conversation_turn",
                    "session_id": sid,
                    "role": msg.get("role", "unknown"),
                    "timestamp": msg.get("timestamp", ""),
                },
            ),
        )
        sid = "session-integration"
        buf.add_message(sid, "user", "python 学习笔记")
        buf.add_message(sid, "assistant", "好的")
        # 第 3 条触发淘汰：第 1 条被归档到 chroma
        buf.add_message(sid, "user", "继续")

        # 验证写入成功：get_all_memories 应包含 1 条
        all_mems = store.get_all_memories()
        self.assertEqual(len(all_mems), 1)
        archived = all_mems[0]
        self.assertEqual(archived["content"], "python 学习笔记")
        self.assertEqual(archived["metadata"]["type"], "conversation_turn")
        self.assertEqual(archived["metadata"]["session_id"], sid)
        self.assertEqual(archived["metadata"]["role"], "user")
        # timestamp 非空
        self.assertTrue(archived["metadata"]["timestamp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
