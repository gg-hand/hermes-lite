"""MCP 多 Server 管理器。

实现 ``MCPManager``，统一管理多个 MCP Server 连接的生命周期与工具聚合。
通过单一管理器对象即可向所有已连接的 MCP Server 汇总工具列表、按 Server
名称路由工具调用，并在退出时统一关闭。

设计要点：
- ``add_server`` 连接失败返回 ``False``，不抛异常，便于调用方按需处理。
- ``remove_server`` 幂等且异常安全，单个 Server 关闭失败不影响其他。
- ``get_all_tools`` 聚合所有已连接 Server 的工具，并在每个工具上注入
  ``mcp_server`` 字段，供上层路由识别工具归属。
- ``call_tool`` 路由到指定 Server，失败返回错误字符串而非抛异常。
- ``close_all`` 拷贝 keys 后逐个移除，避免迭代时修改字典。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Dict, List

from .client import MCPClient, MCPServerDef

logger = logging.getLogger(__name__)


class MCPManager:
    """管理多个 MCP Server 连接的生命周期。"""

    def __init__(self) -> None:
        self._clients: Dict[str, MCPClient] = {}

    async def add_server(self, server_def: MCPServerDef) -> bool:
        """添加并连接一个 MCP Server。

        参数:
            server_def: MCP Server 定义。

        返回:
            连接成功返回 ``True``，失败返回 ``False``（不抛异常）。
        """
        client = MCPClient(server_def)
        try:
            await client.connect()
        except Exception as e:
            logger.error(f"连接 MCP Server '{server_def.name}' 失败: {e}")
            return False
        self._clients[server_def.name] = client
        logger.info(f"已连接 MCP Server: {server_def.name}")
        return True

    async def remove_server(self, name: str) -> None:
        """移除并关闭一个 MCP Server。

        幂等：若 ``name`` 不存在则直接返回；关闭异常被捕获并记录，不抛出。

        参数:
            name: MCP Server 名称。
        """
        client = self._clients.pop(name, None)
        if client is None:
            return
        try:
            await client.close()
        except Exception as e:
            logger.error(f"关闭 MCP Server '{name}' 失败: {e}")

    def get_all_tools(self) -> List[dict]:
        """聚合所有已连接 MCP Server 的工具列表。

        每个工具 dict 前置注入 ``mcp_server`` 字段，标识工具归属的 Server 名称。

        返回:
            聚合后的工具列表，形如 ``[{"mcp_server": name, **tool}, ...]``。
        """
        tools: List[dict] = []
        for name, client in self._clients.items():
            for tool in client.list_tools():
                tools.append({"mcp_server": name, **tool})
        return tools

    async def call_tool(
        self, server_name: str, tool_name: str, args: dict
    ) -> str:
        """调用指定 MCP Server 上的工具。

        参数:
            server_name: MCP Server 名称。
            tool_name: 工具名称。
            args: 工具输入参数 dict。

        返回:
            工具返回的文本内容；Server 未连接或调用失败时返回错误字符串。
        """
        client = self._clients.get(server_name)
        if client is None:
            return f"MCP Server '{server_name}' 未连接"
        try:
            return await client.call_tool(tool_name, args)
        except Exception as e:
            logger.error(f"调用 MCP 工具 {server_name}.{tool_name} 失败: {e}")
            return f"MCP 工具调用失败: {e}"

    async def close_all(self) -> None:
        """关闭所有已连接的 MCP Server。

        拷贝 keys 后逐个移除，单个 Server 关闭失败不影响其他。
        """
        for name in list(self._clients.keys()):
            await self.remove_server(name)


def _make_mcp_handler(
    srv_name: str, orig_name: str, mgr: MCPManager
):
    """构建 MCP 工具的同步 handler 闭包，内部包装 ``mgr.call_tool`` 协程。

    通过工厂函数显式捕获 ``srv_name``/``orig_name``/``mgr``，避免循环中
    闭包变量延迟绑定到末次迭代值的问题。

    事件循环检测策略：
    - 主事件循环正在运行（如 FastAPI lifespan / async 上下文）→ 新建线程
      跑独立事件循环，避免 ``run_until_complete`` 阻塞主循环。
    - 有事件循环但未运行 → 直接 ``loop.run_until_complete(coro)``。
    - 无事件循环（``RuntimeError``）→ ``asyncio.run(coro)``。

    参数:
        srv_name: MCP Server 名称。
        orig_name: 原始工具名（不含前缀）。
        mgr: ``MCPManager`` 实例。

    返回:
        同步 handler 函数，签名 ``(**kwargs) -> str``。
    """

    def _handler(**kwargs) -> str:
        args_dict = dict(kwargs)
        coro = mgr.call_tool(srv_name, orig_name, args_dict)
        try:
            # 尝试获取正在运行的事件循环
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已有事件循环运行中（如 server.py 的 async 上下文）
                # 创建新线程运行协程避免阻塞
                result: List[str] = []

                def _run() -> None:
                    new_loop = asyncio.new_event_loop()
                    try:
                        result.append(new_loop.run_until_complete(coro))
                    finally:
                        new_loop.close()

                t = threading.Thread(target=_run)
                t.start()
                t.join()
                return result[0] if result else ""
            else:
                return loop.run_until_complete(coro)
        except RuntimeError:
            # 没有事件循环，用 asyncio.run
            return asyncio.run(coro)

    return _handler


def register_mcp_tools_to_registry(
    registry,
    mcp_manager: MCPManager,
    server_name: str,
) -> int:
    """将指定 MCP Server 的工具注册到 ToolRegistry 的 Deferred 层。

    工具名加前缀 ``mcp__{server_name}__{tool_name}`` 避免冲突。单个工具
    注册失败只记录 error 并跳过，不影响其他工具。Server 未连接返回 0。

    参数:
        registry: ``ToolRegistry`` 实例（需提供 ``register_deferred`` 方法）。
        mcp_manager: ``MCPManager`` 实例。
        server_name: MCP Server 名称。

    返回:
        成功注册的工具数量（``int``）。
    """
    # 过滤出指定 Server 的工具（get_all_tools 已注入 mcp_server 字段）
    server_tools = [
        t for t in mcp_manager.get_all_tools()
        if t.get("mcp_server") == server_name
    ]

    count = 0
    for tool in server_tools:
        original_name = tool.get("name", "")
        try:
            registered_name = f"mcp__{server_name}__{original_name}"
            description = tool.get("description", "") + f" [MCP: {server_name}]"
            # MCP 协议用 inputSchema（驼峰），ToolRegistry 用 input_schema（下划线）
            input_schema = tool.get("inputSchema", {})
            handler = _make_mcp_handler(server_name, original_name, mcp_manager)
            registry.register_deferred(
                name=registered_name,
                description=description,
                input_schema=input_schema,
                handler=handler,
            )
            count += 1
        except Exception as e:
            logger.error(f"注册 MCP 工具 {server_name}.{original_name} 失败: {e}")

    return count
