"""外壳路由:最小 /chat + /chat/stream(SSE 是事件流编码器)。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()


def _get_pipeline(request: Request):
    return request.app.state.pipeline


def _get_session_locks(request: Request):
    return request.app.state.session_locks


@router.post("/chat")
async def chat(request: Request, body: Dict[str, Any]):
    """非流式对话:收集事件拼字符串返回。

    会话并发互斥(§18.7/B3 根治):同 session 对话经 session 级 asyncio.Lock
    串行化 —— 覆盖整个对话流,防交错读历史/交错落盘/丢消息。
    """
    pipeline = _get_pipeline(request)
    session_id = body.get("session_id")
    if not session_id:
        import uuid

        session_id = f"user_{uuid.uuid4().hex[:12]}"
    user_input = body.get("user_input", "")
    if not user_input:
        return JSONResponse(status_code=400, content={"error": "user_input 不能为空"})

    lock = await _get_session_locks(request).get(session_id)
    async with lock:
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
        # 会话并发互斥:锁覆盖整个 SSE 流(done/error 后释放)
        lock = await _get_session_locks(request).get(session_id)
        async with lock:
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


@router.post("/reload")
async def reload(request: Request):
    """热重载(§5 L-3 原子替换 + 回滚保旧链):supervisor.reload + pipeline 重绑钩子链。

    流程:清除配置缓存 → 重新加载 + core 段校验(失败拒绝) → supervisor.reload
    (spawn 新进程 → registry.rebuild 新链 setup 成功 → teardown 旧链 → 关闭旧进程;
    任一步失败回滚保旧链)→ pipeline.rebind_hooks。
    """
    from ..core.config import clear_config_cache, core_config_from, load_config

    pipeline = request.app.state.pipeline
    registry = request.app.state.registry
    supervisor = request.app.state.supervisor
    config_path = request.app.state.config_path
    build_host_declaration = request.app.state.build_host_declaration

    try:
        clear_config_cache()
        new_cfg = load_config(config_path)
        new_core_config = core_config_from(new_cfg)  # 配置非法 → 拒绝重载(保旧链)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"热重载配置非法,拒绝重载: {e}"},
        )
    try:
        new_chain = await supervisor.reload(
            new_cfg, registry, host_builder=build_host_declaration
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"热重载失败,已回滚保旧链: {e}"},
        )
    new_chain.hook_timeout = new_core_config.hook_timeout  # 保留配置的超时值
    pipeline.rebind_hooks(new_chain)
    return {
        "reloaded": True,
        "branches": [name for name, _ in registry.entries],
    }


@router.get("/health")
async def health():
    """健康检查(与项目健康检查约定对齐:curl -s http://127.0.0.1:8000/health)。"""
    return JSONResponse(content={"status": "ok"})


@router.get("/")
async def index():
    return JSONResponse(
        content={
            "name": "teage_liu2",
            "version": "0.1.0",
            "endpoints": [
                "POST /chat", "POST /chat/stream",
                "POST /reload", "GET /health",
            ],
        }
    )
