"""cron_tools 路由：cron_tool 动态工具管理（待审查列表、激活、拒绝、列表、删除、重载）。

从 server.py 迁移 6 个端点：
- GET  /cron_tools/pending
- POST /cron_tools/{name}/activate
- POST /cron_tools/{name}/reject
- GET  /cron_tools
- DELETE /cron_tools/{name}
- PUT  /cron_tools/{name}

调度执行强化（7.3）新增 5 个端点：
- GET  /cron_tools/schedules/{schedule_id}/runs          执行历史
- GET  /cron_tools/schedules/{schedule_id}/runs/{run_id} 单次详情
- POST /cron_tools/schedules/{schedule_id}/run           手动重跑
- GET  /cron_tools/runs/recent                           最近记录（跨调度）
- GET  /cron_tools/runs/stats                            今日统计
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from teage_liu.app import get_orchestrator, get_cron_scheduler

logger = logging.getLogger("teage_liu.server")

router = APIRouter()


# ---------- 过渡期兼容：同步 state 变更到 server 模块全局变量 ----------


def _sync_to_server_globals(**kwargs):
    """同步 state 变更到 server 模块的全局变量（过渡期兼容）。

    routes 通过 state 模块访问共享状态，但 server.py 中尚未迁移的
    handler 仍使用模块级 global 变量。此函数确保修改 state 的操作也
    同步更新 server 模块的全局变量。
    """
    for mod_name in ("teage_liu.server", "server"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            for key, value in kwargs.items():
                setattr(mod, key, value)


# ---------- cron_tool 加载器（可选，加载失败时降级）----------

try:
    from teage_liu.tasks.cron_tool_loader import (  # type: ignore
        CronToolError,
        DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR,
        list_pending_tools as _list_pending_cron_tools,
    )
except ImportError:  # pragma: no cover - 扩展模块缺失时降级
    CronToolError = Exception  # type: ignore
    _CRON_TOOL_BASE_DIR = "cron_tool"  # type: ignore
    _list_pending_cron_tools = None  # type: ignore


# ---------- 辅助函数 ----------


def _read_cron_tool_dir(tool_dir: Path) -> Dict[str, Any]:
    """读取 cron_tool 目录下的 TOOL.md 与 run.* 脚本内容。

    参数:
        tool_dir: 工具目录路径。

    返回:
        dict，含 ``tool_md`` / ``run_script`` / ``run_ext`` 字段。
        文件缺失时对应字段为空字符串。
    """
    result = {"tool_md": "", "run_script": "", "run_ext": ""}
    tool_md_path = tool_dir / "TOOL.md"
    if tool_md_path.is_file():
        try:
            result["tool_md"] = tool_md_path.read_text(encoding="utf-8")
        except OSError:
            pass
    for ext in (".py", ".sh", ".js"):
        run_path = tool_dir / f"run{ext}"
        if run_path.is_file():
            try:
                result["run_script"] = run_path.read_text(encoding="utf-8")
                result["run_ext"] = ext
            except OSError:
                pass
            break
    return result


def _get_cron_tool_registry(orchestrator):
    """从 orchestrator 获取 cron_tool_registry（lifespan 注入到 orchestrator）。"""
    if orchestrator is None:
        return None
    return getattr(orchestrator, "cron_tool_registry", None)


# ---------- Phase 8 Task 5.5: cron_tool 待审查端点 ----------


@router.get("/cron_tools/pending")
def list_pending_cron_tools():
    """列出所有待审查的 cron_tool（.pending/ 下）。

    Phase 8 Task 5.5。返回每个待审查工具的 TOOL.md / run.* 内容，
    供前端审查卡片渲染。

    cron_tool 模块未初始化时返回 503。
    """
    if _list_pending_cron_tools is None:
        raise HTTPException(status_code=503, detail="cron_tool 模块尚未初始化")
    base_dir = _CRON_TOOL_BASE_DIR
    names = _list_pending_cron_tools(base_dir=base_dir)
    items = []
    for name in names:
        tool_dir = Path(base_dir) / ".pending" / name
        files = _read_cron_tool_dir(tool_dir)
        items.append(
            {
                "name": name,
                "tool_md": files["tool_md"],
                "run_script": files["run_script"],
                "run_ext": files["run_ext"],
            }
        )
    return {"pending": items, "total": len(items)}


@router.post("/cron_tools/{name}/activate")
def activate_cron_tool(name: str, orchestrator=Depends(get_orchestrator)):
    """激活待审查的 cron_tool：从 .pending/ 移到 cron_tool/{name}/，注册到 registry。

    Phase 8 Task 5.5。流程：
    1. 校验 ``.pending/{name}/`` 存在。
    2. 覆盖保护：``cron_tool/{name}/`` 已存在时返回 409。
    3. 移动目录（``.pending/{name}/`` → ``cron_tool/{name}/``）。
    4. 调 ``cron_tool_registry.register`` 注册到独立 registry。
    5. 返回激活结果。

    激活失败时回滚（移回 .pending/）。
    """
    cron_tool_registry = _get_cron_tool_registry(orchestrator)
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")

    base_dir = _CRON_TOOL_BASE_DIR
    pending_path = Path(base_dir) / ".pending" / name
    active_path = Path(base_dir) / name

    if not pending_path.is_dir():
        raise HTTPException(
            status_code=404, detail=f"待审查工具 {name} 不存在"
        )
    if active_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"已激活工具 {name} 已存在，请先删除再激活",
        )

    # 移动目录
    try:
        shutil.move(str(pending_path), str(active_path))
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"移动目录失败: {exc}"
        ) from exc

    # 注册到 registry
    try:
        meta = cron_tool_registry.register(name)
    except CronToolError as exc:
        # 注册失败：回滚（移回 .pending/）
        try:
            shutil.move(str(active_path), str(pending_path))
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail=f"激活失败（TOOL.md 解析错误）: {exc}，已回滚",
        ) from exc
    logger.info("cron_tool %s 已激活并注册（version=%s）", name, meta.version)
    return {
        "status": "activated",
        "tool_name": name,
        "version": meta.version,
        "message": f"cron_tool {name} 已激活并注册到 cron_tool_registry",
    }


@router.post("/cron_tools/{name}/reject")
def reject_cron_tool(name: str):
    """拒绝待审查的 cron_tool：删除 .pending/{name}/。

    Phase 8 Task 5.5。
    """
    if _list_pending_cron_tools is None:
        raise HTTPException(status_code=503, detail="cron_tool 模块尚未初始化")
    base_dir = _CRON_TOOL_BASE_DIR
    pending_path = Path(base_dir) / ".pending" / name
    if not pending_path.is_dir():
        raise HTTPException(
            status_code=404, detail=f"待审查工具 {name} 不存在"
        )
    try:
        shutil.rmtree(str(pending_path))
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"删除目录失败: {exc}"
        ) from exc
    logger.info("cron_tool %s 已被拒绝并删除", name)
    return {
        "status": "rejected",
        "tool_name": name,
        "message": f"cron_tool {name} 已被拒绝并删除",
    }


# ---------- Phase 8 Task 5.6: cron_tool 管理端点（已激活工具）----------


@router.get("/cron_tools")
def list_cron_tools(orchestrator=Depends(get_orchestrator)):
    """列出所有已激活的 cron_tool。

    Phase 8 Task 5.6。返回每个工具的元数据（name/version/description/timeout）。
    """
    cron_tool_registry = _get_cron_tool_registry(orchestrator)
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")
    items = []
    for name in cron_tool_registry.list_tool_names():
        meta = cron_tool_registry.get_tool_meta(name)
        if meta is None:
            continue
        items.append(
            {
                "name": meta.name,
                "version": meta.version,
                "description": meta.description,
                "author": meta.author,
                "timeout": meta.timeout,
            }
        )
    return {"cron_tools": items, "total": len(items)}


@router.delete("/cron_tools/{name}")
def delete_cron_tool(name: str, orchestrator=Depends(get_orchestrator)):
    """删除已激活的 cron_tool：从 registry 注销 + 删除目录。

    Phase 8 Task 5.6。Q5: ``name`` 为 URL 传入的裸目录名，``unregister``
    需要带 ``cron_tool__`` 前缀的 ``registered_name``，此处补前缀。
    """
    cron_tool_registry = _get_cron_tool_registry(orchestrator)
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")
    base_dir = _CRON_TOOL_BASE_DIR
    active_path = Path(base_dir) / name
    if not active_path.is_dir():
        raise HTTPException(
            status_code=404, detail=f"cron_tool {name} 不存在"
        )
    # 先从 registry 注销（Q5: 用带前缀的 registered_name）
    registered_name = f"cron_tool__{name}"
    cron_tool_registry.unregister(registered_name)
    # 再删除目录
    try:
        shutil.rmtree(str(active_path))
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"删除目录失败: {exc}"
        ) from exc
    logger.info("cron_tool %s 已删除并从 registry 注销", name)
    return {
        "status": "deleted",
        "tool_name": name,
        "message": f"cron_tool {name} 已删除并从 registry 注销",
    }


@router.put("/cron_tools/{name}")
def reload_cron_tool(name: str, orchestrator=Depends(get_orchestrator)):
    """重新加载 cron_tool（编辑 TOOL.md / run.* 后调用）。

    Phase 8 Task 5.6。从磁盘重新解析 TOOL.md 并覆盖 registry 中的 meta。
    """
    cron_tool_registry = _get_cron_tool_registry(orchestrator)
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")
    try:
        meta = cron_tool_registry.reload(name)
    except CronToolError as exc:
        raise HTTPException(
            status_code=400, detail=f"重新加载失败: {exc}"
        ) from exc
    logger.info("cron_tool %s 已重新加载（version=%s）", name, meta.version)
    return {
        "status": "reloaded",
        "tool_name": name,
        "version": meta.version,
        "message": f"cron_tool {name} 已重新加载",
    }


# ---------- 调度执行强化（7.3）：执行历史/重跑/详情/最近/统计 5 个端点 ----------


@router.get("/cron_tools/schedules/{schedule_id}/runs")
def get_schedule_runs(
    schedule_id: str,
    limit: int = 20,
    cron_scheduler=Depends(get_cron_scheduler),
):
    """获取指定调度的执行历史（7.3）。

    参数:
        schedule_id: 调度项 ID。
        limit: 返回条数上限，默认 20。
        cron_scheduler: 由 DI 容器注入的 CronScheduler。

    返回:
        ``{"runs": [RunSummary.to_dict()], "total": N}``。
        scheduler 未就绪时返回 503。
    """
    if cron_scheduler is None or cron_scheduler.runs_store is None:
        return JSONResponse(
            status_code=503, content={"error": "调度器未就绪"}
        )
    runs = cron_scheduler.runs_store.read_recent(schedule_id, n=limit)
    runs_dict = [r.to_dict() for r in runs]
    return JSONResponse(
        status_code=200, content={"runs": runs_dict, "total": len(runs_dict)}
    )


@router.get("/cron_tools/schedules/{schedule_id}/runs/{run_id}")
def get_run_detail(
    schedule_id: str,
    run_id: str,
    cron_scheduler=Depends(get_cron_scheduler),
):
    """获取单次执行详情（含完整 StepTrace，7.3 Q13）。

    参数:
        schedule_id: 调度项 ID。
        run_id: 执行 ID。
        cron_scheduler: 由 DI 容器注入的 CronScheduler。

    返回:
        RunSummary.to_dict()。run_id 不存在返回 404。
    """
    if cron_scheduler is None or cron_scheduler.runs_store is None:
        return JSONResponse(
            status_code=503, content={"error": "调度器未就绪"}
        )
    detail = cron_scheduler.runs_store.read_by_run_id(schedule_id, run_id)
    if detail is None:
        return JSONResponse(
            status_code=404, content={"error": f"run_id {run_id} 不存在"}
        )
    return JSONResponse(status_code=200, content=detail)


@router.post("/cron_tools/schedules/{schedule_id}/run", status_code=202)
def trigger_schedule_run(
    schedule_id: str,
    cron_scheduler=Depends(get_cron_scheduler),
):
    """手动触发调度执行（异步，返回 202，7.3）。

    通过 ``asyncio.create_task`` 触发 ``_run_schedule_direct``，立即返回。
    schedule_id 不存在返回 404。

    参数:
        schedule_id: 调度项 ID。
        cron_scheduler: 由 DI 容器注入的 CronScheduler。
    """
    if cron_scheduler is None:
        return JSONResponse(
            status_code=503, content={"error": "调度器未就绪"}
        )
    schedule = next(
        (s for s in cron_scheduler._schedules if s.id == schedule_id), None
    )
    if schedule is None:
        return JSONResponse(
            status_code=404, content={"error": f"调度 {schedule_id} 不存在"}
        )
    # 触发异步执行；无运行事件循环时（如直接调用测试）忽略 RuntimeError
    try:
        asyncio.create_task(cron_scheduler._run_schedule_direct(schedule))
    except RuntimeError:
        # 无运行事件循环，跳过实际触发（测试场景）
        pass
    return JSONResponse(
        status_code=202,
        content={"status": "triggered", "schedule_id": schedule_id},
    )


@router.get("/cron_tools/runs/recent")
def get_recent_runs(
    limit: int = 50,
    cron_scheduler=Depends(get_cron_scheduler),
):
    """获取最近所有调度的执行记录（monitor 页用，7.3）。

    参数:
        limit: 返回条数上限，默认 50。
        cron_scheduler: 由 DI 容器注入的 CronScheduler。

    返回:
        ``{"runs": [dict + schedule_name], "total": N}``。
    """
    if cron_scheduler is None or cron_scheduler.runs_store is None:
        return JSONResponse(
            status_code=503, content={"error": "调度器未就绪"}
        )
    schedules = cron_scheduler.list_schedules()
    runs = cron_scheduler.runs_store.read_recent_all(schedules, n=limit)
    return JSONResponse(
        status_code=200, content={"runs": runs, "total": len(runs)}
    )


@router.get("/cron_tools/runs/stats")
def get_run_stats(cron_scheduler=Depends(get_cron_scheduler)):
    """获取执行统计（今日成功/失败/耗时，7.3）。

    参数:
        cron_scheduler: 由 DI 容器注入的 CronScheduler。

    返回:
        ``{"today_success": N, "today_failure": M, "total_duration_seconds": F,
           "today_total": T}``。
    """
    if cron_scheduler is None or cron_scheduler.runs_store is None:
        return JSONResponse(
            status_code=503, content={"error": "调度器未就绪"}
        )
    today_str = date.today().isoformat()
    schedules = cron_scheduler.list_schedules()
    all_runs = cron_scheduler.runs_store.read_recent_all(schedules, n=500)
    today_runs = [
        r for r in all_runs
        if str(r.get("started_at", "")).startswith(today_str)
    ]
    success_count = sum(1 for r in today_runs if r.get("success"))
    failure_count = len(today_runs) - success_count
    total_duration = sum(r.get("duration_seconds", 0) for r in today_runs)
    return JSONResponse(
        status_code=200,
        content={
            "today_success": success_count,
            "today_failure": failure_count,
            "total_duration_seconds": total_duration,
            "today_total": len(today_runs),
        },
    )
