"""标准 A2A 客户端（StdA2AClient）。

与旧 A2AClient（自定义 JSON-RPC 方法）并存：
- 发现：GET {base}/.well-known/agent-card.json（60s TTL 缓存），取代 register_remote_agent
- 方法：message/send、message/stream(SSE)、tasks/get|list|cancel、tasks/resubscribe
- 线格式：规范方言（message/send 风格方法名 + 小写枚举）
- 重试：仅 ConnectError/ReadTimeout/WriteTimeout，指数退避（与旧客户端一致）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import httpx

from teage_liu.multiagent.a2a_std.models import Message

logger = logging.getLogger(__name__)

_CARD_TTL_SECONDS = 60.0
_RETRY_BACKOFF = (0.5, 1.0, 2.0)


class StdA2AClientError(Exception):
    """标准 A2A 客户端错误。"""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class StdA2AClient:
    """标准 A2A v1.0 客户端。

    config 结构（与旧 A2AClient 一致）：
        config["a2a"]["remote_endpoints"] = [{"name": "...", "url": "http://base"}]
    """

    def __init__(self, config: dict) -> None:
        a2a_cfg = config.get("a2a", {}) or {}
        self._endpoints: list[dict] = list(a2a_cfg.get("remote_endpoints") or [])
        timeout = float(a2a_cfg.get("timeout_seconds", 10))
        self._retry_count = int(a2a_cfg.get("retry_count", 2))
        self._client = httpx.AsyncClient(timeout=timeout)
        self._card_cache: dict[str, tuple[float, dict]] = {}

    @property
    def endpoints(self) -> list[str]:
        """已配置的端点名列表。"""
        return [ep["name"] for ep in self._endpoints]

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------

    async def fetch_agent_card(self, endpoint_name: str) -> dict:
        """拉取对端 Agent Card（60s TTL 缓存）。"""
        cached = self._card_cache.get(endpoint_name)
        if cached and time.time() - cached[0] < _CARD_TTL_SECONDS:
            return cached[1]
        base = self._endpoint_url(endpoint_name)
        try:
            resp = await self._client.get(f"{base}/.well-known/agent-card.json")
        except httpx.HTTPError as e:
            raise StdA2AClientError(f"Agent Card 拉取失败 ({endpoint_name}): {e}") from e
        if resp.status_code != 200:
            raise StdA2AClientError(
                f"Agent Card HTTP {resp.status_code} ({endpoint_name})"
            )
        card = resp.json()
        self._card_cache[endpoint_name] = (time.time(), card)
        return card

    async def get_jsonrpc_url(self, endpoint_name: str) -> str:
        """对端 JSON-RPC 端点：card.url 优先，缺省回退 {base}/a2a/std/jsonrpc。"""
        try:
            card = await self.fetch_agent_card(endpoint_name)
            url = card.get("url") or ""
            if url:
                return url.rstrip("/")
        except StdA2AClientError:
            pass
        return self._endpoint_url(endpoint_name).rstrip("/") + "/a2a/std/jsonrpc"

    # ------------------------------------------------------------------
    # 标准方法
    # ------------------------------------------------------------------

    async def message_send(
        self,
        endpoint_name: str,
        message: Message,
        context_id: Optional[str] = None,
    ) -> dict:
        """message/send：返回 Task wire dict。"""
        params: dict[str, Any] = {"message": message.model_dump(by_alias=True)}
        if context_id:
            params["contextId"] = context_id
        result = await self._call(endpoint_name, "message/send", params)
        return result

    async def message_stream(
        self,
        endpoint_name: str,
        message: Message,
        context_id: Optional[str] = None,
        on_event: Optional[Any] = None,
    ) -> AsyncIterator[dict]:
        """message/stream：SSE 事件流（{method, params, id}）。"""
        params: dict[str, Any] = {"message": message.model_dump(by_alias=True)}
        if context_id:
            params["contextId"] = context_id
        async for event in self._stream(endpoint_name, "message/stream", params):
            if on_event:
                await on_event(event)
            yield event

    async def tasks_get(
        self, endpoint_name: str, task_id: str, context_id: Optional[str] = None
    ) -> dict:
        params: dict[str, Any] = {"id": task_id}
        if context_id:
            params["contextId"] = context_id
        return await self._call(endpoint_name, "tasks/get", params)

    async def tasks_list(
        self, endpoint_name: str, context_id: Optional[str] = None
    ) -> list:
        params: dict[str, Any] = {}
        if context_id:
            params["contextId"] = context_id
        return await self._call(endpoint_name, "tasks/list", params)

    async def tasks_cancel(
        self, endpoint_name: str, task_id: str, context_id: Optional[str] = None
    ) -> dict:
        params: dict[str, Any] = {"id": task_id}
        if context_id:
            params["contextId"] = context_id
        return await self._call(endpoint_name, "tasks/cancel", params)

    async def tasks_resubscribe(
        self,
        endpoint_name: str,
        task_id: str,
        context_id: Optional[str] = None,
        last_event_id: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """tasks/resubscribe：重放 Last-Event-ID 之后的事件。"""
        params: dict[str, Any] = {"id": task_id}
        if context_id:
            params["contextId"] = context_id
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        async for event in self._stream(
            endpoint_name, "tasks/resubscribe", params, headers=headers
        ):
            yield event

    async def wait_for_terminal(
        self,
        endpoint_name: str,
        task_id: str,
        context_id: Optional[str] = None,
        timeout: float = 60.0,
    ) -> dict:
        """轮询 tasks/get 直至终态（对应旧 _wait_for_response）。"""
        from teage_liu.multiagent.a2a_std.models import TaskState

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            task = await self.tasks_get(endpoint_name, task_id, context_id)
            try:
                state = TaskState.parse((task.get("status") or {}).get("state", ""))
            except ValueError:
                state = None
            if state is not None and state.is_terminal:
                return task
            if asyncio.get_event_loop().time() > deadline:
                raise StdA2AClientError(
                    f"等待任务终态超时 ({timeout}s): {task_id} state={state}"
                )
            await asyncio.sleep(0.5)

    async def call_all_endpoints(self, method: str, **kwargs) -> dict[str, Any]:
        """并行调用所有端点（结果 dict，失败为 StdA2AClientError 实例）。"""

        async def _call(name: str) -> Any:
            fn = getattr(self, method, None)
            if fn is None:
                raise StdA2AClientError(f"未知方法: {method}")
            return await fn(name, **kwargs)

        results = await asyncio.gather(*[_call(ep["name"]) for ep in self._endpoints])
        return dict(zip([ep["name"] for ep in self._endpoints], results))

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _endpoint_url(self, endpoint_name: str) -> str:
        for ep in self._endpoints:
            if ep["name"] == endpoint_name:
                return str(ep["url"]).rstrip("/")
        raise StdA2AClientError(f"Unknown endpoint: {endpoint_name}")

    async def _call(self, endpoint_name: str, method: str, params: dict) -> Any:
        url = await self.get_jsonrpc_url(endpoint_name)
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        last_exc: Optional[Exception] = None
        for attempt in range(self._retry_count + 1):
            try:
                resp = await self._client.post(
                    url, json=payload, headers={"A2A-Version": "1.0"}
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                last_exc = e
                if attempt < self._retry_count:
                    await asyncio.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
                    continue
                raise StdA2AClientError(
                    f"网络错误 ({endpoint_name} {method}): {e}"
                ) from e
            body = resp.json()
            if "error" in body:
                err = body["error"]
                raise StdA2AClientError(
                    f"{err.get('message', 'A2A error')}", code=err.get("code")
                )
            return body.get("result")

        raise StdA2AClientError(f"重试耗尽: {last_exc}")  # pragma: no cover

    async def _stream(
        self,
        endpoint_name: str,
        method: str,
        params: dict,
        headers: Optional[dict] = None,
    ) -> AsyncIterator[dict]:
        """SSE 事件流：解析 event:/data: 帧为 {method, params, id}。"""
        url = await self.get_jsonrpc_url(endpoint_name)
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        req_headers = {"A2A-Version": "1.0", "Accept": "text/event-stream"}
        if headers:
            req_headers.update(headers)
        try:
            async with self._client.stream("POST", url, json=payload, headers=req_headers) as resp:
                if resp.status_code != 200:
                    raise StdA2AClientError(f"SSE HTTP {resp.status_code} ({endpoint_name})")
                event_method: Optional[str] = None
                event_id: Optional[str] = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_method = line[len("event:"):].strip()
                    elif line.startswith("id:"):
                        event_id = line[len("id:"):].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:"):].strip()
                        if not data:
                            continue
                        try:
                            frame = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        # 兼容两种帧：{method, params, id}（我方）与 {jsonrpc, result/error, id}
                        if "params" in frame and "method" in frame:
                            yield {
                                "method": frame["method"],
                                "params": frame.get("params") or {},
                                "id": frame.get("id") or event_id,
                            }
                        elif "result" in frame:
                            yield {
                                "method": event_method or "result",
                                "params": frame["result"],
                                "id": frame.get("id") or event_id,
                            }
                        elif "error" in frame:
                            err = frame["error"]
                            raise StdA2AClientError(
                                f"{err.get('message', 'A2A error')}", code=err.get("code")
                            )
                        event_method = None
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            raise StdA2AClientError(f"SSE 网络错误 ({endpoint_name}): {e}") from e

    async def close(self) -> None:
        await self._client.aclose()
