"""A2A 扩展：旧自定义方法挂到标准端点（A2A-Extensions 头门控）。

标准方法（Phase B）完全达标后，旧方法（register_remote_agent / heartbeat /
collab_message / director_broadcast / agent_message 等 12 个）作为扩展保留，
保证已部署的本地↔服务器实例平滑过渡。扩展方法名声明在 Agent Card extensions 列表，
未在请求头 A2A-Extensions 声明时返回 -32601。

扩展 handler 复用 a2a_gateway.py 的既有实现（bb_root, params, lock_manager,
schema_validator 四参签名），行为与旧端点完全一致。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from teage_liu.multiagent.a2a_gateway import (
    _acquire_lock,
    _agent_message,
    _append_message,
    _collab_message_impl,
    _director_broadcast,
    _heartbeat,
    _list_agents,
    _read_director_md,
    _read_file,
    _read_messages,
    _register_remote_agent,
    _release_lock,
)
from teage_liu.multiagent.file_lock import LockManager
from teage_liu.multiagent.schema_validator import SchemaValidator

logger = logging.getLogger(__name__)

# 扩展方法名 → 配置中的默认扩展列表（与 a2a_gateway 分发表一一对应）
EXTENSION_METHODS = [
    "list_agents",
    "read_messages",
    "append_message",
    "acquire_lock",
    "release_lock",
    "read_director_md",
    "register_remote_agent",
    "heartbeat",
    "read_file",
    "director_broadcast",
    "agent_message",
    "collab_message",
]

# handler 签名：async (params: dict) -> dict
ExtensionHandler = Callable[[dict], Awaitable[dict]]


def build_extension_registry(bb_root: Path, config: dict) -> dict[str, ExtensionHandler]:
    """构建扩展注册表：method -> 封装好 bb_root/锁/校验器的 handler。"""
    lock_manager = LockManager(bb_root)
    schema_validator = SchemaValidator()

    multiagent_cfg = config.get("multiagent", {}) or {}
    collab_local_agent_id = (
        (multiagent_cfg.get("worker", {}) or {}).get("agent_id", "worker_001")
    )

    def _wrap(fn: Callable) -> ExtensionHandler:
        async def handler(params: dict) -> dict:
            return await fn(bb_root, params, lock_manager, schema_validator)
        return handler

    async def _collab_message(params: dict) -> dict:
        return await _collab_message_impl(
            bb_root, params, lock_manager, schema_validator, collab_local_agent_id,
        )

    registry: dict[str, ExtensionHandler] = {
        "list_agents": _wrap(_list_agents),
        "read_messages": _wrap(_read_messages),
        "append_message": _wrap(_append_message),
        "acquire_lock": _wrap(_acquire_lock),
        "release_lock": _wrap(_release_lock),
        "read_director_md": _wrap(_read_director_md),
        "register_remote_agent": _wrap(_register_remote_agent),
        "heartbeat": _wrap(_heartbeat),
        "read_file": _wrap(_read_file),
        "director_broadcast": _wrap(_director_broadcast),
        "agent_message": _wrap(_agent_message),
        "collab_message": _collab_message,
    }
    return registry
