"""config 路由。从 server.py 提取。"""
from __future__ import annotations

import logging
import os
import yaml

from fastapi import APIRouter, HTTPException

from config import clear_config_cache
from config import load_config

from schemas.config import ConfigResponse, ConfigUpdateRequest, ConfigUpdateResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
orchestrator = None

# 常量(从 server.py 复制)
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")

@router.get("/config", response_model=ConfigResponse)
def get_config():
    """读取当前配置文件内容。"""
    try:
        config = load_config(CONFIG_PATH)
        return ConfigResponse(config=config)
    except Exception as e:
        logger.exception("读取配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")

@router.put("/config", response_model=ConfigUpdateResponse)
def update_config(req: ConfigUpdateRequest):
    """更新配置文件并写入磁盘，运行时参数即时生效。

    配置分两类处理：
    1. **可热更新**（memory 阈值/top_k/max_turns、tools 循环上限等）：
       写入磁盘后立即通过 :func:`_apply_runtime_config` 修改内存中
       orchestrator 组件的属性，无需重启即时生效。
    2. **需重启**（llm provider/model/api_key、存储路径、服务端口、
       memory 路径类配置）：仅写入磁盘，需重启服务才能生效，
       响应中 ``needs_restart=True``。

    写入流程（在 :data:`_config_write_lock` 串行锁保护下执行）：
    1. 读取旧配置用于比较（读取失败视为空字典）；
    2. :func:`_deep_merge_config` 深度合并新旧配置，未在新配置出现的段保留旧值，
       避免部分更新丢失其他段；
    3. :func:`_validate_config_schema` 校验合并后配置的结构与字段类型，
       失败时抛 ``HTTPException(400)`` 且不写盘；
    4. :func:`_backup_config` 备份旧配置到 ``config.yaml.bak``，备份失败仅记录
       warning 不中断流程；
    5. :func:`_atomic_write_config` 原子写入：先写 ``.tmp`` 临时文件并 ``fsync``
       刷盘，再通过 ``os.replace`` 原子替换，避免写入中途崩溃导致配置损坏；
    6. :func:`_apply_runtime_config` 将可热更新项即时应用到内存中 orchestrator 组件；
    7. :func:`_check_needs_restart` 比较新旧配置判断是否需要重启。

    响应消息与日志在锁外构造，以减少锁持有时间。
    """
    try:
        with _config_write_lock:
            # 1. 读取旧配置用于比较（读取失败视为空字典）
            try:
                old_config = load_config(CONFIG_PATH)
            except Exception:
                old_config = {}

            # 2. 深度合并：新配置覆盖旧配置，未在新配置出现的段保留旧值
            merged_config = _deep_merge_config(old_config, req.config)

            # 3. Schema 校验：失败时返回 400 且不写盘（校验在备份/写盘之前）
            try:
                _validate_config_schema(merged_config)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            # 4. 备份旧配置（失败仅 warning，不中断后续流程）
            _backup_config(CONFIG_PATH)

            # 5. 原子写入合并后的新配置到磁盘
            _atomic_write_config(CONFIG_PATH, merged_config)

            # 5.5 使配置缓存失效，避免快速连续 PUT 时 mtime 未变
            clear_config_cache()

            # 6. 应用热更新到运行时组件（用 merged_config 而非 req.config）
            applied = _apply_runtime_config(merged_config)

            # 7. 检查是否需要重启（比较 old_config 与 merged_config）
            needs_restart = _check_needs_restart(old_config, merged_config)

        # 锁外构造响应消息与日志，减少锁持有时长
        if needs_restart:
            message = "配置已保存。部分项（LLM/路径/端口）需重启服务生效。"
        elif applied:
            ok_count = sum(1 for v in applied.values() if v)
            message = f"配置已保存并即时生效（{ok_count} 项热更新）。"
        else:
            message = "配置已保存。"
        logger.info("配置已通过 API 更新，needs_restart=%s, applied=%s", needs_restart, applied)
        return ConfigUpdateResponse(
            status="saved", message=message, needs_restart=needs_restart
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("更新配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"更新配置失败: {e}")


# ---------- Phase 7 Task 4: 记忆 Dashboard 端点 ----------
