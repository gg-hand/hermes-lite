"""chat 路由：对话接口（非流式、流式 SSE、取消）。

从 server.py 迁移 3 个端点：
- POST /chat          非流式对话
- POST /chat/stream   流式对话（Server-Sent Events）
- POST /chat/cancel   中断流式对话
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from teage_liu.app import get_orchestrator, get_session_logger, get_stream_manager
from teage_liu.schemas.chat import ChatRequest, CancelRequest, ChatResponse

logger = logging.getLogger("teage_liu.server")

router = APIRouter()

# ---------- 可选依赖（与 server.py 一致的 try/except 降级）----------

from teage_liu.config import get_llm_timeouts, load_config
from teage_liu.stream_manager import StreamCancelled
from teage_liu.breakpoint_detector import BreakpointDetector
from teage_liu.llm.client import ActivityTimeout
def _now_iso() -> str:
    """返回当前时间的 ISO 格式字符串。"""
    return datetime.now().isoformat()


def _sse_event(data: dict) -> str:
    """将 dict 序列化为 SSE 事件字符串（``data: <json>\\n\\n``）。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _get_config_path() -> str:
    """获取 CONFIG_PATH（兼容测试 patch src.server.CONFIG_PATH）。"""
    server_mod = sys.modules.get("teage_liu.server") or sys.modules.get("server")
    if server_mod is not None:
        return getattr(server_mod, "CONFIG_PATH", "config.yaml")
    return "config.yaml"


def _main_session_collab_guidance(session_id: Optional[str]) -> Optional[str]:
    """主会话协作引导段（条件注入，extra_system_prompt）。

    仅当全部满足时返回引导文本，否则返回 None（主 SYSTEM_PROMPT 不变）：
    - ``multiagent.enabled`` 且 ``main_session_collab.enabled`` 且 ``inject_prompt``
    - ``a2a.enabled``（工具已注册，避免引导存在但无工具可用）
    - session_id 非 ``cron:`` / ``multiagent_`` 前缀（协作/调度会话不注入）
    """
    try:
        config = load_config(_get_config_path())
        multiagent_cfg = config.get("multiagent", {}) or {}
        if not multiagent_cfg.get("enabled"):
            return None
        msc = multiagent_cfg.get("main_session_collab", {}) or {}
        if not msc.get("enabled", False) or not msc.get("inject_prompt", True):
            return None
        a2a_cfg = config.get("a2a", {}) or {}
        if not a2a_cfg.get("enabled"):
            return None
        if session_id and (
            session_id.startswith("cron:") or session_id.startswith("multiagent_")
        ):
            return None
        from teage_liu.llm.prompts import build_main_session_collab_guidance

        return build_main_session_collab_guidance(msc)
    except Exception:
        return None


# ---------- 端点 ----------


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest,
               orchestrator=Depends(get_orchestrator),
               session_logger=Depends(get_session_logger),
               stream_manager=Depends(get_stream_manager)):
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

    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    try:
        session_id = req.session_id
        if not session_id:
            session_id = session_logger.create_session()
            logger.info("新建会话: %s", session_id)

        cancel_event = None
        if stream_manager is not None:
            cancel_event = stream_manager.register(session_id)

        try:
            response_text = await orchestrator.chat(
                session_id, req.message, cancel_event=cancel_event,
                extra_system_prompt=_main_session_collab_guidance(session_id),
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
def chat_stream(req: ChatRequest,
                orchestrator=Depends(get_orchestrator),
                session_logger=Depends(get_session_logger),
                stream_manager=Depends(get_stream_manager)):
    """流式对话接口（Server-Sent Events）。

    与 :http:post:`/chat` 等价，但通过 SSE 实时推送 LLM 文本增量与
    工具调用事件，前端可逐字渲染。
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")

    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

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
        breakpoint_detector = BreakpointDetector() if BreakpointDetector is not None else None
        accumulated_text = ""

        try:
            yield _sse_event({
                "type": "session",
                "session_id": session_id,
                "timestamp": _now_iso(),
            })

            try:
                _req_cfg = load_config(_get_config_path()) if load_config is not None else {}
                if get_llm_timeouts is not None:
                    _activity_timeout, _stream_total_timeout = get_llm_timeouts(_req_cfg)
                else:
                    _activity_timeout, _stream_total_timeout = 300, 300

                _chat_stream = orchestrator.chat_stream(
                    session_id,
                    req.message,
                    cancel_event=cancel_event,
                    is_cron=False,
                    stream_manager=stream_manager,
                    extra_system_prompt=_main_session_collab_guidance(session_id),
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

                    if etype == "text":
                        accumulated_text += event.get("text", "")
                    if (
                        stream_manager is not None
                        and stream_manager.is_graceful_pending(session_id)
                        and breakpoint_detector is not None
                        and breakpoint_detector.should_break(accumulated_text)
                    ):
                        if cancel_event is not None:
                            cancel_event.set()
                        graceful_msg = stream_manager.pop_graceful_message(
                            session_id
                        )
                        if orchestrator is not None:
                            orchestrator.msg_persistence.save_interrupt_notice(
                                session_id, graceful_msg
                            )

                    if cancel_event is not None and cancel_event.is_set():
                        yield _sse_event({"type": "interrupt"})
                        return

                    if etype == "done":
                        done_evt = {
                            "type": "done",
                            "response": event.get("response", ""),
                            "timestamp": _now_iso(),
                            "is_complete": event.get("is_complete", True),
                            "termination_reason": event.get("termination_reason", "normal"),
                        }
                        usage = event.get("usage")
                        if usage is not None:
                            done_evt["usage"] = usage
                        reasoning_stats = event.get("reasoning_stats")
                        if reasoning_stats is not None:
                            done_evt["reasoning_stats"] = reasoning_stats
                        content_blocks = event.get("content_blocks")
                        if content_blocks is not None:
                            done_evt["content_blocks"] = content_blocks
                        stop_reason = event.get("stop_reason")
                        if stop_reason is not None:
                            done_evt["stop_reason"] = stop_reason
                        yield _sse_event(done_evt)
                    elif etype == "reasoning":
                        yield _sse_event(event)
                    else:
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
                err_str = str(e)
                err_type = type(e).__name__
                if "401" in err_str or "Authentication" in err_type:
                    friendly = "API Key 无效或已过期，请在设置中检查 LLM API Key 配置"
                elif "429" in err_str:
                    friendly = "请求频率过高，请稍后重试"
                elif "529" in err_str or "overloaded" in err_str.lower():
                    friendly = "LLM 服务过载，请稍后重试"
                elif "timeout" in err_str.lower() or "Timeout" in err_type:
                    friendly = "请求超时，请检查网络后重试"
                else:
                    friendly = err_str
                logger.exception("流式对话失败: %s", e)
                yield _sse_event({"type": "error", "message": friendly, "reason": err_str})
        finally:
            if stream_manager is not None and cancel_event is not None:
                stream_manager.unregister_event(session_id, cancel_event)

    return StreamingResponse(
        async_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/chat/cancel")
async def cancel_stream(req: CancelRequest,
                        stream_manager=Depends(get_stream_manager),
                        orchestrator=Depends(get_orchestrator)):
    """中断正在进行的流式对话。

    模式：
    - ``immediate``：立即设置 cancel_event，LLM 输出在下一个 token 处停止
    - ``graceful``（首次）：暂存用户新消息，等待自然断点后自动中断
    - ``graceful``（二次）：已有 graceful pending 时再次调用 → force kill
      当前运行中的子进程（如 bash_exec 下载），立即中断并注入用户新消息
    """
    if stream_manager is None:
        raise HTTPException(status_code=503, detail="StreamManager 尚未初始化")

    if req.mode == "graceful" and req.new_message:
        if stream_manager.is_graceful_pending(req.session_id):
            status, msg = stream_manager.force_cancel(req.session_id)
            if msg and orchestrator is not None:
                try:
                    from teage_liu.agent.tools.shell_tools import kill_running_process
                    kill_running_process()
                except ImportError:
                    pass
                orchestrator.msg_persistence.save_interrupt_notice(req.session_id, msg)
                logger.info(
                    "Force killed: session=%s, message injected", req.session_id
                )
            return {"status": "force_killed", "session_id": req.session_id}
        else:
            stream_manager.register_graceful(req.session_id, req.new_message)
            logger.info("Graceful cancel pending: session=%s", req.session_id)
            return {"status": "breakpoint_pending", "session_id": req.session_id}

    ok = stream_manager.cancel(req.session_id)
    await stream_manager.trigger_cancel(req.session_id)
    status = "cancelling" if ok else "already_ended"
    logger.info("Cancel stream: session=%s -> %s", req.session_id, status)
    return {"status": status, "session_id": req.session_id}
