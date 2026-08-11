# Teage Agent SDK API 参考

## TeageAgent 类

### 构造函数

```python
TeageAgent(
    agent_id: str,
    capabilities: list[str] | None = None,
    private_key=None,
    bb_root: str | None = None,
    forward_endpoint: str = "",
    remote_endpoints: list[dict] | None = None,
    a2a_server_host: str = "0.0.0.0",
    a2a_server_port: int = 18401,
    heartbeat_interval_seconds: int = 10,
    transport: A2ATransport | None = None,
)
```

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| agent_id | str | 是 | Agent 唯一 ID（3-32 字符） |
| capabilities | list[str] | 否 | 能力列表 |
| private_key | Ed25519PrivateKey | 否 | 私钥（写操作签名用） |
| bb_root | str | 否 | 黑板根目录路径（DirectorInjector 轮询 directive） |
| forward_endpoint | str | 否 | 工作台 Forward API URL |
| remote_endpoints | list[dict] | 否 | 其他 agent 的 A2A Server 端点列表 |
| a2a_server_host | str | 否 | 本 agent A2A Server 监听地址，默认 0.0.0.0 |
| a2a_server_port | int | 否 | 本 agent A2A Server 监听端口，默认 18401 |
| heartbeat_interval_seconds | int | 否 | 心跳间隔，默认 10 秒 |
| transport | A2ATransport | 否 | 注入传输层（测试用） |

### 方法

#### `start() -> None`
启动 agent：A2A Server + 注册 + 心跳循环。

#### `stop() -> None`
停止 agent：A2A Server + 心跳 + 传输层。

#### `register() -> dict`
注册到工作台 A2A Gateway。返回注册结果。

#### `send_message(content: str, to_agent: str) -> dict`
发送消息给目标 agent（A2A 点对点，不经工作台中转）。自动归档副本到 collaboration.md。
返回 `{"ok": bool, "received_by": str}`。

#### `forward_a2a_message(original_from: str, original_to: str, content: str, message_id: str) -> dict`
归档 A2A 消息副本到工作台 collaboration.md（Forward API）。
返回 `{"ok": bool, "seq": int, "deduplicated": bool}`。

#### `query_agent(target_agent: str) -> dict`
查询目标 agent 的能力（A2A 点对点）。
返回 `{"agent_id": str, "capabilities": list[str]}`。

#### `get_directive_context() -> str`
获取待注入的 Director 上下文（搭便车机制）。Agent 调用 LLM 前调用，返回值拼到 system prompt。

### 回调

#### `on_message: Callable[[dict], Awaitable[None]] | None`
A2A Server 收到消息时的回调函数，由用户设置。

## A2A Server JSON-RPC 方法（agent 侧）

SDK 的 A2A Server 暴露以下 JSON-RPC 方法供其他 agent 调用：

| 方法 | 签名 | 说明 |
|---|---|---|
| send_message | ({from, to, content, message_id, signature}) -> {ok, received_by} | 接收点对点消息 |
| query_capabilities | () -> {agent_id, capabilities} | 查询本 agent 能力 |
| ping | () -> {ok, agent_id, pong} | 健康检查 |

## A2A Gateway JSON-RPC 方法（工作台侧）

SDK 通过 A2A Gateway 调用以下方法（仅文件访问代理，非通信通道）：

| 方法 | 签名 | 说明 |
|---|---|---|
| register_remote_agent | ({agent_id, role, capabilities, a2a_endpoint}) -> {registered} | 注册 agent |
| heartbeat | ({agent_id, status?, current_task?}) -> {ok} | 心跳上报 |
| read_collab_messages | ({collab_id?, limit?}) -> {messages: list} | 读取协作消息归档 |
| read_messages | ({limit?}) -> {messages: list} | 读取主对话消息 |
| read_file | ({path}) -> {content, exists} | 读取白名单文件 |
| list_agents | () -> {agents: list} | 列出 active agents |
| acquire_lock | ({lock_name, agent_id, ttl_seconds}) -> {fencing_token} | 获取锁 |
| release_lock | ({lock_name, agent_id, fencing_token}) -> {released} | 释放锁 |

> **注意**：A2A Gateway 的 `append_message` 已标记为 deprecated，agent 应使用 Forward API（`POST /api/multiagent/collab/forward`）归档消息副本。

## 错误码

| 代码 | 异常类 | 说明 |
|---|---|---|
| -32700 | SDKError | 解析错误 |
| -32600 | ProtocolError | 无效请求 |
| -32601 | ProtocolError | 方法不存在 |
| -32602 | ProtocolError | 参数无效 |
| -32603 | SDKError | 内部错误 |
| -32001 | AuthenticationError | 签名验证失败 |
| -32002 | ProtocolError | 路径沙箱违规 |
| -32003 | TransportError | 限流 |
| -32004 | TransportError | 锁获取失败 |
