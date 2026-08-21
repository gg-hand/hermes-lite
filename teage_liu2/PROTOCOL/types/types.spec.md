# types 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `types.schema.json` 是 types 域的语言无关规范。宿主实现必须满足本规范的行为条款。

## 1. 范围

定义 core 与扩展之间一切交互值对象：`SessionId` / `Message` / `ContentBlock` / `ToolSchema` / `Usage` / `Injection` / `Snapshot` / 命名约束 / 资源上限阈值。

## 2. 值对象定义

| 值对象 | 结构 | 说明 |
|---|---|---|
| `SessionId` | `string` | 会话唯一标识 |
| `Message` | `{role: user\|assistant\|system\|tool, content: string \| ContentBlock[]}` | 一条对话消息 |
| `ContentBlock` | `{type: text\|tool_use\|tool_result\|thinking, ...payload}` | Anthropic 风格内容块 |
| `ToolSchema` | `{name, description, input_schema}` | 工具声明（`input_schema` 为 JSON Schema） |
| `Usage` | `{input_tokens, output_tokens, cache_read_input_tokens?, cache_creation_input_tokens?, ...}` | token 用量 |
| `Injection` | `{layer, content, priority=0, key?}` | 注入项（layer 见 §3） |
| `Snapshot` | 见 §4 | 不可变对话状态视图 |

schema 权威定义见 `types.schema.json`。

## 3. 注入层

五层（稳定度从高到低）：

| 层 | 枚举值 | 位置 | 典型内容 |
|---|---|---|---|
| L0 | `STABLE_SYSTEM` | system 稳定区 | 画像主体 / 全局规则 |
| L1 | `SYSTEM` | system 末位 | 会话级指令 |
| L2 | `PREFIX` | messages[0] 前置 | 检索记忆 / 环境信息 |
| L3 | `MID` | 历史中间 | 对话背景 |
| L4 | `BEFORE_INPUT` | 当前输入前 | 意图引导 / 瞬时指令 |

**行为条款 T-1（注入去重与裁剪）**: 同 `key` 后声明覆盖先声明；层内 `priority` 降序 + 注册序稳定，超预算整段丢弃不截半。
**行为条款 T-2（注入永不落盘）**: 注入内容只进 effective_messages，永不写入历史库。

## 4. ContextSnapshot

```
session_id / user_input / round / started_at
history / system_text / messages / tools / extra / stop / revision
```

**行为条款 T-3（不可变只读）**: 快照对扩展呈现为不可变只读视图；扩展不得修改快照本身，只能通过返回 Action 变更。
**行为条款 T-4（快照递增）**: `revision` 为快照版本号，由 host 独占递增——每次 action 批次应用后 +1；扩展只读，扩展提交的 revision 非法值一律拒绝（§15-A1 协议边界校验）。round（第几轮 LLM 调用）与 revision（快照第几个版本）不同源。
**行为条款 T-5（结构共享）**: 宿主实现禁止对快照做 `deepcopy`；采用 O(n) 浅拷贝 + 尾部追加 + 共享元素引用（v1.0 落地形态），传输层 delta 是传输内部重组，协议对扩展永远呈现完整快照。

## 5. 命名约束（协议强制，防注入）

| 标识 | 正则 | 说明 |
|---|---|---|
| `extension_name` | `^[a-z0-9_]+$` | 禁点，消除 `{name}.` 前缀解析歧义 |
| `kind` | `^[a-z0-9_.]+$` | 允许点 |
| `SetExtra` key | `^[a-z0-9_]+\.[a-z0-9_.]+$` | 分支段 + 自由子键段 |

**行为条款 T-6（命名校验）**: 上述三个白名单在 schema 级强制，非法值一律拒绝 + error 级日志，绝不静默容忍（安全边界，§15-A2）。

## 6. 持久文档 schema 化例外

**行为条款 T-7（doc 为扩展私有数据）**: 扩展经 StorageProvider 自管的持久文档（doc）内部结构由**写入方自治**——内核只透传、不解析、不校验、不消费。扩展间若需共享 doc 结构，须经 RFC 新增协议级 kind schema，不得隐式依赖对方内部格式。

## 7. 资源上限阈值（§15-A6，v1.0.0 定案）

宿主实现**必须**实施以下上限，不可无界（防 DoS）。阈值具体数值为协议常量，进 `types.schema.json` 的 `ResourceLimits`：

| 上限 | 数值 | 语义 |
|---|---|---|
| `max_snapshot_bytes` | 2 MiB | 快照总体积上限（防超大注入/append 撑爆内存与上下文） |
| `max_message_bytes` | 512 KiB | 单条消息 content 体积上限 |
| `max_messages_per_conversation` | 2000 条 | 单次对话消息总条数上限（防无限 append） |

**注意**: 消息条数上限为独立配置项，与 `history_window_messages`（历史读取窗口）、`max_loops`（循环轮数）语义不同，不得复用现有项顶替。
**行为条款 T-8（超限处理）**: 任一上限被突破 → 对应通道拒绝（`HOOK_INVALID_ACTION` / `STORAGE_*` 错误码），error 级日志，对话按错误语义继续或终止（见 errors 域责任矩阵）。

## 8. 版本与演进

- 本域 schema 版本随协议族整体 `protocol_version` 演进（semver，见 evolution 域）。
- 值对象新增字段 = minor 演进；删除/改名 = major 演进（双版本共存过渡期）。
