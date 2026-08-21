# events 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `events.schema.json` 是 events 域的语言无关规范。事件流是内核**唯一的对外输出通道**。

## 1. 事件集合（8+ 种，全部 schema 化）

| 事件 | 时机 | 关键字段 |
|---|---|---|
| `step_start` | 一次 LLM 调用开始 | step |
| `text_delta` | 文本增量 | text |
| `reasoning_delta` | 推理增量（可选） | text, signature |
| `step_end` | 一次 LLM 调用结束 | content_blocks / stop_reason / usage |
| `tool_use` | LLM 请求工具 | name, input, original_input?, effective_input? |
| `tool_result` | 工具结果回传 | name, tool_use_id, result, is_error, modified? |
| `done` | 对话结束 | 统一 9 键 |
| `error` | 对话失败（不产生 done） | message, code? |

**done 事件统一 9 键**（所有终止路径一致）：

```
{type: "done", session_id, response, messages, is_complete,
 termination_reason, usage, content_blocks, stop_reason}
```

**事件演进**: `type: "custom:*"` 为自定义事件，core 透传不解析；成熟后经 RFC 升级为正式事件（minor 版本）。
**行为条款 E-1（事件源契约）**: 事件流必以 `done` 或 `error` 收尾；`done`/`error` 是终态事件，必须可靠送达外壳（非 best-effort，不可丢弃）。
**行为条款 E-2（未知类型宽容）**: 消费方对未知 type 的策略 = 透传或记录，绝不崩溃。

## 2. 事件流三层（定案）

| 层 | 内容 | 传输方式 | 消费方 |
|---|---|---|---|
| **L1 热路径** | `text_delta` / `reasoning_delta` 逐字增量 | 宿主实现内部直传，**绝不外发**（协议只定格式、不定传输） | 外壳（同进程零拷贝引用） |
| **L2 结构化摘要** | StepSummary / AfterResponse | 钩子调用（冷路径，快照+action） | 全部扩展（含异语言） |
| **L3 观测通知** | `tool_use` / `tool_result` / `step_end` / 聚合事件 | 异步批处理（transport `event` 消息，可背压） | 观测类扩展（observe capability） |

**行为条款 E-3（L1 不变量）**: L1 绝不进 transport——逐字增量只活在宿主内，任何扩展（含同语言）都不接收 L1。
**行为条款 E-4（L2 唯一语义通道）**: 扩展获取对话内容的唯一语义通道是 L2 结构化摘要；文本呈现/审计用摘要，不用逐字增量。
**行为条款 E-5（L3 异步旁路）**: L3 允许批处理、允许背压丢帧（best-effort），绝不影响主对话流程；transport 的 `event` 消息类型 = L3 观测通知，非热路径透传。
**行为条款 E-6（外壳直通事件）**: `step_start` / `done` / `error` 为外壳直通事件（与 L1 同列：宿主内部直传，不进 transport、不投 L3）——`done`/`error` 必须可靠送达外壳；扩展观测"对话结束"走 after/on_error 钩子（L2）或 step_end（L3）。
**行为条款 E-7（L3 订阅声明）**: 扩展声明 `capabilities: [..., "observe"]` 即订阅 L3 观测通知，无需额外握手；非 observe 扩展不接收 L3。
**行为条款 E-8（step_end 双呈现）**: L2 = `after_step` 钩子携带的 StepSummary；L3 = 原始 `step_end` 事件。扩展二选一订阅，同一扩展不得同时从两条通道收取同一事件的重复副本。

## 3. 观测类扩展只读约束

**行为条款 E-9（只读强制）**: 扩展声明 `capabilities` 含 `"observe"` 时，其钩子返回 action（含 SetExtra/AppendMessage/SetStop）由 core **忽略并记录**（error 级日志，`HOOK_TERMINAL_ACTION_IGNORED`），对话状态不受任何影响。此约束为 core 内核级安全 A4"capabilities 授权面强制"的组成部分。

## 4. 外壳进程边界

外壳（FastAPI/Tauri）与 host 默认同进程——L1 零拷贝引用成立的前提。若未来 Tauri 独立进程，外壳与 host 之间的通道由 host 实现层自定（不在扩展 transport 协议范围内）。

## 5. 版本与演进

- 新增事件 = minor 演进（老扩展不消费则零破坏）；变更既有事件结构 = major 演进。
- 本域 schema 随协议族 `protocol_version` 演进（见 evolution 域）。
