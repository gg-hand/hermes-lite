"""Phase 4 Task 10: MCPClient 与 MCPManager 测试。"""

from __future__ import annotations

import asyncio
import json
import unittest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "teage_liu"))

from teage_liu.mcp.client import MCPClient, MCPServerDef
from teage_liu.mcp.manager import MCPManager


class TestMCPServerDef(unittest.TestCase):
    """MCPServerDef dataclass 测试。"""

    def test_mcp_server_def_dataclass(self):
        """dataclass 字段验证。"""
        d = MCPServerDef(
            name="github",
            transport="stdio",
            command="npx",
            args=["@anthropic/mcp-server-github"],
        )
        self.assertEqual(d.name, "github")
        self.assertEqual(d.transport, "stdio")
        self.assertEqual(d.command, "npx")
        self.assertEqual(d.args, ["@anthropic/mcp-server-github"])
        self.assertIsNone(d.url)
        self.assertEqual(d.env, {})

    def test_mcp_server_def_http(self):
        """HTTP transport 配置。"""
        d = MCPServerDef(name="db", transport="http", url="https://mcp/db")
        self.assertEqual(d.transport, "http")
        self.assertEqual(d.url, "https://mcp/db")
        self.assertIsNone(d.command)


class TestMCPClient(unittest.TestCase):
    """MCPClient 测试。"""

    def test_call_tool_extracts_text_content(self):
        """响应 content 提取 text 类型。"""
        # 用 mock httpx.AsyncClient 测试
        def run_test():
            server_def = MCPServerDef(name="t", transport="http", url="http://mock")
            client = MCPClient(server_def)
            # mock _send_request 返回含 content 的 result
            client._send_request = AsyncMock(return_value={
                "content": [
                    {"type": "text", "text": "line1"},
                    {"type": "image", "image": "..."},  # 应被忽略
                    {"type": "text", "text": "line2"},
                ]
            })
            result = asyncio.run(client.call_tool("search", {"q": "test"}))
            self.assertEqual(result, "line1\nline2")
        run_test()

    def test_error_response_raises_runtime_error(self):
        """响应含 error 字段时抛 RuntimeError。"""
        server_def = MCPServerDef(name="t", transport="http", url="http://mock")
        client = MCPClient(server_def)
        client._send_request = AsyncMock(side_effect=RuntimeError("MCP 错误: {\"code\": -1}"))
        with self.assertRaises(RuntimeError):
            asyncio.run(client.call_tool("search", {}))

    def test_connect_stdio_mock(self):
        """stdio 传输 mock 验证 initialize + tools/list。"""
        def run_test():
            server_def = MCPServerDef(name="t", transport="stdio", command="echo")
            client = MCPClient(server_def)
            # mock 子进程
            mock_process = AsyncMock()
            mock_process.stdin = MagicMock()
            mock_process.stdin.write = MagicMock()
            mock_process.stdin.drain = AsyncMock()
            mock_process.stdout = MagicMock()
            mock_process.stderr = MagicMock()
            mock_process.terminate = MagicMock()
            mock_process.wait = AsyncMock()
            # readline 依次返回 initialize 与 tools/list 响应
            mock_process.stdout.readline = AsyncMock(side_effect=[
                b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{}}}\n',
                b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"search","description":"search"}]}}\n',
            ])
            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                asyncio.run(client.connect())
            self.assertTrue(client._connected)
            self.assertEqual(len(client._tools), 1)
            self.assertEqual(client._tools[0]["name"], "search")
        run_test()

    def test_connect_http_mock(self):
        """HTTP 传输 mock 验证 JSON-RPC 请求格式。"""
        def run_test():
            server_def = MCPServerDef(name="t", transport="http", url="http://mock")
            client = MCPClient(server_def)
            # mock httpx.AsyncClient
            mock_http = MagicMock()
            mock_http.post = AsyncMock()
            # 第一次 POST 返回 initialize 响应，第二次返回 tools/list
            mock_http.post.side_effect = [
                MagicMock(json=lambda: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}),
                MagicMock(json=lambda: {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search"}]}}),
            ]
            mock_http.aclose = AsyncMock()
            with patch("httpx.AsyncClient", return_value=mock_http):
                asyncio.run(client.connect())
            self.assertTrue(client._connected)
            self.assertEqual(len(client._tools), 1)
            # 验证 POST 调用次数（initialize + tools/list）
            self.assertEqual(mock_http.post.call_count, 2)
        run_test()


class TestMCPManager(unittest.TestCase):
    """MCPManager 测试。"""

    def test_mcp_manager_add_remove(self):
        """add_server + remove_server 流程。"""
        def run_test():
            manager = MCPManager()
            # mock MCPClient
            mock_client = MagicMock()
            mock_client.connect = AsyncMock()
            mock_client.close = AsyncMock()
            mock_client._connected = True
            mock_client.defn = MCPServerDef(name="test", transport="stdio", command="x")
            mock_client.list_tools = MagicMock(return_value=[])
            with patch("teage_liu.mcp.manager.MCPClient", return_value=mock_client):
                ok = asyncio.run(manager.add_server(MCPServerDef(name="test", transport="stdio", command="x")))
            self.assertTrue(ok)
            self.assertIn("test", manager._clients)
            asyncio.run(manager.remove_server("test"))
            self.assertNotIn("test", manager._clients)
            # remove 不存在的 name 应幂等
            asyncio.run(manager.remove_server("nonexistent"))
        run_test()

    def test_mcp_manager_get_all_tools(self):
        """聚合多 Server 工具。"""
        def run_test():
            manager = MCPManager()
            # 添加两个 mock client
            for name, tools in [("s1", [{"name": "t1"}]), ("s2", [{"name": "t2"}])]:
                mock_client = MagicMock()
                mock_client.connect = AsyncMock()
                mock_client._connected = True
                mock_client.defn = MCPServerDef(name=name, transport="stdio", command="x")
                mock_client.list_tools = MagicMock(return_value=tools)
                with patch("teage_liu.mcp.manager.MCPClient", return_value=mock_client):
                    asyncio.run(manager.add_server(MCPServerDef(name=name, transport="stdio", command="x")))
            all_tools = manager.get_all_tools()
            self.assertEqual(len(all_tools), 2)
            # 每个工具应含 mcp_server 字段
            servers = {t["mcp_server"] for t in all_tools}
            self.assertEqual(servers, {"s1", "s2"})
        run_test()


if __name__ == "__main__":
    unittest.main()
