"""主干装配:固定管线 + 钩子链 + 历史落盘。

流程(与 SUBSYSTEM-SPI.md §1.1 一致):
    输入 → [钩子链 before] → LLM(裸形态单步 / 循环形态) → [钩子链 after] → 输出

职责:
- 构建 BranchContext(历史 / 基础 system)
- 依序调用枝干钩子(build_system → build_injection → before)
- 按配置选择形态:bare(单步)/ loop(React 循环,默认)
- 消费事件流,在 done 前持久化 user / assistant 消息(历史落盘)
- done 后调用 after 钩子
- 事件流全程透传,持久化不影响流式输出

M1 边界:
- 会话标题首轮异步生成:后置(update_session_title 接口已就绪)
- after 钩子粒度:M1 为整个对话一次;M2 细化到每步
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from .history import HistoryStore
from .hooks import BranchContext, HookChain
from .llm import LLMClient
from .loop import ReactLoop
from .step import StepExecutor
from .types import (
    EV_DONE,
    EV_ERROR,
    EV_STEP_END,
    EV_TEXT_DELTA,
    TERMINATION_INTERCEPTED,
    TERMINATION_NORMAL,
    Event,
    Message,
    normalize_history,
)

logger = logging.getLogger(__name__)

# M1 默认系统提示词(可从 config core.system_prompt 覆盖)
DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的 AI 助手。请用中文回答。"

# 形态常量
MODE_BARE = "bare"     # 单次 LLM 调用,无循环
MODE_LOOP = "loop"     # React 循环(默认)


class ChatPipeline:
    """主干:零枝干可跑;枝干经 HookChain 注册,此处代码不再改动。"""

    def __init__(
        self,
        llm_client: LLMClient,
        history_store: HistoryStore,
        hooks: Optional[HookChain] = None,
        mode: str = MODE_LOOP,
        max_loops: int = 50,
        base_system_prompt: Optional[str] = None,
    ) -> None:
        self.llm_client = llm_client
        self.history_store = history_store
        self.hooks = hooks or HookChain()
        self.mode = mode
        self.base_system_prompt = base_system_prompt or DEFAULT_SYSTEM_PROMPT
        self.step = StepExecutor(llm_client)
        if mode == MODE_LOOP:
            self.loop = ReactLoop(
                llm_client, self.hooks, max_loops=max_loops
            )
        else:
            self.loop = None

    async def chat_stream(
        self,
        session_id: str,
        user_input: str,
        system: Optional[str] = None,
        cancel_event: Optional[Any] = None,
    ) -> AsyncIterator[Event]:
        """对话入口(async generator,事件流)。"""
        # 1. 会话与历史
        self.history_store.ensure_session(session_id)
        raw_history = self.history_store.get_session_messages(session_id) or []
        history: List[Message] = normalize_history(raw_history)

        # 2. 构建 BranchContext
        ctx = BranchContext(session_id, user_input)
        ctx.history = history
        ctx.system_text = system or self.base_system_prompt

        # 3. 钩子:build_system(稳定前缀)→ build_injection(动态注入)→ before
        await self.hooks.build_system_all(ctx)
        injection = await self.hooks.build_injection_all(ctx)
        ctx.messages = list(history) + [{"role": "user", "content": user_input}]
        await self.hooks.before_all(ctx)

        if ctx.stop:
            # 枝干拦截:不调 LLM,直接 done
            yield {
                "type": EV_DONE,
                "session_id": session_id,
                "response": "对话已被枝干拦截",
                "messages": ctx.messages,
                "is_complete": False,
                "termination_reason": TERMINATION_INTERCEPTED,
            }
            return

        # 4. 注入文本(如有)前置到 messages[0](缓存失效区)
        effective_messages: List[Message] = ctx.messages
        if injection:
            effective_messages = [
                {"role": "user", "content": injection},
                *ctx.messages,
            ]

        # 5. 执行形态(bare 单步 / loop 循环),收集事件并持久化
        response_text = ""
        done_event: Optional[Event] = None
        buffered: List[Event] = []

        if self.mode == MODE_BARE:
            # bare 形态:透传 step 事件,在 step_end 后由主干补发 done
            # (统一事件流契约:每次对话必以 done 收尾,loop 形态由 loop 发)
            text_parts: List[str] = []
            for ev in [e async for e in self.step.execute(
                effective_messages,
                tools=None,
                system=ctx.effective_system,
                cancel_event=cancel_event,
                session_id=session_id,
            )]:
                buffered.append(ev)
                if ev.get("type") == EV_TEXT_DELTA:
                    text_parts.append(ev.get("text", ""))
                elif ev.get("type") == EV_STEP_END:
                    response_text = "".join(text_parts)
                    stop_reason = ev.get("stop_reason", "end_turn")
                    done_event = {
                        "type": EV_DONE,
                        "session_id": session_id,
                        "response": response_text,
                        "messages": effective_messages,
                        "is_complete": stop_reason == "end_turn",
                        "termination_reason": TERMINATION_NORMAL,
                        "usage": ev.get("usage"),
                        "content_blocks": ev.get("content_blocks", []),
                        "stop_reason": stop_reason,
                    }
                yield ev
            if done_event:
                buffered.append(done_event)
                yield done_event
        else:
            async for ev in self.loop.run_stream(
                ctx, effective_messages,
                system=ctx.effective_system,
                cancel_event=cancel_event,
            ):
                buffered.append(ev)
                if ev.get("type") == EV_DONE:
                    done_event = ev
                    response_text = ev.get("response", "") or ""
                elif ev.get("type") == EV_ERROR:
                    response_text = ""
                yield ev

        # 6. 持久化(user + assistant;M1 不持久化 tool 消息,工具枝干 M2 带回)
        self.history_store.log_message(session_id, "user", user_input)
        if response_text:
            self.history_store.log_message(session_id, "assistant", response_text)

        # 7. after 钩子(整个对话一次;M2 细化到每步)
        await self.hooks.after_all(
            ctx, {"text": response_text, "done_event": done_event}
        )

    # ------------------------------------------------------------------
    # 便捷同步入口(收集事件拼字符串,供 CLI / 测试使用)
    # ------------------------------------------------------------------
    async def chat(
        self,
        session_id: str,
        user_input: str,
        system: Optional[str] = None,
        cancel_event: Optional[Any] = None,
    ) -> str:
        """非流式:收集事件返回最终文本。"""
        text_parts: List[str] = []
        async for ev in self.chat_stream(
            session_id, user_input, system=system, cancel_event=cancel_event
        ):
            if ev.get("type") == "text_delta":
                text_parts.append(ev.get("text", ""))
        return "".join(text_parts)
