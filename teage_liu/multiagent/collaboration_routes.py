"""协作消息 API 路由（Task 3，含 A2/A3/A4 修复 + D2 容器注册模式 + Task 12 索引）。

端点（prefix=/api/multiagent/collab）：
- GET  /messages                获取协作消息列表（支持 collab_id 查询参数）
- POST /append                  写入协作消息
- POST /forward                 转发 A2A 消息（带 message_id 去重）
- POST /broadcast               用户发布广播（from=user, type=request）
- POST /announce                agent 上线/下线/能力更新
- POST /directive               Director 注入 directive（Task 5 用）
- GET  /agents                  获取接入工作台的 agent 列表（A4：registry + announce 合并）
- GET  /collabs                 列出所有协作索引（Task 12）
- POST /collabs                 手动创建/更新协作索引条目（Task 12）
- GET  /sse                     协作消息 SSE 通道（Task 4）
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from teage_liu.multiagent.blackboard import (
    append_collab_message,
    read_collab_messages,
    read_all_active_collab_messages,
    read_collab_index,
    update_collab_index,
    _get_global_writer,
)


def _get_bb_root() -> Path:
    """获取黑板根目录。

    优先用 TEAGE_BB_ROOT 环境变量（测试可设置），其次从当前实例 config 的
    multiagent.blackboard_dir 读取，最后回退到默认 data/blackboard。

    修复：原实现硬编码 data/blackboard，导致 multiagent.blackboard_dir 配置
    自定义路径时（如 blackboard_a2a_1），工作台 REST 路由与 WorkerAdapter 读写
    不同文件，广播永远到不了 worker。
    """
    env_root = os.environ.get("TEAGE_BB_ROOT")
    if env_root:
        return Path(env_root)
    # 从当前实例 config 读取 multiagent.blackboard_dir
    try:
        from teage_liu.config import load_config
        config_path = os.environ.get("TEAGE_CONFIG", "config.yaml")
        config = load_config(config_path)
        bb_dir = config.get("multiagent", {}).get("blackboard_dir")
        if bb_dir:
            return Path(bb_dir)
    except Exception:
        pass
    return Path("data/blackboard")


# ========== Pydantic 请求模型 ==========


class AppendRequest(BaseModel):
    """写入协作消息请求"""
    message: dict
    collab_id: Optional[str] = None


class ForwardRequest(BaseModel):
    """转发 A2A 消息请求（A3 修复：from_ 字段用 alias 接收前端 from）"""
    message_id: str
    from_: str = Field(..., alias="from")
    to: str
    content: str
    via: str = "a2a"
    signature: str = ""

    model_config = {"populate_by_name": True}


class BroadcastRequest(BaseModel):
    """用户广播请求。

    协作生命周期模型：
    - start_collab=True：发起新协作（生成 collab_id，写入 index active），
      适用于 Director 发起一个新任务话题。
    - start_collab=False + collab_id=X：向现有协作 X 追加 Director 信息
      （协作进行中补充上下文/规则），不新建协作。
    - start_collab=False 且 collab_id 为空：拒绝（broadcast 必须归属某协作，
      不允许游离全局广播，避免重新引入全局污染）。
    """
    content: str
    start_collab: bool = False
    collab_id: Optional[str] = None


class AnnounceRequest(BaseModel):
    """agent announce 请求"""
    agent_id: str
    action: str  # online/offline/capability_update
    capabilities: list[str] = []
    endpoint: str = ""
    agent_name: str = ""


class DirectiveRequest(BaseModel):
    """Director 注入 directive 请求（含字段级强化）"""
    content: str
    rule_type: str = "ordering"
    target: str = "*"
    priority: str = "normal"
    deadline: Optional[int] = None
    issued_by: str = "UserDirector"


class CollabIndexRequest(BaseModel):
    """协作索引更新请求（Task 12）"""
    collab_id: str
    title: str = ""
    status: str = "active"
    participants: list[str] = []


# ========== 路由工厂 ==========


def create_collab_router(container=None) -> APIRouter:
    """创建协作消息路由器。

    D2 修复：使用工厂函数，便于在 app.py 中作为容器组件注册。

    Args:
        container: DI 容器（可选）。提供时用于获取 a2a_client，使
            broadcast 路由能将广播转发到所有 A2A remote_endpoints。
            None 时（如单元测试）broadcast 仅写入本地 blackboard。
    """
    router = APIRouter(prefix="/api/multiagent/collab", tags=["collaboration"])

    @router.get("/messages")
    async def get_messages(
        collab_id: Optional[str] = None,
        before_seq: Optional[int] = None,
        after_seq: Optional[int] = None,
        limit: Optional[int] = None,
        include_archived: bool = True,
    ):
        """获取协作消息列表。

        Args:
            collab_id: 协作 ID；None 表示聚合所有协作 + 全局消息
            before_seq: 游标分页（仅 collab_id 指定时生效），仅返回 seq 严格小于此值的消息
            after_seq: 增量游标（仅 collab_id 指定时生效），仅返回 seq 严格大于此值的消息
            limit: 返回数量上限，取最新 N 条；None 或 0 表示不限制
            include_archived: 是否包含归档协作的消息（仅 collab_id=None 聚合视图生效）。
                - True（默认）：聚合 active + archived 协作，工作台全局视图可见历史归档消息。
                - False：仅聚合 active 协作（旧行为），归档协作消息被隐藏。
                指定 collab_id 时本参数无效（单协作查询本就含归档）。
        """
        bb_root = _get_bb_root()
        if collab_id is not None:
            # 查特定协作（含归档）：按 seq 分页
            messages = await read_collab_messages(
                bb_root, collab_id=collab_id,
                before_seq=before_seq, after_seq=after_seq, limit=limit,
            )
        else:
            # 聚合所有协作 + 全局消息（按时间戳合并）。
            # seq 为 per-collab，跨文件不全局连续；before_seq/after_seq 作后置粗过滤
            # （对纯全局消息等价于原 seq 分页，对混合来源为粗略过滤）。
            messages = await read_all_active_collab_messages(
                bb_root, include_archived=include_archived,
            )
            if before_seq is not None:
                messages = [
                    m for m in messages
                    if not (isinstance(m.get("seq"), int) and m["seq"] >= before_seq)
                ]
            if after_seq is not None:
                messages = [
                    m for m in messages
                    if not (isinstance(m.get("seq"), int) and m["seq"] <= after_seq)
                ]
            if limit and limit > 0:
                messages = messages[-limit:]
        return {"messages": messages, "total": len(messages)}

    @router.post("/append")
    async def append_message(req: AppendRequest):
        """写入协作消息（A2 修复：返回 seq + deduplicated）"""
        # 主会话协作（main_*）对工作台只读：拒绝工作台写入口
        if req.collab_id and str(req.collab_id).startswith("main_"):
            raise HTTPException(
                status_code=403,
                detail="主会话协作（main_*）为只读观察对象，工作台不可写入",
            )
        bb_root = _get_bb_root()
        seq, deduplicated = await append_collab_message(
            bb_root, req.message, req.collab_id
        )
        return {"ok": True, "seq": seq, "deduplicated": deduplicated}

    @router.post("/forward")
    async def forward_message(req: ForwardRequest):
        """转发 A2A 消息到协作黑板（强制 ed25519 签名校验）。

        这是 agent 主动归档 A2A 消息副本的入口，不是 agent 间通信通道。
        Agent 间通信走 A2A 点对点，forward 仅让工作台能观察到交流过程。
        """
        from teage_liu.multiagent.message_signature import MessageSignatureVerifier

        bb_root = _get_bb_root()

        # 签名校验
        signature = req.signature or ""
        signer_id = req.from_ or ""
        if not signature:
            raise HTTPException(
                status_code=401,
                detail=f"Missing signature for agent {signer_id}",
            )

        # 待校验 message（与 agent 签名内容一致，不含 endpoint 注入的 type 字段）
        message = {
            "from": req.from_,
            "to": req.to,
            "content": req.content,
            "via": req.via,
            "message_id": req.message_id,
        }

        verifier = MessageSignatureVerifier(bb_root)
        if not verifier.verify_message(message, signature, signer_id):
            raise HTTPException(
                status_code=401,
                detail=f"Signature verification failed for agent {signer_id}",
            )

        # 校验通过后注入 type=relay（工作台归档分类，非 agent 签名内容）
        message["type"] = "relay"
        # 用全局 writer 确保锁互斥 + 自动 message_id 去重
        writer = _get_global_writer(bb_root)
        seq, deduplicated = await writer.append(message)
        return {"ok": True, "seq": seq, "deduplicated": deduplicated}

    @router.post("/broadcast")
    async def broadcast(req: BroadcastRequest):
        """Director 发布广播（from=director, type=request, to=*）。

        协作生命周期模型：
        - start_collab=True：发起新协作（生成 collab_id + index active），写首条消息。
        - start_collab=False + collab_id：向现有协作追加 Director 信息（协作中补上下文）。
        - 两者皆无：拒绝（broadcast 必须归属某协作，不允许游离全局广播）。

        跨实例转发：本地写入后并行调用所有 remote_endpoints 的 director_broadcast，
        对端 worker 把广播写入其本地 collabs/{collab_id}.md（对端 WorkerAdapter 轮询拾取）。
        使用相同 message_id 实现跨实例幂等去重。
        """
        import hashlib
        import uuid
        from teage_liu.multiagent.blackboard import update_collab_index

        bb_root = _get_bb_root()

        # 主会话协作（main_*）对工作台只读：拒绝向 main_* 协作广播
        if req.collab_id and str(req.collab_id).startswith("main_"):
            raise HTTPException(
                status_code=403,
                detail="主会话协作（main_*）为只读观察对象，工作台不可写入",
            )

        # 生命周期：确定本次广播归属的 collab_id
        if req.start_collab:
            # 发起新协作：每次 start 生成新 collab_id（uuid)，即使内容相同也是新协作
            collab_id = f"collab_{uuid.uuid4().hex[:12]}"
            # 写入协作索引（active）；标题取内容前 40 字
            title = req.content.strip().split("\n", 1)[0][:40] or "(无标题协作)"
            await update_collab_index(
                bb_root, collab_id, title=title, status="active", participants=[]
            )
        elif req.collab_id:
            # 追加到现有协作（Director 协作中补信息）
            collab_id = req.collab_id
        else:
            # 游离广播：拒绝。broadcast 必须归属某协作。
            raise HTTPException(
                status_code=400,
                detail="broadcast 必须 start_collab=True 发起新协作，或提供 collab_id 追加到现有协作；"
                       "不允许无归属的游离广播。",
            )

        # message_id 基于 content + collab_id hash，确保相同内容在不同协作中 message_id 不同，
        # 避免 worker 的 message_id 去重误判（旧协作已响应 → 新协作同内容被跳过）。
        content_hash = hashlib.md5((req.content + collab_id).encode("utf-8")).hexdigest()[:16]
        message_id = f"director_broadcast_{content_hash}"

        message = {
            "from": "director",
            "type": "request",
            "content": req.content,
            "to": "*",
            "message_id": message_id,
            "collab_id": collab_id,
        }
        seq, deduplicated = await append_collab_message(
            bb_root, message, collab_id=collab_id
        )

        # A2A 跨实例转发（best-effort，失败不影响本地写入结果）
        forwarded: list[dict] = []
        a2a_client = None
        if container is not None:
            try:
                a2a_client = container.get("a2a_client")
            except Exception:
                a2a_client = None

        if a2a_client is not None:
            try:
                results = await a2a_client.call_all_endpoints(
                    "director_broadcast",
                    {
                        "content": req.content,
                        "message_id": message_id,
                        "collab_id": collab_id,
                    },
                )
                for ep_name, res in results.items():
                    if isinstance(res, Exception):
                        forwarded.append({
                            "endpoint": ep_name,
                            "ok": False,
                            "error": str(res),
                        })
                    else:
                        forwarded.append({
                            "endpoint": ep_name,
                            "ok": True,
                            "result": res,
                        })
            except Exception as e:
                # 转发整体失败（如所有端点不可达）记录但不抛
                forwarded.append({"endpoint": "*", "ok": False, "error": str(e)})

        return {
            "ok": True,
            "seq": seq,
            "deduplicated": deduplicated,
            "collab_id": collab_id,
            "started": req.start_collab,
            "forwarded": forwarded,
        }

    @router.post("/announce")
    async def announce(req: AnnounceRequest):
        """agent 上线/下线/能力更新"""
        bb_root = _get_bb_root()
        message = {
            "from": req.agent_id,
            "type": "announce",
            "action": req.action,
            "capabilities": req.capabilities,
            "endpoint": req.endpoint,
            "agent_name": req.agent_name,
        }
        seq, _ = await append_collab_message(bb_root, message)
        return {"ok": True, "seq": seq}

    @router.post("/directive")
    async def inject_directive(req: DirectiveRequest):
        """Director 注入 directive（UserDirector 入口，Task 5 Step 6.5 用）"""
        bb_root = _get_bb_root()
        message = {
            "from": "director",
            "type": "directive",
            "content": req.content,
            "rule_type": req.rule_type,
            "target": req.target,
            "priority": req.priority,
            "issued_by": req.issued_by,
        }
        if req.deadline is not None:
            message["deadline"] = req.deadline
        seq, _ = await append_collab_message(bb_root, message)
        return {"ok": True, "seq": seq}

    @router.get("/agents")
    async def get_agents():
        """获取接入工作台的 agent 列表（A4 修复：registry + announce 合并）。

        registry 为权威来源，announce 消息补充最新能力/状态。
        """
        bb_root = _get_bb_root()
        agents = {}

        # 来源1：AgentRegistry（权威）
        try:
            from teage_liu.multiagent.agent_registry import AgentRegistry
            from teage_liu.multiagent.schema_validator import SchemaValidator

            registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
            registry_agents = await registry.list_active_agents()
            for a in registry_agents:
                agents[a["agent_id"]] = a
        except Exception:
            # registry 不可用时降级为只看 announce
            pass

        # 来源2：announce 消息（补充最新能力/名称/状态）
        messages = await read_collab_messages(bb_root)
        for msg in messages:
            if msg.get("type") != "announce":
                continue
            agent_id = msg.get("from", "")
            if not agent_id:
                continue
            action = msg.get("action", "")
            if action == "online":
                agents[agent_id] = {
                    "agent_id": agent_id,
                    "agent_name": msg.get("agent_name", agent_id),
                    "capabilities": msg.get("capabilities", []),
                    "endpoint": msg.get("endpoint", ""),
                    "status": "active",
                }
            elif action == "offline" and agent_id in agents:
                agents[agent_id]["status"] = "inactive"

        return {"agents": list(agents.values())}

    # ========== Task 12: 协作索引 ==========

    @router.get("/collabs")
    async def list_collabs():
        """列出所有协作索引"""
        bb_root = _get_bb_root()
        index = await read_collab_index(bb_root)
        return {"collabs": index, "total": len(index)}

    @router.post("/collabs")
    async def upsert_collab(req: CollabIndexRequest):
        """手动创建/更新协作索引条目（upsert）"""
        bb_root = _get_bb_root()
        await update_collab_index(
            bb_root, req.collab_id,
            title=req.title, status=req.status,
            participants=req.participants,
        )
        return {"ok": True, "collab_id": req.collab_id}

    @router.get("/events")
    async def list_events(before_ts: Optional[str] = None, limit: int = 50):
        """列出活动事件（按时间倒序）。

        供工作台左栏「活动档案时间线」使用。从协作消息提取关键事件：
        - agent_online / agent_offline（announce）
        - user_broadcast（from=user, type=request）
        - directive_injected（from=director, type=directive）

        Args:
            before_ts: ISO 时间戳游标，仅返回 ts 严格小于此值的事件；None 表示从头开始
            limit: 返回数量上限，默认 50

        Returns:
            events: 事件列表（每条含 event_type/ts/agent_id/summary/seq）
            next_before_ts: 下一次分页应使用的游标（最早事件的 ts）；
                           无更多数据时为 None
        """
        from teage_liu.multiagent.event_extractor import extract_events

        bb_root = _get_bb_root()
        # 聚合所有 active 协作 + 全局消息提取事件（归档协作不再进默认活动流）
        messages = await read_all_active_collab_messages(bb_root)
        events = extract_events(messages, before_ts=before_ts, limit=limit)

        # 计算 next_before_ts：最早事件的 ts（仅当返回数量等于 limit 时提示可能有更多）
        next_before_ts = None
        if events and len(events) == limit:
            next_before_ts = events[-1].get("ts") or None

        return {"events": events, "next_before_ts": next_before_ts}

    @router.get("/sse")
    async def collab_sse():
        """协作消息 SSE 通道。

        连接时推送已有消息，然后轮询推送新消息（基于 seq 增量）。
        """
        bb_root = _get_bb_root()

        async def event_stream():
            # 聚合视图：按 (timestamp, collab_id, seq) 复合键跟踪增量，
            # 因为 seq 是 per-collab 的，跨协作不全局连续。
            def _msg_key(m: dict) -> tuple:
                return (
                    str(m.get("timestamp", "")),
                    str(m.get("collab_id", "")),
                    int(m.get("seq", 0) or 0),
                )

            # 连接时记录当前最新键，不推送历史（前端通过 GET /messages 加载）
            last_key: tuple = ("", "", 0)
            try:
                messages = await read_all_active_collab_messages(bb_root)
                for msg in messages:
                    k = _msg_key(msg)
                    if k > last_key:
                        last_key = k
            except Exception:
                pass

            # 推送初始事件（告知前端 SSE 已连接）
            yield f"data: {json.dumps({'type': 'collab_sse_connected', 'data': {'last_key': list(last_key)}}, ensure_ascii=False)}\n\n"

            # 轮询推送新消息（1秒间隔，仅推送复合键 > last_key 的增量）
            while True:
                await asyncio.sleep(1)
                try:
                    messages = await read_all_active_collab_messages(bb_root)
                    new_messages = [m for m in messages if _msg_key(m) > last_key]
                    new_messages.sort(key=_msg_key)
                    for msg in new_messages:
                        payload = {
                            "type": "collab_message_append",
                            "data": {"message": msg},
                        }
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        last_key = _msg_key(msg)
                except Exception:
                    # 读取失败时静默继续
                    pass

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
