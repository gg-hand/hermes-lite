"""skills 路由。从 server.py 提取。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from agent.skill_tools import _make_skill_activate_handler
from skill.loader import SkillLoader, load_skill_to_registry, register_skill_stub
from skill.loader import load_skill_to_registry, register_skill_stub
from skill.loader import register_skill_stub

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
orchestrator = None
skill_loader = None

@router.get("/skills")
def list_skills():
    """列出所有已发现的 Skill，含启用/禁用状态。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    try:
        discovered = skill_loader.discover()
    except Exception:
        discovered = []
    skill_state = _load_skill_state()
    disabled_list = skill_state.get("disabled", [])
    skills = []
    for meta in discovered:
        skills.append({
            "name": meta.name,
            "version": meta.version,
            "description": meta.description,
            "disabled": meta.name in disabled_list,
        })
    return {"skills": skills, "total": len(skills)}

@router.get("/skills/{name}")
def get_skill(name: str):
    """获取指定 Skill 的详细信息。

    基于 SkillMeta（来自 ``_metas`` 缓存或 ``discover()``）返回元数据，
    不再调 ``skill_loader.load()`` 读空的 ``tools.py``。同时返回软禁用状态
    与 stub 注册状态。
    """
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    # 优先从 _metas 缓存取 meta，未命中则 discover 一次刷新缓存
    meta = None
    if hasattr(skill_loader, "_metas"):
        meta = skill_loader._metas.get(name)
    if meta is None:
        try:
            for m in skill_loader.discover():
                if m.name == name:
                    meta = m
                    break
        except Exception as e:
            logger.warning("discover 扫描失败: %s", e)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' 不存在")

    registry = orchestrator.tool_registry
    # 公开 API get_full_schema 返回空 dict 表示工具未注册
    stub_schema = registry.get_full_schema(f"skill__{name}") if hasattr(registry, "get_full_schema") else {}
    return {
        "name": meta.name,
        "version": meta.version,
        "description": meta.description,
        "body_preview": (meta.body or "")[:200],
        "resources": meta.resources or [],
        "disabled": registry.is_skill_disabled(name),
        "stub_registered": bool(stub_schema),
    }

@router.post("/skills/{name}/reload")
def reload_skill(name: str):
    """热重载指定 Skill：双路径注册（stub 激活按钮 + 业务工具）。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    try:
        registry = orchestrator.tool_registry

        # 清除前缀匹配的旧业务工具（skill__{name}__*）
        # 注意：保留 skill__{name} 激活按钮本身，由 register_skill_stub 覆盖更新
        prefix = f"skill__{name}__"
        for store_key in ("_core_tools", "_deferred_tools", "_loaded_tools"):
            store = getattr(registry, store_key, {})
            for tname in list(store.keys()):
                if tname.startswith(prefix):
                    registry.unregister(tname)
        # 同步移除旧激活按钮（register_skill_stub 会重新注册）
        try:
            registry.unregister(f"skill__{name}")
        except Exception:
            pass

        # 重新加载 Skill（reload 会清 _skills/_metas 缓存并重新 import）
        skill = skill_loader.reload(name)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' 不存在或加载失败")

        stub_registered = False

        # 新路径：注册 skill__{name} 激活按钮到 Core Tier
        if register_skill_stub is not None and _make_skill_activate_handler is not None:
            try:
                # 获取最新 meta（reload 已刷新 _metas 缓存）
                meta = None
                if hasattr(skill_loader, "_metas"):
                    meta = skill_loader._metas.get(name)
                if meta is None and hasattr(skill_loader, "_parse_meta"):
                    skill_file = Path(skill_loader.skill_dir) / name / "SKILL.md"
                    if skill_file.exists():
                        meta = skill_loader._parse_meta(skill_file)
                if meta is not None:
                    handler = _make_skill_activate_handler(
                        skill_loader, orchestrator, name
                    )
                    register_skill_stub(registry, meta, handler)
                    stub_registered = True
            except Exception as e:
                logger.error("reload 时注册 skill stub %s 失败: %s", name, e)

        # 旧路径：注册业务工具到 Deferred Tier（向后兼容，tools.py 非空时）
        if skill.tools:
            load_skill_to_registry(registry, skill)

        logger.info(
            "Skill 已热重载: %s（stub=%s，%d 个业务工具）",
            name, stub_registered, len(skill.tools),
        )
        return {
            "status": "reloaded",
            "skill_name": name,
            "tool_count": len(skill.tools),
            "stub_registered": stub_registered,
            "message": f"Skill '{name}' 已重新加载",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Skill 热重载失败: %s", e)
        raise HTTPException(status_code=500, detail=f"热重载失败: {e}")

@router.post("/skills/{name}/toggle")
def toggle_skill(name: str):
    """切换 Skill 启用/禁用状态。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    state = _load_skill_state()
    disabled_list = state.get("disabled", [])
    name_lower = name.lower()

    # 检查锁定列表
    locked_list = state.get("locked", [])
    if any(ln.lower() == name_lower for ln in locked_list):
        raise HTTPException(status_code=403, detail=f"Skill '{name}' 已锁定，不可切换")

    if name in disabled_list:
        # 启用：从禁用清单移除，清除软禁用标记，重新加载并走双路径注册
        disabled_list.remove(name)
        state["disabled"] = disabled_list
        _save_skill_state(state)

        # 清除软禁用标记（使 schema 中 enabled: False 移除）
        orchestrator.tool_registry.enable_skill(name)

        stub_registered = False
        try:
            skill_loader.unload(name)
            skill = skill_loader.load(name)
            if skill is not None:
                # 新路径：注册 skill__{name} 激活按钮到 Core Tier
                if register_skill_stub is not None and _make_skill_activate_handler is not None:
                    try:
                        meta = None
                        if hasattr(skill_loader, "_metas"):
                            meta = skill_loader._metas.get(name)
                        if meta is None and hasattr(skill_loader, "_parse_meta"):
                            skill_file = Path(skill_loader.skill_dir) / name / "SKILL.md"
                            if skill_file.exists():
                                meta = skill_loader._parse_meta(skill_file)
                        if meta is not None:
                            handler = _make_skill_activate_handler(
                                skill_loader, orchestrator, name
                            )
                            register_skill_stub(orchestrator.tool_registry, meta, handler)
                            stub_registered = True
                    except Exception as e:
                        logger.error("启用 Skill 时注册 stub %s 失败: %s", name, e)

                # 旧路径：注册业务工具到 Deferred Tier（向后兼容）
                if skill.tools:
                    load_skill_to_registry(orchestrator.tool_registry, skill)
        except Exception as e:
            logger.warning("启用 Skill 后重新加载失败: %s", e)
        logger.info("Skill '%s' 已启用（stub=%s）", name, stub_registered)
        return {
            "status": "enabled",
            "skill_name": name,
            "stub_registered": stub_registered,
            "message": f"Skill '{name}' 已启用",
        }
    else:
        # 禁用：软禁用（schema 标 enabled: False，执行抛 ToolNotFoundError），
        # 工具仍保留在 registry 中保持 schema 稳定，记入禁用清单
        orchestrator.tool_registry.disable_skill(name)
        disabled_list.append(name)
        state["disabled"] = disabled_list
        _save_skill_state(state)
        logger.info("Skill '%s' 已禁用（软禁用）", name)
        return {
            "status": "disabled",
            "skill_name": name,
            "message": f"Skill '{name}' 已禁用",
        }

@router.delete("/skills/{name}")
def delete_skill(name: str):
    """注销并删除指定的 Skill。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    # 从 registry 卸载工具
    prefix = f"skill__{name}__"
    for store_key in ("_core_tools", "_deferred_tools", "_loaded_tools"):
        store = getattr(orchestrator.tool_registry, store_key, {})
        for tname in list(store.keys()):
            if tname.startswith(prefix):
                orchestrator.tool_registry.unregister(tname)

    # 清理加载缓存
    skill_loader.unload(name)

    # 删除磁盘目录
    skill_dir = skill_loader.skill_dir / name
    if skill_dir.exists():
        try:
            shutil.rmtree(str(skill_dir))
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"删除 Skill 目录失败: {e}")

    # 清理状态
    state = _load_skill_state()
    disabled_list = state.get("disabled", [])
    if name in disabled_list:
        disabled_list.remove(name)
        state["disabled"] = disabled_list
        _save_skill_state(state)

    logger.info("Skill '%s' 已删除", name)
    return {
        "status": "deleted",
        "skill_name": name,
        "message": f"Skill '{name}' 已注销并删除",
    }
