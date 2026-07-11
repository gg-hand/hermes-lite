"""health 路由：健康检查、监控指标、信号池仪表盘。

Task 9: 从 server.py 迁移 5 个端点。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("hermes.server")

router = APIRouter()


def _now_iso() -> str:
    return datetime.now().isoformat()


@router.get("/health")
def deep_health():
    import state

    VERSION = "0.1.0"
    health_checker = state.health_checker
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
def get_metrics():
    import state
    from config import load_config

    config_path = os.environ.get("HERMES_CONFIG", "config.yaml")

    metrics_collector = state.metrics_collector
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
def get_metrics_history(days: int = Query(default=30, ge=1, le=90)):
    import state

    metrics_store = state.metrics_store
    if metrics_store is None:
        return JSONResponse([])
    try:
        records = metrics_store.get_history(days)
        return JSONResponse(records)
    except Exception as e:
        logger.error("查询监控历史失败: %s", e)
        return JSONResponse([], status_code=500)


@router.post("/metrics/reset")
def reset_metrics():
    import state

    metrics_collector = state.metrics_collector
    if metrics_collector is None:
        return JSONResponse({"ok": False, "error": "监控未启用"}, status_code=400)
    metrics_collector.reset()
    state.metrics_baseline_reset = True
    logger.info("监控指标已重置（metrics_collector + baseline）")
    return JSONResponse({"ok": True})


@router.get("/metrics/signals")
def get_signals_metrics():
    import state

    orchestrator = state.orchestrator
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
