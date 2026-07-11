"""cron_tools 路由。从 server.py 提取。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from agent.cron_tool_registry import CronToolRegistry
from tasks.cron_tool_loader import CronToolError
from tasks.cron_tool_loader import DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR
from tasks.cron_tool_loader import list_pending_tools as _list_pending_cron_tools

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
cron_tool_registry = None

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
def activate_cron_tool(name: str):
    """激活待审查的 cron_tool：从 .pending/ 移到 cron_tool/{name}/，注册到 registry。

    Phase 8 Task 5.5。流程：
    1. 校验 ``.pending/{name}/`` 存在。
    2. 覆盖保护：``cron_tool/{name}/`` 已存在时返回 409。
    3. 移动目录（``.pending/{name}/`` → ``cron_tool/{name}/``）。
    4. 调 ``cron_tool_registry.register`` 注册到独立 registry。
    5. 返回激活结果。

    激活失败时回滚（移回 .pending/）。
    """
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
def list_cron_tools():
    """列出所有已激活的 cron_tool。

    Phase 8 Task 5.6。返回每个工具的元数据（name/version/description/timeout）。
    """
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
def delete_cron_tool(name: str):
    """删除已激活的 cron_tool：从 registry 注销 + 删除目录。

    Phase 8 Task 5.6。
    """
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")
    base_dir = _CRON_TOOL_BASE_DIR
    active_path = Path(base_dir) / name
    if not active_path.is_dir():
        raise HTTPException(
            status_code=404, detail=f"cron_tool {name} 不存在"
        )
    # 先从 registry 注销
    cron_tool_registry.unregister(name)
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
def reload_cron_tool(name: str):
    """重新加载 cron_tool（编辑 TOOL.md / run.* 后调用）。

    Phase 8 Task 5.6。从磁盘重新解析 TOOL.md 并覆盖 registry 中的 meta。
    """
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


# ---------- Phase 8 Task 6: Skill 管理端点 ----------
