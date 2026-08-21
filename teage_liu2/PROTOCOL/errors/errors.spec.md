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

## 5. 版本与演进

- 新增错误码 = minor 演进（老实现忽略未知码）；删除/改名错误码 = major 演进。
- 责任矩阵随错误码版本化同步演进。
