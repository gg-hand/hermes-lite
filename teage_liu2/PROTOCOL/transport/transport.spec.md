# transport 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `transport.schema.json` 是 transport 域的语言无关规范。

## 1. 通道（传输绑定双形态）

- **stdio 绑定**（子进程，LSP 模式）: **跨进程（异语言）扩展的唯一通道**；
- **进程内绑定**（同语言）: `Branch` 协议绑定 = 消息语义的**零成本实现**（直接函数调用，序列化豁免，行为套件同一用例覆盖）——"消息语义"指扩展侧仍只能经快照+action / storage_* / invoke_llm / task_* 消息与宿主交互，**不得绕过协议直接抓宿主对象引用**（零拷贝引用仅限快照/值对象）；
- core 与协议只负责**本地这一端的传输协议**，不承载任何远程网络通信。

## 2. 远程调用（A2A）

扩展有远程协作需求时自行实现远程协议（连接/认证/编解码），core 对"扩展内部如何与远端通信"零感知、零约束。

## 3. 帧格式

- **可插拔编码层**: JSON 起步（v1.0 唯一基线）→ MessagePack（v1.x minor 选项）/ protobuf 砍掉（记取舍）；
- 帧: `{type, payload, encoding: full|delta, protocol_version}`；
- **delta 格式 = 极简增量**（与 action 语义对齐，非 RFC 6902）: `{base_revision, ops: [append_messages[] | overwrite{tools|system|extra_kv|stop}]}`——消息序列在钩子间近似 append-only，天然高效；
- **delta fallback**: 接收方 base_revision 不匹配（进程重启/断链重连）→ 请求 `full` 重发；delta 仅优化传输、**不承载正确性**。

## 4. 消息类型（v1.0 全集）

`invoke_hook / invoke_tool / invoke_llm / storage_write / storage_read / storage_query / storage_delete / task_register / task_cancel / event / heartbeat / shutdown`

**行为条款 T-1（event = L3 观测通知）**: `event` 消息类型 = 事件流 L3 观测通知（异步批处理，非 L1 热路径透传）。
**行为条款 T-2（storage_* = 宿主存储唯一通道）**: payload 含 kind + 数据，kind 必须带 `{extension_name}.` 前缀且 extension_name 匹配 `^[a-z0-9_]+$`，非法前缀拒绝（schema 级校验）。
**行为条款 T-3（invoke_llm = 扩展调 LLM 唯一通道）**: payload 含 role/messages；走 LLMAdapter 直调，协议级防重入（不进钩子链）。
**行为条款 T-4（task_* = 后台任务通道）**: payload = `{task_id, description}`（task_cancel = `{task_id}`）；后台任务归属扩展进程——宿主只登记（可观测/取消协调），不承载执行；扩展 teardown 时自取消其任务，宿主 shutdown 与热重载重建时 cancel_all 兜底。
**行为条款 T-5（invoke_tool vs invoke_hook 分工）**: `invoke_hook` 为通用钩子调用（参数 = Invocation{hook, snapshot, args}，返回 ActionResult）；`invoke_tool` 为工具执行专用消息（payload 仅 name+input，**免快照序列化**），语义 = 扩展声明 `capabilities: [..., "tool_executor"]` 时 core 派发工具的首选通道；未声明 tool_executor 的扩展，工具执行退化为 invoke_hook(on_tool_call)；两种路径结果等价，仅传输开销不同。
**错误响应面（2026-09-10 登记）**: 宿主对入站消息的失败响应形如 `{error: {code, message}}`，其 `code` 取自 **errors 域的小写子命名空间**（`invalid_frame` / `unknown_message_type` / `invalid_payload` / `kind_prefix_violation` / `capability_not_declared` / `unavailable` / `storage_failed` / `llm_call_failed` / `task_rejected` / `internal_error`），完整定义与枚举见 `errors.spec.md` §5.1 与 `errors.schema.json#/definitions/TransportErrorCode`。

**行为条款 T-6（多扩展派发聚合）**: 工具派发按**注册序**遍历全部扩展（含 tool_executor 者走 `invoke_tool`，未含者走 `invoke_hook(on_tool_call)`），单个扩展不执行该工具（语义等价 NotImplemented）则继续下一扩展，全部均不执行 → `no_tool_executor` 友好终止——两种通道的"不执行"信号统一计入 no_tool_executor 判定。

## 5. 异语言扩展接入

异语言扩展经 `RemoteBranchAdapter` 接入，对 core 是普通扩展，core 不感知对方语言。

## 6. 版本与演进

- 新增消息类型 = minor 演进；变更既有消息结构 = major 演进。
