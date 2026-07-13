"""schedules 路由：调度项 CRUD、执行历史、审计日志、隔离记忆。

从 server.py 迁移 11 个端点：
- GET    /schedules
- GET    /schedules/runs
- POST   /schedules
- PUT    /schedules/{schedule_id}
- DELETE /schedules/{schedule_id}
- POST   /schedules/{schedule_id}/trigger
- GET    /schedules/{schedule_id}/history
- GET    /schedules/{schedule_id}/audit
- GET    /schedules/{schedule_id}/audit/{run_id}
- GET    /schedules/{schedule_id}/memories
- DELETE /schedules/{schedule_id}/memories/{memory_id}

Task 10 (DI 重构): 改用 FastAPI Depends 注入，去除对 state 模块的依赖。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import (
    get_audit_logger,
    get_cron_scheduler,
    get_orchestrator,
    get_session_logger,
)
from schemas.schedules import (
    ScheduleCreateRequest,
    ScheduleUpdateRequest,
    ScheduleListResponse,
    ScheduleResponse,
)

logger = logging.getLogger("hermes.server")

router = APIRouter()


# ---------- CronExpr（可选，加载失败时降级为 None） ----------

try:
    from tasks.cron_expr import CronExpr  # type: ignore
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from .tasks.cron_expr import CronExpr  # type: ignore
    except ImportError:  # pragma: no cover
        CronExpr = None  # type: ignore


# ---------- Phase 6: 调度管理端点 ----------


@router.get("/schedules", response_model=ScheduleListResponse)
def list_schedules(cron_scheduler=Depends(get_cron_scheduler)):
    """列出所有调度项。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    return ScheduleListResponse(schedules=cron_scheduler.list_schedules())


@router.get("/schedules/runs")
def list_recent_runs(
    limit: int = Query(20, ge=1, le=100, description="返回条数上限"),
    cron_scheduler=Depends(get_cron_scheduler),
):
    """跨调度项读取最近 N 条执行记录（带时间戳）。

    遍历所有调度项的 ``runs.jsonl``，合并按 ``started_at`` 倒序，取前 ``limit``
    条。每个 run 含 ``schedule_id`` / ``schedule_name`` / ``started_at`` /
    ``finished_at`` / ``success`` / ``llm_summary`` / ``tool_calls`` 等字段。

    用于前端「调度会话」区块默认展示带时间戳的执行历史。

    参数:
        limit: 返回条数上限，默认 20，范围 1-100。

    返回:
        ``{"runs": [RunSummary.to_dict() + schedule_name], "total": N}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    runs_store = getattr(cron_scheduler, "runs_store", None)
    if runs_store is None:
        # runs_store 懒初始化，未触发过执行时可能为 None
        return {"runs": [], "total": 0}
    try:
        schedules = cron_scheduler.list_schedules()
        runs = runs_store.read_recent_all(schedules, n=limit)
    except Exception as e:
        logger.exception("查询跨调度项执行历史失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询执行历史失败: {e}")
    return {"runs": runs, "total": len(runs)}


@router.post("/schedules", response_model=ScheduleResponse)
def create_schedule(req: ScheduleCreateRequest,
                    cron_scheduler=Depends(get_cron_scheduler)):
    """新增调度项。cron 非法返回 400。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    # 先校验 cron 合法性
    if CronExpr is not None:
        try:
            CronExpr(req.cron)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"cron 表达式非法: {e}")
    # 校验 workflow 配置（若提供）
    if req.workflow:
        try:
            from tasks.workflow import WorkflowSpec  # type: ignore
            WorkflowSpec.from_dict(req.workflow)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"workflow 配置非法: {e}")
        except ImportError:
            # workflow 模块不可用时仅记录，不阻断创建（向后兼容）
            logger.warning("workflow 模块不可用，跳过 workflow 配置校验")
    try:
        sched_id = cron_scheduler.add_schedule({
            "id": req.id,
            "name": req.name,
            "cron": req.cron,
            "task": req.task,
            "enabled": req.enabled,
            "workflow": req.workflow,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ScheduleResponse(schedule_id=sched_id, message="已创建调度项")


@router.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: str, req: ScheduleUpdateRequest,
                    cron_scheduler=Depends(get_cron_scheduler)):
    """更新调度项。仅 enabled 即时生效；cron/task/name 变更需重启。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    fields = req.model_dump(exclude_none=True)
    # 校验 cron 合法性（如提供）
    if "cron" in fields and CronExpr is not None:
        try:
            CronExpr(fields["cron"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"cron 表达式非法: {e}")
    try:
        ok = cron_scheduler.update_schedule(schedule_id, fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="调度项不存在")
    # 判断是否需要重启：仅 enabled 变更不需要
    needs_restart = any(k in fields for k in ("cron", "task", "name", "workflow"))
    return {"status": "ok", "needs_restart": needs_restart}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str,
                    cron_scheduler=Depends(get_cron_scheduler)):
    """删除调度项。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    ok = cron_scheduler.delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="调度项不存在")
    return {"status": "ok"}


@router.post("/schedules/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: str,
                           cron_scheduler=Depends(get_cron_scheduler),
                           orchestrator=Depends(get_orchestrator)):
    """立即触发调度项一次。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    # 异步触发，不阻塞响应
    asyncio.create_task(cron_scheduler.trigger_now(orchestrator, schedule_id))
    return {"status": "ok", "message": "已触发"}


# ---------- Phase 8 Task 1.8: 调度执行历史端点 ----------


@router.get("/schedules/{schedule_id}/history")
def get_schedule_history(
    schedule_id: str,
    limit: int = Query(10, ge=1, le=100, description="返回条数上限"),
    cron_scheduler=Depends(get_cron_scheduler),
    session_logger=Depends(get_session_logger),
):
    """获取指定调度项的执行历史（最近 N 条 assistant 回复）。

    Phase 8 Task 1.8。按 ``session_id="cron:{schedule_id}"`` 查 session_logger，
    取最近 ``limit`` 条 assistant 角色消息（倒序，最新在前）。每条返回
    id / content / created_at / tool_name。

    调度项不存在时返回 404；session_logger 未初始化时返回 503。

    返回:
        ``{"history": [{id, content, created_at, tool_name}], "total": N}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    # 校验调度项存在
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if session_logger is None:
        raise HTTPException(status_code=503, detail="session_logger 尚未初始化")
    cron_session_id = f"cron:{schedule_id}"
    try:
        rows = session_logger.get_recent_role_messages(
            cron_session_id, "assistant", limit=limit
        )
    except Exception as e:
        logger.exception("查询调度执行历史失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询历史失败: {e}")
    history = [
        {
            "id": r.get("id"),
            "content": r.get("content", ""),
            "created_at": r.get("created_at", ""),
            "tool_name": r.get("tool_name"),
        }
        for r in rows
    ]
    return {"history": history, "total": len(history)}


# ---------- Phase 8 Task 4.5: 调度审计端点 ----------


@router.get("/schedules/{schedule_id}/audit")
def get_schedule_audit(
    schedule_id: str,
    limit: int = Query(20, ge=1, le=500, description="返回条数上限"),
    run_id: Optional[str] = Query(None, description="可选执行批次 ID 过滤"),
    cron_scheduler=Depends(get_cron_scheduler),
    audit_logger=Depends(get_audit_logger),
):
    """获取指定调度项的工具调用审计日志。

    Phase 8 Task 4.5。调 ``audit_logger.get_by_schedule(schedule_id, limit,
    run_id)`` 返回该调度项最近 ``limit`` 条工具调用审计记录（倒序，最新在前）。
    可选 ``run_id`` 查询参数进一步按执行批次筛选。

    每条记录含 timestamp / session_id / tool_name / tool_input / result /
    is_error / duration_ms / decision_source / schedule_id / run_id 字段。
    旧记录缺少 Task 4.4 新字段时由 ``_normalize_entry`` 填充默认值。

    调度项不存在时返回 404；audit_logger 未初始化时返回空列表（向后兼容）。
    监控禁用时返回空列表（与 ``/audit/logs`` 行为一致）。

    返回:
        ``{"logs": [...], "total": N}``
    """
    from config import load_config

    config_path = os.environ.get("HERMES_CONFIG", "config.yaml")

    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if audit_logger is None:
        return {"logs": [], "total": 0}
    # 监控禁用时返回空列表（与 /audit/logs 行为一致）
    try:
        config = load_config(config_path)
        if not config.get("monitoring", {}).get("enabled", True):
            return {"logs": [], "total": 0}
    except Exception:
        pass
    try:
        logs = audit_logger.get_by_schedule(
            schedule_id, limit=limit, run_id=run_id
        )
    except Exception as e:
        logger.exception("查询调度审计日志失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询审计日志失败: {e}")
    return {"logs": logs, "total": len(logs)}


@router.get("/schedules/{schedule_id}/audit/{run_id}")
def get_schedule_audit_by_run(schedule_id: str, run_id: str,
                              cron_scheduler=Depends(get_cron_scheduler),
                              audit_logger=Depends(get_audit_logger)):
    """获取指定调度项某次执行批次的工具调用审计日志。

    Phase 8 Task 4.5。调 ``audit_logger.get_by_run_id(schedule_id, run_id)``
    返回该调度项指定 ``run_id`` 的所有工具调用审计记录（正序，最早在前，
    便于按执行时间线复盘）。

    调度项不存在时返回 404；audit_logger 未初始化时返回空列表；监控禁用时
    返回空列表。

    返回:
        ``{"logs": [...], "total": N, "run_id": "<run_id>"}``
    """
    from config import load_config

    config_path = os.environ.get("HERMES_CONFIG", "config.yaml")

    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if audit_logger is None:
        return {"logs": [], "total": 0, "run_id": run_id}
    try:
        config = load_config(config_path)
        if not config.get("monitoring", {}).get("enabled", True):
            return {"logs": [], "total": 0, "run_id": run_id}
    except Exception:
        pass
    try:
        logs = audit_logger.get_by_run_id(schedule_id, run_id)
    except Exception as e:
        logger.exception("查询调度审计日志（run_id）失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询审计日志失败: {e}")
    return {"logs": logs, "total": len(logs), "run_id": run_id}


# ---------- Phase 8 Task 1.9: 调度记忆端点 ----------


@router.get("/schedules/{schedule_id}/memories")
def list_schedule_memories(
    schedule_id: str,
    limit: int = Query(20, ge=1, le=200, description="返回条数上限"),
    cron_scheduler=Depends(get_cron_scheduler),
    orchestrator=Depends(get_orchestrator),
):
    """列出指定调度项的隔离记忆（namespace=cron, cron_id=schedule_id）。

    Phase 8 Task 1.9。调 ``chroma_store.get_all_memories(namespace="cron",
    cron_id=schedule_id)`` 取该调度项独占的记忆条目，截断到 ``limit``。

    调度项不存在时返回 404；ChromaMemoryStore 未初始化时返回 503。

    返回:
        ``{"memories": [{id, content, metadata}], "total": N}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    # 用 cron_id 解析后的值（cron_id 字段为 None 时回退到 id）
    cron_id = schedule.get_cron_id()
    try:
        raw = orchestrator.chroma_store.get_all_memories(
            namespace="cron", cron_id=cron_id
        )
    except Exception as e:
        logger.exception("查询调度记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询记忆失败: {e}")
    memories = [
        {
            "id": item.get("id", ""),
            "content": item.get("content", ""),
            "metadata": item.get("metadata", {}),
        }
        for item in raw[:limit]
    ]
    return {"memories": memories, "total": len(memories)}


@router.delete("/schedules/{schedule_id}/memories/{memory_id}")
def delete_schedule_memory(schedule_id: str, memory_id: str,
                           cron_scheduler=Depends(get_cron_scheduler),
                           orchestrator=Depends(get_orchestrator)):
    """删除指定调度项的某条隔离记忆。

    Phase 8 Task 1.9。**校验 ``cron_id`` 一致**防止跨调度项误删：先查
    ``get_all_memories(namespace="cron", cron_id=schedule.get_cron_id())``，
    若 ``memory_id`` 不在结果集中返回 404（既覆盖记忆不存在，也覆盖
    memory_id 属于其他调度项的情况）。

    调度项不存在时返回 404；ChromaMemoryStore 未初始化时返回 503。

    返回:
        ``{"status": "ok", "deleted_id": memory_id}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    cron_id = schedule.get_cron_id()
    try:
        raw = orchestrator.chroma_store.get_all_memories(
            namespace="cron", cron_id=cron_id
        )
    except Exception as e:
        logger.exception("校验调度记忆归属失败: %s", e)
        raise HTTPException(status_code=500, detail=f"校验记忆失败: {e}")
    # 校验 memory_id 属于该调度项（cron_id 一致）
    existing_ids = {item.get("id") for item in raw}
    if memory_id not in existing_ids:
        raise HTTPException(
            status_code=404,
            detail=f"记忆 {memory_id} 不存在或不属于调度项 {schedule_id}",
        )
    try:
        orchestrator.chroma_store.delete_memory(memory_id)
    except Exception as e:
        logger.exception("删除调度记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"删除记忆失败: {e}")
    logger.info(
        "已删除调度项 %s 的隔离记忆: %s", schedule_id, memory_id
    )
    return {"status": "ok", "deleted_id": memory_id}
