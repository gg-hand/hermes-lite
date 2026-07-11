"""health 路由。从 server.py 提取。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, JSONResponse

from config import load_config
from monitoring.health import HealthChecker

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
orchestrator = None
metrics_collector = None
metrics_store = None
_metrics_baseline_reset = None
health_checker = None

# 常量(从 server.py 复制)
VERSION = "0.1.0"
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")

@router.get("/health")
def deep_health():
    """深度健康检查，逐层检测所有子系统组件。

    返回 JSON，含 overall status、summary 计数与各子系统检测结果。
    critical 级别组件不可用时返回 HTTP 503。
    """
    global health_checker
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
    """返回当前指标快照。

    若监控未启用或 metrics_collector 未初始化，返回空字典。
    """
    if metrics_collector is None:
        return JSONResponse({})
    # 检查当前配置是否禁用监控（支持热更新 enabled=false）
    try:
        config = load_config(CONFIG_PATH)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({})
    except Exception:
        pass
    return JSONResponse(metrics_collector.snapshot())

@router.get("/metrics/history")
def get_metrics_history(days: int = Query(default=30, ge=1, le=90)):
    """返回最近 N 天的监控历史数据（按日期升序）。

    用于前端监控面板的历史趋势展示。若持久化未启用或 store 未初始化，
    返回空列表。
    """
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
    """重置所有指标计数器与直方图，并同步重置持久化 baseline。

    前端监控面板"重置指标"按钮调用。重置后：
    - metrics_collector 的所有计数器清零
    - metrics_persist_loop 的 baseline 同步重置，避免重置后 delta 丢失
    - 已持久化的历史数据不受影响
    """
    global _metrics_baseline_reset
    if metrics_collector is None:
        return JSONResponse({"ok": False, "error": "监控未启用"}, status_code=400)
    metrics_collector.reset()
    _metrics_baseline_reset = True
    logger.info("监控指标已重置（metrics_collector + baseline）")
    return JSONResponse({"ok": True})

@router.get("/metrics/signals")
def get_signals_metrics():
    """Phase 2 反馈监控：返回信号池仪表盘数据。

    用于监控面板渲染"攻略进度条"，按 section 分组展示信号累积状态。
    若 orchestrator 或 signal_pool 未初始化，返回空结构。
    """
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
