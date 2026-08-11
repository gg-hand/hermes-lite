"""config 路由：配置读取与更新（含热重载逻辑）。

Task 11: 从 server.py 迁移 2 个端点。重构后直接从 config_helpers / config
导入辅助函数，不再通过 server 模块 re-export 访问。CONFIG_PATH 仍从 server
模块读取（常量保留在 server.py，且测试通过 patch("server.CONFIG_PATH") 注入）。
"""
from __future__ import annotations

import logging
import os
import sys

from fastapi import APIRouter, HTTPException, Request
from teage_liu.schemas.config import ConfigResponse, ConfigUpdateRequest, ConfigUpdateResponse

# 直接从源模块导入辅助函数（不再依赖 server.py re-export）
from teage_liu.config import (
    load_config, clear_config_cache, write_config_with_sensitive_separation,
    mask_sensitive_config, unmask_sensitive_config,
)
from teage_liu.config_helpers import (
    _config_write_lock,
    _deep_merge_config,
    _validate_config_schema,
    _backup_config,
    _apply_runtime_config,
    _check_needs_restart,
)

logger = logging.getLogger("teage_liu.server")

router = APIRouter()


def _server():
    """获取已加载的 server 模块（兼容 src.server 和 server）。

    仅用于读取 CONFIG_PATH 常量（仍保留在 server.py）。
    """
    return sys.modules.get("teage_liu.server") or sys.modules.get("server")


def _get_config_path() -> str:
    """获取 CONFIG_PATH（兼容测试 patch src.server.CONFIG_PATH / server.CONFIG_PATH）。"""
    srv = _server()
    if srv is not None:
        return getattr(srv, "CONFIG_PATH", "config.yaml")
    return "config.yaml"


@router.get("/config", response_model=ConfigResponse)
def get_config():
    try:
        config = load_config(_get_config_path())
        # Task 23：API Key 等敏感字段脱敏后返回前端，防止 DevTools 窃取
        masked = mask_sensitive_config(config)
        return ConfigResponse(config=masked)
    except Exception as e:
        logger.exception("读取配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")


@router.put("/config", response_model=ConfigUpdateResponse)
async def update_config(req: ConfigUpdateRequest, request: Request):
    config_path = _get_config_path()
    try:
        with _config_write_lock:
            try:
                old_config = load_config(config_path)
            except Exception:
                old_config = {}

            merged_config = _deep_merge_config(old_config, req.config)

            # Task 23：前端提交的脱敏字段（含 ****）还原为服务端实际值，
            # 防止前端用脱敏值覆盖真实 API Key
            merged_config = unmask_sensitive_config(merged_config, old_config)

            try:
                _validate_config_schema(merged_config)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            _backup_config(config_path)
            # 敏感字段分离：API Key 等实际值写入 .env，config.yaml 保留 ${VAR} 占位符
            env_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), ".env")
            write_config_with_sensitive_separation(merged_config, config_path, env_path)
            clear_config_cache()

            applied = _apply_runtime_config(merged_config, old_config=old_config)
            needs_restart = _check_needs_restart(old_config, merged_config)

            reloaded_components: list[str] = []
            if not needs_restart:
                try:
                    from teage_liu.app import get_container
                    from teage_liu.container import detect_changed_sections
                    container = get_container()
                    if container is not None:
                        changed_sections = detect_changed_sections(old_config, merged_config)
                        if changed_sections:
                            reloaded_components = container.reload(changed_sections, merged_config)
                except Exception as e:
                    logger.warning("容器热重载失败（降级到原有热更新）: %s", e)

            # 热重载后重启持有旧实例的后台 task
            if reloaded_components:
                try:
                    task_registry = request.app.state.task_registry
                    from teage_liu.background_loops import cleanup_loop, file_cleanup_loop, metrics_persist_loop

                    if "metrics_collector" in reloaded_components or "metrics_store" in reloaded_components:
                        new_mc = container.get("metrics_collector")
                        new_ms = container.get("metrics_store")
                        reset_event = getattr(request.app.state, "metrics_reset_event", None)
                        await task_registry.restart(
                            "metrics_persist",
                            lambda: metrics_persist_loop(new_mc, new_ms, reset_event)
                        )

                    if "session_logger" in reloaded_components or "metrics_store" in reloaded_components:
                        await task_registry.restart(
                            "cleanup",
                            lambda: cleanup_loop(
                                container.get("session_logger"),
                                container.get("metrics_store"),
                                container.get("orchestrator"),
                            )
                        )

                    if "upload_manager" in reloaded_components:
                        await task_registry.restart(
                            "file_cleanup",
                            lambda: file_cleanup_loop(container.get("upload_manager"))
                        )
                except AttributeError:
                    pass  # task_registry 不存在（测试环境）
                except Exception as e:
                    logger.warning("热重载后重启后台 task 失败: %s", e)

        if needs_restart:
            message = "配置已保存。部分项（路径/端口/规则）需重启服务生效。"
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
