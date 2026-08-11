# A2A 标准协议（v1.0）实现说明

本文档声明 teage-liu 的 Agent-to-Agent 协议层对 **标准 A2A v1.0** 的合规实现。
**标准 A2A 是底线**——协议层完整达标后才谈扩展。

## 1. 达标声明

- **Agent Card**：`GET /.well-known/agent-card.json`（RFC 8615），`protocolVersion: "1.0"`，
  `preferredTransport: "JSONRPC"`，含 `supportedInterfaces`（官方 SDK v1.0 方言的端点声明）。
- **传输**：JSON-RPC 2.0 over HTTP(S)，`POST /a2a/std/jsonrpc`（card.url 指向此处）。
- **完整方法集**：`message/send`、`message/stream`(SSE)、`tasks/get`、`tasks/list`、
  `tasks/cancel`、`tasks/resubscribe`、`tasks/pushNotificationConfig/set|get|list|delete`、
  `agent/getAuthenticatedExtendedCard`。
- **数据模型**：`Message{role, messageId, contextId, parts[]}`、`Part`（Text/File/Data 判别联合）、
  `Task{id, contextId, status, artifacts[], history[]}`、`TaskState` 八态、SSE 事件
  （`TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent`）。
- **生命周期**：`submitted → working`；非终态中断 `input-required`（审批桥驱动）/
  `auth-required`；终态 `completed / failed / canceled / rejected`（终态不可重启，
  新工作须在同一 contextId 下新建 Task）。**任何路径必发终态事件**，流以终态事件收尾后关闭。
- **task id 服务端生成**（`t_<hex12>`，客户端不得自造）。
- **流式**：SSE 帧 `id: <taskId:seq>`（Last-Event-ID 断点续传）+ `event:` + `data:`（JSON-RPC 信封）。
- **错误码**：标准 JSON-RPC 码 + A2A 标准库码 `-32001 TaskNotFound / -32002 TaskNotCancelable
  / -32003 PushNotificationNotSupported / -32004 UnsupportedOperation / -32005
  ContentTypeNotSupported / -32006 MessageException`。
- **鉴权**：`security.api_key` 非空时 card 声明 bearer scheme（`securitySchemes` map 形式，
  官方 SDK v1.0 实现），`/a2a/std/jsonrpc` 继承全局 Bearer 中间件；`/.well-known/` 免鉴权（发现公开）。
- **合规验证**：
  - 官方 `a2a-sdk` 1.1.2 客户端互操作测试（`tests/multiagent/test_a2a_std_conformance.py`）：
    卡片发现 → SendStreamingMessage 流式 → GetTask → 终态取消拒绝（错误码映射）→ 取消成功。
  - `@a2a-compliance/cli card <url>`：**6 passed / 0 failed / tier: FULL_FEATURED**。

## 2. 双方言（wire dialect）

a2a-protocol.org 规范 JSON schema（早期）与官方 a2a-sdk 1.1.2（v1.0 protobuf 方言）存在分歧：

| 维度 | 规范风格（canonical） | SDK 1.1.2 方言（sdk） |
|---|---|---|
| 方法名 | `message/send` | `SendMessage`（PascalCase） |
| 枚举 | `working` / `user` | `TASK_STATE_WORKING` / `ROLE_USER` |
| Part | `{"kind":"text","text":...}` | `{"text":...}`（无 kind，oneof 推断） |
| message/send 结果 | Task 直出 | `{"task": Task}`（SendMessageResponse） |
| SSE data 帧 | JSON-RPC 信封 + method/params | `{"jsonrpc","result": StreamResponse}` |
| Card 端点 | `url` 字段 | `supportedInterfaces[].url` |
| securitySchemes | list | map（scheme 名 → scheme） |

**策略：请求侧嗅探方言，响应按请求方言输出**（`teage_liu/multiagent/a2a_std/dialect.py`）——
SDK 客户端与规范风格客户端均可互通；内部 Task 记录始终存规范小写值。

## 3. 架构

```
标准 A2A 面（teage_liu/multiagent/a2a_std/）
├── models.py         pydantic 数据模型（含双方言枚举/Part 解析）
├── exceptions.py     A2A 标准错误码
├── jsonrpc.py        JSON-RPC 2.0 分发器
├── task_store.py     Task 持久化（{bb}/tasks/a2a/tasks.json）+ 事件集线器
├── task_manager.py   Task 生命周期（终态守卫 / SSE 流 / resubscribe 重放）
├── agent_card.py     Agent Card 构建（url/supportedInterfaces/securitySchemes）
├── engine_adapter.py 标准面 ↔ 内部引擎（collab 写入 + TaskDriver 轮询映射）
├── handlers.py       标准方法 handlers（按方言输出）
├── sse.py            SSE 帧构造
├── router.py         GET /.well-known/agent-card.json + POST /a2a/std/jsonrpc
├── client.py         StdA2AClient（card 发现 + 标准方法 + wait_for_terminal）
├── dialect.py        双方言嗅探/转换/过滤
├── extensions.py     旧自定义方法（A2A-Extensions 头门控）
└── approval_bridge.py 审批请求 → input-required
```

- **标准面 ↔ 内部引擎映射**（engine_adapter.py / TaskDriver，只读轮询，写仍归 worker/director）：

| 标准 A2A | 内部 |
|---|---|
| `task.id` | `t_<hex12>`（存 tasks.json） |
| `task.contextId` | `collab_id` / `session_id` |
| `working` | 内部 `request` 写入 `collabs/{cid}.md` |
| `completed` | 内部 `consensus`/`end` 或 `status/result: completed` |
| `failed` | 内部 `error` 或 `result: failed` |
| `input-required` | 审批桥写 `status:"input_required"` |
| `artifacts[]` | 每个内部 `response`/`result` → `Artifact("response_<seq>")` |
| `history[]` | collab 消息 → 标准 Message |

- **双平面保留**：worker 协作面（blackboard/collabs）与主会话面（channel=main_session）
  均映射到标准 Task 模型；`a2a.standard.mode: "legacy" | "standard"` 控制工具走
  标准通道（`message/send` + `wait_for_terminal`）还是旧通道（`collab_message` 广播）。

## 4. 扩展（标准达标后的兼容层）

旧自定义方法（`list_agents`、`read_messages`、`append_message`、`acquire_lock`、
`release_lock`、`read_director_md`、`register_remote_agent`、`heartbeat`、`read_file`、
`director_broadcast`、`agent_message`、`collab_message`）经 **A2A-Extensions 头门控**
挂载在标准端点（`extensions.py`），声明在 card `extensions` 列表；未声明扩展头的请求
看不到扩展方法（-32601）。旧端点 `POST /a2a/jsonrpc` 保持原样，已部署实例平滑过渡。
弃用路径：`a2a.standard.extensions_enabled: false` → 扩展方法全部 -32601；未来大版本移除旧网关。

## 5. 配置

```yaml
a2a:
  enabled: true
  remote_endpoints: [{name, url}]      # 对端实例 base URL
  standard:
    enabled: true
    name: "teagent-lu"                 # card name（缺省 agent_id）
    base_url: ""                       # 空 → 开发态从 TEAGE_HOST/PORT 推导；生产显式设公网 URL
    endpoint_url: ""                   # 空 → base_url + /a2a/std/jsonrpc
    capabilities: {streaming: true, push_notifications: false}
    mode: "legacy"                     # legacy | standard（工具通道；达标后翻转）
    task_store_path: ""                # 缺省 {blackboard_dir}/tasks/a2a/tasks.json
    extensions_enabled: true
    extensions: [...]                  # 扩展方法白名单（缺省全部）
```

## 6. 运维

- **nginx 无需改动**：`location /` 已透传 `/.well-known/` 与 `/a2a/std/`，
  `proxy_buffering off` 已支持 SSE。
- **部署时合规门禁**：`npx --yes @a2a-compliance/cli card <公网URL>` 须 FULL_FEATURED。
- **测试**：`pytest tests/multiagent/test_a2a_std_*.py -v`；
  e2e：`pytest tests/multiagent/test_a2a_std_e2e.py -m e2e`；
  合规 CLI（慢）：`pytest tests/multiagent/test_a2a_std_conformance.py -m slow`。
