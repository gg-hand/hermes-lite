"""SDK 传输层：封装 A2A Client（出站）+ Forward API（归档）。

设计原则：
- call_agent() 调用其他 agent 的 A2A Server（点对点，不经工作台）
- forward_to_workbench() 归档消息副本到工作台 collaboration.md
- 签名由 A2AClient.call_method 单点负责（避免双重签名）
- A2AClientError 映射为 SDK 异常层级
- 全链路异步

与旧版的区别：
- 旧版 call() 调用 A2A Gateway 方法（list_agents/append_message 等）
- 新版 call_agent() 调用其他 agent 的 A2A Server（send_message/query_capabilities 等）
- 新增 forward_to_workbench() 调用工作台 Forward API 归档消息
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from teage_liu.multiagent.a2a_client import A2AClient, A2AClientError
from teage_liu.multiagent.message_signature import sign_message
from teage_liu.sdk.exceptions import (
    AuthenticationError,
    ProtocolError,
    SDKError,
    TransportError,
)

logger = logging.getLogger(__name__)

# A2A 错误码到 SDK 异常的映射
_ERROR_CODE_MAP = {
    -32001: AuthenticationError,
    -32002: ProtocolError,
    -32003: TransportError,
    -32004: TransportError,
    -32602: ProtocolError,
}


class A2ATransport:
    """A2A 传输层封装。

    同时管理：
    - A2A Client：出站调用其他 agent 的 A2A Server（签名由 A2AClient 单点负责）
    - Forward API client：归档消息副本到工作台 collaboration.md
    """

    def __init__(
        self,
        agent_id: str,
        private_key=None,
        bb_root: Optional[Path] = None,
        forward_endpoint: str = "",
        remote_endpoints: Optional[list[dict]] = None,
        timeout_seconds: int = 10,
        retry_count: int = 2,
    ) -> None:
        """初始化传输层。

        Args:
            agent_id: 本 agent 的 ID
            private_key: ed25519 私钥（写操作签名用）
            bb_root: 黑板根目录（用于本地 agent 注册公钥）
            forward_endpoint: 工作台 Forward API URL（如 http://localhost:18400）
            remote_endpoints: 其他 agent 的 A2A Server 端点列表
            timeout_seconds: HTTP 超时
            retry_count: 网络错误重试次数
        """
        self._agent_id = agent_id
        self._private_key = private_key
        self._bb_root = bb_root
        self._forward_endpoint = forward_endpoint.rstrip("/")

        # A2A Client（调用其他 agent 的 A2A Server）
        config = {
            "a2a": {
                "remote_endpoints": remote_endpoints or [],
                "timeout_seconds": timeout_seconds,
                "retry_count": retry_count,
            }
        }
        self._a2a_client = A2AClient(
            config,
            signer_id=agent_id if private_key else None,
            private_key=private_key,
        )

        # Forward API HTTP client（归档到工作台）
        self._forward_client: Optional[httpx.AsyncClient] = None

    def _ensure_forward_client(self) -> httpx.AsyncClient:
        if self._forward_client is None:
            self._forward_client = httpx.AsyncClient(timeout=10)
        return self._forward_client

    async def call_agent(
        self, target_agent: str, method: str, params: dict
    ) -> Any:
        """调用其他 agent 的 A2A Server 方法（点对点，不经工作台）。

        Args:
            target_agent: 目标 agent 的 endpoint 名称
            method: JSON-RPC 方法名（如 send_message / query_capabilities / ping）
            params: 方法参数

        Returns:
            JSON-RPC result 字段

        Raises:
            AuthenticationError: 签名/认证失败
            TransportError: 网络/传输错误
            SDKError: 其他 SDK 错误
        """
        # 签名由 A2AClient.call_method 单点负责（避免双重签名）
        try:
            return await self._a2a_client.call_method(target_agent, method, params)
        except A2AClientError as e:
            raise self._map_error(e) from e

    async def forward_to_workbench(
        self,
        original_from: str,
        original_to: str,
        content: str,
        message_id: str,
    ) -> dict:
        """归档 A2A 消息副本到工作台 collaboration.md（Forward API）。

        这是 agent 主动让工作台观察 A2A 交流的入口，不是通信通道。

        Args:
            original_from: 原始发送者 agent_id
            original_to: 原始接收者 agent_id
            content: 消息内容
            message_id: 消息唯一 ID（去重用）

        Returns:
            {"ok": bool, "seq": int, "deduplicated": bool}

        Raises:
            AuthenticationError: 工作台拒绝签名
            TransportError: 网络错误
        """
        message = {
            "from": original_from,
            "to": original_to,
            "content": content,
            "via": "a2a",
            "message_id": message_id,
        }
        signature = sign_message(message, self._private_key) if self._private_key else ""

        payload = {
            "from": original_from,
            "to": original_to,
            "content": content,
            "via": "a2a",
            "message_id": message_id,
            "signature": signature,
        }

        client = self._ensure_forward_client()
        try:
            resp = await client.post(
                f"{self._forward_endpoint}/api/multiagent/collab/forward",
                json=payload,
            )
            if resp.status_code == 401:
                raise AuthenticationError(
                    f"Workbench rejected forward: {resp.json().get('detail', '')}"
                )
            if resp.status_code >= 400:
                raise TransportError(
                    f"Workbench forward failed: HTTP {resp.status_code}"
                )
            return resp.json()
        except httpx.HTTPError as e:
            raise TransportError(f"Forward network error: {e}") from e

    async def post_collab_message(self, message: dict) -> dict:
        """S4 G1: 写入协作轮次消息到工作台协作黑板。

        与 forward_to_workbench（归档 A2A 副本为 relay）不同，本方法写入的是
        带 collab_id+collab_round+type 的协作轮次消息（response/request），
        供 SDK agent 参与多轮协作。复用工作台 `/append` 管理接口（透传完整
        message dict，含 collab_round 等所有字段）。

        Args:
            message: 完整协作消息 dict（含 from/to/type/content/collab_id/
                     collab_round/accept/message_id 等字段）

        Returns:
            工作台响应（含 ok/seq/deduplicated）

        Raises:
            TransportError: 网络错误或工作台返回 4xx/5xx
        """
        if not self._forward_endpoint:
            raise TransportError(
                "post_collab_message requires forward_endpoint configured"
            )
        client = self._ensure_forward_client()
        payload = {"message": message}
        if message.get("collab_id"):
            payload["collab_id"] = message["collab_id"]
        try:
            resp = await client.post(
                f"{self._forward_endpoint}/api/multiagent/collab/append",
                json=payload,
            )
            if resp.status_code == 401:
                raise AuthenticationError(
                    f"Workbench rejected post_collab_message: {resp.json().get('detail', '')}"
                )
            if resp.status_code >= 400:
                raise TransportError(
                    f"Workbench post_collab_message failed: HTTP {resp.status_code}"
                )
            return resp.json()
        except httpx.HTTPError as e:
            raise TransportError(f"post_collab_message network error: {e}") from e

    def _map_error(self, err: A2AClientError) -> SDKError:
        """将 A2AClientError 映射为 SDK 异常。

        无 code 的 A2AClientError 通常是网络/传输错误（如 Connection refused、
        Retry exhausted），默认映射为 TransportError。
        """
        code = err.code
        if code in _ERROR_CODE_MAP:
            exc_cls = _ERROR_CODE_MAP[code]
            return exc_cls(str(err), code=str(code))
        return TransportError(str(err), code=None)

    async def close(self) -> None:
        """关闭传输层。"""
        await self._a2a_client.close()
        if self._forward_client:
            await self._forward_client.aclose()
            self._forward_client = None
