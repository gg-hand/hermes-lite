# hooks 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `hooks.schema.json` 是 hooks 域的语言无关规范。hooks 域是 core 的核心交互模型。

## 1. 交互模型（推翻共享可变 ctx）

```
core ── Invocation{ hook, snapshot, args } ──▶ 扩展
扩展 ── ActionResult{ actions[] } ──────────▶ core 应用
```

**行为条款 H-1（消息传递内核）**: core 与扩展之间只交换**不可变快照（Snapshot）→ Action 消息**，不存在共享可变状态。扩展不得以对象引用方式访问宿主内部。

## 2. Action 全集（6 种）

| Action | 语义 | 应用规则 |
|---|---|---|
| `AppendMessage(m)` | 追加一条消息 | 叠加（按应用序） |
| `SetTools(tools)` | 覆盖工具 schema 列表 | 后应用覆盖先应用 |
| `SetExtra(key, value)` | 写入扩展间共享数据 | 覆盖，key 白名单 `^[a-z0-9_]+\.[a-z0-9_.]+$` |
| `SetStop(reason)` | 拦截整个对话 | 覆盖 |
| `SetSystem(text)` | 覆盖基础 system | 覆盖 |
| `ModifyToolSchema(name, ...)` | 修改单个工具 schema | 覆盖（定向，非整体覆盖） |

**行为条款 H-2（覆盖语义即组合语义）**: append 叠加、set 后覆盖先（应用序）——无需额外优先级语法。

**行为条款 H-3（AppendMessage 角色限制）**: 仅允许追加 `role=user` 消息；追加 assistant/tool 角色冲突 user/assistant 交替约束（行为套件负断言锚定）。扩展确有追加 assistant 需求 → 走 RFC 新语义。

## 3. 钩子全集（11 钩子）

| 钩子 | 时机 | 输入 | 输出 |
|---|---|---|---|
| `setup(config, host)` | 启动一次 | 配置段 + 宿主能力声明 | ok/error（失败=启动失败，逆序回滚） |
| `teardown()` | 关闭 | — | ok（幂等，异常仅告警；**无 action**） |
| `build_injections(snapshot)` | 每次对话组装前 | snapshot | Injection[] |
| `inject_round(snapshot)` | loop 每轮 step 前 | snapshot | Injection \| none（layer 强制 BEFORE_INPUT） |
| `before(snapshot)` | 注入声明收集后、组装收口前 | snapshot | Action[]（全部 6 种；SetStop 短路） |
| `pre_tool_call(snapshot, name, input)` | 工具执行前 | snapshot + 工具 | ToolDecision(allow/reject/modify) |
| `on_tool_call(snapshot, name, input)` | 工具执行 | snapshot + 工具 | result（NotImplemented = 不执行） |
| `post_tool_call(snapshot, name, input, result, duration)` | 工具执行后 | snapshot + 结果 | Action[]（全部 6 种，增量收口校验） |
| `after_step(snapshot, StepSummary)` | 每轮 step 后 | snapshot + 轮摘要 | Action[]（全部 6 种，增量收口校验） |
| `after(snapshot, AfterResponse)` | 对话完成 | snapshot + 完成摘要 | Action[]（**一律忽略+记录**） |
| `on_error(snapshot, error)` | 失败/断连/拦截 | snapshot + 错误 | Action[]（**一律忽略+记录**） |

**行为条款 H-4（每钩子 Action 权限）**: 非终态钩子（before/post_tool_call/after_step）统一允许全部 6 种 Action；终态钩子（after/on_error）返回类型仍为 Action[]（协议输出统一）但 core 一律忽略并记录；`build_injections`/`inject_round` 输出 Injection（非 Action）；`pre_tool_call` 输出 ToolDecision；`on_tool_call` 输出 result；`teardown` 无 action。

**行为条款 H-5（SetStop 短路）**: 任一扩展返回 SetStop → 短路后续扩展的同名钩子调用 → before 场景跳过组装收口与 LLM、直接 done(intercepted)；轮中场景（post_tool_call/after_step）终止后续轮，同样 done(intercepted)。

## 4. 调用语义（协议强制）

- **顺序**: 注册序正序调用；`after` / `on_error` / `teardown` 逆序（洋葱模型）。
- **短路**: 任一扩展返回 SetStop → 短路后续扩展的同名钩子调用（§H-5）。
- **隔离**: 单个扩展异常/超时 → 跳过该扩展 + 记录，不中断对话。
- **合法性双保险**: 应用全部 action 后 + 组装后，校验消息结构（角色白名单/交替），非法 → `error` 事件，不发送 LLM。

**行为条款 H-6（钩子超时）**: 宿主以 `asyncio.wait_for` 包裹每个钩子（默认 5s，可配置 `core.hook_timeout`），超时跳过该扩展并记日志——扩展不得卡死宿主。`on_tool_call` 不加超时（工具执行由工具自身负责超时）。

## 5. Action 应用时序（立即应用 + 原子批次）

**行为条款 H-7（立即应用）**: 每个扩展返回的 action 批次在调用下一个扩展前**立即、原子地**应用到快照。
**行为条款 H-8（原子批次）**: 单个扩展的**全部合法 action** 作为一批原子应用——同一扩展不得观察到自己的半批次状态；下一个扩展看到完整批次推进后的快照。
**行为条款 H-9（两层校验分离）**:
- **schema 级校验**（结构合法性：SetExtra key 白名单 / 消息角色 / action op 合法）: 批次**应用前**逐条校验；非法条 → 拒绝 + error 级日志 + 跳过该扩展批次内剩余 action；合法条仍以批次原子推进。
- **语义级校验**（上下文合法：消息交替 / tool_result 配对）: 组装后统一校验，失败 → `error` 事件，不发送 LLM。
**行为条款 H-10（快照递增）**: 每个扩展收到的是"应用了前序扩展全部 action 的最新快照"（可见性事实）。`revision` 每次 action 批次应用后 +1（见 types 域 T-4）。
**行为条款 H-11（覆盖规则，仅非终态钩子适用）**: set 类 action **后应用者覆盖先应用者**——按调用（应用）序生效。正序钩子调用序 = 注册序 → 后注册覆盖先注册。终态钩子不参与覆盖规则（action 一律忽略）。
**行为条款 H-12（叠加规则）**: append 类 action 全部生效，顺序 = 调用（应用）序。
**行为条款 H-13（merge 协同时序）**: `AppendMessage` 只负责"声明追加"，相邻 user 消息的合并时机固定在组装收口阶段统一执行（保留 `merge_consecutive_user_messages` 语义），扩展无法绕过交替校验。

## 6. 收口四连（一次对话的完整时序）

```
① 注入声明收集（build_injections, 注册序）
② before（注册序, action 立即应用）
③ 组装收口（Assembler: 注入分层叠加 → merge 相邻 user → 语义级校验）
④ 送 LLM
```

**行为条款 H-14（收口四连）**: 上述时序为协议定案。before 的 SetSystem 覆盖基础 system，注入的 STABLE_SYSTEM/SYSTEM 层在收口阶段叠加于其上（注入永远盖过 before 的裸 system）。before 的 AppendMessage(role=user) 插入 user_input 之后，与注入一起经收口 merge 合并。**禁止** AppendMessage 追加 assistant/tool 角色。② 中任一扩展返回 SetStop → 短路后续 before 调用 → 跳过 ③④ → done(intercepted)。

**行为条款 H-15（增量收口不变量）**: **任何进入 LLM 的消息序列，必过 merge 相邻 user + 语义级校验**——协议级真不变量：
- 首轮: 全量收口四连；
- 后续轮: inject_round 全收集（合并规则见 H-16）→ 增量收口（merge + 语义校验）→ 送 LLM；
- 轮中钩子（post_tool_call/after_step）的 AppendMessage 应用后，同样在下次送 LLM 前过增量收口；校验失败 → `HOOK_INVALID_ACTION` → error 事件，不送 LLM。

**行为条款 H-16（inject_round 多声明合并规则）**: 多扩展各返回一条 Injection 时——① 按注册序拼接；② 参与同层 key 去重（后注册覆盖先注册）；③ 受 BEFORE_INPUT 层预算裁剪；④ 与 build_injections 声明的 BEFORE_INPUT 注入叠加顺序 = **build_injections 先、inject_round 后**（对话级注入在前，轮级注入更靠近当前输入）。

**行为条款 H-17（可见性约束）**: 快照始终是"当前真实状态"的只读视图；扩展不得修改快照本身，只能通过返回 action 变更。

## 7. 工具路径四连时序

```
tool_use 检测 → ① pre_tool_call 链（注册序; reject 短路; modify 叠加）
             → ② tool_use 事件（携带 original_input + effective_input[发生修改时]）
             → ③ on_tool_call / invoke_tool 执行（tool_executor 专用通道）
             → ④ tool_result 事件（modified 标记）→ 回喂 → 下一轮前过增量收口
```

**行为条款 H-18（② 先于 ③）**: tool_use 事件要携带 `effective_input`，pre_tool_call 链必须整体前移到 tool_use 事件发出之前——loop 重构的真实时序变更。
**行为条款 H-19（reject 短路完整性）**: ① 的 reject 短路后，仍发 tool_use 事件（携带原始 input）与 tool_result 事件（is_error, tool_rejected）——观测完整性要求事件流不缺环。

**行为条款 H-20（modify 分支语义）**: `pre_tool_call` 返回 `modify(new_input)` = 工具调用管道中的变形器——修改后的 input 作为该工具实际执行输入；多级 modify 按应用序后覆盖先；修改后重过工具 input_schema（schema 级校验），非法 → 视同策略拒绝（`tool_rejected`）；审计可见：tool_use 事件携带 `original_input` 与 `effective_input`，tool_result 带 `modified: true` 标记。首个 reject 短路；allow 不短路。

## 8. 版本与演进

- `pre_tool_call`/`post_tool_call`/`after_step` 属**演进面的钩子集合新增**（core 只定义触发点，扩展声明实现与否，老扩展零感知）。
- 新增钩子/action = minor 演进；变更既有交互语义 = major 演进。
