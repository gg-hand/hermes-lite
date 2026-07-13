"""MessagePersistence 测试:消息持久化、中断通知、历史清理、沉淀触发。

从 Orchestrator 提取的持久化职责:
- persist_new_messages: 将 React 循环新增 messages 持久化到 history_buffer
- save_interrupt_notice: 暂存中断通知到内存
- sanitize_history: 清理历史消息，确保 user/assistant 交替约束
- is_empty_assistant: 判断 assistant content 是否为空
- flush_consolidation: 强制触发记忆沉淀
- trigger_consolidation: 异步触发记忆沉淀
- maybe_flush_on_switch: 会话切换时自动 flush 旧会话
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes"))

import pytest
from unittest.mock import MagicMock, AsyncMock
from hermes.agent.msg_persistence import MessagePersistence


class TestPersistNewMessages:
    def test_empty_messages_persists_user_and_response(self):
        """new_messages 为空时降级为仅存 user_input + response_text。"""
        buffer = MagicMock()
        orch = MagicMock()
        orch.history_buffer = buffer
        mp = MessagePersistence(orchestrator=orch)
        mp.persist_new_messages("s1", [], "hi", "hello")
        buffer.add_message.assert_any_call("s1", "user", "hi")
        buffer.add_message.assert_any_call("s1", "assistant", "hello")

    def test_with_messages_perserves_each(self):
        """有 messages 时逐条持久化。"""
        buffer = MagicMock()
        orch = MagicMock()
        orch.history_buffer = buffer
        mp = MessagePersistence(orchestrator=orch)
        messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        mp.persist_new_messages("s1", messages, "q", "a")
        assert buffer.add_message.call_count == 2

    def test_skip_messages_without_role_or_content(self):
        """缺少 role 或 content 的消息跳过。"""
        buffer = MagicMock()
        orch = MagicMock()
        orch.history_buffer = buffer
        mp = MessagePersistence(orchestrator=orch)
        messages = [
            {"role": "user", "content": "q"},
            {"role": None, "content": "bad"},
            {"content": "no_role"},
            {"role": "assistant", "content": "a"},
        ]
        mp.persist_new_messages("s1", messages, "q", "a")
        assert buffer.add_message.call_count == 2  # 只存有 role+content 的


class TestSaveInterruptNotice:
    def test_with_new_message(self):
        """有新消息时存储含新消息的通知。"""
        orch = MagicMock()
        orch._pending_interrupt_notices = {}
        mp = MessagePersistence(orchestrator=orch)
        mp.save_interrupt_notice("s1", "用户新消息")
        assert "s1" in orch._pending_interrupt_notices
        notice = orch._pending_interrupt_notices["s1"]
        assert "用户新消息" in notice["content"]
        assert "timestamp" in notice

    def test_without_new_message(self):
        """无新消息时存储默认通知。"""
        orch = MagicMock()
        orch._pending_interrupt_notices = {}
        mp = MessagePersistence(orchestrator=orch)
        mp.save_interrupt_notice("s1")
        notice = orch._pending_interrupt_notices["s1"]
        assert "中断了回复" in notice["content"]


class TestSanitizeHistory:
    def test_empty_history(self):
        assert MessagePersistence.sanitize_history([]) == []
        assert MessagePersistence.sanitize_history(None) is None

    def test_single_message_unchanged(self):
        history = [{"role": "user", "content": "hi"}]
        assert MessagePersistence.sanitize_history(history) == history

    def test_merge_consecutive_user(self):
        """连续 user 消息合并。"""
        history = [
            {"role": "user", "content": "msg1"},
            {"role": "user", "content": "msg2"},
        ]
        result = MessagePersistence.sanitize_history(history)
        assert len(result) == 1
        assert "msg1" in result[0]["content"]
        assert "msg2" in result[0]["content"]

    def test_pop_empty_assistant_before_user(self):
        """user 前的空 assistant 被弹出。"""
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "q2"},
        ]
        result = MessagePersistence.sanitize_history(history)
        # 空 assistant 被弹出，两个 user 合并
        assert len(result) == 1
        assert "q" in result[0]["content"]
        assert "q2" in result[0]["content"]


class TestIsEmptyAssistant:
    def test_none(self):
        assert MessagePersistence.is_empty_assistant(None) is True

    def test_empty_string(self):
        assert MessagePersistence.is_empty_assistant("") is True

    def test_whitespace_string(self):
        assert MessagePersistence.is_empty_assistant("   ") is True

    def test_non_empty_string(self):
        assert MessagePersistence.is_empty_assistant("hello") is False

    def test_empty_list(self):
        assert MessagePersistence.is_empty_assistant([]) is True

    def test_list_with_text(self):
        content = [{"type": "text", "text": "hello"}]
        assert MessagePersistence.is_empty_assistant(content) is False

    def test_list_with_tool_use(self):
        """含 tool_use 块的不算空。"""
        content = [{"type": "tool_use", "id": "1", "name": "x", "input": {}}]
        assert MessagePersistence.is_empty_assistant(content) is False

    def test_list_with_empty_text_only(self):
        """仅含空 text 块的算空。"""
        content = [{"type": "text", "text": ""}, {"type": "text", "text": "  "}]
        assert MessagePersistence.is_empty_assistant(content) is True


class TestFlushConsolidation:
    def test_no_engine_returns_empty(self):
        """consolidation_engine 为 None 时返回空 dict。"""
        orch = MagicMock()
        orch.consolidation_engine = None
        mp = MessagePersistence(orchestrator=orch)
        assert mp.flush_consolidation() == {}

    def test_calls_force_consolidate(self):
        """有 engine 时调用 force_consolidate。"""
        orch = MagicMock()
        orch.consolidation_engine = MagicMock()
        orch.consolidation_engine.force_consolidate.return_value = {"added": 1}
        mp = MessagePersistence(orchestrator=orch)
        result = mp.flush_consolidation("s1")
        assert result == {"added": 1}
        orch.consolidation_engine.force_consolidate.assert_called_once_with(
            session_id="s1"
        )

    def test_engine_error_returns_empty(self):
        """engine 抛异常时返回空 dict。"""
        orch = MagicMock()
        orch.consolidation_engine = MagicMock()
        orch.consolidation_engine.force_consolidate.side_effect = RuntimeError("fail")
        mp = MessagePersistence(orchestrator=orch)
        assert mp.flush_consolidation() == {}


class TestTriggerConsolidation:
    def test_no_engine_skips(self):
        orch = MagicMock()
        orch.consolidation_engine = None
        mp = MessagePersistence(orchestrator=orch)
        # 不应抛异常
        asyncio.run(mp.trigger_consolidation("s1"))

    def test_calls_consolidate(self):
        orch = MagicMock()
        orch.consolidation_engine = MagicMock()
        orch.consolidation_engine.consolidate = MagicMock(return_value={})
        mp = MessagePersistence(orchestrator=orch)
        asyncio.run(mp.trigger_consolidation("s1"))
        orch.consolidation_engine.consolidate.assert_called_once()
