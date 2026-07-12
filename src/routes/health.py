"""health 路由：健康检查、监控指标、信号池仪表盘。

Task 9: 从 server.py 迁移 5 个端点。
Task 8 (DI 重构): 改用 FastAPI Depends 注入，去除对 state 模块的直接引用。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app import (
    get_health_checker,
    get_metrics_collector,
    get_metrics_store,
    get_orchestrator,
)

logger = logging.getLogger("hermes.server")

router = APIRouter()


def _now_iso() -> str:
    return datetime.now().isoformat()


@router.get("/health")
def deep_health(health_checker=Depends(get_health_checker)):
    VERSION = "0.1.0"
    if health_checker is None:
        return JSONResponse(
            content={
                "status": "unhealthy",
                "timestamp": _now_iso(),
                "version": VERSION,
                "summary": {"total": 0, "ok": 0, "warning": 0, "critical": 1},
                "checks": {"health_checker": {
                    "status": "critical",
                    "message": "HealthChecker 尚未初始化",
                    "detail": None,
                }},
            },
            status_code=503,
        )
    result = health_checker.run_all()
    result["version"] = VERSION
    status_code = 503 if result["status"] == "unhealthy" else 200
    return JSONResponse(content=result, status_code=status_code)


@router.get("/metrics")
def get_metrics(metrics_collector=Depends(get_metrics_collector)):
    from config import load_config

    config_path = os.environ.get("HERMES_CONFIG", "config.yaml")

    if metrics_collector is None:
        return JSONResponse({})
    try:
        config = load_config(config_path)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({})
    except Exception:
        pass
    return JSONResponse(metrics_collector.snapshot())


@router.get("/metrics/history")
def get_metrics_history(days: int = Query(default=30, ge=1, le=90),
                        metrics_store=Depends(get_metrics_store)):
    if metrics_store is None:
        return JSONResponse([])
    try:
        records = metrics_store.get_history(days)
        return JSONResponse(records)
    except Exception as e:
        logger.error("查询监控历史失败: %s", e)
        return JSONResponse([], status_code=500)


@router.post("/metrics/reset")
def reset_metrics(request: Request,
                  metrics_collector=Depends(get_metrics_collector)):
    if metrics_collector is None:
        return JSONResponse({"ok": False, "error": "监控未启用"}, status_code=400)
    metrics_collector.reset()
    request.app.state.metrics_reset_event.set()
    logger.info("监控指标已重置（metrics_collector + baseline）")
    return JSONResponse({"ok": True})


@router.get("/metrics/signals")
def get_signals_metrics(orchestrator=Depends(get_orchestrator)):
    if orchestrator is None or getattr(orchestrator, "signal_pool", None) is None:
        return JSONResponse({
            "signals": [],
            "sections": {},
            "summary": {},
            "threshold": 7,
        })
    try:
        return JSONResponse(orchestrator.signal_pool.get_dashboard_data())
    except Exception as e:
        logger.warning("信号池仪表盘数据获取失败: %s", e)
        return JSONResponse({
            "signals": [],
            "sections": {},
            "summary": {},
            "threshold": 7,
            "error": str(e),
        })
