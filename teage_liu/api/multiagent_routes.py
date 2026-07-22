"""multiagent REST 端点：状态查询 + 消息/审计读取 + Director 信息 + SSE。

Plan 4 Task 1：实现 multiagent_alert SSE 通道与状态查询端点。

端点：
- GET /api/multiagent/status           协作总览状态
- GET /api/multiagent/agents           agent 列表
- GET /api/multiagent/messages         消息列表（支持 limit 参数）
- GET /api/multiagent/audit            审计记录（支持 limit 参数）
- GET /api/multiagent/director         director.md 内容
- GET /api/multiagent/sse              multiagent_alert SSE 通道

事件类型（SSE）：
- initial                 首次推送
- director_state_change   Director 健康状态变更
- agent_join              新 agent 加入
- agent_leave             agent 离线
- autonomous_enter        进入自治模式
- autonomous_exit         退出自治模式
- message_append          新消息追加
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)


def create_multiagent_router(container) -> APIRouter:
    """创建 multiagent 路由器。

    Args:
        container: DI 容器（需有 .config 属性）

    Returns:
        APIRouter 实例。multiagent.enabled=False 时所有端点返回 404。
    """
    router = APIRouter(prefix="/api/multiagent", tags=["multiagent"])
    config = container.config
    multiagent_cfg = config.get("multiagent", {}) or {}

    # 如果 multiagent 未启用，所有端点返回 404
    if not multiagent_cfg.get("enabled"):
        @router.get("/{path:path}")
        async def not_found(path: str):
            raise HTTPException(status_code=404, detail="multiagent disabled")

        return router

    bb_dir = multiagent_cfg.get("blackboard_dir", "data/blackboard")
    bb_root = Path(bb_dir)

    @router.get("/status")
    async def get_status() -> dict:
        """获取协作总览状态。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.blackboard import read_director_md, read_json
        from teage_liu.multiagent.schema_validator import SchemaValidator

        status = read_json(bb_root / "status.json") if (bb_root / "status.json").exists() else {}
        director_md = await read_director_md(bb_root) or {}
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        agents = await registry.list_active_agents()

        # 判断 Director 健康状态（基于 last_director_tick）
        director_state = _determine_director_state(director_md)
        director_agent_id = status.get("director_signature", "") or director_md.get("director_id", "")
        director_epoch = director_md.get("current_epoch", 0)
        last_tick = director_md.get("last_director_tick", "")

        autonomous_mode = status.get("recovery_stage", "idle") == "autonomous"

        return {
            "enabled": True,
            "role": multiagent_cfg.get("role", "worker"),
            "director": {
                "agent_id": director_agent_id,
                "epoch": director_epoch,
                "last_tick": last_tick,
                "state": director_state,
            },
            "agents": agents,
            "autonomous_mode": autonomous_mode,
        }

    @router.get("/agents")
    async def get_agents() -> dict:
        """获取 agent 列表。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        agents = await registry.list_active_agents()
        return {"agents": agents}

    @router.get("/messages")
    async def get_messages(limit: int = 100) -> dict:
        """获取消息列表（最近 limit 条）。"""
        from teage_liu.multiagent.blackboard import read_messages

        messages = await read_messages(bb_root)
        return {"messages": messages[-limit:] if limit > 0 else messages}

    @router.get("/audit")
    async def get_audit(limit: int = 100) -> dict:
        """获取审计记录（最近 limit 条）。"""
        from teage_liu.multiagent.blackboard import read_audit_records

        records = await read_audit_records(bb_root, limit=limit if limit > 0 else 100)
        return {"records": records}

    @router.get("/director")
    async def get_director() -> dict:
        """获取 director.md frontmatter 内容。"""
        from teage_liu.multiagent.blackboard import read_director_md

        data = await read_director_md(bb_root)
        return data or {}

    @router.get("/sse")
    async def sse_stream(request: Request) -> StreamingResponse:
        """multiagent_alert SSE 通道。

        事件类型：
        - initial: 首次推送当前状态
        - director_state_change: Director 健康状态变更
        - agent_join / agent_leave: agent 列表变更
        - autonomous_enter / autonomous_exit: 自治模式切换
        - message_append: 新消息追加
        """
        async def event_stream():
            last_status = None
            # 首次推送 initial 事件
            try:
                current_status = await get_status()
                yield _format_sse("initial", current_status)
                last_status = current_status
            except Exception as e:
                logger.error("SSE initial 推送失败: %s", e)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    current_status = await get_status()
                    event_type = _detect_event_type(last_status, current_status)
                    if event_type:
                        yield _format_sse(event_type, current_status)
                    last_status = current_status
                except Exception as e:
                    logger.error("SSE 流异常: %s", e)
                await asyncio.sleep(2)  # 2 秒轮询

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _format_sse(event_type: str, data: Any) -> str:
    """格式化 SSE 事件块。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


def _determine_director_state(director_md: dict) -> str:
    """根据 director.md 的 last_director_tick 判断健康状态。

    Returns:
        healthy | degraded | autonomous | fault | unknown
    """
    if not director_md:
        return "unknown"
    last_tick = director_md.get("last_director_tick")
    if not last_tick:
        return "unknown"
    try:
        # 兼容 ISO 8601 末尾 Z 或 +00:00
        tick_str = last_tick.replace("Z", "+00:00") if isinstance(last_tick, str) else ""
        tick_dt = datetime.fromisoformat(tick_str)
        if tick_dt.tzinfo is None:
            tick_dt = tick_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age = (now - tick_dt).total_seconds()
        if age < 30:
            return "healthy"
        if age < 60:
            return "degraded"
        if age < 120:
            return "autonomous"
        return "fault"
    except (ValueError, TypeError):
        return "unknown"


def _detect_event_type(old: dict | None, new: dict) -> str | None:
    """检测状态变更事件类型。"""
    if old is None:
        return None  # initial 已在流首推送
    old_director = old.get("director", {}) or {}
    new_director = new.get("director", {}) or {}
    if old_director.get("state") != new_director.get("state"):
        return "director_state_change"
    if old.get("autonomous_mode", False) != new.get("autonomous_mode", False):
        return "autonomous_enter" if new.get("autonomous_mode") else "autonomous_exit"
    old_agents = {a.get("agent_id") for a in old.get("agents", []) if a.get("agent_id")}
    new_agents = {a.get("agent_id") for a in new.get("agents", []) if a.get("agent_id")}
    if new_agents - old_agents:
        return "agent_join"
    if old_agents - new_agents:
        return "agent_leave"
    return None
