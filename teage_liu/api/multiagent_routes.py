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
import os
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

        @router.post("/{path:path}")
        async def not_found_post(path: str):
            raise HTTPException(status_code=404, detail="multiagent disabled")

        return router

    # 优先使用 TEAGE_BB_ROOT 环境变量（与 collab_router 保持一致）
    # 避免相对路径在不同工作目录下解析到错误位置
    import os as _os
    bb_dir = _os.environ.get("TEAGE_BB_ROOT") or multiagent_cfg.get("blackboard_dir", "data/blackboard")
    bb_root = Path(bb_dir)

    from teage_liu.multiagent.director_manager import create_director_manager
    director_manager = create_director_manager(config)

    @router.get("/status")
    async def get_status() -> dict:
        """获取协作总览状态。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.blackboard import (
            read_director_md, read_json, _read_last_message_seq,
            read_collab_messages,
        )
        from teage_liu.multiagent.schema_validator import SchemaValidator

        status = read_json(bb_root / "status.json") if (bb_root / "status.json").exists() else {}
        director_md = await read_director_md(bb_root) or {}
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        agents = await registry.list_active_agents()

        # 获取进程级状态（stopped/crashed/starting/healthy/degraded）
        # 用于区分"主动停止（休眠）"与"故障崩溃"，避免误判为 fault
        try:
            proc_status = await director_manager.status()
        except Exception as e:
            logger.warning("读取 director_manager.status() 失败，回退到心跳判定: %s", e)
            proc_status = None
        # 融合进程级状态与心跳延迟，判定 Director 健康状态
        director_state = _determine_director_state_v2(director_md, proc_status)
        # 计算故障/异常原因（healthy 时为空字符串），供前端 tooltip 展示
        director_fault_reason = _determine_director_fault_reason(director_md, director_state)
        director_agent_id = status.get("director_signature", "") or director_md.get("director_id", "")
        director_epoch = director_md.get("current_epoch", 0)
        last_tick = director_md.get("last_director_tick", "")

        autonomous_mode = status.get("recovery_stage", "idle") == "autonomous"

        # 读取最新消息 seq，供 SSE 检测新消息
        last_message_seq = await _read_last_message_seq(bb_root)

        # 本地 agent_id（worker 模式下从 worker 配置取，director 模式下从 director 配置取）
        local_agent_id = ""
        if multiagent_cfg.get("role") == "worker":
            local_agent_id = multiagent_cfg.get("worker", {}).get("agent_id", "")
        elif multiagent_cfg.get("role") == "director":
            local_agent_id = director_agent_id

        # epoch_history：从 collab directive 消息提取最近 5 个节点
        # 每条 directive 视为一次 epoch 推进；若无历史则仅返回当前 epoch 单节点
        epoch_history: list[dict] = []
        try:
            collab_msgs = await read_collab_messages(bb_root)
            directive_msgs = [
                m for m in collab_msgs
                if m.get("type") == "directive" and m.get("from") == "director"
            ]
            # 取最新 5 条，按时间倒序
            directive_msgs_sorted = sorted(
                directive_msgs,
                key=lambda m: m.get("timestamp", ""),
                reverse=True,
            )[:5]
            # epoch 编号：从当前 epoch 倒推（最新 directive = 当前 epoch）
            for idx, m in enumerate(directive_msgs_sorted):
                epoch_history.append({
                    "epoch": director_epoch - idx,
                    "ts": m.get("timestamp", ""),
                    "seq": m.get("seq", 0),
                })
        except Exception:
            # 提取失败时降级为空列表，不阻塞 status 响应
            pass

        # 若 epoch_history 为空，至少返回当前 epoch 单节点
        if not epoch_history and director_epoch:
            epoch_history = [{
                "epoch": director_epoch,
                "ts": last_tick or "",
                "seq": 0,
            }]

        return {
            "enabled": True,
            "role": multiagent_cfg.get("role", "worker"),
            "local_agent_id": local_agent_id,
            "director": {
                "agent_id": director_agent_id,
                "epoch": director_epoch,
                "last_tick": last_tick,
                "state": director_state,
                "fault_reason": director_fault_reason,
            },
            "agents": agents,
            "autonomous_mode": autonomous_mode,
            "last_message_seq": last_message_seq,
            "epoch_history": epoch_history,
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

    # ========== Agent Card 查看（只读） ==========

    @router.get("/worker/config")
    async def get_worker_config() -> dict:
        """获取当前本地 worker 配置（只读查看）。"""
        try:
            adapter = container.get("multiagent_adapter")
            if adapter is None:
                # director 模式下无 worker adapter，从 config 读取
                worker_cfg = multiagent_cfg.get("worker", {})
                return {"worker": worker_cfg, "role": multiagent_cfg.get("role", "worker")}
            config = await adapter.get_worker_config()
            return {"worker": config, "role": multiagent_cfg.get("role", "worker")}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取 worker 配置失败: {e}")

    @router.get("/agents/{agent_id}/card")
    async def get_agent_card(agent_id: str) -> dict:
        """查看指定 agent 的 agent_card（只读）。

        从 bb_root/agents/{agent_id}.md 读取 YAML frontmatter。
        """
        from teage_liu.multiagent.blackboard import read_yaml_frontmatter

        card_path = bb_root / "agents" / f"{agent_id}.md"
        if not card_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' 的 card 不存在")
        try:
            # read_yaml_frontmatter 是同步函数，返回 (frontmatter_dict, body_str)
            frontmatter, _body = read_yaml_frontmatter(card_path)
            return {"agent_id": agent_id, "card": frontmatter or {}}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取 agent_card 失败: {e}")

    @router.post("/director/start")
    async def start_director() -> dict:
        result = await director_manager.start()
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("message", "启动失败"))
        return result

    @router.post("/director/stop")
    async def stop_director() -> dict:
        result = await director_manager.stop()
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("message", "停止失败"))
        return result

    @router.post("/director/restart")
    async def restart_director() -> dict:
        result = await director_manager.restart()
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("message", "重启失败"))
        return result

    @router.get("/director/status")
    async def director_status() -> dict:
        return await director_manager.status()

    @router.post("/agents/register", deprecated=True, summary="[Deprecated] 使用 A2A register_remote_agent 替代")
    async def register_agent(payload: dict) -> dict:
        """[已废弃] 远程 agent 注册，请改用 A2A Gateway 的 register_remote_agent 方法。

        此端点保留向后兼容，未来版本将移除。
        """
        agent_id = (payload.get("agent_id") or "").strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id 不能为空")

        # 检查是否已存在
        agents_dir = os.path.join(bb_root, "agents")
        agent_file = os.path.join(agents_dir, f"{agent_id}.md")
        if os.path.exists(agent_file):
            raise HTTPException(status_code=409, detail=f"agent {agent_id} 已存在")

        agent_data = {
            "agent_id": agent_id,
            "role": payload.get("role", "worker"),
            "status": "active",
            "protocol_version": "1.0.0",
            "agent_version": "1.0.0",
            "capabilities": payload.get("capabilities", []),
            "specialties": [],
            "auth_method": payload.get("auth_method", "local"),
            "endpoint": payload.get("endpoint", ""),
            "owner": "",
            "max_concurrent_tasks": 3,
            "heartbeat_interval_seconds": 10,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "trust_score": 100,
            "trust_history": [],
            "extensions": {},
            "leave_reason": "",
            "left_at": "",
            "dangerous_tools": [],
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "host": None,
            "pid": None,
        }

        # 写入 agent_card.md
        os.makedirs(agents_dir, exist_ok=True)
        frontmatter = "---\n"
        for k, v in agent_data.items():
            if isinstance(v, (list, dict)):
                frontmatter += f"{k}: {v}\n"
            elif isinstance(v, str) and v:
                frontmatter += f"{k}: '{v}'\n"
            elif v is not None:
                frontmatter += f"{k}: {v}\n"
            else:
                frontmatter += f"{k}: null\n"
        frontmatter += "---\n\n# Agent Card\n"

        with open(agent_file, "w", encoding="utf-8") as f:
            f.write(frontmatter)

        logger.info("Agent 注册: %s", agent_id)
        return {"ok": True, "message": f"agent {agent_id} 已注册"}

    @router.post("/agents/{agent_id}/heartbeat")
    async def agent_heartbeat(agent_id: str, payload: dict) -> dict:
        """更新 agent 心跳。"""
        agent_file = os.path.join(bb_root, "agents", f"{agent_id}.md")
        if not os.path.exists(agent_file):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} 不存在")

        # 读取现有内容
        with open(agent_file, "r", encoding="utf-8") as f:
            content = f.read()

        # 更新 last_heartbeat
        new_ts = datetime.now(timezone.utc).isoformat()
        lines = content.split("\n")
        updated = []
        for line in lines:
            if line.startswith("last_heartbeat:"):
                updated.append(f"last_heartbeat: '{new_ts}'")
            elif line.startswith("status:"):
                updated.append(f"status: {payload.get('status', 'active')}")
            else:
                updated.append(line)

        with open(agent_file, "w", encoding="utf-8") as f:
            f.write("\n".join(updated))

        return {"ok": True, "next_heartbeat_due": 10}

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
            last_seen_seq = 0
            # 首次推送 initial 事件
            try:
                current_status = await get_status()
                yield _format_sse("initial", current_status)
                last_status = current_status
                last_seen_seq = current_status.get("last_message_seq", 0)
            except Exception as e:
                logger.error("SSE initial 推送失败: %s", e)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    current_status = await get_status()
                    # 检测状态变更事件（director/agent/autonomous）
                    event_type = _detect_event_type(last_status, current_status)
                    if event_type:
                        yield _format_sse(event_type, current_status)

                    # 检测新消息：当 last_message_seq 增长时，推送 message_append
                    current_seq = current_status.get("last_message_seq", 0)
                    if current_seq > last_seen_seq:
                        from teage_liu.multiagent.blackboard import read_messages
                        all_messages = await read_messages(bb_root)
                        new_messages = [
                            m for m in all_messages
                            if m.get("seq", 0) > last_seen_seq
                        ]
                        if new_messages:
                            yield _format_sse("message_append", {"messages": new_messages})
                        last_seen_seq = current_seq

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


def _determine_director_state_v2(
    director_md: dict,
    director_status: dict | None = None,
) -> str:
    """融合进程级状态与心跳延迟，判定 Director 健康状态。

    修复 bug：旧逻辑仅看心跳延迟，Director 主动停止后心跳过期被误判为 fault
    （红色故障），实际应区分"主动停止（休眠）"与"故障崩溃"。

    优先级：
    1. 进程级状态（director_manager.status()）作为顶层判定
       - stopped（未启动，无 PID 文件）→ 直接返回 stopped
       - crashed（PID 文件存在但进程不在）→ 直接返回 crashed
       - starting（进程刚启动，无心跳）→ 直接返回 starting
    2. 进程运行中（running=True）时，进一步用心跳延迟细分
       healthy / degraded / autonomous / fault
    3. 无进程级信息时，回退到只看心跳的旧行为（向后兼容）

    Args:
        director_md: director.md frontmatter（心跳时间戳）
        director_status: director_manager.status() 的返回值（可选）

    Returns:
        stopped | starting | crashed | healthy | degraded | autonomous | fault | unknown
    """
    if director_status:
        proc_state = director_status.get("state", "")
        running = director_status.get("running", False)
        # 进程级终态：stopped（休眠）/ crashed（崩溃）→ 直接返回，不查心跳
        if proc_state == "stopped":
            return "stopped"
        if proc_state == "crashed":
            return "crashed"
        if proc_state == "starting":
            return "starting"
        # running=True 时，进一步用心跳延迟细分到 healthy/degraded/autonomous/fault
        # 覆盖进程在跑但心跳停了（僵死）的真实故障场景
        if running:
            return _determine_director_state(director_md)
    # 无进程级信息时回退到只看心跳的旧行为（向后兼容）
    return _determine_director_state(director_md)


def _determine_director_fault_reason(director_md: dict, state: str) -> str:
    """根据 director 状态与心跳延迟返回人类可读的故障/异常原因。

    状态正常（healthy）时返回空字符串；其余状态返回对应说明，供前端 tooltip 展示。

    Args:
        director_md: director.md frontmatter（可能为 None/空）
        state: _determine_director_state_v2 的返回值

    Returns:
        故障原因文本，无异常时为空字符串
    """
    if state == "healthy":
        return ""
    # 进程级状态：stopped/crashed/starting（无需心跳信息）
    if state == "stopped":
        return "Director 未启动（已休眠），可点击启动按钮唤醒"
    if state == "crashed":
        return "Director 进程已崩溃，请重启"
    if state == "starting":
        return "Director 正在启动中，请稍候"
    if state == "unknown":
        if director_md is None:
            return "Director 元数据缺失，无法读取状态"
        if not director_md.get("last_director_tick"):
            return "Director 未上报心跳，可能未启动或刚初始化"
        return "Director 心跳时间戳格式无效，无法解析"
    # degraded / autonomous / fault：基于心跳延迟给出说明
    last_tick = director_md.get("last_director_tick") if director_md else ""
    age_str = ""
    try:
        tick_str = last_tick.replace("Z", "+00:00") if isinstance(last_tick, str) else ""
        tick_dt = datetime.fromisoformat(tick_str)
        if tick_dt.tzinfo is None:
            tick_dt = tick_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - tick_dt).total_seconds()
        age_str = f"{age:.0f} 秒"
    except (ValueError, TypeError):
        age_str = "未知时长"
    if state == "degraded":
        return f"Director 心跳延迟 {age_str}，性能降级（阈值 30 秒）"
    if state == "autonomous":
        return f"Director 心跳延迟 {age_str}，已进入自治模式（阈值 60 秒）"
    if state == "fault":
        return f"Director 心跳超时 {age_str}，判定为故障（阈值 120 秒）"
    return ""


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
