# errors 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `errors.schema.json` 是 errors 域的语言无关规范。
> **范围标注**: 错误码体系、`tool_rejected` 终止原因、`pre_tool_call` 触发点均为 v1.0 新增设计，非既有资产。

## 1. 终止原因枚举（7 种）

`normal / max_loops / user_cancel / no_tool_executor / llm_error / intercepted / tool_rejected`

`tool_rejected` 为 v1.0 新增（现有代码仅 6 个 `TERMINATION_*` 常量、无 `TERMINATION_TOOL_REJECTED`）。

## 2. 错误码体系

前缀：`LLM_* / LOOP_* / HOOK_* / TOOL_* / STORAGE_* / CONFIG_*`。每个错误码有捕获层、上报方式、兜底行为（错误责任矩阵协议化）。

**v1.0 错误码全集**（落地时行为套件逐条锚定）：

| 错误码 | 捕获层 | 上报 | 兜底 |
|---|---|---|---|
| LLM_TIMEOUT / LLM_CANCELED / LLM_STREAM_FAILED / LLM_API_ERROR（主对话） | step 层 | error 事件 | pipeline finally 落盘 |
| LLM_TIMEOUT / LLM_API_ERROR（invoke_llm） | **扩展自身**（经 transport 透传） | `{error}` 响应 | 扩展自降级（记忆巩固失败仅告警等） |
| LOOP_MAX_REACHED | loop 层 | logger.warning | done(max_loops) |
| HOOK_TIMEOUT / HOOK_EXCEPTION | HookChain | logger.error | 跳过该扩展 |
| HOOK_INVALID_ACTION | HookChain 应用层 | logger.error + 跳过该条 | 批次内剩余 action 跳过，对话继续 |
| HOOK_TERMINAL_ACTION_IGNORED | HookChain 终态 | logger.error | 忽略（终态钩子 action） |
| TOOL_NO_EXECUTOR | dispatch 层 | logger.warning | done(no_tool_executor) |
| TOOL_EXEC_FAILED | dispatch 层 | tool_result is_error 回喂 | loop 回喂继续 |
| TOOL_REJECTED_BY_POLICY | pre_tool_call | tool_result is_error 回喂 | loop 回喂继续 |
| TOOL_MODIFY_INVALID | pre_tool_call | tool_result is_error 回喂 | loop 回喂继续 |
| STORAGE_WRITE_FAILED | persist/storage 层 | logger.error | 对话继续（旁路） |
| STORAGE_READ_FAILED | persist/storage 层 | logger.error | 扩展 setup 失败或降级 |
| CONFIG_UNKNOWN_KEY / CONFIG_INVALID_VALUE / CONFIG_MISSING_KEY | 配置加载 | 抛错（启动失败） | 可读错误 |

**行为条款 R-1（禁止裸 except 静默吞错）**: 责任矩阵协议强制，捕获点必须带日志。

## 3. 错误码 ↔ 终止原因映射

二者非一一对应——终止原因描述"对话如何结束"，错误码描述"具体失败点"：

| 终止原因 | 关联错误码（v1.0） | 事件呈现 |
|---|---|---|
| `normal` | —（无错误） | done(is_complete=true) |
| `max_loops` | LOOP_MAX_REACHED（记录级） | done(is_complete=false) |
| `user_cancel` | —（用户动作，非错误；流中断时补 LLM_CANCELED） | done(is_complete=false) |
| `no_tool_executor` | TOOL_NO_EXECUTOR | done(is_complete=false) |
| `llm_error` | LLM_TIMEOUT / LLM_CANCELED / LLM_STREAM_FAILED / LLM_API_ERROR | error 事件（不产生 done） |
| `intercepted` | —（枝干策略，非错误） | done(is_complete=false) |
| `tool_rejected` | TOOL_REJECTED_BY_POLICY / TOOL_MODIFY_INVALID | tool_result is_error 回喂（可能继续至 done） |

## 4. 工具执行语义边界（消除歧义）

| 情形 | 判定 | 终止原因 | 行为 |
|---|---|---|---|
| `on_tool_call` 返回 NotImplemented | 该扩展不执行此工具 | — | dispatch 继续尝试后续扩展 |
| 全部扩展均 NotImplemented | 能力缺失（无执行者） | `no_tool_executor` | 友好终止，保留已产出文本 |
| `pre_tool_call` 返回 reject | 有执行者但被策略拒绝 | `tool_rejected` | 回喂 LLM（tool_result is_error），让其调整 |

> 区别: `no_tool_executor` = "没有执行者"（能力缺失，终止）；`tool_rejected` = "有执行者但被拦"（意图阻断，可回喂再试）。

## 5. 错误码可观测面（2026-09-10 定则）

每个错误码必须至少落在一个可观测面上，**禁止"只在注释/常量里存在"**（此前 18 码中仅 2 码有发射点，其余为文档态）：

| 面 | 位置 | 覆盖码 |
|---|---|---|
| ① 事件面 | `error` 事件的 `code` 字段（`events` 域已允许 `code?`） | LLM_TIMEOUT / LLM_CANCELED / LLM_STREAM_FAILED / LLM_API_ERROR / HOOK_INVALID_ACTION |
| ② 日志面 | 日志文本 `CODE: message` 前缀 | LOOP_MAX_REACHED / HOOK_TIMEOUT / HOOK_EXCEPTION / HOOK_TERMINAL_ACTION_IGNORED / TOOL_NO_EXECUTOR / STORAGE_WRITE_FAILED / STORAGE_READ_FAILED |
| ③ 响应面 | transport `{error:{code,message}}`（小写子命名空间，见 §5.1） | 见 §5.1（共 10 码） |
| ④ 异常/启动失败面 | 抛错的 `ValueError` 消息 `CODE: message` 前缀（启动失败 = 可读错误） | CONFIG_UNKNOWN_KEY / CONFIG_INVALID_VALUE / CONFIG_MISSING_KEY |
| ⑤ **待落地**（暂以 `tool_result is_error` 回喂承载，尚无日志/事件发射点） | — | TOOL_EXEC_FAILED / TOOL_REJECTED_BY_POLICY / TOOL_MODIFY_INVALID |

**行为条款 R-2（可观测性）**: 新增错误码必须同时声明其可观测面并落地发射点。**当前锚定状态（2026-09-10）**：
- 事件面（①）由 `tests_core/test_error_codes_events.py` 锚定（LLM_API_ERROR 事件 + 错误事件 `code ∈ ERROR_CODES`）；
- 日志面（②）由行为套件用例 `error-codes-20`（logging 捕获 4 码）+ `hook-isolation-19`（HOOK_EXCEPTION）锚定；
- 异常面（④）由用例 `config-domain-21` 锚定（码出现在抛错消息中）；
- 响应面（③）由用例 06/11/12/17 以精确布尔断言锚定其**拒绝结果**（不校验码字符串本身）；
- ⑤ 三码尚未落地，已登记于 PENDING P-8 入协议条件 ④。

### 5.1 transport 消息级错误码（③ 响应面子命名空间）

`core/transport.py` 的 `ERR_*` 常量是 errors 域在**消息响应**面的子命名空间（小写 snake，语义与上表一致，非独立体系）：

`invalid_frame` / `unknown_message_type` / `invalid_payload` / `kind_prefix_violation` / `capability_not_declared` / `unavailable` / `storage_failed` / `llm_call_failed` / `task_rejected` / `internal_error`

> 实现常量：`core/transport.py` 的 `ERR_*`（与 `errors.schema.json` 的 `TransportErrorCode` 枚举逐字一致）。行为套件用例 `storage-prefix-transport-11` / `invoke-llm-12` / `host-port-inprocess-17` 以**精确布尔断言**（`true`，非 `{type: boolean}`）锚定 `kind_prefix_violation` / `invalid_payload` / `capability_not_declared` 的**拒绝结果**；码字符串本身未逐字断言（待 P-8 收口时评估是否增强）。

## 6. 版本与演进

- 新增错误码 = minor 演进（老实现忽略未知码）；删除/改名错误码 = major 演进。
- 责任矩阵随错误码版本化同步演进。
