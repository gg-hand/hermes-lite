# lifecycle 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `lifecycle.schema.json` 是 lifecycle 域的语言无关规范。lifecycle 域覆盖扩展进程级生命周期与扩展声明模型。

## 1. 生命周期协议

**行为条款 L-1（setup 幂等可重入）**: `setup` 幂等可重入（热重载反复调用）；任一失败 → 逆序 teardown 已成功者 → 启动失败（可读错误）。
**行为条款 L-2（teardown 幂等）**: `teardown` 幂等；异常仅告警，逆序继续。
**行为条款 L-3（热重载原子替换）**: build 新链 → 全部 setup 成功 → 原子替换；失败 → 回滚保持旧链。
**行为条款 L-4（扩展进程重建时序，"接近原子"）**: ① spawn 并握手新进程 → ② 新扩展全部 setup 成功 → ③ 才 teardown + 优雅关闭旧进程（含旧扩展后台任务取消：扩展自取消 + 宿主 cancel_all 兜底）→ ④ 原子替换引用。② 失败 → 杀新进程、保留旧进程与旧链（回滚保持可用；回滚路径不取消旧链后台任务）。重建窗口期心跳/监管仍归旧链，替换完成后再移交新链。
**行为条款 L-5（进程归属粒度）**: 进程归属粒度 = **每扩展一进程**（非链级进程池）——重建链只影响被替换扩展的进程，无级联重建。
**行为条款 L-6（崩溃隔离）**: 扩展进程崩溃不影响 core 主对话（隔离语义）；崩溃自动重启属部署/外壳层运维职责（§15-B1，非 core 范围）。
**行为条款 L-7（终态钩子 action 一律忽略）**: `after` / `on_error` 返回的 Action[] 协议强制**忽略并记录**（error 级日志，记 `HOOK_TERMINAL_ACTION_IGNORED`），不区分 observe/策略扩展。终态钩子的数据写入一律经 storage 消息通道（§storage/transport）。
**行为条款 L-8（宿主能力声明与 storage 通道）**: `setup(config, host)` 的 `host` 为**纯数据声明**（宿主→扩展的能力面）：含宿主提供的协议域列表、storage 域声明（通道 = transport `storage_*` 消息）、该扩展的 kind 命名空间前缀（`{extension_name}.`）。扩展**不得**以对象引用方式访问宿主存储——宿主存储的唯一访问通道 = transport storage 消息。术语澄清: `host`（宿主能力声明，宿主→扩展）与 §extension 的 `capabilities`（扩展→宿主）方向相反。
**行为条款 L-9（扩展调 LLM 通道）**: 扩展调用 LLM 的唯一协议通道 = transport `invoke_llm` 消息——payload `{role, messages, system?, max_tokens?, ...}`；走宿主 LLMAdapter 直调（多角色路由），**协议级防重入**（绝不进入 pipeline/钩子链）；调用前提 = 扩展声明 `capabilities: [..., "llm"]`；失败 → 响应 `{error: {code, message}}`，core 只透传、扩展自降级。
**行为条款 L-10（会话态协议化）**: 快照 extra 会话内延续——构建时从 SessionStore 恢复同 session_id 的 extra 命名空间作为快照 extra 基座；对话结束（done/error 路径）时快照最终 extra 写回 SessionStore。扩展经 SetExtra 写入的数据跨同 session 的多次对话延续。
**行为条款 L-11（会话并发互斥）**: 同 session_id 的并发对话由外壳串行化（会话级互斥是外壳责任，非 core 能力）——core 快照单对话无共享；core 不检测、不防御并发，若外壳未串行化，extra 恢复/写回表现为 last-writer-wins 或丢失更新（语义未定义）。

## 2. 扩展声明模型

```
Extension = { name, lang, transport, protocol_version, hooks_implemented[], capabilities[] }
name 匹配 ^[a-z0-9_]+$（禁点, §types 前缀隔离的可判定前提）
```

- **声明式钩子**: 扩展声明实现哪些钩子，core 只调用已声明的（减少无效交互）。
- **能力声明（集合）**: `capabilities` 为集合（可组合，自由声明），v1.0 枚举：
  - `observe`: 观测只读——钩子返回的 action 被 core 忽略并记录；
  - `tool_executor`: 工具执行者——启用 transport `invoke_tool` 专用消息；
  - `llm`: 可调用 `invoke_llm` 消息；
  - `self_hosted_storage`: 自持存储——扩展自管文件/向量库/外部存储（沙箱归扩展+部署层）；
  - 组合示例: `observe + tool_executor` = 只读执行者；`observe + llm` = 观测型 LLM 消费者；
  - 缺省（空集）= 策略/工具类，可正常返回 action。
- **能力是授权面而非行为面**: core 只强制声明面（未声明 llm 却调 invoke_llm → 拒绝等，§15-A4）；细粒度 action 权限 / invoke_llm 配额 / 自持沙箱均为非 core 范围（§15-B）。
- **版本协商**: core 与扩展互验 `protocol_version`，降级兼容或拒绝（可读错误）。

## 3. 会话级生命周期边界

会话/对话级生命周期由外壳管理，不在扩展生命周期协议范围内（lifecycle 域只覆盖扩展进程级 setup/teardown/热重载）。

## 4. 版本与演进

- 新增 capability = minor 演进；删除/改名 capability = major 演进。
- Extension 声明结构变更走协议版本化（见 evolution 域）。
