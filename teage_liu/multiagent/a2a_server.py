"""A2A Server（agent 侧）：接收其他 agent 的点对点 JSON-RPC 通信。

设计原则：
- 每个 agent 自带 A2A Server，监听独立端口
- 不依赖工作台，agent 间直接通信
- 入站消息强制 ed25519 签名校验（防伪造）
- 支持自定义 handler 注册（业务方法）
- 内置方法：send_message / query_capabilities / ping

与 A2A Gateway 的区别：
- A2A Gateway 在工作台进程内，是文件访问代理
- A2A Server 在 agent 进程内，是 agent 间通信入口
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# 消息回调类型
MessageCallback = Callable[[dict], Awaitable[None]]


class A2AServer:
    """A2A Server（agent 侧）。

    接收其他 agent 的 JSON-RPC 调用，支持：
    - 自定义 handler 注册
    - 内置 send_message / query_capabilities / ping 方法
    - 入站消息签名校验
    - 消息回调通知（上层处理业务逻辑）
    """

    def __init__(
        self,
        agent_id: str,
        bb_root: Path,
        capabilities: Optional[list[str]] = None,
    ) -> None:
        """初始化 A2A Server。

        Args:
            agent_id: 本 agent 的 ID
            bb_root: 黑板根目录（用于加载签名公钥）
            capabilities: 本 agent 的能力列表
        """
        self._agent_id = agent_id
        self._bb_root = bb_root
        self._capabilities = capabilities or []
        self._handlers: dict[str, Callable[[dict], Awaitable[dict]]] = {}
        self.on_message_callback: Optional[MessageCallback] = None

        # 注册内置方法
        self._register_builtin_handlers()

    def _register_builtin_handlers(self) -> None:
        """注册内置 JSON-RPC 方法。"""
        self._handlers["send_message"] = self._handle_send_message
        self._handlers["query_capabilities"] = self._handle_query_capabilities
        self._handlers["ping"] = self._handle_ping
        self._handlers["read_director_md"] = self._handle_read_director_md

    def register_handler(
        self, method: str, handler: Callable[[dict], Awaitable[dict]]
    ) -> None:
        """注册自定义 JSON-RPC 方法。

        Args:
            method: 方法名
            handler: 异步处理函数 (params: dict) -> dict
        """
        self._handlers[method] = handler

    def create_router(self) -> APIRouter:
        """创建 FastAPI 路由。"""
        router = APIRouter()

        @router.post("/a2a/jsonrpc")
        async def jsonrpc_endpoint(request: Request) -> JSONResponse:
            """JSON-RPC 2.0 端点。"""
            try:
                payload = await request.json()
            except Exception as e:
                return JSONResponse(
                    {"jsonrpc": "2.0", "error": {
                        "code": -32700, "message": f"Parse error: {e}"
                    }, "id": None},
                    status_code=400,
                )

            method = payload.get("method", "")
            params = payload.get("params", {}) or {}
            req_id = payload.get("id")

            handler = self._handlers.get(method)
            if handler is None:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                    "id": req_id,
                })

            try:
                result = await handler(params)
                return JSONResponse({
                    "jsonrpc": "2.0", "result": result, "id": req_id,
                })
            except SignatureError as e:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "error": {"code": -32001, "message": str(e)},
                    "id": req_id,
                }, status_code=401)
            except Exception as e:
                logger.exception("A2A Server 处理 %s 异常", method)
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32603,
                        "message": f"Internal error: {e}",
                    },
                    "id": req_id,
                }, status_code=500)

        return router

    async def _handle_send_message(self, params: dict) -> dict:
        """内置 send_message 方法：接收其他 agent 的消息。

        强制 ed25519 签名校验，校验通过后触发回调。
        验签内容为完整 params（移除 signature 字段后），与客户端
        A2AClient._sign_params 的签名内容保持一致。
        """
        from teage_liu.multiagent.message_signature import (
            MessageSignatureVerifier,
        )

        # 验签内容 = params 移除 signature 字段后（与客户端签名内容一致）
        message = {
            k: v for k, v in params.items() if k != "signature"
        }
        signature = params.get("signature", "")
        signer_id = message.get("from", "")

        if not signature:
            raise SignatureError(f"Missing signature for agent {signer_id}")

        verifier = MessageSignatureVerifier(self._bb_root)
        if not verifier.verify_message(message, signature, signer_id):
            raise SignatureError(
                f"Signature verification failed for agent {signer_id}"
            )

        # 触发消息回调（透传完整 message，含 message_id）
        if self.on_message_callback:
            try:
                await self.on_message_callback({**message, "signature_valid": True})
            except Exception as e:
                logger.warning("消息回调异常: %s", e)

        logger.info("A2A Server %s 收到来自 %s 的消息", self._agent_id, signer_id)
        return {"ok": True, "received_by": self._agent_id}

    async def _handle_query_capabilities(self, params: dict) -> dict:
        """内置 query_capabilities 方法：返回本 agent 能力。"""
        return {
            "agent_id": self._agent_id,
            "capabilities": self._capabilities,
        }

    async def _handle_ping(self, params: dict) -> dict:
        """内置 ping 方法：健康检查。"""
        return {"ok": True, "agent_id": self._agent_id, "pong": True}

    async def _handle_read_director_md(self, params: dict) -> dict:
        """内置 read_director_md 方法：返回本实例 Director 元信息。

        供远程 Election 收集候选使用。返回格式与 Election 期望一致：
            {"director": {"agent_id": str, "epoch": int, "last_tick": str}}
        无 Director 时返回 {}。

        字段对齐（GAP-2）：director.md 原字段为 current_epoch / last_director_tick，
        此处转换为 Election 期望的 epoch / last_tick，并补充 agent_id。
        """
        from teage_liu.multiagent.blackboard import read_director_md, read_json

        # 优先从 status.json 读取（含 agent_id / epoch / last_tick，与 Election 期望一致）
        status_path = self._bb_root / "status.json"
        if status_path.exists():
            status = read_json(status_path) or {}
            director = status.get("director")
            if isinstance(director, dict) and director.get("agent_id"):
                return {"director": director}

        # 回退：从 director.md 读取并转换字段
        director_md = await read_director_md(self._bb_root)
        if director_md:
            return {
                "director": {
                    "agent_id": self._agent_id,
                    "epoch": director_md.get("current_epoch", 0),
                    "last_tick": director_md.get("last_director_tick", ""),
                }
            }
        return {}


class SignatureError(Exception):
    """签名校验失败。"""
    pass
