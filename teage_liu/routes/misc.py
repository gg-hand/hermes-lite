"""misc 路由：静态页面、工具清单、审计日志、reasoning 开关、检索、重启、记忆冲刷。

Task 8: 从 server.py 迁移 12 个端点到此。
Task 10 (DI 重构): 改用 FastAPI Depends 注入 + 软重启改用容器 API，去除对 state 模块的依赖。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from teage_liu.app import (
    get_audit_logger,
    get_container_or_raise,
    get_orchestrator,
    get_session_logger,
    get_stream_manager,
)
from teage_liu.schemas.common import FlushResponse

logger = logging.getLogger("teage_liu.server")

router = APIRouter()

_WEB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web",
)


def _now_iso() -> str:
    return datetime.now().isoformat()


# 模块级变量，替代 state.soft_restart_in_progress
_soft_restart_in_progress = False


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
async def list_tools_inventory(orchestrator=Depends(get_orchestrator)):
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
def get_audit_logs(limit: int = Query(50, ge=1, le=1000),
                   audit_logger=Depends(get_audit_logger)):
    from teage_liu.config import load_config

    config_path = os.environ.get("TEAGE_CONFIG", "config.yaml")

    if audit_logger is None:
        return JSONResponse({"logs": []})
    try:
        config = load_config(config_path)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({"logs": []})
    except Exception:
        pass
    return JSONResponse({"logs": audit_logger.get_recent(limit)})


@router.get("/audit/since")
def get_audit_since(
    cursor: float = Query(0.0, ge=0.0, description="unix epoch seconds, 返回 timestamp >= cursor 的记录"),
    limit: int = Query(1000, ge=1, le=10000, description="返回的最大条数"),
    entry_type: Optional[str] = Query(None, description="可选记录类型过滤: tool_call / guardrail"),
    include_rotated: bool = Query(False, description="是否扫描 .rotated 轮转归档文件（全量读取场景）"),
    audit_logger=Depends(get_audit_logger),
):
    """增量读取审计日志。

    返回 audit.jsonl 中 timestamp >= cursor 的记录，按时间正序排列。
    调用方应保存响应中的 cursor 字段，下次请求时作为参数传入以获取新增条目。

    含等于语义：read_since(cursor) 会包含 timestamp == cursor 的记录，
    便于幂等重试。调用方需自行去重。

    include_rotated=True 时扫描所有 .rotated 轮转归档文件，用于全量
    读取场景（如首次初始化、数据迁移）。默认 False 只读当前文件。
    """
    from teage_liu.config import load_config

    config_path = os.environ.get("TEAGE_CONFIG", "config.yaml")

    if audit_logger is None:
        return JSONResponse({"entries": [], "cursor": cursor})
    try:
        config = load_config(config_path)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({"entries": [], "cursor": cursor})
    except Exception:
        pass
    entries = audit_logger.read_since(
        cursor, limit=limit, entry_type=entry_type, include_rotated=include_rotated
    )
    new_cursor = audit_logger.get_cursor()
    return JSONResponse({"entries": entries, "cursor": new_cursor})


# ---------- reasoning 开关 ----------

@router.post("/reasoning/toggle")
def reasoning_toggle(req: dict = Body(...),
                     orchestrator=Depends(get_orchestrator)):
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
def reasoning_status(orchestrator=Depends(get_orchestrator)):
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
    session_logger=Depends(get_session_logger),
):
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
def flush_consolidation(background_tasks: BackgroundTasks,
                        orchestrator=Depends(get_orchestrator)):
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
async def restart_server(request: Request,
                         container=Depends(get_container_or_raise)):
    """软重启：通过容器 API 重建 Orchestrator/health_checker/etl_engine + 重启 cron task。

    使用模块级 ``_soft_restart_in_progress`` 替代 ``state.soft_restart_in_progress``，
    使用 ``container.get()`` + ``container.set_instance()`` 替代 ``state.xxx = yyy``。
    """
    global _soft_restart_in_progress
    if _soft_restart_in_progress:
        raise HTTPException(status_code=409, detail="软重启已在进行中")
    _soft_restart_in_progress = True
    try:
        # 0. 简易配置校验
        from teage_liu.config import load_config, validate_required_env_vars
        config_path = os.environ.get("TEAGE_CONFIG", "config.yaml")
        new_config = load_config(config_path)
        try:
            validate_required_env_vars(new_config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"配置校验失败: {e}")

        # 1. 等待活跃流
        stream_manager = container.get("stream_manager")
        if stream_manager is not None:
            active = stream_manager.active_count()
            if active > 0:
                logger.info("软重启: 等待 %d 个进行中的流完成（最多 30s）...", active)
                for _ in range(30):
                    if stream_manager.active_count() == 0:
                        break
                    await asyncio.sleep(1)
                remaining = stream_manager.active_count()
                if remaining > 0:
                    logger.warning(
                        "软重启: %d 个流未在等待时间内完成，继续重启", remaining
                    )

        # 2. 关闭旧 Orchestrator
        old_orch = container.get("orchestrator")
        if old_orch is None:
            raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
        try:
            await asyncio.to_thread(old_orch.shutdown)
        except Exception as e:
            logger.warning("软重启: 旧 Orchestrator 关闭异常（已忽略）: %s", e)

        # 3. 手动创建新 Orchestrator
        from teage_liu.orchestrator import Orchestrator
        try:
            new_orch = await asyncio.to_thread(
                Orchestrator,
                config_path=config_path,
                metrics=container.get("metrics_collector"),
                audit_logger=container.get("audit_logger"),
                approval_manager=container.get("approval_manager"),
                task_manager=container.get("task_manager"),
            )
        except Exception as e:
            logger.exception("软重启: 新 Orchestrator 构建失败，服务不可用")
            raise HTTPException(
                status_code=500,
                detail=f"新 Orchestrator 构建失败: {e}",
            )

        # 4. 预热 ChromaDB
        if new_orch.chroma_store is not None:
            try:
                new_orch.chroma_store.query_memory("warmup", top_k=1, reinforce=False)
            except Exception as e:
                logger.warning("软重启: ChromaDB 预热失败: %s", e)

        # 5. 通过 set_instance 替换容器中的实例
        container.set_instance("orchestrator", new_orch)

        # 5.5 重建 health_checker（持有新 orchestrator 引用）
        from teage_liu.monitoring.health import HealthChecker
        new_hc = HealthChecker(
            orchestrator=new_orch,
            session_logger_global=container.get("session_logger"),
            mcp_manager=container.get("mcp_manager"),
            skill_loader=container.get("skill_loader"),
            metrics_collector=container.get("metrics_collector"),
            proposal_store=container.get("proposal_store"),
        )
        container.set_instance("health_checker", new_hc)

        # 5.6 重建 etl_engine（捕获新 orchestrator 的 chroma_store/llm_client）
        from teage_liu.app import _create_etl_engine
        new_etl = _create_etl_engine(
            container.config,
            upload_manager=container.get("upload_manager"),
            orchestrator=new_orch,
        )
        if new_etl is not None:
            container.set_instance("etl_engine", new_etl)

        # 6. 重启 cron_task（持有新 Orchestrator 引用）
        task_registry = request.app.state.task_registry
        cron_scheduler = container.get("cron_scheduler")
        if cron_scheduler is not None:
            await task_registry.restart(
                "cron",
                lambda: cron_scheduler.run_loop(new_orch)
            )

        logger.info("软重启: Orchestrator/health_checker/etl_engine 已替换，cron_task 已重启")
        return {"status": "ok", "message": "软重启完成"}

    finally:
        _soft_restart_in_progress = False
