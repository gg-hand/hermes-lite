"""MCP (Model Context Protocol) 客户端适配器。

提供 MCPClient 连接外部 MCP Server，支持 stdio/SSE/HTTP 三种 transport。
兼容 JSON-RPC 2.0 协议，与 Anthropic 推动的 MCP 开放协议兼容。

典型用法：
    server_def = MCPServerDef(name="github", transport="stdio",
                              command="npx", args=["@anthropic/mcp-server-github"])
    client = MCPClient(server_def)
    await client.connect()
    tools = client.list_tools()
    result = await client.call_tool("search_repos", {"q": "python"})
    await client.close()
"""
