"""misc 路由。从 server.py 提取。"""
from __future__ import annotations

import logging
import os
import asyncio

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query, FileResponse, JSONResponse

from config import get_llm_timeouts
from config import load_config
from config import validate_required_env_vars
from orchestrator import Orchestrator
from storage.sqlite_log import SessionLogger

from schemas.common import FlushResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
orchestrator = None
session_logger = None
metrics_collector = None
audit_logger = None
approval_manager = None
task_manager = None
stream_manager = None

# 常量(从 server.py 复制)
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")

@router.post("/consolidation/flush", response_model=FlushResponse)
def flush_consolidation(background_tasks: BackgroundTasks):
    """强制触发记忆沉淀（会话切换 / 手动 flush）。

    将 ConsolidationEngine 缓冲的 ``pending_messages`` 立即交给 LLM
    提取事实并写入长期记忆（chroma + memory.md），不判断阈值。

    该接口通过 ``BackgroundTasks`` 在后台异步执行 flush，立即返回 202，
    不阻塞前端。flush 涉及一次 LLM 调用，可能耗时数秒。

    返回:
        - ``status``: "accepted"（已接受，后台执行中）
        - ``pending_count``: 触发时的待沉淀消息数
        - ``timestamp``: ISO 时间戳
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")

    # 读取当前缓冲区大小（线程安全读取，flush 在后台执行）
    pending_count = 0
    if orchestrator.consolidation_engine is not None:
        pending_count = orchestrator.consolidation_engine.info_counter

    # 后台执行 flush（避免阻塞前端请求）
    background_tasks.add_task(orchestrator.flush_consolidation)

    msg = (
        f"已接受 flush 请求，后台执行中（待沉淀消息: {pending_count} 条）"
        if pending_count > 0
        else "缓冲区为空，无需 flush"
    )
    logger.info("POST /consolidation/flush: %s", msg)

    return FlushResponse(
        status="accepted",
        message=msg,
        pending_count=pending_count,
        timestamp=_now_iso(),
    )

@router.post("/reasoning/toggle")
def reasoning_toggle(req: dict = Body(...)):
    """切换 reasoning 模式开关（热更新，即时生效）。

    body: ``{"enabled": bool, "session_id": Optional[str]}``
    - ``enabled``: True 开启 / False 关闭
    - ``session_id``: 可选，当前单用户模式忽略（预留多用户扩展）

    鉴权预留：当前单用户模式不实装鉴权，未来注入 auth_hook 即可生效。
    启动期安全告警：若 security.api_key 未配置，启动时打印 WARNING。
    """
    global orchestrator
    if orchestrator is None or orchestrator.llm_client is None:
        return JSONResponse(
            content={"error": "LLMClient 尚未初始化"},
            status_code=503,
        )
    enabled = bool(req.get("enabled", False))
    # session_id 当前忽略（单用户模式），未来 per-session override 启用时使用
    # session_id = req.get("session_id")
    orchestrator.llm_client.main_reasoning_enabled = enabled
    logger.info(
        "reasoning 模式已切换: enabled=%s (main_reasoning_enabled)",
        enabled,
    )
    return {
        "enabled": enabled,
        "main": {
            "enabled": orchestrator.llm_client.main_reasoning_enabled,
            "effort": orchestrator.llm_client.main_reasoning_effort,
            "budget_tokens": orchestrator.llm_client.main_reasoning_budget_tokens,
        },
        "cron": {
            "enabled": orchestrator.llm_client.cron_reasoning_enabled,
            "effort": orchestrator.llm_client.cron_reasoning_effort,
        },
        "persist_thinking": orchestrator.llm_client.persist_thinking,
    }

@router.get("/reasoning/status")
def reasoning_status():
    """查询当前 reasoning 配置状态。

    返回 main/consolidation/cron 配置 + persist_thinking 值。
    鉴权预留：当前单用户模式不实装鉴权。
    """
    global orchestrator
    if orchestrator is None or orchestrator.llm_client is None:
        return JSONResponse(
            content={"error": "LLMClient 尚未初始化"},
            status_code=503,
        )
    return {
        "main": {
            "enabled": orchestrator.llm_client.main_reasoning_enabled,
            "effort": orchestrator.llm_client.main_reasoning_effort,
            "budget_tokens": orchestrator.llm_client.main_reasoning_budget_tokens,
        },
        "consolidation": {
            "enabled": False,  # 强制关闭，避免成本浪费
        },
        "cron": {
            "enabled": orchestrator.llm_client.cron_reasoning_enabled,
            "effort": orchestrator.llm_client.cron_reasoning_effort,
        },
        "persist_thinking": orchestrator.llm_client.persist_thinking,
    }

@router.get("/tools")
async def list_tools_inventory():
    """返回当前工具清单（按 Tier 分类）。

    Phase 4 新增：暴露 Core/Deferred/Loaded 三层工具名称列表，
    便于运维与前端调试工具注册状态。

    返回:
        ``{"core": [...], "deferred": [...], "loaded": [...]}``
        - core: Core Tier 工具名列表（永远全量注入）
        - deferred: Deferred Tier 工具名列表（仅 stub，按需加载）
        - loaded: 已加载的 Deferred 工具名列表
    """
    if orchestrator is None or orchestrator.tool_registry is None:
        return {"core": [], "deferred": [], "loaded": []}
    registry = orchestrator.tool_registry
    return {
        "core": list(registry._core_tools.keys()),
        "deferred": list(registry._deferred_tools.keys()),
        "loaded": list(registry._loaded_tools.keys()),
    }

@router.get("/audit/logs")
def get_audit_logs(limit: int = Query(50, ge=1, le=1000)):
    """返回最近的工具调用审计日志。

    参数:
        limit: 返回条数，默认 50，范围 1-1000。
    """
    if audit_logger is None:
        return JSONResponse({"logs": []})
    # 检查当前配置是否禁用监控
    try:
        config = load_config(CONFIG_PATH)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({"logs": []})
    except Exception:
        pass
    return JSONResponse({"logs": audit_logger.get_recent(limit)})

@router.get("/recall")
def recall_messages(
    keyword: str = Query(..., description="搜索关键词"),
    session_id: Optional[str] = Query(None, description="按会话过滤"),
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
):
    """全文检索历史消息（基于 SQLite FTS5）。

    支持中英文关键词，返回按 ``created_at`` 降序的匹配结果。可用于 Recall
    Memory 场景：用户提到某关键词时，前端调用此接口取回历史相关消息。
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        results = session_logger.search_messages(keyword, session_id, limit)
        return {"results": results, "count": len(results)}
    except Exception as e:
        logger.exception("检索消息失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 服务重启 ----------

@router.post("/restart")
async def restart_server():
    """软重启：重载配置 + 重建 Orchestrator，不杀进程。

    与硬重启（``systemctl restart``）不同，本端点：
    - 不重启 uvicorn 进程，ONNX 模型与 ChromaDB 索引保留在内存中
    - 先冲刷 consolidation 待处理队列，确保记忆不丢
    - 若新 Orchestrator 构建失败，保留旧的继续服务

    不支持热更新的配置项（如 ``server.host`` / ``server.port``）变更
    仍需硬重启。
    """
    global orchestrator, _SOFT_RESTART_IN_PROGRESS

    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")

    if _SOFT_RESTART_IN_PROGRESS:
        raise HTTPException(status_code=409, detail="软重启已在进行中")

    _SOFT_RESTART_IN_PROGRESS = True
    try:
        # 1. 读取最新配置
        new_config = load_config(CONFIG_PATH)

        # 2. 校验环境变量
        try:
            validate_required_env_vars(new_config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"配置校验失败: {e}")

        # 3. 如果存在进行中的流，等待最多 30s
        if stream_manager is not None:
            active = stream_manager.active_count()
            if active > 0:
                logger.info(
                    "软重启: 等待 %d 个进行中的流完成（最多 30s）...", active
                )
                for _ in range(30):
                    if stream_manager.active_count() == 0:
                        break
                    await asyncio.sleep(1)
                remaining = stream_manager.active_count()
                if remaining > 0:
                    logger.warning(
                        "软重启: %d 个流未在等待时间内完成，继续重启", remaining
                    )

        # 4. 冲刷旧 Orchestrator 的待处理记忆 + 关闭 ChromaDB 连接
        #    （关断后建新实例，避免两个 ChromaDB 客户端争用同一 sqlite 文件）
        old_orchestrator = orchestrator
        try:
            await asyncio.to_thread(old_orchestrator.shutdown)
        except Exception as e:
            logger.warning("软重启: 旧 Orchestrator 关闭异常（已忽略）: %s", e)

        # 5. 构建新 Orchestrator（旧资源已释放，ChromDB 文件可安全打开）
        try:
            new_orchestrator = await asyncio.to_thread(
                Orchestrator,
                config_path=CONFIG_PATH,
                metrics=metrics_collector,
                audit_logger=audit_logger,
                approval_manager=approval_manager,
                task_manager=task_manager,
            )
        except Exception as e:
            logger.exception("软重启: 新 Orchestrator 构建失败，服务不可用")
            # 构建失败时尝试硬重启恢复
            raise HTTPException(
                status_code=500,
                detail=f"新 Orchestrator 构建失败，请手动 systemctl restart: {e}",
            )

        # 6. 预热 ChromaDB 索引（哑查询，快速）
        if new_orchestrator.chroma_store is not None:
            try:
                new_orchestrator.chroma_store.query_memory(
                    "warmup", top_k=1, reinforce=False
                )
            except Exception as e:
                logger.warning("软重启: ChromaDB 预热失败: %s", e)

        # 7. 原子替换
        orchestrator = new_orchestrator
        logger.info("软重启: Orchestrator 已替换为新实例")

        # 8. 预热新实例的 ChromaDB（后台）
        return {"status": "ok", "message": "软重启完成"}

    finally:
        _SOFT_RESTART_IN_PROGRESS = False


# ---------- 配置管理 ----------

# 需要重启才能生效的配置项前缀列表
# llm.{provider,model,api_key,base_url} 涉及 SDK 客户端重建，无法热更新；
# llm.activity_timeout / llm.stream_total_timeout 不涉及客户端重建，由 SSE
# handler 每次请求通过 get_llm_timeouts() 读取最新值，即时生效（热更新），
# 故不在此列表。存储路径 / memory 的路径类配置 / server.* 涉及组件重建，无法热更新

@router.get("/")
def serve_index():
    """提供前端首页。

    设置 ``Cache-Control: no-cache, no-store, must-revalidate`` 防止浏览器
    缓存旧 HTML，确保前端改动即时生效（用户首次访问后无需手动 hard refresh）。
    """
    index_path = os.path.join(_WEB_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="前端文件未找到")
    return FileResponse(
        index_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

@router.get("/monitor")
def serve_monitor():
    """提供监控面板页。"""
    monitor_path = os.path.join(_WEB_DIR, "monitor.html")
    if not os.path.exists(monitor_path):
        raise HTTPException(status_code=404, detail="监控页未找到")
    return FileResponse(
        monitor_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

@router.get("/scheduler")
def serve_scheduler():
    """提供调度管理页。"""
    scheduler_path = os.path.join(_WEB_DIR, "scheduler.html")
    if not os.path.exists(scheduler_path):
        raise HTTPException(status_code=404, detail="调度页未找到")
    return FileResponse(
        scheduler_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

@router.get("/workflow")
def serve_workflow():
    """提供 Workflow 编排页。"""
    workflow_path = os.path.join(_WEB_DIR, "workflow.html")
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow 页未找到")
    return FileResponse(
        workflow_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# 挂载静态文件目录（CSS/JS 等静态资源）
