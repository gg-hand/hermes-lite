# cron_tool 动态工具系统

cron_tool 是 Phase 8 Task 5 引入的「Layer 2 能力扩展层」，让 Agent 在不重启
服务、不污染用户会话 tools schema（缓存命中区）的前提下，通过子进程方式执行
LLM 生成的自定义工具。

## 三层能力扩展架构

teage-liu 的能力扩展分为三层，按「稳定性」与「激活成本」递增：

| 层级 | 名称         | 稳定性 | 激活成本               | 缓存影响 | 典型场景                       |
| ---- | ------------ | ------ | ---------------------- | -------- | ------------------------------ |
| L1   | 内置模板     | 最高   | 改代码 + 重启          | 无       | directory_watch / summary 等   |
| L2   | cron_tool    | 中     | LLM 生成 → 用户审查 → 激活 | 仅 cron 会话可见，用户会话字节级稳定 | send_email / fetch_stock 等 |
| L3   | MCP (Phase 9) | 低    | 进程间 RPC + 协议适配   | 视配置而定 | 外部服务集成                   |

### Layer 1: 内置模板（最稳定）

固化在 `src/tasks/workflow/` 下的 Python 模板（directory_watch / summary /
email_notify / cleanup_suggest / research / custom）。变更需改代码 + 重启服务，
但 system prompt 字节级稳定，缓存命中率最高。适用于通用、高频、稳定的工作流。

### Layer 2: cron_tool（本 Task 5 实装）

LLM 通过 `write_cron_tool` 工具生成新的 TOOL.md + run.* 脚本，写入
`cron_tool/.pending/{name}/` 等待用户审查。用户在前端审查卡片上点击「激活」后，
工具从 `.pending/` 移到 `cron_tool/{name}/`，注册到**独立的**
`CronToolRegistry`（不进全局 ToolRegistry）。

- **子进程执行**：通过 `subprocess.run` 调用 `cron_tool/{name}/run.*`，语言无关
  （Python / Shell / Node.js 均可，按文件扩展名识别解释器），崩溃隔离
- **缓存约束**：cron_tool 仅在 cron 调度会话内可见，用户会话的 tools schema
  字节级不变（缓存硬约束 1）
- **HIL 审查**：所有 LLM 生成的工具必须经用户激活后才注册，防止恶意代码

### Layer 3: MCP（Phase 9，规划中）

通过 Model Context Protocol 接入外部服务，支持进程间 RPC 与标准化协议适配。
稳定性最低（依赖外部服务可用性），但扩展性最强。详见 Phase 9 设计文档。

## 目录结构

```
cron_tool/
├── README.md                  ← 本文档
├── .pending/                  ← LLM 生成工具的暂存区（待审查）
│   └── {tool_name}/
│       ├── TOOL.md
│       └── run.py
└── {tool_name}/               ← 已激活的工具
    ├── TOOL.md
    └── run.py
```

## TOOL.md frontmatter schema

参考 `skills/calculator/SKILL.md` 的 frontmatter 格式，简化为以下字段：

```yaml
---
name: send_email           # 工具名（唯一标识，与目录名一致）
version: 1.0.0             # 语义化版本
description: 发送邮件通知   # 工具描述（展示给 LLM）
author: llm-generated      # 作者（llm-generated / 用户名）
timeout: 30                # 子进程超时秒数（默认 30）
input_schema:              # Anthropic tool use 格式的输入 schema
  type: object
  properties:
    to:
      type: string
      description: 收件人邮箱
  required: [to]
---

# 工具说明（markdown 正文，可选）
```

## run.* 接口约定

工具的执行入口为 `run.*` 脚本，按文件扩展名识别解释器：

- `run.py` → `python run.py`
- `run.sh` → `bash run.sh`
- `run.js` → `node run.js`

### 输入输出协议

- **输入**（stdin）：JSON 字符串，结构为
  `{"input": {...工具入参...}, "context": {"session_id": "...", "schedule_id": "...", "current_time": "..."}}`
- **输出**（stdout）：JSON 字符串，结构为 `{"result": "..."}`
  （成功）或 `{"error": "...", "error_type": "..."}` （失败）
- **超时**：默认 30s，可由 TOOL.md 的 `timeout` 字段覆盖
- **崩溃隔离**：子进程异常退出不影响主进程，主进程捕获后返回结构化错误 JSON

## 完整生命周期

```
┌─────────────┐   write_cron_tool    ┌──────────────┐  用户点击「激活」  ┌─────────────┐
│  用户对话   │ ───────────────────> │ .pending/    │ ────────────────> │ cron_tool/  │
│ (LLM 生成)  │  写入 TOOL.md+run.*  │ {name}/      │  POST /activate   │ {name}/     │
└─────────────┘                      └──────────────┘                   └──────┬──────┘
                                          │                                     │
                                          │ 用户拒绝                              │ register
                                          │ POST /reject                         ▼
                                          ▼                              ┌─────────────────┐
                                    删除 .pending/{name}/                │ CronToolRegistry│
                                                                        │ (独立，不进全局)  │
                                                                        └────────┬────────┘
                                                                                 │
                                            调度项引用 tool_name + active_tools_snapshot 锁定
                                                                                 │
                                                                                 ▼
┌──────────────┐  触发执行        ┌──────────────────────┐  子进程执行   ┌──────────────┐
│ CronScheduler│ ───────────────> │ custom 模板 /        │ ───────────> │ run.* 脚本   │
│ (cron 会话)  │  _build_cron_tools│ ReactLoop 直接调用   │  stdin JSON  │ (Python/Shell│
└──────────────┘                  │ tools_override 过滤   │ <─────────── │ /Node.js)    │
                                  └──────────────────────┘  stdout JSON └──────────────┘
```

## 管理 API 与端点

### 关键模块

| 模块 | 职责 |
| ---- | ---- |
| `src/tasks/cron_tool_loader.py` | TOOL.md 解析 + 子进程执行（`load_tool` / `execute_tool` / `list_tools` / `list_pending_tools`） |
| `src/agent/cron_tool_registry.py` | 独立注册中心 `CronToolRegistry`（`register` / `unregister` / `reload` / `load_all` / `get_tools_schema` / `execute_tool`） |
| `src/agent/cron_tool_writer.py` | `write_cron_tool` 工具注册（LLM 调用，写入 `.pending/`） |
| `src/tasks/workflow/custom.py` | `custom` 工作流模板，引用 cron_tool 并调用 `execute_tool` 生成报告 |

### HTTP 端点（SubTask 5.5 / 5.6）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| `GET` | `/cron_tools/pending` | 列出待审查工具（`.pending/` 下） |
| `POST` | `/cron_tools/{name}/activate` | 激活：`.pending/` → `cron_tool/{name}/`，注册到 registry |
| `POST` | `/cron_tools/{name}/reject` | 拒绝：删除 `.pending/{name}/` |
| `GET` | `/cron_tools` | 列出所有已激活工具 |
| `DELETE` | `/cron_tools/{name}` | 删除已激活工具：注销 + 删除目录 |
| `PUT` | `/cron_tools/{name}` | 重新加载（编辑 TOOL.md / run.* 后热更新） |

### LLM 工具

| 工具名 | 注册位置 | 说明 |
| ---- | ---- | ---- |
| `write_cron_tool` | 全局 `ToolRegistry`（用户会话可用） | LLM 生成 cron_tool，写入 `.pending/`，返回 `pending_review: true` 触发前端审查卡片 |

## 缓存约束（与 Phase 8 Task 5.7 对齐）

cron_tool 系统严格遵守以下缓存硬约束：

1. **用户会话 tools schema 字节级稳定**：cron_tool schema **绝不**注入全局 `ToolRegistry`，仅通过独立的 `CronToolRegistry` 管理。用户会话的 `_build_enhanced_context` 返回 `tools_override=None`，由 `ReactLoop` 从全局 registry 取 schema。
2. **请求级过滤**：cron 调度会话触发时，`Orchestrator._build_cron_tools(session_id)` 从 `active_tools_snapshot`（调度项创建时锁定）读取工具名列表，对全局 registry schema 做过滤，再合并 `CronToolRegistry` 的 schema，作为 `tools_override` 传入 `ReactLoop.run()`。
3. **派发隔离**：`ReactLoop._execute_tool_with_dispatch` 优先查 `cron_tool_registry`，未命中再回退全局 `tool_registry`。cron_tool 名与全局工具名解耦，互不干扰。
4. **不修改全局 registry**：`CronToolRegistry` 是独立对象，`register` / `unregister` / `reload` 仅影响 cron 会话可见的工具集，全局 `ToolRegistry` 字节级不变。
5. **system_text 稳定**：`custom` 模板的 `build_system_prompt` 返回固定 prompt（禁含动态变量），cron_tool 执行结果属缓存失效区（注入 `messages[0]`）。

## 示例

见 `cron_tool/echo_text/`（原样回显输入文本的最小示例）。
