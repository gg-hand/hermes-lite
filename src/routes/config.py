"""config 路由：配置读取与更新（含热重载逻辑）。

Task 11: 从 server.py 迁移 2 个端点。
辅助函数仍保留在 server.py，通过 sys.modules 兼容两种导入方式。
"""
from __future__ import annotations

import logging
import sys

from fastapi import APIRouter, HTTPException
from schemas.config import ConfigResponse, ConfigUpdateRequest, ConfigUpdateResponse

logger = logging.getLogger("hermes.server")

router = APIRouter()


def _server():
    """获取已加载的 server 模块（兼容 src.server 和 server）。"""
    return sys.modules.get("src.server") or sys.modules.get("server")


@router.get("/config", response_model=ConfigResponse)
def get_config():
    srv = _server()
    try:
        config = srv.load_config(srv.CONFIG_PATH)
        return ConfigResponse(config=config)
    except Exception as e:
        logger.exception("读取配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")


@router.put("/config", response_model=ConfigUpdateResponse)
def update_config(req: ConfigUpdateRequest):
    srv = _server()
    try:
        with srv._config_write_lock:
            try:
                old_config = srv.load_config(srv.CONFIG_PATH)
            except Exception:
                old_config = {}

            merged_config = srv._deep_merge_config(old_config, req.config)

            try:
                srv._validate_config_schema(merged_config)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            srv._backup_config(srv.CONFIG_PATH)
            srv._atomic_write_config(srv.CONFIG_PATH, merged_config)
            srv.clear_config_cache()

            applied = srv._apply_runtime_config(merged_config)
            needs_restart = srv._check_needs_restart(old_config, merged_config)

            reloaded_components: list[str] = []
            if not needs_restart:
                try:
                    from app import get_container
                    from container import detect_changed_sections
                    container = get_container()
                    if container is not None:
                        changed_sections = detect_changed_sections(old_config, merged_config)
                        if changed_sections:
                            reloaded_components = container.reload(changed_sections, merged_config)
                except Exception as e:
                    logger.warning("容器热重载失败（降级到原有热更新）: %s", e)

        if needs_restart:
            message = "配置已保存。部分项（LLM/路径/端口）需重启服务生效。"
        elif reloaded_components:
            message = f"配置已保存。{len(reloaded_components)} 个组件已热重载。"
        elif applied:
            ok_count = sum(1 for v in applied.values() if v)
            message = f"配置已保存并即时生效（{ok_count} 项热更新）。"
        else:
            message = "配置已保存。"
        logger.info("配置已通过 API 更新，needs_restart=%s, applied=%s, reloaded=%s",
                    needs_restart, applied, reloaded_components)
        return ConfigUpdateResponse(
            status="saved", message=message, needs_restart=needs_restart
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("更新配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"更新配置失败: {e}")
