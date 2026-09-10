"""外壳路由:最小 /chat + /chat/stream(SSE 是事件流编码器)+ 会话历史分页查询。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.transport import DEFAULT_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

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
        done_response: Optional[str] = None
        async for ev in pipeline.chat_stream(
            session_id, user_input, system=body.get("system")
        ):
            if ev.get("type") == "text_delta":
                text_parts.append(ev.get("text", ""))
            elif ev.get("type") == "done":
                done_reason = ev.get("termination_reason")
                # 响应单一事实源 = done.response(与流式口径一致):拦截路径
                # 无 text_delta,只有 done.response 有提示文案;loop 多轮的
                # 全量 text_delta 拼接会混入中间轮文本(P2-1 回归锚定)
                done_response = ev.get("response") or "".join(text_parts)
            elif ev.get("type") == "error":
                return JSONResponse(
                    status_code=502,
                    content={"error": ev.get("message", "LLM 调用失败"), "session_id": session_id},
                )
    return {
        "response": done_response or "".join(text_parts),
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
                # 事件 → SSE 帧(data: {json}\n\n)
                payload = json.dumps(ev, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                # 注意:done/error 后不得提前 return 关闭生成器 —— pipeline
                # 契约是收尾事件之后还执行 after/on_error 终态钩子(观测扩展
                # 落盘 audit.* 依赖);提前关闭会导致 after 永不触发。
                # pipeline 在钩子执行完自然结束,循环随即退出。

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    request: Request, session_id: str, limit: int = 30, before_id: Optional[int] = None
):
    """会话历史分页查询(前端回放)。

    - limit:单页条数(默认 30,上限 200),取**最近** N 条,正序返回;
    - before_id:向上翻页游标,只取 id < before_id 的更早消息;
    - has_more = 本页取满 → 前端据此决定是否继续展示"加载更早"。

    SQLite 同步 IO → asyncio.to_thread 包裹,不阻塞事件循环。
    """
    if limit < 1:
        limit = 1
    elif limit > 200:
        limit = 200
    store = _get_pipeline(request).history_store
    try:
        msgs = await asyncio.to_thread(
            store.get_session_messages, session_id, limit, before_id
        )
    except Exception as e:
        return JSONResponse(
            status_code=500, content={"error": f"历史读取失败: {e}"}
        )
    return {
        "session_id": session_id,
        "messages": msgs,
        "has_more": len(msgs) == limit,
    }


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
        # 统一扩展目录树(2026-09-08):重扫 extensions_root(新增/移除/manifest
        # 变更在此生效)+ 校验声明 + 合并 stdio 字段;失败 → 400 拒绝重载(保旧链)
        from ..core.extension_loader import make_directory_loader, wire_extensions

        new_cfg, new_specs, _disabled = wire_extensions(new_cfg)
        # loader 必须在 supervisor.reload(内部 registry.rebuild 消费 loader)之前就位;
        # 若后续 reload 失败回滚,旧链实例不受影响(loader 只在 build 时消费)
        registry.set_directory_loader(make_directory_loader(new_specs))
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
    # 同语言扩展身份重新注册(rebuild 产生新实例;新出现的扩展名在此入库,
    # 已移除的扩展身份残留在 bus 侧无副作用 —— kind 前缀按名隔离)
    from .app import register_inprocess_extension_identities

    register_inprocess_extension_identities(request.app.state.transport_bus, registry)
    # 同语言 observe 扩展的 L3 订阅重新接线(rebuild 产生新实例,覆盖同 name
    # 订阅;prev_names 使已移出链的扩展被退订,不误伤 supervisor 的 stdio 订阅)
    subscribe_l3 = request.app.state.subscribe_inprocess_l3
    if subscribe_l3 is not None:
        prev_names = getattr(request.app.state, "inprocess_l3_names", None)
        request.app.state.inprocess_l3_names = subscribe_l3(
            request.app.state.l3_sink, new_chain, prev_names=prev_names
        )
    return {
        "reloaded": True,
        # registry.entries 是 (Branch, 配置段) 元组表 —— 取 Branch.name,
        # 不能直接返回 Branch 实例(FastAPI 序列化会钻进 host_port/bus 触
        # _thread.lock 不可编码 → 500;reload 本身在此前已完成)
        "branches": [branch.name for branch, _ in registry.entries],
    }


@router.get("/health")
async def health(request: Request):
    """健康检查(与项目健康检查约定对齐:curl -s http://127.0.0.1:7878/health)。

    protocol_version 显式区分协议契约版本(PROTOCOL/ v1.0.0)与应用自身版本,
    避免与 app.version 混淆。
    storage_metrics 仅 stdio 代理后端存在时出现(降级优先:任何异常不致
    健康检查失败)。
    """
    payload: Dict[str, Any] = {
        "status": "ok",
        "protocol_version": DEFAULT_PROTOCOL_VERSION,
    }
    try:
        store = request.app.state.pipeline.history_store
        snap = getattr(store, "metrics_snapshot", None)
        if callable(snap):
            payload["storage_metrics"] = snap()
    except Exception as e:  # noqa: BLE001 - 降级:指标缺失不影响健康检查
        logger.debug("存储指标不可用(降级,已忽略): %s", e)
    return JSONResponse(content=payload)


@router.get("/")
async def index():
    return JSONResponse(
        content={
            "name": "teage_liu2",
            "version": "0.1.0",
            "protocol_version": DEFAULT_PROTOCOL_VERSION,
            "endpoints": [
                "POST /chat", "POST /chat/stream",
                "GET /sessions/{session_id}/messages",
                "POST /reload", "GET /health",
            ],
        }
    )
