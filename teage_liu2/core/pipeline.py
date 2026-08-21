"""主干装配:固定管线 + 钩子链(阶段 2 协议化)+ 历史落盘。

流程(§4.4 收口四连,与 SUBSYSTEM-SPI.md §1.1 一致):
    输入 → [① build_injections 注入声明收集]
         → [② before(注册序,action 立即应用;SetStop 短路 → done(intercepted))]
         → [③ 组装收口(注入分层叠加 → merge 相邻 user → 语义级校验)]
         → [④ 送 LLM(裸形态单步 / 循环形态)]
         → [after(逆序,终态钩子 action 一律忽略)] / [on_error]

职责:
- 构建 ContextSnapshot(不可变;历史 / 基础 system / 初始消息)
- 依序调用枝干钩子(build_injections → before → 收口 → LLM)
- 按配置选择形态:bare(单步)/ loop(React 循环,默认)
- 消费事件流,事件驱动消息级落盘(user 前置 / step_end 落盘 assistant /
  tool_result 聚合落盘 / finally 兜底 partial)
- done 后调用 after 钩子;失败/断连/拦截调用 on_error 钩子
- 事件流全程透传,持久化不影响流式输出
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from .assembler import finalize_conversation
from .errors import TERMINATION_INTERCEPTED, TERMINATION_NORMAL
from .event_stream import EventStream
from .history import HistoryStore
from .hooks import HookChain
from .injection import Injection
from .llm import LLMClient
from .loop import ReactLoop
from .modes import MODE_BARE, MODE_LOOP, BareMode
from .step import StepExecutor
from .types import (
    EV_DONE,
    EV_ERROR,
    EV_STEP_END,
    EV_STEP_START,
    EV_TEXT_DELTA,
    EV_TOOL_RESULT,
    AfterResponse,
    Block,
    Event,
    Message,
    Snapshot,
    extract_text,
    normalize_history,
    tool_result_block,
)

logger = logging.getLogger(__name__)

# M1 默认系统提示词(可从 config core.system_prompt 覆盖)
DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的 AI 助手。请用中文回答。"


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
        injection_budget: Optional[Dict[str, int]] = None,
        history_window_messages: int = 100,
        storage_writer: Optional[Any] = None,
        event_stream: Optional[EventStream] = None,
        session_store: Optional[Any] = None,
    ) -> None:
        self.llm_client = llm_client
        self.history_store = history_store
        self.hooks = hooks or HookChain()
        # L3 观测拦截点(阶段 3):事件流转处调用 event_stream.route_l3
        # (tool_use/tool_result/step_end 进 L3 旁路;L1/shell 直通事件不受影响)
        self.event_stream = event_stream
        # 会话态容器(§5 L-10):快照 extra 会话内延续的存取载体 —— 构建时从
        # SessionStore 恢复 extra 基座、对话结束写回最终 extra(外壳以 session
        # 级锁串行化同 session 并发,core 单快照无共享,§15 并发契约)
        self.session_store = session_store
        self.mode = mode
        self.base_system_prompt = base_system_prompt or DEFAULT_SYSTEM_PROMPT
        # 分层注入预算(I1):None → DEFAULT_LAYER_BUDGETS
        self.injection_budget = injection_budget
        # 历史读取窗口(D2):保留最近 N 条;token 预算归 M2 condenser
        self.history_window_messages = history_window_messages
        # StorageWriter 异步单写者(§18.1):全部 SQLite 写经写队列,
        # 根治同步落盘阻塞事件循环;None = 兼容同步直写(旧调用方/测试)
        self.storage_writer = storage_writer
        self.step = StepExecutor(llm_client)
        # E6:形态实例字典(bare/loop;新形态 = 新类 + 一行注册);
        # 未知 mode 构造即失败(启动失败,非运行时崩溃,Q8 根治)
        mode_instances = {
            MODE_BARE: BareMode(self.step),
            MODE_LOOP: ReactLoop(llm_client, self.hooks, max_loops=max_loops),
        }
        if mode not in mode_instances:
            raise ValueError(
                f"未知形态: {mode!r}(可选: {', '.join(sorted(mode_instances))})"
            )
        self._mode_instance = mode_instances[mode]

    async def chat_stream(
        self,
        session_id: str,
        user_input: str,
        system: Optional[str] = None,
        cancel_event: Optional[Any] = None,
    ) -> AsyncIterator[Event]:
        """对话入口(async generator,事件流)。

        落盘机制(事件驱动,消息级,D1):
        ① user 消息 —— 事件流开始前立即落盘(此后断连/LLM 失败都不丢用户输入)
        ② assistant —— step_end 到达时落盘(content_blocks JSON + 纯文本 + D3 字段)
        ③ tool_results —— 本轮 tool_result 缓冲,下轮 step_start 聚合落盘
           (配对 tool_use_id,重启重建配对结构)
        ④ 兜底 —— finally:断连/异常时已产出的部分文本与工具结果也落盘
        落盘失败不阻断对话(logger.error,持久化是旁路,不静默)。
        """
        # 1. 会话与历史
        #    顺序关键:先读历史,再落盘 user —— 若先落盘再读,本轮 user
        #    会混入 history 导致当前输入重复
        self.history_store.ensure_session(session_id)
        raw_history = self.history_store.get_session_messages(
            session_id, limit=self.history_window_messages
        ) or []
        # ① user 立即落盘(此后断连/LLM 失败都不丢;事件流开始时库中即可见)
        #    flush 语义 = await commit(§18.1:保"断连不丢输入"契约)
        await self._persist(session_id, "user", user_input, flush=True)
        history: List[Message] = normalize_history(raw_history)

        # 2. 构建 ContextSnapshot(不可变;revision=0,host 独占递增)
        #    会话态延续(§5 L-10):从 SessionStore 恢复同 session_id 的 extra
        #    作为快照 extra 基座(扩展经 SetExtra 写入的数据跨多次对话延续)
        extra_base: Dict[str, Any] = {}
        if self.session_store is not None:
            try:
                extra_base = dict(self.session_store.get(session_id))
            except Exception as e:
                logger.error("会话态 extra 恢复失败(会话 %s): %s", session_id, e)
        snapshot = Snapshot(
            session_id=session_id,
            user_input=user_input,
            round=0,
            started_at=datetime.now().isoformat(),
            history=history,
            system_text=system or self.base_system_prompt,
            messages=list(history) + [{"role": "user", "content": user_input}],
            extra=extra_base,
            revision=0,
        )

        # 3. 收口四连(§4.4):
        #    ① 注入声明收集(build_injections,注册序)
        #    ② before(注册序,action 立即应用;SetStop 短路 → done(intercepted))
        injections: List[Injection] = await self.hooks.build_injections_all(snapshot)
        snapshot, stop_reason = await self.hooks.before_all(snapshot)

        text_parts: List[str] = []
        assistant_persisted = False
        done_event: Optional[Event] = None
        error_message: Optional[str] = None
        # 工具结果缓冲:本轮 tool_result 聚合为一条 user(tool_results) 落盘
        tool_result_buffer: List[Block] = []

        async def flush_tool_results() -> None:
            """聚合落盘本轮 tool_results 为一条 user 消息(配对 tool_use_id)。"""
            if not tool_result_buffer:
                return
            blocks = list(tool_result_buffer)
            tool_result_buffer.clear()
            await self._persist(
                session_id, "user",
                extract_text(blocks),
                content_blocks=blocks,
                message_type="tool_results",
            )

        try:
            if stop_reason is not None:
                # SetStop 短路:跳过组装收口与 LLM,直接 done(intercepted,9 键齐整)
                yield {
                    "type": EV_DONE,
                    "session_id": session_id,
                    "response": "对话已被枝干拦截",
                    "messages": snapshot.messages,
                    "is_complete": False,
                    "termination_reason": TERMINATION_INTERCEPTED,
                    "usage": None,
                    "content_blocks": [],
                    "stop_reason": "end_turn",
                }
                return

            # ③ 组装收口(注入分层叠加 → merge 相邻 user → 语义级校验)
            effective_system, effective_messages, problem = finalize_conversation(
                injections, snapshot, budgets=self.injection_budget,
            )
            if problem:
                logger.error("消息序列不合法(会话 %s): %s", session_id, problem)
                yield {
                    "type": EV_ERROR,
                    "session_id": session_id,
                    "message": f"消息序列不合法: {problem}",
                }
                return

            # ④ 执行形态(bare 单步 / loop 循环),事件流统一处理
            #    事件源契约:必以 done 或 error 收尾(bare 由主干在 step_end 补发 done)
            async for ev in self._iter_events(
                snapshot, effective_messages, effective_system,
                cancel_event, session_id,
            ):
                etype = ev.get("type")
                if etype == EV_TEXT_DELTA:
                    text_parts.append(ev.get("text", ""))
                elif etype == EV_ERROR:
                    error_message = ev.get("message") or "LLM 调用失败"
                elif etype == EV_STEP_START:
                    # 上一轮工具结果聚合落盘(本轮开始 = 上轮工具轮次结束)
                    await flush_tool_results()
                elif etype == EV_STEP_END:
                    # ② assistant 消息级落盘(content_blocks + D3 字段)
                    blocks = ev.get("content_blocks", []) or []
                    usage = ev.get("usage") or {}
                    reasoning_text = "".join(
                        b.get("thinking", "")
                        for b in blocks
                        if isinstance(b, dict) and b.get("type") == "thinking"
                    )
                    if blocks:
                        await self._persist(
                            session_id, "assistant",
                            extract_text(blocks),
                            content_blocks=blocks,
                            token_count=int(usage.get("output_tokens", 0) or 0),
                            reasoning=reasoning_text or None,
                            message_type="assistant",
                        )
                        assistant_persisted = True
                elif etype == EV_TOOL_RESULT:
                    # ③ tool_result 缓冲(配对 tool_use_id,本轮末聚合落盘)
                    tool_result_buffer.append(
                        tool_result_block(
                            ev.get("tool_use_id", ""),
                            ev.get("result", ""),
                            is_error=bool(ev.get("is_error")),
                        )
                    )
                elif etype == EV_DONE:
                    done_event = ev
                    await flush_tool_results()
                    # response 优先采用事件源自带值(loop = 最后轮文本;
                    # bare = 空,主干补收集)。禁止全轮 text_delta 拼接覆盖
                    if not ev.get("response"):
                        ev["response"] = "".join(text_parts)
                    response_text = ev.get("response", "")
                    # assistant 已在 step_end 落盘;此处仅兜底无 step_end 的
                    # 收尾路径(拦截路径不落盘系统提示文本)
                    if (
                        response_text
                        and not assistant_persisted
                        and ev.get("termination_reason") != TERMINATION_INTERCEPTED
                    ):
                        await self._persist(
                            session_id, "assistant", response_text,
                            message_type="assistant",
                        )
                        assistant_persisted = True
                # L3 观测拦截点(§3.2/§18.5):tool_use/tool_result/step_end 进旁路;
                # L1 热路径/shell 直通事件由 route_l3 内部判定,旁路失败不阻断对话
                self._route_l3(ev)
                yield ev

            # 6. after 钩子(仅正常完成;断连/异常不触发;终态钩子 action 一律忽略)
            if done_event is not None:
                await self.hooks.after_all(
                    snapshot,
                    AfterResponse(
                        # 与 done.response 单一事实源一致(loop = 最后轮文本);
                        # 禁止全轮 text_delta 拼接(P2-1 回归锚定)
                        text=done_event.get("response")
                        or "".join(text_parts),
                        content_blocks=done_event.get("content_blocks", []) or [],
                        usage=done_event.get("usage"),
                        done_event=done_event,
                    ),
                )
        finally:
            # ④ 兜底:断连/异常时已产出的数据全部保留(工具结果 + 部分文本)
            await flush_tool_results()
            if not assistant_persisted:
                partial = "".join(text_parts)
                if partial:
                    logger.info("落盘兜底:保存部分输出(会话 %s, %d 字符)", session_id, len(partial))
                    await self._persist(
                        session_id, "assistant", partial, message_type="assistant"
                    )
            # E2:失败/断连/拦截 → on_error(正常完成不触发;记忆巩固等
            # 收尾只对完整对话生效;终态钩子 action 一律忽略)
            if done_event is None:
                await self.hooks.on_error_all(
                    snapshot, Exception(error_message or "对话未完成(中断/失败/拦截)")
                )
            # 会话态 extra 写回(§5 L-10):done/error 路径均写回最终快照 extra,
            # 跨同 session 多次对话延续。最终快照 = 形态实例推进后的终态
            # (loop 各推进点已更新 final_snapshot;bare/SetStop 短路 = 构建后快照)。
            self._persist_session_extra(
                session_id,
                getattr(self._mode_instance, "final_snapshot", None) or snapshot,
            )

    def _persist_session_extra(self, session_id: str, snapshot: Snapshot) -> None:
        """会话态 extra 写回(§5 L-10):对话结束(done/error 路径)时快照最终
        extra 写回 SessionStore,跨同 session 多次对话延续。

        失败仅 error 日志(会话态是旁路,不阻断对话);并发安全依赖外壳
        session 级锁串行化(§15 L-11,core 单快照无共享)。
        """
        if self.session_store is None:
            return
        try:
            store = self.session_store.get(session_id)
            store.clear()
            store.update(snapshot.extra or {})
        except Exception as e:
            logger.error("会话态 extra 写回失败(会话 %s): %s", session_id, e)

    def rebind_hooks(self, new_hooks: HookChain) -> None:
        """热重载后重绑钩子链(§5 L-3):registry.rebuild 生成新链,同步本实例引用。

        loop 形态内部持有 HookChain 引用,必须一并重绑,否则热重载不生效。
        """
        self.hooks = new_hooks
        mode_instance = getattr(self, "_mode_instance", None)
        if mode_instance is not None and hasattr(mode_instance, "hooks"):
            mode_instance.hooks = new_hooks
        logger.info("pipeline 钩子链已重绑(%d 个枝干)", len(new_hooks.branches))

    def _route_l3(self, event: Event) -> None:
        """L3 观测拦截点(阶段 3):事件 → event_stream.route_l3(异步旁路)。

        旁路失败仅 error 日志,绝不阻断主对话流(§3.2 不变量)。
        """
        if self.event_stream is None:
            return
        try:
            self.event_stream.route_l3(event)
        except Exception as e:
            logger.error("L3 观测拦截失败(旁路,不阻断对话): %s", e)

    async def _iter_events(
        self,
        snapshot: Snapshot,
        messages: List[Message],
        system: Optional[str],
        cancel_event: Optional[Any],
        session_id: str,
    ) -> AsyncIterator[Event]:
        """事件源:形态实例统一接口 run_stream,必以 done/error 收尾(E6)。"""
        async for ev in self._mode_instance.run_stream(
            snapshot,
            messages,
            system=system,
            cancel_event=cancel_event,
            session_id=session_id,
        ):
            yield ev

    async def _persist(
        self,
        session_id: str,
        role: str,
        content: str,
        content_blocks: Optional[List[Block]] = None,
        token_count: int = 0,
        reasoning: Optional[str] = None,
        message_type: Optional[str] = None,
        flush: bool = False,
    ) -> None:
        """落盘(持久化旁路):经 StorageWriter 异步单写者(§18.1)。

        - ``flush=True``:user 前置,await 到写线程 commit 完成(保"断连不丢输入")
        - ``flush=False``:background,入队即返(assistant / tool_result / 兜底)
        失败不阻断对话,但必须 error 级日志,不静默(§8 责任矩阵:落盘失败
        捕获层 persist,兜底 = 对话继续)。
        无 storage_writer(None,兼容旧调用方/测试)→ 同步直写。

        消息级(D1):content_blocks 供 LLM 重建,content 纯文本保 FTS。
        """
        def _do() -> None:
            self.history_store.log_message(
                session_id, role, content,
                content_blocks=content_blocks,
                token_count=token_count,
                reasoning=reasoning,
                message_type=message_type,
            )

        writer = self.storage_writer
        if writer is None:
            try:
                _do()
            except Exception as e:
                logger.error(
                    "落盘失败(session=%s, role=%s, %d 字符): %s",
                    session_id, role, len(content), e,
                )
            return
        try:
            if flush:
                await writer.enqueue_flush(_do)
            else:
                writer.enqueue_background(_do)
        except Exception as e:
            logger.error(
                "落盘入队失败(session=%s, role=%s, %d 字符): %s",
                session_id, role, len(content), e,
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
