"""消息持久化:历史缓冲写入、中断通知暂存、历史清理、沉淀触发。

从 Orchestrator 提取的持久化职责:
- persist_new_messages: 将 React 循环新增 messages 持久化到 history_buffer
- save_interrupt_notice: 暂存中断通知到内存
- sanitize_history: 清理历史消息，确保 user/assistant 交替约束
- is_empty_assistant: 判断 assistant content 是否为空
- flush_consolidation: 强制触发记忆沉淀
- trigger_consolidation: 异步触发记忆沉淀
- maybe_flush_on_switch: 会话切换时自动 flush 旧会话

采用方法对象模式：MessagePersistence 持有 Orchestrator 引用，
因为持久化依赖 history_buffer / consolidation_engine / _pending_interrupt_notices
等多个 Orchestrator 状态。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MessagePersistence:
    """消息持久化管理器（方法对象模式，持有 orchestrator 引用）。"""

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator

    # ------------------------------------------------------------------
    # history_buffer 持久化
    # ------------------------------------------------------------------

    def persist_new_messages(
        self,
        session_id: str,
        new_messages: List[Dict[str, Any]],
        user_input: str,
        response_text: str,
    ) -> None:
        """将本轮 React 循环新增的完整 messages 持久化到 history_buffer。

        ``new_messages`` 为空（流中断或异常）时降级为仅存 user_input +
        response_text，保证至少保留本轮纯文本对话。
        """
        buffer = getattr(self._orch, "history_buffer", None)
        if buffer is None:
            return
        if not new_messages:
            buffer.add_message(session_id, "user", user_input)
            buffer.add_message(session_id, "assistant", response_text)
            return
        for msg in new_messages:
            role = msg.get("role")
            content = msg.get("content")
            if role is None or content is None:
                continue
            buffer.add_message(session_id, role, content)

    # ------------------------------------------------------------------
    # 中断通知
    # ------------------------------------------------------------------

    def save_interrupt_notice(
        self, session_id: str, new_message: Optional[str] = None
    ) -> None:
        """中断发生时，暂存 InterruptNotice 到内存。

        通知将在下次 chat()/chat_stream() 开始时作为独立 system 消息注入到
        enhanced_history。
        """
        if new_message:
            content = (
                "【系统通知：用户中断了回复】\n"
                f"用户的新消息如下：\n{new_message}\n\n"
                "请直接响应用户的新消息，不要续写被中断的内容。"
            )
        else:
            content = "【系统通知：用户中断了回复】请等待用户的下一条指令。不要续写被中断的内容。"

        notices = getattr(self._orch, "_pending_interrupt_notices", None)
        if notices is None:
            notices = {}
            self._orch._pending_interrupt_notices = notices
        notices[session_id] = {
            "content": content,
            "timestamp": time.time(),
        }
        logger.info("InterruptNotice 已暂存: %s", session_id)

    # ------------------------------------------------------------------
    # 历史清理
    # ------------------------------------------------------------------

    @staticmethod
    def sanitize_history(
        history: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """清理历史消息，确保 user/assistant 交替约束。

        1. ``[..., user, user]`` → 合并
        2. ``[..., user, system_notice, user]`` → 保留（system 隔开）
        3. ``[..., assistant(空/半截), user]`` → 弹出空 assistant
        """
        if not history or len(history) < 2:
            return history
        cleaned: List[Dict[str, Any]] = [history[0]]
        for msg in history[1:]:
            last = cleaned[-1]
            # 弹出末尾空 assistant（content 为 None/""/[]/纯空 text 块）
            if (
                msg.get("role") == "user"
                and last.get("role") == "assistant"
                and MessagePersistence.is_empty_assistant(last.get("content"))
            ):
                cleaned.pop()
                last = cleaned[-1] if cleaned else None
                if last is None:
                    cleaned.append(msg)
                    continue
            # 合并连续 user 消息（system_notice 隔开的不合并）
            if (
                msg.get("role") == "user"
                and last.get("role") == "user"
                and isinstance(last.get("content"), str)
                and isinstance(msg.get("content"), str)
            ):
                cleaned[-1] = {
                    **last,
                    "content": f"{last['content']}\n{msg['content']}",
                }
            else:
                cleaned.append(msg)
        return cleaned

    @staticmethod
    def is_empty_assistant(content: Any) -> bool:
        """判断 assistant content 是否为空（None / "" / [] / 纯空 text 块）。

        含 tool_use 块的不算空。
        """
        if content is None or content == "":
            return True
        if isinstance(content, list):
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )
            if has_tool_use:
                return False
            has_substance = any(
                isinstance(b, dict)
                and (
                    b.get("type") != "text"
                    or (isinstance(b.get("text"), str) and b.get("text").strip())
                )
                for b in content
            )
            return not has_substance
        if isinstance(content, str):
            return not content.strip()
        return False

    # ------------------------------------------------------------------
    # 记忆沉淀
    # ------------------------------------------------------------------

    def flush_consolidation(self, session_id: Optional[str] = None) -> Dict[str, int]:
        """强制触发记忆沉淀（不判断阈值）。

        用于会话切换 / 会话结束 / 前端手动触发等场景。若缓冲为空则跳过。
        """
        engine = getattr(self._orch, "consolidation_engine", None)
        if engine is None:
            logger.debug("ConsolidationEngine 未启用，跳过 flush")
            return {}
        try:
            return engine.force_consolidate(session_id=session_id)
        except Exception as e:
            logger.warning("flush consolidation 失败: %s", e)
            return {}

    async def trigger_consolidation(
        self, session_id: Optional[str] = None
    ) -> None:
        """触发记忆沉淀流程（异步，通过 to_thread 避免阻塞事件循环）。"""
        engine = getattr(self._orch, "consolidation_engine", None)
        if engine is None:
            logger.debug("ConsolidationEngine 未启用，跳过 consolidation")
            return
        try:
            await asyncio.to_thread(engine.consolidate, session_id=session_id)
        except Exception as e:
            logger.warning("consolidation 执行失败: %s", e)

    async def maybe_flush_on_switch(self, session_id: str) -> None:
        """会话切换时自动 flush 旧会话的沉淀。

        若 ``_last_session_id`` 与当前 ``session_id`` 不同，且
        ``consolidation_engine.pending_messages`` 非空，则调用
        :meth:`flush_consolidation` 强制沉淀上一个会话的对话。
        """
        engine = getattr(self._orch, "consolidation_engine", None)
        last_session_id = getattr(self._orch, "_last_session_id", None)
        if engine is None:
            self._orch._last_session_id = session_id
            return
        if (
            last_session_id is not None
            and last_session_id != session_id
            and engine.pending_messages
        ):
            logger.info(
                "检测到会话切换 %s -> %s，flush 旧会话的沉淀缓冲（%d 条消息）",
                last_session_id,
                session_id,
                engine.info_counter,
            )
            await asyncio.to_thread(self.flush_consolidation, session_id=last_session_id)
        self._orch._last_session_id = session_id
