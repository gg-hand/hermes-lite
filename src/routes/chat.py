"""chat 路由。从 server.py 提取。"""
from __future__ import annotations

import logging
import json
import os
import asyncio
import threading

from fastapi import APIRouter, HTTPException, FileResponse, StreamingResponse

from config import get_llm_timeouts
from config import load_config
from orchestrator import Orchestrator
from stream_manager import StreamManager, StreamCancelled
from stream_manager import StreamCancelled
from breakpoint_detector import BreakpointDetector
from llm.client import ActivityTimeout
from storage.sqlite_log import SessionLogger

from schemas.chat import ChatRequest, CancelRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
orchestrator = None
session_logger = None
stream_manager = None

# 常量(从 server.py 复制)
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """对话接口。

    流程:
        1. session_id 为空时调用 session_logger.create_session() 新建；
        2. 调用 orchestrator.chat(session_id, message) 获取回复；
        3. 返回会话 ID、回复与时间戳。
    异常时返回 500。
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")

    # 校验消息非空
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    try:
        session_id = req.session_id
        if not session_id:
            session_id = session_logger.create_session()
            logger.info("新建会话: %s", session_id)

        # Phase 9+ 取消支持：为非流式 /chat 创建 cancel_event
        cancel_event = None
        if stream_manager is not None:
            cancel_event = stream_manager.register(session_id)

        try:
            response_text = await orchestrator.chat(
                session_id, req.message, cancel_event=cancel_event,
            )
        finally:
            if stream_manager is not None and cancel_event is not None:
                stream_manager.unregister_event(session_id, cancel_event)

        return ChatResponse(
            session_id=session_id,
            response=response_text,
            timestamp=_now_iso(),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("处理 /chat 请求失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")

@router.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """流式对话接口（Server-Sent Events）。

    与 :http:post:`/chat` 等价，但通过 SSE 实时推送 LLM 文本增量与
    工具调用事件，前端可逐字渲染。

    SSE 事件格式（每行 ``data: <json>\\n\\n``）：
        - ``{"type": "session", "session_id": str, "timestamp": str}``：
          会话信息（首个事件，告知前端 session_id）。
        - ``{"type": "text", "text": str}``：
          LLM 输出文本增量。
        - ``{"type": "tool", "name": str, "input": dict, "result": str, "is_error": bool}``：
          工具调用事件。
        - ``{"type": "approval_request", "approval_id": str, "tool_name": str, "tool_input": dict, "reason": str, "risk_level": str}``：
          HIL 审批请求事件，前端应弹出审批确认框并调用
          ``POST /approvals/{approval_id}/resolve`` 提交决定。
        - ``{"type": "approval_resolved", "approval_id": str, "decision": str, "reason": str}``：
          审批决定事件，``decision`` 为 approve/deny。
        - ``{"type": "round_start", "loop_idx": int}``：
          ReactLoop 每轮循环开始事件，前端应为此轮创建独立的 streamMsg，
          避免多轮文本被 done.response 覆盖丢失。
        - ``{"type": "todo_init", "session_id": str, "todo": dict}``：
          plan_task 工具执行后事件，含完整 todo 列表（goal/steps/completed）。
        - ``{"type": "todo_update", "session_id": str, "todo": dict}``：
          update_todo 工具执行后事件，todo 字段为变更后的完整列表。
        - ``{"type": "todo_complete", "session_id": str, "todo": dict}``：
          所有 step 完成后事件，标记 plan 整体完成。
        - ``{"type": "done", "response": str, "timestamp": str}``：
          整个对话结束事件。
        - ``{"type": "error", "message": str}``：
          错误事件（流中途异常）。

    响应 Content-Type 为 ``text/event-stream``。
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")

    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    # 校验 / 创建 session_id（在生成器外完成，便于失败时直接返回 HTTP 错误）
    session_id = req.session_id
    if not session_id:
        try:
            session_id = session_logger.create_session()
            logger.info("新建会话 (stream): %s", session_id)
        except Exception as e:
            logger.exception("创建会话失败: %s", e)
            raise HTTPException(status_code=500, detail=f"创建会话失败: {e}")

    async def async_event_generator():
        """SSE 异步事件生成器（带 5min 硬超时控制）。

        客户端断连时 FastAPI 会调 generator.aclose() → 注入 GeneratorExit，
        try/finally 保证 StreamManager 资源释放。
        """
        cancel_event: Optional[threading.Event] = None
        if stream_manager is not None:
            cancel_event = stream_manager.register(session_id)
        breakpoint_detector = BreakpointDetector()
        accumulated_text = ""

        try:
            # 首个事件：会话信息
            yield _sse_event({
                "type": "session",
                "session_id": session_id,
                "timestamp": _now_iso(),
            })

            try:
                # 每次请求读取最新配置，支持 llm.activity_timeout /
                # llm.stream_total_timeout 热更新（无需重启即时生效）
                _req_cfg = load_config(CONFIG_PATH)
                _activity_timeout, _stream_total_timeout = get_llm_timeouts(_req_cfg)
                # 逐事件超时：每个事件完成后重置 _stream_total_timeout 倒计时，
                # 避免多轮工具调用的累计耗时超过单次硬限。
                # 只有 LLM 或工具真正卡住（300s 无任何事件）才会触发超时。
                _chat_stream = orchestrator.chat_stream(
                    session_id,
                    req.message,
                    cancel_event=cancel_event,
                    is_cron=False,
                    stream_manager=stream_manager,
                ).__aiter__()
                while True:
                    try:
                        event = await asyncio.wait_for(
                            _chat_stream.__anext__(),
                            timeout=_stream_total_timeout,
                        )
                    except StopAsyncIteration:
                        break

                    etype = event.get("type")

                    # graceful 模式断点检测（在 orchestrator 事件之间）
                    if etype == "text":
                        accumulated_text += event.get("text", "")
                    if (
                        stream_manager is not None
                        and stream_manager.is_graceful_pending(session_id)
                        and breakpoint_detector.should_break(accumulated_text)
                    ):
                        # 到达自然断点，触发中断
                        if cancel_event is not None:
                            cancel_event.set()
                        # 保存 InterruptNotice（含用户新消息）
                        graceful_msg = stream_manager.pop_graceful_message(
                            session_id
                        )
                        if orchestrator is not None:
                            orchestrator._save_interrupt_notice(
                                session_id, graceful_msg
                            )

                    # 中断检测
                    if cancel_event is not None and cancel_event.is_set():
                        yield _sse_event({"type": "interrupt"})
                        return

                    if etype == "done":
                        # done 事件补充 timestamp，透传 usage/reasoning_stats 等
                        # 白名单字段，不透传 messages（体积大）
                        done_evt = {
                            "type": "done",
                            "response": event.get("response", ""),
                            "timestamp": _now_iso(),
                            "is_complete": event.get("is_complete", True),
                            "termination_reason": event.get("termination_reason", "normal"),
                        }
                        # usage 字段透传（含 reasoning_tokens）
                        usage = event.get("usage")
                        if usage is not None:
                            done_evt["usage"] = usage
                        # reasoning_stats 字段透传（effort/budget/reasoning_tokens）
                        reasoning_stats = event.get("reasoning_stats")
                        if reasoning_stats is not None:
                            done_evt["reasoning_stats"] = reasoning_stats
                        # content_blocks 字段透传（含 thinking block，保留原始顺序）
                        content_blocks = event.get("content_blocks")
                        if content_blocks is not None:
                            done_evt["content_blocks"] = content_blocks
                        # stop_reason 字段透传
                        stop_reason = event.get("stop_reason")
                        if stop_reason is not None:
                            done_evt["stop_reason"] = stop_reason
                        yield _sse_event(done_evt)
                    elif etype == "reasoning":
                        # reasoning 增量事件原样透传（含 text/signature）
                        yield _sse_event(event)
                    else:
                        # text / tool / approval_request / approval_resolved /
                        # round_start / todo_init / todo_update / todo_complete
                        # 事件原样透传
                        yield _sse_event(event)
            except asyncio.TimeoutError:
                logger.warning(
                    "流式对话超时（%ss 无事件），session=%s",
                    _stream_total_timeout, session_id,
                )
                yield _sse_event({"type": "error", "message": "请求超时，请重试"})
                yield _sse_event({"type": "interrupt"})
            except StreamCancelled:
                logger.info("流被用户中断: session=%s", session_id)
                yield _sse_event({"type": "interrupt"})
            except ActivityTimeout:
                # per-token 活跃超时：LLM 在 activity_timeout 秒内未返任何 token
                logger.warning(
                    "LLM 响应超时（%ss 无输出），session=%s",
                    _activity_timeout, session_id,
                )
                yield _sse_event({
                    "type": "error",
                    "message": f"LLM 响应超时（{_activity_timeout}s 无输出）",
                })
                yield _sse_event({"type": "interrupt"})
            except Exception as e:
                logger.exception("流式对话失败: %s", e)
                yield _sse_event({"type": "error", "message": str(e)})
        finally:
            # 保证 GeneratorExit（客户端断连）/ 正常退出 / 异常都执行清理
            # Phase 9+ 所有权感知：使用 unregister_event 防止误删新流的 event
            if stream_manager is not None and cancel_event is not None:
                stream_manager.unregister_event(session_id, cancel_event)

    return StreamingResponse(
        async_event_generator(),
        media_type="text/event-stream",
        headers={
            # 禁用 nginx / 代理缓冲，确保实时推送
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

@router.post("/chat/cancel")
async def cancel_stream(req: CancelRequest):
    """中断正在进行的流式对话。

    模式：
    - ``immediate``：立即设置 cancel_event，LLM 输出在下一个 token 处停止
    - ``graceful``（首次）：暂存用户新消息，等待自然断点后自动中断
    - ``graceful``（二次）：已有 graceful pending 时再次调用 → force kill
      当前运行中的子进程（如 bash_exec 下载），立即中断并注入用户新消息

    返回 ``status`` 字段：
    - ``"cancelling"``：已设置取消信号
    - ``"breakpoint_pending"``：graceful 模式，已暂存消息，等待断点
    - ``"force_killed"``：二次 graceful，已强杀进程并注入消息
    - ``"already_ended"``：会话不存在或已结束（幂等）
    """
    if stream_manager is None:
        raise HTTPException(status_code=503, detail="StreamManager 尚未初始化")

    if req.mode == "graceful" and req.new_message:
        if stream_manager.is_graceful_pending(req.session_id):
            # 第二次点击 → force kill
            status, msg = stream_manager.force_cancel(req.session_id)
            if msg and orchestrator is not None:
                # 强杀当前子进程
                try:
                    from .agent.builtin_tools import kill_running_process
                    kill_running_process()
                except ImportError:
                    pass
                orchestrator._save_interrupt_notice(req.session_id, msg)
                logger.info(
                    "Force killed: session=%s, message injected", req.session_id
                )
            return {"status": "force_killed", "session_id": req.session_id}
        else:
            # 第一次点击 → graceful 等待
            stream_manager.register_graceful(req.session_id, req.new_message)
            logger.info("Graceful cancel pending: session=%s", req.session_id)
            return {"status": "breakpoint_pending", "session_id": req.session_id}

    ok = stream_manager.cancel(req.session_id)
    # immediate 模式主路径：trigger_cancel 调用注册的 cancel_callback
    # （``await stream.close()``）主动断开 LLM HTTP 连接，不等下一个 token。
    # 未注册 callback（流未启动或已结束）时返回 False，降级为仅 cancel_event
    # 兜底路径（在下一个 chunk 到达时检测 cancel_event.is_set()）。
    await stream_manager.trigger_cancel(req.session_id)
    status = "cancelling" if ok else "already_ended"
    logger.info("Cancel stream: session=%s -> %s", req.session_id, status)
    return {"status": status, "session_id": req.session_id}

@router.get("/chat")
def serve_chat():
    """提供对话页（与展示首页分离后的交互页面）。"""
    chat_path = os.path.join(_WEB_DIR, "chat.html")
    if not os.path.exists(chat_path):
        raise HTTPException(status_code=404, detail="对话页未找到")
    return FileResponse(
        chat_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
