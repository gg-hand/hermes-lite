"""HistoryBuffer FIFO 配对淘汰单元测试 — Phase 9 Task 6 方案 A。

验证 tool_use/tool_result 配对原子删除行为，覆盖以下场景：

1. test_pair_evicted_atomically              - 头部 assistant(tool_use)+user(tool_result) 同时淘汰
2. test_pair_both_archived                   - 配对两条都归档到 archive_callback
3. test_plain_text_still_evicted_singly      - 普通文本消息仍按单条 FIFO 淘汰
4. test_orphan_tool_result_at_head_cleaned   - 头部孤立 tool_result 被单条清理
5. test_assistant_tool_use_without_result    - 头部 tool_use 后非 tool_result 时按单条淘汰
6. test_no_pair_when_only_one_msg_left       - 仅剩 1 条 tool_use 时不越界配对删除
7. test_helper_is_tool_use_msg               - _is_tool_use_msg 辅助方法
8. test_helper_is_tool_result_msg            - _is_tool_result_msg 辅助方法
9. test_pair_eviction_under_cron_session     - cron session 也保证配对完整性
10. test_multi_pair_consecutive_eviction     - 连续多条配对都原子删除

运行方式：
    python -m pytest tests/test_history_buffer.py -v
    python -m unittest tests.test_history_buffer -v
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.storage.history_buffer import HistoryBuffer  # noqa: E402


# ---------------------------------------------------------------------------
# 测试数据构造辅助
# ---------------------------------------------------------------------------

def make_tool_use_msg(tool_use_id: str, name: str = "search") -> Dict[str, Any]:
    """构造 assistant(tool_use) 消息（Anthropic 风格 content block 列表）。"""
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "调用工具"},
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": name,
                "input": {"q": "test"},
            },
        ],
    }


def make_tool_result_msg(tool_use_id: str, result: str = "ok") -> Dict[str, Any]:
    """构造 user(tool_result) 消息（Anthropic 风格 content block 列表）。"""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result,
            }
        ],
    }


def make_text_msg(role: str, text: str) -> Dict[str, Any]:
    """构造纯文本消息（content 为 str）。"""
    return {"role": role, "content": text}


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestFIFOPairEviction(unittest.TestCase):
    """验证 FIFO 淘汰时 tool_use/tool_result 配对原子删除。"""

    def test_pair_evicted_atomically(self):
        """头部 assistant(tool_use) + user(tool_result) 时，FIFO 同时删除两条。

        场景：max_turns=2，添加 [tool_use, tool_result, text]，
        第 3 条 text 触发 FIFO：应同时删除前两条（配对），仅保留 text。
        """
        buf = HistoryBuffer(max_turns=2)
        sid = "session-pair"
        buf.add_message(sid, "assistant", make_tool_use_msg("tu_1")["content"])
        buf.add_message(sid, "user", make_tool_result_msg("tu_1")["content"])
        # 触发 FIFO：应原子删除前两条
        buf.add_message(sid, "user", "新消息")

        history = buf.get_history(sid)
        # 仅保留 1 条（max_turns=2，添加 3 条淘汰 2 条配对）
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "新消息")
        # 不应残留孤立 tool_result
        for msg in history:
            self.assertNotEqual(msg.get("role"), "user_with_orphan_result")

    def test_pair_both_archived(self):
        """配对删除时，assistant(tool_use) 与 user(tool_result) 都归档。"""
        archived: List[Dict[str, Any]] = []

        def archive_cb(sid: str, msg: Dict[str, Any]) -> None:
            archived.append({"sid": sid, "role": msg.get("role"), "msg": msg})

        buf = HistoryBuffer(max_turns=2, archive_callback=archive_cb)
        sid = "session-archive"
        buf.add_message(sid, "assistant", make_tool_use_msg("tu_arch")["content"])
        buf.add_message(sid, "user", make_tool_result_msg("tu_arch")["content"])
        # 触发配对淘汰
        buf.add_message(sid, "user", "trigger")

        # 两条都被归档
        self.assertEqual(len(archived), 2)
        # 第 1 条是 assistant(tool_use)
        self.assertEqual(archived[0]["sid"], sid)
        self.assertEqual(archived[0]["role"], "assistant")
        # 第 2 条是 user(tool_result)
        self.assertEqual(archived[1]["sid"], sid)
        self.assertEqual(archived[1]["role"], "user")
        # 当前 history 不含被淘汰的两条
        history = buf.get_history(sid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "trigger")

    def test_plain_text_still_evicted_singly(self):
        """普通文本消息仍按单条 FIFO 淘汰（不受配对逻辑影响）。"""
        archived: List[str] = []

        def archive_cb(sid: str, msg: Dict[str, Any]) -> None:
            archived.append(msg.get("content", ""))

        buf = HistoryBuffer(max_turns=2, archive_callback=archive_cb)
        sid = "session-text"
        buf.add_message(sid, "user", "A")
        buf.add_message(sid, "user", "B")
        # 触发 FIFO：仅淘汰 1 条（A）
        buf.add_message(sid, "user", "C")

        # 普通文本单条淘汰：仅归档 A
        self.assertEqual(archived, ["A"])
        history = buf.get_history(sid)
        self.assertEqual([m["content"] for m in history], ["B", "C"])

    def test_orphan_tool_result_at_head_cleaned(self):
        """头部是孤立 tool_result（前面无 tool_use）时，单条清理并归档。

        场景：模拟 tool_use 已被先前轮次删除，仅留下 user(tool_result)。
        FIFO 应直接淘汰该孤立 tool_result（不配对），避免残留。
        """
        archived: List[Dict[str, Any]] = []

        def archive_cb(sid: str, msg: Dict[str, Any]) -> None:
            archived.append(msg)

        buf = HistoryBuffer(max_turns=2, archive_callback=archive_cb)
        sid = "session-orphan"
        # 直接构造孤立 tool_result 在头部
        buf.add_message(sid, "user", make_tool_result_msg("orphan_1")["content"])
        buf.add_message(sid, "user", "正常消息")
        # 触发 FIFO：孤立 tool_result 被单条淘汰
        buf.add_message(sid, "user", "新消息")

        # 仅淘汰 1 条（孤立 tool_result）
        self.assertEqual(len(archived), 1)
        # 被淘汰的是孤立 tool_result
        content = archived[0].get("content")
        self.assertIsInstance(content, list)
        self.assertEqual(content[0].get("type"), "tool_result")
        # 当前 history 不含孤立 tool_result
        history = buf.get_history(sid)
        self.assertEqual(len(history), 2)
        for msg in history:
            # 不应有 tool_result 残留
            c = msg.get("content")
            if isinstance(c, list):
                for block in c:
                    self.assertNotEqual(
                        block.get("type"), "tool_result",
                        "history 不应残留孤立 tool_result",
                    )

    def test_assistant_tool_use_without_result(self):
        """头部是 assistant(tool_use) 但下一条不是 user(tool_result) 时按单条淘汰。

        场景：[tool_use, text]，max_turns=1，添加 text 触发 FIFO。
        tool_use 后跟 text（非 tool_result），不应误触发配对删除。
        """
        archived: List[Dict[str, Any]] = []

        def archive_cb(sid: str, msg: Dict[str, Any]) -> None:
            archived.append(msg)

        buf = HistoryBuffer(max_turns=1, archive_callback=archive_cb)
        sid = "session-mixed"
        buf.add_message(sid, "assistant", make_tool_use_msg("tu_solo")["content"])
        # 第 2 条触发 FIFO：仅淘汰 tool_use（下一条不是 tool_result，不配对）
        buf.add_message(sid, "user", "text-after")

        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].get("role"), "assistant")
        history = buf.get_history(sid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "text-after")

    def test_no_pair_when_only_one_msg_left(self):
        """仅剩 1 条 tool_use 时不会越界配对删除（边界保护）。

        场景：[tool_use]，max_turns=0 时虽不实际发生（max_turns >= 1），
        但用 max_turns=1 添加 2 条 [tool_use, text] 时，
        第 2 条触发 FIFO：仅删 tool_use（无下一条可配对，因 len(history)==1 时
        不满足 len >= 2 条件）。
        """
        buf = HistoryBuffer(max_turns=1)
        sid = "session-edge"
        buf.add_message(sid, "assistant", make_tool_use_msg("tu_edge")["content"])
        # 此时 history = [tool_use]，长度 1，不超 max_turns=1
        self.assertEqual(len(buf.get_history(sid)), 1)
        # 添加第 2 条触发 FIFO：删除 tool_use 单条
        buf.add_message(sid, "user", "second")
        history = buf.get_history(sid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "second")

    def test_pair_eviction_under_cron_session(self):
        """cron session 也保证 tool_use/tool_result 配对完整性。

        场景：cron_max_turns=2，cron session 添加 [tool_use, tool_result, text]，
        第 3 条触发 FIFO：应原子删除前两条（配对）。
        """
        archived: List[Dict[str, Any]] = []

        def archive_cb(sid: str, msg: Dict[str, Any]) -> None:
            archived.append(msg)

        buf = HistoryBuffer(
            max_turns=50,
            cron_max_turns=2,
            archive_callback=archive_cb,
        )
        sid = "cron:session-1"
        buf.add_message(sid, "assistant", make_tool_use_msg("tu_cron")["content"])
        buf.add_message(sid, "user", make_tool_result_msg("tu_cron")["content"])
        # 触发 cron FIFO：应配对删除
        buf.add_message(sid, "user", "新消息")

        # 两条都被归档（配对）
        self.assertEqual(len(archived), 2)
        self.assertEqual(archived[0].get("role"), "assistant")
        self.assertEqual(archived[1].get("role"), "user")
        # 当前 history 仅保留 1 条
        history = buf.get_history(sid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "新消息")

    def test_multi_pair_consecutive_eviction(self):
        """连续多对 tool_use/tool_result 都原子删除。

        场景：max_turns=2，依次添加 tu_1, tr_1, tu_2, tr_2, "final"。
        执行轨迹：
          add tu_1 → history=[tu_1]                      (len=1, 无 FIFO)
          add tr_1 → history=[tu_1, tr_1]                (len=2, 无 FIFO)
          add tu_2 → history=[tu_1, tr_1, tu_2]          (len=3, FIFO 配对删 tu_1+tr_1)
                                                          → history=[tu_2]
          add tr_2 → history=[tu_2, tr_2]                (len=2, 无 FIFO)
          add final→ history=[tu_2, tr_2, final]         (len=3, FIFO 配对删 tu_2+tr_2)
                                                          → history=[final]
        最终归档 4 条（2 assistant + 2 user），history 仅剩 final。
        """
        archived: List[Dict[str, Any]] = []

        def archive_cb(sid: str, msg: Dict[str, Any]) -> None:
            archived.append(msg)

        buf = HistoryBuffer(max_turns=2, archive_callback=archive_cb)
        sid = "session-multi-pair"
        buf.add_message(sid, "assistant", make_tool_use_msg("tu_1")["content"])
        buf.add_message(sid, "user", make_tool_result_msg("tu_1")["content"])
        # 触发 FIFO：配对删 tu_1 + tr_1
        buf.add_message(sid, "assistant", make_tool_use_msg("tu_2")["content"])
        # 此时 history=[tu_2]，len=1 不超 max_turns=2，无 FIFO
        buf.add_message(sid, "user", make_tool_result_msg("tu_2")["content"])
        # 此时 history=[tu_2, tr_2]，len=2 不超，无 FIFO
        # 触发 FIFO：配对删 tu_2 + tr_2
        buf.add_message(sid, "user", "final")

        # 当前 history 仅保留 final
        history = buf.get_history(sid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "final")
        # 共归档 4 条：2 条 assistant(tool_use) + 2 条 user(tool_result)
        roles = [m.get("role") for m in archived]
        self.assertEqual(roles.count("assistant"), 2)
        self.assertEqual(roles.count("user"), 2)
        # 关键：assistant 与 user 数量相等，证明每次都是配对删除（无孤立残留）
        self.assertEqual(
            roles.count("assistant"), roles.count("user"),
            "配对删除应保证 tool_use 与 tool_result 数量相等",
        )


class TestHistoryBufferHelpers(unittest.TestCase):
    """验证 _is_tool_use_msg / _is_tool_result_msg 辅助方法。"""

    def test_helper_is_tool_use_msg(self):
        """_is_tool_use_msg 正确识别 assistant(tool_use) 消息。"""
        # 正例：assistant 含 tool_use 块
        self.assertTrue(HistoryBuffer._is_tool_use_msg(make_tool_use_msg("tu_1")))
        # 反例：user 消息
        self.assertFalse(HistoryBuffer._is_tool_use_msg(make_tool_result_msg("tu_1")))
        # 反例：assistant 纯文本（content 为 str）
        self.assertFalse(HistoryBuffer._is_tool_use_msg(make_text_msg("assistant", "hi")))
        # 反例：assistant content 为 list 但无 tool_use 块
        self.assertFalse(HistoryBuffer._is_tool_use_msg({
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        }))
        # 反例：user 消息含 tool_use 块（角色不符）
        self.assertFalse(HistoryBuffer._is_tool_use_msg({
            "role": "user",
            "content": [{"type": "tool_use", "id": "x", "name": "n", "input": {}}],
        }))

    def test_helper_is_tool_result_msg(self):
        """_is_tool_result_msg 正确识别 user(tool_result) 消息。"""
        # 正例：user 含 tool_result 块
        self.assertTrue(HistoryBuffer._is_tool_result_msg(make_tool_result_msg("tu_1")))
        # 反例：assistant 消息
        self.assertFalse(HistoryBuffer._is_tool_result_msg(make_tool_use_msg("tu_1")))
        # 反例：user 纯文本（content 为 str）
        self.assertFalse(HistoryBuffer._is_tool_result_msg(make_text_msg("user", "hi")))
        # 反例：user content 为 list 但无 tool_result 块
        self.assertFalse(HistoryBuffer._is_tool_result_msg({
            "role": "user",
            "content": [{"type": "text", "text": "hi"}],
        }))
        # 反例：assistant 消息含 tool_result 块（角色不符）
        self.assertFalse(HistoryBuffer._is_tool_result_msg({
            "role": "assistant",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": ""}],
        }))


if __name__ == "__main__":
    unittest.main(verbosity=2)
