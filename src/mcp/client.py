"""MCP 客户端适配器实现。

实现 ``MCPClient``，通过 stdio/SSE/HTTP 三种 transport 连接外部 MCP Server，
使用 JSON-RPC 2.0 协议交换消息。连接建立后缓存工具列表，调用 ``call_tool``
返回拼接后的文本结果。

设计要点：
- stdio：``asyncio.create_subprocess_exec`` 启动子进程，stdin/stdout 行交换。
- sse/http：``httpx.AsyncClient`` POST JSON-RPC 请求（SSE 暂简化为普通 POST）。
- ``connect()`` 失败时抛异常，不设置 ``_connected``。
- ``close()`` 捕获所有异常并记录 warning，保证清理不抛异常。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MCPServerDef:
    """MCP Server 定义（dataclass）。

    Attributes:
        name: Server 名称（唯一标识，由 MCPManager 用于路由）。
        transport: 传输方式，``"stdio"`` | ``"sse"`` | ``"http"``。
        command: stdio 模式下启动子进程的命令（如 ``"npx"``）。
        args: stdio 模式下传给子进程的参数列表。
        url: sse/http 模式下远程 MCP Server 的 URL。
        env: stdio 模式下注入子进程的环境变量。
    """

    name: str
    transport: str  # "stdio" | "sse" | "http"
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    url: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)


class MCPClient:
    """MCP 客户端，连接单个 MCP Server 并暴露 ``list_tools``/``call_tool``。

    兼容 JSON-RPC 2.0 协议，与 Anthropic 推动的 MCP 开放协议兼容。

    典型用法：
        >>> client = MCPClient(server_def)
        >>> await client.connect()
        >>> tools = client.list_tools()
        >>> result = await client.call_tool("search_repos", {"q": "python"})
        >>> await client.close()

    Attributes:
        defn: 关联的 ``MCPServerDef``。
    """

    def __init__(self, server_def: MCPServerDef) -> None:
        """初始化 MCP 客户端。

        参数:
            server_def: MCP Server 定义。
        """
        self.defn = server_def
        self._process: Optional[asyncio.subprocess.Process] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._tools: List[dict] = []
        self._connected: bool = False
        self._request_id: int = 0  # 自增计数器

    async def connect(self) -> None:
        """连接 MCP Server 并完成初始化握手。

        - 根据 ``transport`` 分发到 ``_connect_stdio()`` 或 ``_connect_http()``。
        - 发送 ``initialize`` 请求完成协议握手。
        - 发送 ``tools/list`` 获取工具列表并缓存。
        - 标记 ``_connected = True``。

        Raises:
            ValueError: 不支持的 transport 值。
            RuntimeError: MCP Server 返回错误或连接关闭。
        """
        if self.defn.transport == "stdio":
            await self._connect_stdio()
        elif self.defn.transport in ("sse", "http"):
            await self._connect_http()
        else:
            raise ValueError(f"不支持的 transport: {self.defn.transport}")

        # 协议握手
        await self._send_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "hermes-lite", "version": "0.1.0"},
        })

        # 获取工具列表
        result = await self._send_request("tools/list", {})
        self._tools = result.get("tools", [])

        self._connected = True
        logger.info(
            "MCP Server '%s' 已连接（transport=%s，工具数=%d）",
            self.defn.name,
            self.defn.transport,
            len(self._tools),
        )

    async def _connect_stdio(self) -> None:
        """启动 stdio 子进程。"""
        if not self.defn.command:
            raise ValueError("stdio transport 需要 command 字段")
        env = {**self.defn.env} if self.defn.env else None
        self._process = await asyncio.create_subprocess_exec(
            self.defn.command,
            *self.defn.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        logger.debug(
            "已启动 stdio MCP Server '%s'（pid=%s）",
            self.defn.name,
            self._process.pid,
        )

    async def _connect_http(self) -> None:
        """创建 httpx.AsyncClient（SSE 与 HTTP 共用，SSE 暂简化为普通 POST）。"""
        if not self.defn.url:
            raise ValueError(f"{self.defn.transport} transport 需要 url 字段")
        self._http_client = httpx.AsyncClient(base_url=self.defn.url)
        logger.debug(
            "已创建 HTTP client for MCP Server '%s'（base_url=%s）",
            self.defn.name,
            self.defn.url,
        )

    def list_tools(self) -> List[dict]:
        """返回缓存的工具列表。

        返回:
            工具 schema 列表（由 ``tools/list`` 缓存）。
        """
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        """调用 MCP Server 上的工具。

        参数:
            name: 工具名称。
            arguments: 工具输入参数 dict。

        返回:
            工具返回的文本内容（多个 text item 以 ``\\n`` 拼接）。

        Raises:
            RuntimeError: MCP Server 返回协议错误。
        """
        result = await self._send_request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        texts: List[str] = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
        return "\n".join(texts)

    async def _send_request(self, method: str, params: dict) -> dict:
        """发送 JSON-RPC 2.0 请求。

        根据 transport 分发到 ``_send_stdio`` 或 ``_send_http``。

        参数:
            method: JSON-RPC 方法名（如 ``"initialize"``、``"tools/list"``）。
            params: 方法参数 dict。

        返回:
            JSON-RPC ``result`` 字段（dict）。
        """
        if self.defn.transport == "stdio":
            return await self._send_stdio(method, params)
        return await self._send_http(method, params)

    async def _send_stdio(self, method: str, params: dict) -> dict:
        """通过子进程 stdin/stdout 交换 JSON-RPC 消息。"""
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        data = (json.dumps(request, ensure_ascii=False) + "\n").encode()
        assert self._process is not None
        assert self._process.stdin is not None
        self._process.stdin.write(data)
        await self._process.stdin.drain()

        assert self._process.stdout is not None
        line = await self._process.stdout.readline()
        if not line:  # b""
            raise RuntimeError("MCP Server 连接关闭")
        response = json.loads(line.decode())
        if "error" in response:
            raise RuntimeError(f"MCP 错误: {response['error']}")
        return response.get("result", {})

    async def _send_http(self, method: str, params: dict) -> dict:
        """通过 httpx POST JSON-RPC 请求。"""
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        assert self._http_client is not None
        response = await self._http_client.post("/", json=request)
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"MCP 错误: {data['error']}")
        return data.get("result", {})

    async def close(self) -> None:
        """关闭连接，释放子进程/HTTP 客户端。

        任何异常都捕获并记录 warning，保证清理不抛异常。
        """
        if self._process is not None:
            try:
                self._process.terminate()
                await self._process.wait()
            except Exception as e:
                logger.warning(
                    "关闭 stdio MCP Server '%s' 时出错: %s", self.defn.name, e
                )
            self._process = None

        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception as e:
                logger.warning(
                    "关闭 HTTP MCP Server '%s' 时出错: %s", self.defn.name, e
                )
            self._http_client = None

        self._connected = False
