"""A2A 客户端：基于 httpx + JSON-RPC 2.0 的异步跨设备通信客户端。

特性：
- httpx.AsyncClient 全链路异步
- 自动重试（默认 2 次，仅对网络错误重试，JSON-RPC error 不重试）
- ed25519 请求签名（可选）
- async context manager 支持
- 并行调用所有端点（call_all_endpoints）
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class A2AClientError(Exception):
    """A2A 客户端错误。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class A2AClient:
    """A2A 异步客户端。"""

    def __init__(
        self,
        config: dict,
        signer_id: str | None = None,
        private_key=None,
    ):
        self._config = config.get("a2a", {}) or {}
        self._endpoints: list[dict] = self._config.get("remote_endpoints", []) or []
        self._timeout = self._config.get("timeout_seconds", 3)  # 任务 2.2：10→3，对端不通时快速失败
        self._retry_count = self._config.get("retry_count", 1)  # 任务 2.2：2→1（共 2 次调用）
        self._signer_id = signer_id
        self._private_key = private_key
        self._http_client: httpx.AsyncClient | None = None
        self._closed = False

    async def __aenter__(self) -> "A2AClient":
        self._http_client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._closed = True

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._closed:
            self._http_client = httpx.AsyncClient(timeout=self._timeout)
            self._closed = False
        return self._http_client

    def _get_endpoint_url(self, endpoint_name: str) -> str:
        for ep in self._endpoints:
            if ep["name"] == endpoint_name:
                return ep["url"].rstrip("/") + "/a2a/jsonrpc"
        raise A2AClientError(f"Unknown endpoint: {endpoint_name}")

    async def call_method(
        self,
        endpoint_name: str,
        method: str,
        params: dict,
    ) -> Any:
        """调用指定端点的 JSON-RPC 方法。

        Args:
            endpoint_name: 端点名称。
            method: JSON-RPC 方法名。
            params: 方法参数。

        Returns:
            JSON-RPC result 字段。

        Raises:
            A2AClientError: 网络错误（重试耗尽）或 JSON-RPC error。
        """
        url = self._get_endpoint_url(endpoint_name)
        client = self._ensure_client()

        # 签名（如配置）
        if self._signer_id and self._private_key:
            params = await self._sign_params(params)

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": str(uuid.uuid4()),
        }

        last_error: Exception | None = None
        for attempt in range(self._retry_count + 1):
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    raise A2AClientError(
                        f"HTTP {resp.status_code} from {endpoint_name}: {resp.text[:200]}"
                    )
                data = resp.json()

                if "error" in data:
                    err = data["error"]
                    raise A2AClientError(
                        err.get("message", "Unknown error"),
                        code=err.get("code"),
                    )
                return data.get("result")

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                last_error = e
                logger.warning(
                    "A2A 调用失败 (attempt %d/%d): %s - %s",
                    attempt + 1, self._retry_count + 1, endpoint_name, e,
                )
                if attempt < self._retry_count:
                    # 任务 2.2：指数退避 0.5 → 1.0 → 2.0，上限 2.0s
                    await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
                continue

        raise A2AClientError(f"Retry exhausted: {last_error}")

    async def call_all_endpoints(
        self,
        method: str,
        params: dict,
    ) -> dict[str, Any]:
        """并行调用所有端点，返回 {endpoint_name: result}。

        失败的端点 result 为 A2AClientError 实例。
        """
        # 先确保 HTTP client 已创建，避免并行时 _ensure_client 竞态
        self._ensure_client()

        async def _call(name: str) -> Any:
            try:
                return await self.call_method(name, method, params)
            except A2AClientError as e:
                return e

        names = [ep["name"] for ep in self._endpoints]
        results_list = await asyncio.gather(*[_call(n) for n in names])
        return dict(zip(names, results_list))

    async def _sign_params(self, params: dict) -> dict:
        """对参数签名（ed25519）。

        移除 signature 字段后序列化，与 sign_message 保持一致，
        确保客户端签名内容与服务端验签内容相同。
        """
        msg_copy = {k: v for k, v in params.items() if k != "signature"}
        canonical = json.dumps(msg_copy, sort_keys=True, ensure_ascii=False).encode("utf-8")
        signature = self._private_key.sign(canonical)
        params = {**params, "signature": signature.hex()}
        return params
