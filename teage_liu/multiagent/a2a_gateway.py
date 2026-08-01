"""A2A Gateway 服务端：基于 FastAPI + JSON-RPC 2.0 的跨设备 agent 通信网关。

端点：
- GET  /a2a/health           健康检查
- POST /a2a/jsonrpc          JSON-RPC 2.0 方法调用

JSON-RPC 方法：
- list_agents                列出 active agents
- read_messages              读取 messages.md
- append_message             追加消息（需签名）
- acquire_lock               获取跨设备锁
- release_lock               释放锁
- read_director_md           读取 director.md
- register_remote_agent      注册远程 agent
- heartbeat                  远程 agent 心跳
- read_file                  读取相对路径文件（受路径沙箱约束）

错误码（JSON-RPC 2.0 扩展）：
- -32700 Parse error
- -32600 Invalid Request
- -32601 Method not found
- -32602 Invalid params
- -32603 Internal error
- -32001 Signature verification failed
- -32002 Path sandbox violation
- -32003 Rate limit exceeded
- -32004 Lock acquisition failed
- -32005 Director not available
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from teage_liu.multiagent.agent_registry import AgentRegistry, AgentAlreadyRegisteredError
from teage_liu.multiagent.blackboard import (
    append_audit,
    append_collab_message,
    append_message,
    read_collab_index,
    read_director_md,
    read_messages,
    update_collab_index,
)
from teage_liu.multiagent.file_lock import LockManager
from teage_liu.multiagent.path_sandbox import PathSandboxError, sanitize_path
from teage_liu.multiagent.rate_limiter import RateLimiter
from teage_liu.multiagent.schema_validator import SchemaValidator

logger = logging.getLogger(__name__)

# JSON-RPC 错误码
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_SIGNATURE = -32001
ERR_PATH_SANDBOX = -32002
ERR_RATE_LIMIT = -32003
ERR_LOCK_FAILED = -32004
ERR_DIRECTOR_UNAVAILABLE = -32005

# 路径字段集合（用于 sanitize_dict_paths 检查）
_PATH_FIELDS = frozenset({
    "path", "file_path", "target", "src", "dst",
    "lock_name", "source", "destination",
})

# read_file 允许读取的路径白名单（相对 bb_root 的 glob 模式）
_READABLE_PATHS = frozenset({
    "protocol.md",
    "director.md",
    "messages.md",
    "collaboration.md",
    "collabs/index.md",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SignatureError(Exception):
    """签名校验失败（用于错误码分流）。"""


class LockAcquireError(Exception):
    """锁获取失败（用于错误码分流）。"""


def create_a2a_router(bb_root: Path, config: dict) -> APIRouter:
    """创建 A2A Gateway 路由器。

    Args:
        bb_root: Blackboard 根目录。
        config: a2a 配置段。

    Note:
        限流通过 _check_rate_limit 辅助函数在每个端点显式调用，
        确保 health 与 jsonrpc 端点都受限流约束。
    """
    router = APIRouter(prefix="/a2a", tags=["a2a"])
    a2a_cfg = config.get("a2a", {}) or {}
    rate_limit = a2a_cfg.get("rate_limit_per_second", 100)
    limiter = RateLimiter(rate_limit)
    lock_manager = LockManager(bb_root)
    schema_validator = SchemaValidator()

    # JSON-RPC 方法注册表
    methods: dict[str, Any] = {
        "list_agents": _list_agents,
        "read_messages": _read_messages,
        # [Deprecated] append_message：远程 agent 应改用 Forward API
        "append_message": _append_message,
        "acquire_lock": _acquire_lock,
        "release_lock": _release_lock,
        "read_director_md": _read_director_md,
        "register_remote_agent": _register_remote_agent,
        "heartbeat": _heartbeat,
        "read_file": _read_file,
        # 跨实例广播转发：让对端 worker 把广播写入其本地 collaboration.md，
        # 由对端 WorkerAdapter 轮询拾取并触发 _trigger_urgent_llm。
        "director_broadcast": _director_broadcast,
        # agent-to-agent 消息转发：与 director_broadcast 同机制，但 from=<source_agent_id>，
        # WorkerAdapter._handle_request 通过 to 字段判断是否为自己。
        "agent_message": _agent_message,
    }

    def _check_rate_limit(request: Request) -> JSONResponse | None:
        """限流检查：超限返回 429 JSONResponse，否则返回 None。"""
        client_ip = request.client.host if request.client else "unknown"
        if not limiter.check(client_ip):
            return JSONResponse(
                status_code=429,
                content=_make_error(None, ERR_RATE_LIMIT, "Rate limit exceeded"),
            )
        return None

    @router.get("/health")
    async def health(request: Request) -> Response:
        limited = _check_rate_limit(request)
        if limited is not None:
            return limited
        return JSONResponse(status_code=200, content={"status": "ok", "version": "1.0.0"})

    @router.post("/jsonrpc")
    async def jsonrpc(request: Request) -> Response:
        limited = _check_rate_limit(request)
        if limited is not None:
            return limited

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=200,
                content=_make_error(None, ERR_PARSE, "Parse error"),
            )

        # 批量请求支持
        if isinstance(body, list):
            responses = []
            for req in body:
                responses.append(
                    await _handle_single(req, methods, bb_root, lock_manager, schema_validator)
                )
            return JSONResponse(status_code=200, content=responses)

        result = await _handle_single(body, methods, bb_root, lock_manager, schema_validator)
        return JSONResponse(status_code=200, content=result)

    return router


async def _handle_single(
    req: dict,
    methods: dict,
    bb_root: Path,
    lock_manager: LockManager,
    schema_validator: SchemaValidator,
) -> dict:
    """处理单个 JSON-RPC 请求。"""
    if not isinstance(req, dict):
        return _make_error(None, ERR_INVALID_REQUEST, "Invalid request: not a dict")

    req_id = req.get("id")
    method_name = req.get("method")
    params = req.get("params", {}) or {}

    if not method_name or not isinstance(method_name, str):
        return _make_error(req_id, ERR_INVALID_REQUEST, "Invalid request: missing method")

    if method_name not in methods:
        return _make_error(req_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method_name}")

    try:
        result = await methods[method_name](
            bb_root, params, lock_manager, schema_validator
        )
        return {"jsonrpc": "2.0", "result": result, "id": req_id}
    except PathSandboxError as e:
        return _make_error(req_id, ERR_PATH_SANDBOX, str(e))
    except SignatureError as e:
        return _make_error(req_id, ERR_SIGNATURE, str(e))
    except LockAcquireError as e:
        return _make_error(req_id, ERR_LOCK_FAILED, str(e))
    except Exception as e:
        logger.exception("JSON-RPC internal error")
        return _make_error(req_id, ERR_INTERNAL, f"Internal error: {e}")


def _make_error(req_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": req_id,
    }


# ============================================================================
# JSON-RPC 方法实现
# ============================================================================


async def _list_agents(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """列出 active agents。"""
    registry = AgentRegistry(bb_root, schema_validator)
    agents = await registry.list_active_agents()
    return {"agents": agents}


async def _read_messages(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """读取 messages.md。"""
    limit = params.get("limit", 100)
    messages = await read_messages(bb_root)
    return {"messages": messages[-limit:]}


async def _append_message(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """[Deprecated] 追加消息到 messages.md。

    废弃原因：远程 agent 不应直接写主对话 messages.md，应改用 Forward API
    （POST /api/multiagent/collab/forward）归档 A2A 消息副本到 collaboration.md。

    此方法保留向后兼容，未来版本将移除。签名校验不补强（空实现保持不变）。
    """
    message = params.get("message", {})
    signature = params.get("signature", "")
    signer_id = message.get("from", "")

    if not signature:
        raise SignatureError(f"Missing signature for agent {signer_id}")

    # 路径沙箱：消息内容中的路径字段必须为相对路径
    _sanitize_message_paths(message)

    # 签名校验（可选）：若 bb_root 下存在 director 公钥，则校验
    # 此处简化为：signature 非空即视为通过（生产环境需复用 SignatureVerifier）
    # 若需严格校验，调用方应预先注册 agent 公钥，并通过 SignatureVerifier 验证

    # v3: 启用 schema 验证，确保远程 agent 写入也合规
    await append_message(bb_root, message, validate=True)
    await append_audit(bb_root, {
        "ts": _now_iso(),
        "actor": signer_id,
        "action": "remote_write",
        "target": "messages.md",
        "op_id": params.get("op_id", str(uuid.uuid4())),
        "epoch": message.get("epoch", 0),
        "details": {"source": "a2a_gateway"},
        "prev_hash": "", "hash": "", "signature": signature,
    })
    return {"ok": True, "seq": message.get("seq")}


async def _acquire_lock(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """获取跨设备锁。"""
    lock_name = params.get("lock_name", "")
    agent_id = params.get("agent_id", "")
    fencing_token = params.get("fencing_token", 0)
    ttl = params.get("ttl_seconds", 30)

    # 路径沙箱
    safe_name = sanitize_path(lock_name, bb_root)

    # LockManager.acquire(lock_name, holder, ttl_seconds) -> fencing_token
    try:
        acquired_token = await lock_manager.acquire(safe_name, agent_id, ttl)
    except Exception as e:
        raise LockAcquireError(f"Lock '{lock_name}' acquisition failed: {e}") from e
    return {"acquired": True, "fencing_token": acquired_token}


async def _release_lock(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """释放锁。"""
    lock_name = params.get("lock_name", "")
    agent_id = params.get("agent_id", "")
    fencing_token = params.get("fencing_token", 0)
    safe_name = sanitize_path(lock_name, bb_root)
    try:
        await lock_manager.release(safe_name, agent_id, fencing_token)
    except Exception as e:
        logger.warning("release_lock failed: %s", e)
    return {"released": True}


async def _read_director_md(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """读取 director.md。"""
    data = await read_director_md(bb_root)
    return data or {}


async def _register_remote_agent(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """注册远程 agent（幂等：已注册则视为成功并刷新心跳）。"""
    registry = AgentRegistry(bb_root, schema_validator)
    agent_id = params.get("agent_id", "")
    role = params.get("role", "worker")
    capabilities = params.get("capabilities", [])
    heartbeat_interval = params.get("heartbeat_interval_seconds", 30)

    # 构建完整 agent_card（满足 schema 要求）
    now = _now_iso()
    agent_card = {
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "created_at": now,
        "last_heartbeat": now,
        "heartbeat_interval_seconds": heartbeat_interval,
        "status": "active",
        "role": role,
        "endpoint": params.get("endpoint", ""),
        "owner": params.get("owner", "remote"),
        "capabilities": capabilities,
        "specialties": params.get("specialties", []),
        "auth_method": params.get("auth_method", "signed"),
        "trust_score": params.get("trust_score", 100),
        "trust_history": [],
        "extensions": params.get("extensions", {}),
        "leave_reason": "",
        "left_at": "",
    }
    try:
        await registry.register(agent_card)
    except AgentAlreadyRegisteredError:
        # 幂等：agent 已注册（如客户端重启后重试），刷新心跳并返回成功
        await registry.update_heartbeat(agent_id, now)
    return {"registered": True, "agent_id": agent_id}


async def _heartbeat(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """远程 agent 心跳（字段与 REST 端点统一）。

    接受字段：
    - agent_id: 必填
    - status: 可选（active / busy / idle / degraded）
    - current_task: 可选（当前任务 task_op_id）
    - ts: 可选（自定义时间戳，默认服务端生成）
    """
    agent_id = params.get("agent_id", "")
    if not agent_id:
        return {"ok": False, "reason": "missing agent_id"}

    registry = AgentRegistry(bb_root, schema_validator)
    ts = params.get("ts") or _now_iso()
    await registry.update_heartbeat(agent_id, ts)

    # 可选：更新 agent 状态
    status = params.get("status")
    if status:
        await registry.update_agent_status(agent_id, status)

    return {
        "ok": True,
        "ts": _now_iso(),
        "next_heartbeat_due": 10,
    }


async def _read_file(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """读取相对路径文件（受路径沙箱 + ACL 白名单约束）。

    安全约束：
    - 路径必须相对（sanitize_path 检查）
    - 路径必须在 _READABLE_PATHS 白名单内
    - 解析后路径仍需在 bb_root 内（二次检查）
    """
    rel_path = params.get("path", "")

    # ACL 白名单检查
    if rel_path not in _READABLE_PATHS:
        raise PathSandboxError(
            f"Access denied: path '{rel_path}' not in readable whitelist"
        )

    # 路径沙箱：拒绝绝对路径和路径穿越
    safe = sanitize_path(rel_path, bb_root)
    target = (bb_root / safe).resolve()
    bb_root_resolved = bb_root.resolve()
    # 二次检查：确保解析后路径仍在 bb_root 内
    try:
        target.relative_to(bb_root_resolved)
    except ValueError as e:
        raise PathSandboxError(f"Path escapes bb_root: {rel_path}") from e

    if not target.exists() or not target.is_file():
        return {"content": "", "exists": False}
    content = target.read_text(encoding="utf-8")
    return {"content": content, "exists": True, "path": safe}


async def _director_broadcast(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """跨实例 Director 广播转发。

    由对端 worker 的 broadcast 路由通过 A2A 调用，将对端 Director 广播
    原样写入本地 collaboration.md。本机 WorkerAdapter 轮询拾取新消息后
    由 _handle_request 走 from=director + type=request 紧急路径触发 LLM。

    使用相同 message_id 实现跨实例幂等去重：若对端已转发过此广播，
    本地已存在相同 message_id 的消息，返回 (existing_seq, True)。

    参数：
        content: 广播内容（必填）
        message_id: 跨实例去重 ID（必填，由发起方生成）
        from: 来源标识，默认 "director"
    """
    import hashlib

    content = params.get("content", "")
    if not content:
        raise SignatureError("director_broadcast: content is required")

    # message_id 由发起方生成（基于内容 hash + uuid 后缀），跨实例复用以去重
    message_id = params.get("message_id")
    if not message_id:
        # 兜底：与 broadcast 路由同样的内容 hash 策略
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
        message_id = f"director_broadcast_{content_hash}"

    message = {
        "from": params.get("from", "director"),
        "type": "request",
        "content": content,
        "to": "*",
        "message_id": message_id,
    }
    # 阶段 1.3：广播消息体携带 collab_id，确保被广播发起的协作可被工作台归档分组，
    # 并路由到 collabs/{collab_id}.md（与 send_remote_message / _trigger_urgent_llm 对齐）。
    collab_id = params.get("collab_id")
    if collab_id is not None:
        message["collab_id"] = collab_id
        # 跨实例接收端：若本实例 index 尚无该 collab，创建 active 条目，
        # 让本实例 /messages 聚合也能看到此协作。已存在则不更新（避免复活 archived）。
        try:
            existing = await read_collab_index(bb_root)
            if not any(e.get("collab_id") == collab_id for e in existing):
                title = content.strip().split("\n", 1)[0][:40] or "(无标题协作)"
                # Phase5 E-2：index 更新失败不再静默 pass，有限重试 + 精炼错误
                last_err = None
                for _attempt in range(3):
                    try:
                        await update_collab_index(
                            bb_root, collab_id, title=title, status="active", participants=[]
                        )
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                if last_err is not None:
                    logger.error(
                        "director_broadcast: index 更新失败 collab_id=%s（已重试3次）: %s",
                        collab_id, last_err,
                    )
        except Exception as e:
            logger.error("director_broadcast: index 读取失败 collab_id=%s: %s", collab_id, e)
    seq, deduplicated = await append_collab_message(
        bb_root, message, collab_id=collab_id
    )
    return {"ok": True, "seq": seq, "deduplicated": deduplicated}


async def _agent_message(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """跨实例 agent-to-agent 消息转发。

    由对端 worker 的 send_remote_message 工具通过 A2A 调用，将 agent 间
    协作请求写入本地 collaboration.md。本机 WorkerAdapter 轮询拾取后，
    由 _handle_request 检查 to 字段匹配则触发 _trigger_urgent_llm。

    与 director_broadcast 区别：
    - from 为源 agent_id（非 "director"）
    - to 为目标 agent_id 或 "*"（定向 / 广播），由 _handle_request 按 to 过滤

    使用 message_id 跨实例幂等去重。

    参数：
        from: 源 agent_id（必填）
        to: 目标 agent_id 或 "*"（必填）
        content: 消息内容（必填）
        message_id: 跨实例去重 ID（必填，由发起方生成）
    """
    import hashlib

    from_id = params.get("from", "")
    if not from_id:
        raise SignatureError("agent_message: from is required")

    to_id = params.get("to", "")
    if not to_id:
        raise SignatureError("agent_message: to is required")

    content = params.get("content", "")
    if not content:
        raise SignatureError("agent_message: content is required")

    message_id = params.get("message_id")
    if not message_id:
        content_hash = hashlib.md5(
            f"{from_id}:{to_id}:{content}".encode("utf-8")
        ).hexdigest()[:16]
        message_id = f"agent_message_{content_hash}"

    message = {
        "from": from_id,
        # 阶段 0.2：保留发起方 send_remote_message 工具设置的 msg_type/type
        # （request/response/consensus/end），不再硬编码 "request"，否则
        # 转发的 response/consensus/end 会被误路由到 _handle_request。
        "type": params.get("type", "request"),
        "content": content,
        "to": to_id,
        "message_id": message_id,
    }
    if "msg_type" in params:
        message["msg_type"] = params["msg_type"]
    # 阶段 0.2 / 1.3：透传 collab_id，让消息路由到 collabs/{collab_id}.md
    # （与 send_remote_message 工具 + _director_broadcast 对齐，否则 A2A 转发的
    # 消息会落入全局 collaboration.md，破坏 collab_id 归档分组）。
    collab_id = params.get("collab_id")
    if collab_id is not None:
        message["collab_id"] = collab_id
    seq, deduplicated = await append_collab_message(
        bb_root, message, collab_id=collab_id
    )
    return {"ok": True, "seq": seq, "deduplicated": deduplicated}


def _sanitize_message_paths(message: dict) -> None:
    """递归 sanitize 消息中的路径字段。"""
    for key, value in list(message.items()):
        if key in _PATH_FIELDS:
            if isinstance(value, str):
                # 触发 sanitize_path 检查（违规时抛 PathSandboxError）
                sanitize_path(value, Path("."))
        elif isinstance(value, dict):
            _sanitize_message_paths(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _sanitize_message_paths(item)
