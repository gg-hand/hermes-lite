"""外壳路由:最小 /chat + /chat/stream(SSE 是事件流编码器)。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()


def _get_pipeline(request: Request):
    return request.app.state.pipeline


@router.post("/chat")
async def chat(request: Request, body: Dict[str, Any]):
    """非流式对话:收集事件拼字符串返回。"""
    pipeline = _get_pipeline(request)
    session_id = body.get("session_id")
    if not session_id:
        import uuid

        session_id = f"user_{uuid.uuid4().hex[:12]}"
    user_input = body.get("user_input", "")
    if not user_input:
        return JSONResponse(status_code=400, content={"error": "user_input 不能为空"})

    text_parts: list[str] = []
    done_reason: Optional[str] = None
    async for ev in pipeline.chat_stream(
        session_id, user_input, system=body.get("system")
    ):
        if ev.get("type") == "text_delta":
            text_parts.append(ev.get("text", ""))
        elif ev.get("type") == "done":
            done_reason = ev.get("termination_reason")
        elif ev.get("type") == "error":
            return JSONResponse(
                status_code=502,
                content={"error": ev.get("message", "LLM 调用失败"), "session_id": session_id},
            )
    return {
        "response": "".join(text_parts),
        "session_id": session_id,
        "termination_reason": done_reason,
    }


@router.post("/chat/stream")
async def chat_stream(request: Request, body: Dict[str, Any]):
    """流式对话(SSE):事件编码为 data: JSON 帧。"""
    pipeline = _get_pipeline(request)
    session_id = body.get("session_id")
    if not session_id:
        import uuid

        session_id = f"user_{uuid.uuid4().hex[:12]}"
    user_input = body.get("user_input", "")
    if not user_input:
        return JSONResponse(status_code=400, content={"error": "user_input 不能为空"})

    async def event_source():
        async for ev in pipeline.chat_stream(
            session_id, user_input, system=body.get("system")
        ):
            # 事件 → SSE 帧(data: {json}\n\n);done/error 后结束
            payload = json.dumps(ev, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            if ev.get("type") in ("done", "error"):
                return

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/")
async def index():
    return JSONResponse(
        content={
            "name": "teage_liu2",
            "version": "0.1.0",
            "endpoints": ["POST /chat", "POST /chat/stream"],
        }
    )
