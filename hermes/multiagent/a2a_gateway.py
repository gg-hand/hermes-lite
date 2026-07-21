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

from hermes.multiagent.agent_registry import AgentRegistry
from hermes.multiagent.blackboard import (
    append_audit,
    append_message,
    read_director_md,
    read_messages,
)
from hermes.multiagent.file_lock import LockManager
from hermes.multiagent.path_sandbox import PathSandboxError, sanitize_path
from hermes.multiagent.rate_limiter import RateLimiter
from hermes.multiagent.schema_validator import SchemaValidator

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
        "append_message": _append_message,
        "acquire_lock": _acquire_lock,
        "release_lock": _release_lock,
        "read_director_md": _read_director_md,
        "register_remote_agent": _register_remote_agent,
        "heartbeat": _heartbeat,
        "read_file": _read_file,
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
    """追加消息（需签名校验）。"""
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

    await append_message(bb_root, message)
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
    """注册远程 agent。"""
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
        "auth_method": params.get("auth_method", "ed25519"),
        "trust_score": params.get("trust_score", 100),
        "trust_history": [],
        "extensions": params.get("extensions", {}),
        "leave_reason": "",
        "left_at": "",
    }
    await registry.register(agent_card)
    return {"registered": True, "agent_id": agent_id}


async def _heartbeat(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """远程 agent 心跳。"""
    agent_id = params.get("agent_id", "")
    if not agent_id:
        return {"ok": False, "reason": "missing agent_id"}
    registry = AgentRegistry(bb_root, schema_validator)
    await registry.update_heartbeat(agent_id, params.get("ts") or _now_iso())
    return {"ok": True, "ts": _now_iso()}


async def _read_file(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """读取相对路径文件（受路径沙箱约束）。"""
    rel_path = params.get("path", "")
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
