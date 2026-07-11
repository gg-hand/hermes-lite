"""misc 路由：静态页面、工具清单、审计日志、reasoning 开关、检索、重启、记忆冲刷。

Task 8: 从 server.py 迁移 12 个端点到此。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from schemas.common import FlushResponse

logger = logging.getLogger("hermes.server")

router = APIRouter()

_WEB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web",
)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _sync_to_server_globals(**kwargs):
    """同步 state 变更到 server 模块的全局变量（过渡期兼容）。

    routes 通过 state 模块访问共享状态，但 server.py 中尚未迁移的
    handler 仍使用模块级 global 变量。此函数确保 /restart 等修改
    state 的操作也同步更新 server 模块的全局变量。
    """
    for mod_name in ("server", "src.server"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            for key, value in kwargs.items():
                setattr(mod, key, value)


# ---------- 静态页面 ----------

@router.get("/")
def serve_index():
    index_path = os.path.join(_WEB_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="前端文件未找到")
    return FileResponse(
        index_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/chat")
def serve_chat():
    chat_path = os.path.join(_WEB_DIR, "chat.html")
    if not os.path.exists(chat_path):
        raise HTTPException(status_code=404, detail="对话页未找到")
    return FileResponse(
        chat_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/monitor")
def serve_monitor():
    monitor_path = os.path.join(_WEB_DIR, "monitor.html")
    if not os.path.exists(monitor_path):
        raise HTTPException(status_code=404, detail="监控页未找到")
    return FileResponse(
        monitor_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/scheduler")
def serve_scheduler():
    scheduler_path = os.path.join(_WEB_DIR, "scheduler.html")
    if not os.path.exists(scheduler_path):
        raise HTTPException(status_code=404, detail="调度页未找到")
    return FileResponse(
        scheduler_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/workflow")
def serve_workflow():
    workflow_path = os.path.join(_WEB_DIR, "workflow.html")
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow 页未找到")
    return FileResponse(
        workflow_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------- 工具清单 ----------

@router.get("/tools")
async def list_tools_inventory():
    import state

    orchestrator = state.orchestrator
    if orchestrator is None or orchestrator.tool_registry is None:
        return {"core": [], "deferred": [], "loaded": []}
    registry = orchestrator.tool_registry
    return {
        "core": list(registry._core_tools.keys()),
        "deferred": list(registry._deferred_tools.keys()),
        "loaded": list(registry._loaded_tools.keys()),
    }


# ---------- 审计日志 ----------

@router.get("/audit/logs")
def get_audit_logs(limit: int = Query(50, ge=1, le=1000)):
    import state
    from config import load_config

    config_path = os.environ.get("HERMES_CONFIG", "config.yaml")

    audit_logger = state.audit_logger
    if audit_logger is None:
        return JSONResponse({"logs": []})
    try:
        config = load_config(config_path)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({"logs": []})
    except Exception:
        pass
    return JSONResponse({"logs": audit_logger.get_recent(limit)})


# ---------- reasoning 开关 ----------

@router.post("/reasoning/toggle")
def reasoning_toggle(req: dict = Body(...)):
    import state

    orchestrator = state.orchestrator
    if orchestrator is None or orchestrator.llm_client is None:
        return JSONResponse(
            content={"error": "LLMClient 尚未初始化"},
            status_code=503,
        )
    enabled = bool(req.get("enabled", False))
    orchestrator.llm_client.main_reasoning_enabled = enabled
    logger.info(
        "reasoning 模式已切换: enabled=%s (main_reasoning_enabled)",
        enabled,
    )
    return {
        "enabled": enabled,
        "main": {
            "enabled": orchestrator.llm_client.main_reasoning_enabled,
            "effort": orchestrator.llm_client.main_reasoning_effort,
            "budget_tokens": orchestrator.llm_client.main_reasoning_budget_tokens,
        },
        "cron": {
            "enabled": orchestrator.llm_client.cron_reasoning_enabled,
            "effort": orchestrator.llm_client.cron_reasoning_effort,
        },
        "persist_thinking": orchestrator.llm_client.persist_thinking,
    }


@router.get("/reasoning/status")
def reasoning_status():
    import state

    orchestrator = state.orchestrator
    if orchestrator is None or orchestrator.llm_client is None:
        return JSONResponse(
            content={"error": "LLMClient 尚未初始化"},
            status_code=503,
        )
    return {
        "main": {
            "enabled": orchestrator.llm_client.main_reasoning_enabled,
            "effort": orchestrator.llm_client.main_reasoning_effort,
            "budget_tokens": orchestrator.llm_client.main_reasoning_budget_tokens,
        },
        "consolidation": {
            "enabled": False,
        },
        "cron": {
            "enabled": orchestrator.llm_client.cron_reasoning_enabled,
            "effort": orchestrator.llm_client.cron_reasoning_effort,
        },
        "persist_thinking": orchestrator.llm_client.persist_thinking,
    }


# ---------- 消息检索 ----------

@router.get("/recall")
def recall_messages(
    keyword: str = Query(..., description="搜索关键词"),
    session_id: Optional[str] = Query(None, description="按会话过滤"),
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
):
    import state

    session_logger = state.session_logger
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        results = session_logger.search_messages(keyword, session_id, limit)
        return {"results": results, "count": len(results)}
    except Exception as e:
        logger.exception("检索消息失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 记忆冲刷 ----------

@router.post("/consolidation/flush", response_model=FlushResponse)
def flush_consolidation(background_tasks: BackgroundTasks):
    import state

    orchestrator = state.orchestrator
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")

    pending_count = 0
    if orchestrator.consolidation_engine is not None:
        pending_count = orchestrator.consolidation_engine.info_counter

    background_tasks.add_task(orchestrator.flush_consolidation)

    msg = (
        f"已接受 flush 请求，后台执行中（待沉淀消息: {pending_count} 条）"
        if pending_count > 0
        else "缓冲区为空，无需 flush"
    )
    logger.info("POST /consolidation/flush: %s", msg)

    return FlushResponse(
        status="accepted",
        message=msg,
        pending_count=pending_count,
        timestamp=_now_iso(),
    )


# ---------- 软重启 ----------

@router.post("/restart")
async def restart_server():
    import state
    from config import load_config, validate_required_env_vars
    from orchestrator import Orchestrator

    config_path = os.environ.get("HERMES_CONFIG", "config.yaml")

    orchestrator = state.orchestrator
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")

    if state.soft_restart_in_progress:
        raise HTTPException(status_code=409, detail="软重启已在进行中")

    state.soft_restart_in_progress = True
    _sync_to_server_globals(_SOFT_RESTART_IN_PROGRESS=True)
    try:
        new_config = load_config(config_path)

        try:
            validate_required_env_vars(new_config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"配置校验失败: {e}")

        stream_manager = state.stream_manager
        if stream_manager is not None:
            active = stream_manager.active_count()
            if active > 0:
                logger.info(
                    "软重启: 等待 %d 个进行中的流完成（最多 30s）...", active
                )
                for _ in range(30):
                    if stream_manager.active_count() == 0:
                        break
                    await asyncio.sleep(1)
                remaining = stream_manager.active_count()
                if remaining > 0:
                    logger.warning(
                        "软重启: %d 个流未在等待时间内完成，继续重启", remaining
                    )

        old_orchestrator = orchestrator
        try:
            await asyncio.to_thread(old_orchestrator.shutdown)
        except Exception as e:
            logger.warning("软重启: 旧 Orchestrator 关闭异常（已忽略）: %s", e)

        try:
            new_orchestrator = await asyncio.to_thread(
                Orchestrator,
                config_path=config_path,
                metrics=state.metrics_collector,
                audit_logger=state.audit_logger,
                approval_manager=state.approval_manager,
                task_manager=state.task_manager,
            )
        except Exception as e:
            logger.exception("软重启: 新 Orchestrator 构建失败，服务不可用")
            raise HTTPException(
                status_code=500,
                detail=f"新 Orchestrator 构建失败，请手动 systemctl restart: {e}",
            )

        if new_orchestrator.chroma_store is not None:
            try:
                new_orchestrator.chroma_store.query_memory(
                    "warmup", top_k=1, reinforce=False
                )
            except Exception as e:
                logger.warning("软重启: ChromaDB 预热失败: %s", e)

        state.orchestrator = new_orchestrator
        _sync_to_server_globals(orchestrator=new_orchestrator)
        logger.info("软重启: Orchestrator 已替换为新实例")

        return {"status": "ok", "message": "软重启完成"}

    finally:
        state.soft_restart_in_progress = False
        _sync_to_server_globals(_SOFT_RESTART_IN_PROGRESS=False)
