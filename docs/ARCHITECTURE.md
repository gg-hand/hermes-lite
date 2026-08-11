# Teage Liu 架构总览（Architecture）

> 一份"一眼看懂设计与细节"的项目蓝图。定位：**自托管的个人 AI Agent 服务** —— 长期对话、记忆沉淀、自主任务执行（Cron + Workflow）、多智能体协作（Multi-Agent）。
>
> 补充阅读：[README.md](../README.md)（功能清单）、[DEPLOY.md](../DEPLOY.md)（部署）、[docs/sdk/](sdk/)（外部 Agent SDK）、[docs/plans/](plans/)（演进计划）。

---

## 目录

1. [一句话架构](#1-一句话架构)
2. [技术栈](#2-技术栈)
3. [进程与启动链路](#3-进程与启动链路)
4. [目录结构](#4-目录结构)
5. [一次对话的完整链路](#5-一次对话的完整链路)
6. [智能体核心（agent/）](#6-智能体核心)
7. [工具系统](#7-工具系统)
8. [安全体系](#8-安全体系)
9. [记忆系统（memory/ + storage/）](#9-记忆系统)
10. [配置系统（config/DI/热更新）](#10-配置系统)
11. [Cron 调度（tasks/scheduler）](#11-cron-调度)
12. [Workflow 引擎（tasks/workflow）](#12-workflow-引擎)
13. [多智能体协作（multiagent/）](#13-多智能体协作)
14. [外部 Agent SDK（sdk/）](#14-外部-agent-sdk)
15. [前端（web/）](#15-前端)
16. [API 路由总表](#16-api-路由总表)
17. [数据落盘布局（data/）](#17-数据落盘布局)
18. [监控与可观测性](#18-监控与可观测性)
19. [部署形态](#19-部署形态)
20. [测试体系](#20-测试体系)
21. [演进脉络](#21-演进脉络)

---

## 1. 一句话架构

```
                 ┌────────────────────────────────────────────────────┐
  浏览器 / 桌面壳  │                  FastAPI (uvicorn)                  │
  (web/*)        │                                                    │
  ┌──────┐  SSE  │  ┌──────────────┐    ┌──────────────────────────┐  │
  │ /chat │─────▶│  │ routes/ 路由层 │──▶ │    Orchestrator 主编排器  │  │
  │/monitor│     │  │ (REST + SSE)  │    │   ┌──────────────────┐   │  │
  │/scheduler │   │  └──────┬───────┘    │   │ ChatHandler      │   │  │
  │/workflow │    │         │  DI 容器    │   └────────┬─────────┘   │  │
  │/workbench │    │  ┌──────▼───────┐    │            ▼             │  │
  └──────┘     │  │  │   container   │◀──▶│   ReactLoop (ReAct)      │  │
               │  │  │  (组件注册/热更)│    │   ┌─────┬────┬─────┐    │  │
               │  │  └──────┬───────┘    │   │工具│审批│护栏│审计 │    │  │
               │  │         │            │   └─────┴────┴─────┘    │  │
               │  │  ┌──────▼───────┐    │            │            │  │
               │  │  │ lifespan 装配 │    │   ┌────────▼────────┐   │  │
               │  │  │ 后台循环/预热  │    │   │ 记忆/存储子系统   │   │  │
               │  │  └──────┬───────┘    │   │ memory/ storage/ │   │  │
               │  │         │            │   └──────────────────┘   │  │
               │  └─────────┼────────────┴──────────────────────────┘  │
               │            │ 条件启用                                    │
               │  ┌─────────▼───────────────┐    ┌──────────────────┐  │
               │  │ multiagent/ 多智能体协作  │◀──▶│ sdk/ 外部 Agent  │  │
               │  │ Director/Worker/黑板/A2A │    │ SDK             │  │
               │  └─────────────────────────┘    └──────────────────┘  │
               └────────────────────────────────────────────────────┘
```

**核心设计思想**：

- **单进程、多子系统**：FastAPI 单进程内通过轻量 DI 容器装配全部子系统，前后端同源部署，零外部基础设施（除 LLM API）。
- **分层防御**：PolicyEngine（工具能否调用）→ HIL 审批（人肉确认）→ Guardrails（输入/输出/工具结果三层 PII 与注入防护）。
- **记忆即一等公民**：短期缓冲 → 向量长期记忆 → Markdown 用户画像 三层 + 信号池阈值沉淀 + 衰减排序 + 前缀缓存优化。
- **文件优先（File-First）协作**：多 Agent 协作基于文件系统黑板 + watchdog 事件，跨设备再叠加 A2A HTTP/JSON-RPC，本地零外部依赖。
- **降级优先**：几乎每个可选组件（MCP/Skill/向量库/OCR）缺失时都 try/except 降级运行，保证主对话不中断。

---

## 2. 技术栈

| 类别 | 技术 | 说明 |
|------|------|------|
| 语言 | Python 3.10+（生产 3.11） | 全链路 `async/await`，禁止阻塞事件循环 |
| Web 框架 | FastAPI + Uvicorn | `teage_liu.app:app`；生产 uvloop + httptools |
| LLM 客户端 | `AsyncAnthropic` + `AsyncOpenAI` | 多 Provider：`anthropic / openai / deepseek / qwen`（DeepSeek/Qwen 走 OpenAI 兼容后端）；统一返回 Anthropic 风格 `LLMResponse`，工具 schema 内部互转 |
| 向量存储 | ChromaDB + ONNX `all-MiniLM-L6-v2` | 本地持久化，无需外部向量服务；namespace 区分 `user/cron/file` |
| 结构化存储 | SQLite + FTS5 | `sessions.db`（sessions/messages/messages_fts 三表），会话消息权威存储 |
| 流式 | Server-Sent Events (SSE) | Chat SSE + Collab SSE + Multiagent SSE 三条通道 |
| OCR | PaddleOCR（主）→ Tesseract（兜底）→ 视觉 LLM（预留） | 分层降级 |
| 前端 | 纯 HTML/CSS/JS，无框架 | Design-Token CSS 变量 + 双主题 + 4 强调色 |
| 协作依赖 | watchdog / aiofiles / portalocker / cryptography | 文件监听、跨进程文件锁、ed25519 签名 |
| 桌面壳 | Tauri (Rust) + pyembed Python | Sidecar 模式：Tauri 壳内嵌 Python 运行时子进程 + WebView 前端 |

**版本节奏**：`docs/plans/` 有从 2026-07-11 架构重构 → 多 Agent 协作 → N-Worker 扩展的完整演进记录；当前处于协作系统成熟期（协作隔离、幂等、健康监控为主）。

---

## 3. 进程与启动链路

### 3.1 入口

| 入口 | 命令 | 说明 |
|------|------|------|
| 主入口 | `python -m teage_liu` | [__main__.py](../teage_liu/__main__.py)，预创建 socket（SO_REUSEADDR 防 Windows TIME_WAIT），`app.state.uvicorn_server` |
| ASGI 入口 | `uvicorn teage_liu.app:app` | [app.py](../teage_liu/app.py) |
| 兼容入口 | `python teage_liu/server.py` | 仅保留给旧脚本 |
| 配置覆盖 | 环境变量 `TEAGE_CONFIG` / `TEAGE_SERVER_LOG` / `TEAGE_CRON_TOOL_DIR` | |

### 3.2 lifespan 启动序列（[lifespan.py](../teage_liu/lifespan.py)）

```
1. load_config + 记录 _config_path
2. init_container + register_components        # DI 容器注册全部组件工厂
3. [multiagent.enabled] 注册黑板/注册表/锁/审计/看门狗/恢复/WorkerAdapter|DirectorEngine
4. [a2a.enabled] 挂载 A2A Gateway 路由；RemoteAgentAdapter 启动（worker 角色 + 有远端端点时）
5. [multiagent.enabled] 挂载 /api/multiagent/* 与 /api/multiagent/collab/* 路由
6. 触发工厂创建 15 个组件 → inject 实例到容器
7. 预热：ONNX embedder / ChromaDB / MCP Server 连接与工具注册
8. 注册工具：Skill、cron 工具、cron_tool_registry、bash、文件、A2A 工具
9. 注入 cron_scheduler.tool_registry → 启动后台循环（cleanup / file_cleanup / metrics_persist / cron）
10. 注入 cron 依赖到 PolicyEngine → 注册统一异常处理器
yield（对外服务）
关闭：cancel 后台任务 → stop multiagent adapter → 清理 Director 进程 → close_container（延迟关闭）
```

### 3.3 DI 容器（[container.py](../teage_liu/container.py)）

- **组件单例 + 工厂延迟创建**：`register(name, factory, deps, hot_reloadable)`；`get()` 首次触发工厂。
- **热重载原子重建**：`reload(changed_sections, new_config)` —— 先拓扑排序出"受影响 + 级联依赖者"，全部新建成功才批量替换，失败回滚；旧实例经 `CLOSE_GRACE_PERIOD`（默认 `llm.activity_timeout × max_loops`）延迟关闭。
- **配置段 → 组件映射**：`CONFIG_TO_COMPONENTS` 定义 `llm→orchestrator`、`security→approval_manager/orchestrator`、`multiagent→…` 等。
- **`hot_reloadable=False`** 的组件（orchestrator/stream_manager/…）不参与重建，需软重启 `POST /restart`。

组件清单（15 个通用 + 条件注册）：`session_logger / metrics_collector / metrics_store / audit_logger / approval_manager / task_manager / stream_manager / skill_loader / mcp_manager / upload_manager / etl_engine / cron_scheduler / health_checker / orchestrator / proposal_store`，以及 `a2a_router / a2a_client / multiagent_router / collab_router / multiagent_adapter / watchdog_watcher / …`（条件注册）。

---

## 4. 目录结构

```
teage-liu/
├── teage_liu/                  # 主包
│   ├── __main__.py / server.py # 启动入口
│   ├── app.py                  # FastAPI 实例 + 中间件 + DI 访问器 + 路由注册
│   ├── lifespan.py             # 启动/关闭编排
│   ├── container.py            # DI 容器 + 热重载
│   ├── config.py               # 配置加载（${ENV} 解析 + 敏感字段脱敏/分离）
│   ├── config_helpers.py       # 热更新边界（_RESTART_REQUIRED_KEYS）
│   ├── background_loops.py     # 会话/文件/指标 定时清理
│   ├── background_task_registry.py / stream_manager.py / breakpoint_detector.py
│   ├── errors.py               # 统一错误基类
│   ├── logging_setup.py
│   ├── orchestrator/           # 主编排（__init__ / chat_handler / factories / enhanced_context）
│   ├── agent/                  # ReAct 智能体核心（见 §6）
│   ├── llm/                    # client / prompts / reasoning_profiles
│   ├── memory/                 # 记忆（见 §9）
│   ├── storage/                # chroma_store / sqlite_log / history_buffer
│   ├── files/                  # 上传 / 解析 / 分块 / ETL / 上下文注入
│   ├── guardrails/             # 输入扫描 / 工具脱敏 / 输出过滤
│   ├── multiagent/             # 多智能体协作（见 §13）
│   ├── tasks/                  # 调度 + Workflow（见 §11 §12）
│   ├── routes/                 # REST 路由（见 §16）
│   ├── api/                    # multiagent_routes
│   ├── schemas/                # Pydantic 请求/响应模型
│   ├── sdk/                    # 外部 Agent SDK（见 §14）
│   ├── mcp/                    # MCP 客户端/管理器
│   ├── skill/                  # Skill 加载器
│   ├── monitoring/             # health / metrics / metrics_store
│   └── agent/tools/            # 内置工具实现
├── web/                        # 前端（见 §15）
├── cron_tool/                  # cron_tool 子进程工具（TOOL.md + run.*）
├── skills/                     # 可加载 Skill（bilibili / calculator / deploy）
├── tests/                      # 测试（见 §20）
├── data/                       # 运行时数据（见 §17）
├── docs/                       # 文档（架构/计划/SDK/超能力规格）
├── config.yaml(.example) / config2.yaml / config-a2a-*.yaml   # 本地/双 Worker/A2A 配置
├── requirements.txt / .env.example
├── deploy.sh / restart.sh / start.sh / stop.sh / *.ps1 / teage-liu.service / nginx-teage-liu.conf   # 部署与运维
├── scripts/                    # 运维/验证脚本（collect_perf_metrics / verify_spec_coverage / package_deploy / _fetch_hot）
├── desktop/                    # Tauri 桌面壳（仅 pyembed Python + 构建日志；target 缓存与源码由 feature/desktop-v0.1.0 分支管理）
└── data/blackboard/collaboration.md   # 协作消息落盘（见 §13）
```

> `desktop/src-tauri/{target,pyembed}/` 为构建产物/内嵌 Python 运行时，可忽略。

---

## 5. 一次对话的完整链路

**入口**：`POST /chat/stream`（SSE 流式）或 `POST /chat`（非流式），[routes/chat.py](../teage_liu/routes/chat.py)。

```
前端 sendMessage()
  → routes/chat.py: POST /chat/stream
      → stream_manager.register(session_id)             # 注册取消事件
      → orchestrator.chat_stream(...)                   # Orchestrator.__init__.py
          └─ ChatHandler.chat_stream (orchestrator/chat_handler.py)
              ├─ 0. 会话切换检测 → flush 旧会话沉淀
              ├─ 1. history_buffer.get_history(session)          # 短期历史
              ├─ 1.5 消费暂存的中断通知（InterruptNotice，5min TTL）
              ├─ 2. enhanced_context_builder.build()             # 构建上下文
              │     ├─ system_text：SYSTEM_PROMPT + 用户画像主体（缓存命中区）
              │     └─ enhanced_history：检索记忆 + Agent画像 + 沟通偏好 + 历史教训
              │           + 对话历史（经 condenser 压缩） + 当前输入
              ├─ 3. [非 cron] intent_classifier.classify_intent()  # 轻量意图路由
              ├─ 4. react_loop.run_stream(...)                    # ReAct 主循环
              │     └─ StreamRunner.run_stream()
              │          loop up to max_loops(50):
              │            ├─ yield round_start
              │            ├─ llm_client.chat_main_stream()        # 增量 token
              │            │     └─ yield text / reasoning
              │            ├─ stop_reason==tool_use ?
              │            │     ├─ detect_tool_stuck()            # 卡死检测（软警告/硬终止）
              │            │     ├─ PolicyEngine.check()           # allow/confirm/deny
              │            │     │     └─ confirm → ApprovalManager 审批卡片
              │            │     ├─ ToolExecutor.execute_tool_with_dispatch()
              │            │     │     ├─ cron_tool_registry（cron 路径）
              │            │     │     └─ tool_registry（全局）→ jsonschema 校验 → handler
              │            │     ├─ ErrorClassifier 兜底分类
              │            │     ├─ GuardrailEngine.sanitize_tool_result()  # 工具结果脱敏
              │            │     ├─ AuditLogger.log_tool_call()
              │            │     └─ yield tool 事件
              │            └─ 达到 max_loops → LLM 生成总结 → done(is_complete=False)
              ├─ 5. finally 批量写 session_logger（流中断也保存）
              ├─ 6. history_buffer 持久化本轮消息
              └─ 7. ConsolidationEngine.add_info() → 达阈值(15) → trigger_consolidation()
  → SSE 事件流（session/status/reasoning/text/round_start/tool/todo_*/approval_*/done/error/output_filtered）
```

**非流式路径**（`orchestrator.chat` → `ChatHandler.chat`）额外实现**自动续接**：React 循环 `is_complete=False` 且 TodoList 有未完成步骤时构造续接消息继续跑，总熔断 **200 轮**；空回复连续 2 次直接友好提示。

**协作会话路径**：`system_prompt_override` 非 None 时（`multiagent_` 前缀会话）完全绕开主 SYSTEM_PROMPT / 用户画像 / 检索记忆，用协作专用 prompt（身份声明），避免多 Worker 共享 "Teage Liu" 自称导致身份混乱。

---

## 6. 智能体核心

### 6.1 ReactLoop（[react_loop.py](../teage_liu/agent/react_loop.py)）

- 主循环：LLM 调用 → 解析 content blocks（text/tool_use/thinking）→ 执行工具回传 tool_result → 循环，直至 `end_turn` / `max_loops`。
- `max_loops=50`；委托模式：`StreamRunner`（流式）/ `SyncRunner`（同步），核心逻辑共享。
- 弱引用 `_orchestrator_ref` 防 GC 泄漏；跨轮中断提示通过 `orchestrator._pending_interrupt_notices` 下轮注入。
- 终止原因枚举：`normal / user_cancel / tool_permanent_fail / max_loops`。

### 6.2 工具执行管线（[tool_executor.py](../teage_liu/agent/tool_executor.py)）

- 派发顺序：`cron_tool_registry` → `tool_registry`；失败统一归一化为 `ToolError`。
- `compute_params_hash()`（md5）→ `detect_tool_stuck()` 卡死检测：滑动窗口最近 5 次调用，同参数重复 ≥3 判卡死；`anti_crawler / permanent` 历史命中立即硬终止。
- 取消事件通过 `ContextVar`（`_cancel_context.py`）传播到同步 handler。

### 6.3 结构化异常（[tool_error.py](../teage_liu/agent/tool_error.py)）

`ToolError`（dataclass）+ `ErrorStage`（PRE_EXECUTION / EXECUTION / PROTOCOL）+ **17 种子类**：

| 阶段 | 异常类 |
|------|--------|
| 执行前 | `ParamError / ToolNotFoundError / PolicyDeniedError / UserRejectedError / NonStreamHILError / StuckDetectedError / CancelledError` |
| 执行中 | `NotFoundError / PermissionDeniedError / ToolTimeoutError / TransientError / PermanentError / AntiCrawlerError / AuthRequiredError / InternalError` |
| 协议 | `OrphanToolResultError / LLMFailureError` |

- `to_receipt()`（execution 阶段 tool_result 收据）/ `to_system_block()`（pre_execution 系统注入块）。
- `from_exception()` 归一化非 ToolError；`from_cron_error()` 解析子进程错误 JSON。

### 6.4 会话与流式

- [session_manager.py](../teage_liu/agent/session_manager.py)：`ensure_session` + fire-and-forget 异步标题生成（首轮 5-10 字）。
- [stream_manager.py](../teage_liu/stream_manager.py)：`register/cancel/register_graceful/force_cancel`；`/chat/cancel` 支持 **immediate / graceful（自然断点）/ 二次 force kill** 三级中断。
- [breakpoint_detector.py](../teage_liu/breakpoint_detector.py)：断点检测（graceful 中断在句子边界切出）。
- [msg_persistence.py](../teage_liu/agent/msg_persistence.py)：JSONL 消息落盘 + 中断通知暂存 + 历史清洗（连续 user 合并等）+ 沉淀触发。

### 6.5 意图与元认知

- [intent_classifier.py](../teage_liu/agent/intent_classifier.py)：`SIMPLE_QA / KNOWLEDGE_LOOKUP / MULTI_STEP_TASK / OUT_OF_SCOPE`，复用轻量 LLM（≤200 token），失败降级 SIMPLE_QA、低置信度回退 MULTI_STEP_TASK。
- [meta_cognition.py](../teage_liu/agent/meta_cognition.py)：连续失败 ≥2 或 PERMANENT → 写入 "Agent 自画像" 信号池；检测用户中文失败反馈关键词。
- [error_classifier.py](../teage_liu/agent/error_classifier.py)：web_fetch 走 HTTP 状态码分类，通用路径关键词 + 三重误判防护。

---

## 7. 工具系统

### 7.1 分层注册（[tool_registry.py](../teage_liu/agent/tool_registry.py)）

| 层 | 语义 | 缓存策略 |
|----|------|----------|
| **Core Tier** | 永远全量注入完整 schema | 参与缓存 key，保证 KV cache 100% 命中 |
| **Deferred Tier** | 仅注入轻量 stub（无 input_schema） | 不参与缓存 key，`tool_list` 搜索后加载 |
| **Loaded** | Deferred 加载后的缓存 | `execute_tool` 查找优先 |

### 7.2 内置工具清单

**Core（9 个）**：`file_read / file_write / file_delete / file_listdir / file_edit / file_glob / file_grep / web_fetch / web_search`
**元工具（2 个）**：`tool_list`（搜索并按需加载）、`tool_call`（调用已加载工具）
**条件注册**：`profile_update`（有 consolidation_engine）、`file_list_uploads / file_query / file_read_uploaded`（有 ETL）、`bash_exec`（shell）、`plan_create / plan_update_step`（plan 模式）、`list_remote_agents / send_remote_message`（A2A）、`memory_search / memory_delete / memory_update`（记忆）、6 个 `skill__*`（Skill 管理）、4 个 `cron_*`（调度）

**关键安全细节**：
- `bash_exec`：无 shell 元字符走 `shell=False + shlex.split`；有元字符 `shell=True` 触发 confirm；Windows 多行 `python -c` 改写为临时 .py；跨线程进程追踪 + `kill_running_process` 强杀进程树；输出截断 20000 字符。
- `web_fetch`：三级反爬升级（httpx 标准 → 增强头 → curl_cffi TLS 指纹），域名状态缓存到 `data/domain_state.json`。
- `memory_*`：三层防线（长度 ≤4000 / 14 条黑名单正则 / per-session 频次 5 次）。

### 7.3 cron_tool 子进程隔离（[cron_tool_loader.py](../teage_liu/tasks/cron_tool_loader.py)）

- 独立 `cron_tool/{name}/TOOL.md + run.{py,sh,js}`，**子进程执行**（崩溃隔离、语言无关、按扩展名选解释器），stdin/stdout 传 JSON。
- schema 绝不注入全局 ToolRegistry（缓存硬约束）；`cron_tool_create` 写入 `.pending/` 待人工审查激活。

### 7.4 Skill 系统（[skill/loader.py](../teage_liu/skill/loader.py)）

- 三层结构：`SKILL.md`（元数据）+ body（上下文注入）+ `scripts/`（脚本）或旧式 `tools.py`。
- 6 个管理工具：`skill__template / propose / reload / toggle / list / resource`；状态持久化 `data/skills_state.json`。

### 7.5 MCP 扩展（[mcp/](../teage_liu/mcp/)）

- 三种传输：stdio / SSE / HTTP；工具命名 `mcp__{server}__{tool}` 注册为 Core Tier。
- HIL 分级：`hil=False` server 直接放行，`hil=True` 走审批；>30 工具摘要降级模式。

---

## 8. 安全体系

### 8.1 纵深防御分层

| 层 | 组件 | 时机 | 语义 |
|----|------|------|------|
| **L1 输入扫描** | `InjectionGuard` | 用户输入后 | Prompt 注入检测，`block/warn/off` |
| **L3 工具脱敏** | `ToolOutputSanitizer` | 工具结果回填 LLM 前 | 非白名单工具输出 PII 脱敏 + 长度截断（fail-open） |
| **L4 输出过滤** | `OutputFilter` | LLM 输出后 | 手机号/邮箱/身份证/银行卡/IP 脱敏，前端补发 `output_filtered` 事件 |
| **L5 策略门控** | `PolicyEngine` | 工具调用前 | allow/confirm/deny 三态（fail-closed 硬拦截） |

### 8.2 PolicyEngine（[policy.py](../teage_liu/agent/policy.py)）

`check()` 评估链：禁用→放行 → 读路径黑名单 deny（`read_paths` deny_first）→ MCP HIL → **cron 预授权三层**（granted_tools 列表 + 硬禁止 + path_prefix 匹配）→ bash 命令分类 → write/delete 文件状态决策表 → 元工具内省 → 规则列表（精确优先/正则次之）→ 默认 allow。

- 默认规则：`file_write / bash_exec / tool_call / file_edit / memory_delete / memory_update / skill__*` → **confirm**。
- 硬禁止工具（cron 预授权永不放行）：`memory_delete / bash_exec / tool_call`。
- `CommandClassifier`：读命令（cat/ls…）→ read；删除/高危（rm/git push --force/重定向）→ delete；复合命令拆分 + 引号剥离防误判。

### 8.3 HIL 审批（[approval.py](../teage_liu/agent/approval.py)）

- 状态机 `PENDING → APPROVED/DENIED/EXPIRED`；默认超时 300s 自动拒绝。
- `tool_kind`（generic/file/shell/skill/mcp/memory）驱动前端审批卡片差异化渲染。
- 非流式路径 confirm 自动拒绝（`NonStreamHILError`）。

### 8.4 请求认证

- `security.api_key` 配置后启用 Bearer 认证中间件（`/health` 豁免）；API Key 从 .env 经 `${VAR}` 注入。

---

## 9. 记忆系统

### 9.1 三层架构与数据流

```
对话消息
   │  add_info()
   ▼
ConsolidationEngine（阈值 15 条触发）         短期：HistoryBuffer（FIFO 50 轮 + JSONL 全量）
   │  轻量模型提取事实（surprise-gate 双阈值）         │ 淘汰回调（archive_turns_on_evict）
   ▼                                                   ▼
ChromaDB 长期记忆（namespace: user/cron/file） ◀──── 对话轮次自动归档（conversation_turn）
   │  MemoryRetriever（top_k=5 + 衰减排序 + relevance≥0.6）
   ▼
ContextManager.build → messages[0] 注入区（缓存失效区）
   │
   ▼
SignalPool（同一信号 ≥7 次才写）──▶ memory.md 用户画像（关键词去重 + 分段硬上限 + 备份5份）
```

### 9.2 关键模块

| 模块 | 职责要点 |
|------|----------|
| [consolidation.py](../teage_liu/memory/consolidation.py) | 信息计数达阈值触发；LLM 提取 JSON 事实；去重 0.85；惊讶门控（<0.85 新增 / 0.85~0.92 更新 / ≥0.92 跳过）；cron 会话跳过 user_profile；namespace 按 session 前缀路由；延迟合并队列防写放大 |
| [condenser.py](../teage_liu/memory/condenser.py) | **Masking**（默认）：旧区 tool_result 替换占位符（保留 tool_use，幂等）；**LLM Summarizing**：>100k token 时摘要旧区，失败降级纯 masking |
| [decay.py](../teage_liu/memory/decay.py) | `importance × exp(-days×decay_rate) × (log(1+count)×w + 1)` 三因子 |
| [retrieval.py](../teage_liu/memory/retrieval.py) | 相似度分桶（0.05 精度）+ 桶内 importance 降序 |
| [signal_pool.py](../teage_liu/memory/signal_pool.py) | Jaccard ≥0.7 合并计数；情感加权（+2/+3）；target 分 user/agent |
| [memory_md.py](../teage_liu/memory/memory_md.py) | 类别关键词映射（基本信息/技术栈/工作习惯/兴趣爱好/其他）；分段硬上限；写入前备份 |
| [context_manager.py](../teage_liu/memory/context_manager.py) | 缓存稳定前缀（system + tools）/ 易变 messages[0]（文件注入→Agent自画像→沟通偏好→历史教训→检索记忆），总注入 ≤8000 字符 |
| [chroma_store.py](../teage_liu/storage/chroma_store.py) | PersistentClient + ONNX embedder（单例，~80MB）；`find_duplicates / query_memory(reinforce) / delete_old_entries(TTL)` |
| [sqlite_log.py](../teage_liu/storage/sqlite_log.py) | sessions/messages + FTS5 全文索引，`search_messages` 支持 `/recall` |
| [history_buffer.py](../teage_liu/storage/history_buffer.py) | 内存工作集（FIFO）+ 磁盘 JSONL 全量；tool_use/tool_result **配对原子淘汰**防 400 |

---

## 10. 配置系统

### 10.1 加载（[config.py](../teage_liu/config.py)）

- YAML + `${ENV_VAR}` 占位符递归解析；`load_dotenv(override=True)`（.env 优先）。
- 基于文件 **mtime 的缓存**，高频端点不重复读盘；`clear_config_cache()` 由 PUT /config 调用。
- 启动校验：`llm.main_api_key` 缺失阻止启动；`consolidation_api_key / security.api_key` 缺失仅告警。
- 关键超时：`llm.activity_timeout`（60s，per-token 卡死）、`llm.stream_total_timeout`（300s 兜底）。

### 10.2 敏感字段分离

- `SENSITIVE_FIELDS` 映射：`GET /config` 返回时 `****` 脱敏（`mask_api_key`）；`PUT /config` 时识别脱敏哨兵还原真实值（`unmask_sensitive_config`）。
- 写入时非敏感字段写 config.yaml，敏感字段写 .env 并替换为占位符（`write_config_with_sensitive_separation`）。

### 10.3 热更新边界

- `config_helpers.py` 的 `_RESTART_REQUIRED_KEYS` 定义需重启的键；`guardrails` / `multiagent.blackboard_dir` / `cron.hooks` 等变更需重启。
- `timeout / enabled / rules` 等即时生效；结构变更走 `PUT /config` → `container.reload()` → 或 `POST /restart` 软重启（等待活跃流 ≤30s → 冲刷沉淀 → 重建 Orchestrator/health_checker/etl_engine → 重启 cron task）。

### 10.4 配置段速查

```
llm           主/巩固双模型、Provider、max_context_tokens(200k)、超时
memory        chroma_path、consolidation_threshold(15)、surprise 双阈值、decay、condenser
server        host/port/cors_origins
storage       sqlite_path、session_ttl_days、cleanup_interval_hours
files         上传目录/大小/分块(512,64)/OCR 分层(paddle→tesseract→vision_llm)
history       persistence_dir
tools         max_react_loops(50)/defer_loading_threshold(20)/bash_timeout
monitoring    enabled/daily_persistence/flush_interval_minutes
interrupt     breakpoint_threshold/graceful_timeout
skills        mcp_servers
security      api_key/workspace_root/approval_timeout(300)/rules/read_paths(deny_first)
guardrails    input_scan/sanitizer/output_filter 三层独立开关
web_search    bing/baidu keys
tasks/schedules/cron  调度项 + hooks(validate/catchup/retry/notify) + inject_history + max_concurrency
workflow      default_timeout/default_retry/allowed_templates
multiagent    enabled/role/blackboard_dir + worker + director + collab(轮询/队列/休眠)
a2a           enabled/remote_endpoints/tls
```

---

## 11. Cron 调度

### 11.1 CronScheduler（[scheduler.py](../teage_liu/tasks/scheduler.py)，~1888 行）

- `run_loop` 每 **60s** tick；`CronExpr.matches(now)` + 分钟级去重表防重复触发；`next_run()` 逐分钟推算。
- 调度项 `Schedule`：`id/name/cron/task/enabled/granted_tools/active_tools_snapshot/workflow/generate_llm_summary`；持久化 `data/schedules.yaml`（原子写：tmp + fsync + replace）。
- **会话隔离**：触发用 `cron:{schedule_id}` 会话，`_clear_cron_history()` 清上下文；记忆写入 `namespace="cron" + cron_id`；工具 schema 由 `active_tools_snapshot` 过滤锁定。
- **四层 Hook**（配置 `cron.hooks.*`）：

| Hook | 时机 | 职责 |
|------|------|------|
| `validate` | before_execute | 校验 workflow spec（工具存在性/写操作 path_prefix） |
| `catchup` | run_loop 启动 | 扫描过期调度，按 `skip/execute_once` 补偿 |
| `retry` | on_failure | 固定间隔重试，permanent 类（含 AuthRequired）不重试，达 max_retries 放弃 |
| `notify` | after_execute | 邮件(SMTP)+Webhook 双通道；连续失败 ≥3 自动 disable |

- **双执行路径**：`schedule.workflow` 非空 → Workflow 引擎；否则 `orchestrator.chat(is_cron=True)` 走 legacy LLM 对话。
- **RunSummary** → `data/schedules/{id}/runs.jsonl`（含 step_traces / llm_summary）。

### 11.2 提议-确认协议

- `cron_propose`（LLM 提议，硬禁止工具检测）→ `ProposalStore`（状态机 `proposal_created→pending_confirm→confirmed/modified/rejected→schedule_active`）→ 用户前端确认 → `cron_create` 建调度并锁定工具快照。

---

## 12. Workflow 引擎

### 12.1 数据模型（[spec.py](../teage_liu/tasks/workflow/spec.py)）

- `WorkflowSpec`：`name/version/template(简易模式)/steps(多步模式)/on_failure/timeout_seconds`。
- `StepSpec`：`id/name/type/config/depends_on/condition/on_failure/timeout_seconds`。
- **5 种 step 类型**：`deterministic / llm / tool / react / subworkflow`（subworkflow 为 P2 stub）。

### 12.2 引擎执行（[engine.py](../teage_liu/tasks/workflow/engine.py)）

```
execute(spec, context)
├─ 简易模式 → 直接调旧模板（BUILTIN_TEMPLATES）
├─ 多步模式：
│   ├─ 拓扑排序（DFS 三色 + 环检测 → WorkflowCycleError）
│   └─ 逐 step：
│        ├─ workflow 级 timeout 检查
│        ├─ condition 求值（steps.<id>.outputs.<key> > N 正则）→ 跳过
│        ├─ 首次 probe → NotImplementedError 走 abort
│        └─ on_failure 策略：fallback(执行 fallback step) / skip / abort
│             └─ PERMANENT + abort → 终止整个 workflow
├─ 汇总 metrics + errors → WorkflowResult
└─ 失败 raise WorkflowExecutionError（由 RetryHook 接管整次重跑）
```

### 12.3 StepExecutor（[step_executor.py](../teage_liu/tasks/workflow/step_executor.py)）

| Executor | step.type | 说明 |
|----------|-----------|------|
| `DeterministicExecutor` | deterministic | 调内置模板 |
| `LlmCallExecutor` | llm | 单轮 LLM，注入 depends_on 前置输出 |
| `ToolCallExecutor` | tool | PolicyEngine.check → 工具执行 → audit log → 安全约束（硬禁止工具） |
| `ReactLoopExecutor` | react | ReactLoop 多轮（`asyncio.run` + RuntimeError 兜底） |
| `SubworkflowExecutor` | subworkflow | stub |

### 12.4 内置模板（6 个）

`directory_watch`（目录快照 diff + LLM 分析）、`summary`（会话总结）、`email_notify`（纯 SMTP）、`cleanup_suggest`（低重要度记忆清理建议）、`research`（ReactLoop 自主研究）、`custom`（cron_tool 子进程 + 可选 LLM 报告）。

### 12.5 追踪与校验

- `StepTrace`：每 step 全链路（attempts/status/error_class/outputs/tool_calls/files/duration），供前端 run 卡片 + RetryHook 判定。
- `WorkflowValidator`：id 唯一性 / depends_on 无环 / 模板白名单 / 硬禁止工具 / 写操作 path_prefix / timeout 非负。

---

## 13. 多智能体协作

> 这是近期（2026-07~08）的开发重点，详见 [docs/plans/2026-07-21-multiagent-overview.md](plans/2026-07-21-multiagent-overview.md)。

### 13.1 核心概念

- **角色**：`director`（协调/仲裁/信任分）与 `worker`（参与协作的 agent 实例）。Director 是"可插拔的协议执行者 + 观察者"，非中心化控制器；Worker 具备完整自治能力（Director 故障可自主运行）。
- **通信通道（双轨互补）**：

| | 文件系统黑板 | A2A HTTP 网关 |
|---|---|---|
| 定位 | 同机协作主通道 | 跨设备/跨实例通信 |
| 传输 | atomic_write + YAML frontmatter + watchdog | JSON-RPC 2.0 over httpx |
| 消息格式 | YAML frontmatter 块（`---...---`） | params dict + ed25519 签名 |
| 并发控制 | CAS / FileLock / 跨进程文件锁 | 远程锁（acquire_lock/release_lock） |

**A2A 是桥梁**：跨设备消息由网关写入本地黑板文件，本地 Worker 仍通过文件通道轮询拾取，最终消息格式统一。

### 13.2 黑板目录（Blackboard）

```
data/blackboard/
├── status.json            # 全局状态（version CAS / epoch / active_agents / locks / director）
├── director.md            # Director 元协议（frontmatter: epoch / heartbeat / turn_policy）
├── messages.md            # 主消息流（A2A 任务结果归档）
├── messages.pending.md    # 非本机轮次待 flush 消息
├── messages.replay_candidates.md
├── collaboration.md       # 全局协作消息（announce/relay）
├── agents/                # {id}.md 卡片 + {id}.state.json（幂等状态）+ keys/{id}.pem
├── collabs/               # index.md 协作索引 + {collab_id}.md 单协作完整消息流
├── audit/audit.jsonl      # append-only + SHA-256 hash 链
├── locks/                 # director.lock / audit.lock / .collab.{id}.lock
├── tasks/ schemas/ snapshots/   # 预留
```

**消息类型**：`request / response / consensus / end / extend / directive / announce / relay / status / result`。
字段含 `seq / from / to / message_id / collab_id / collab_round / reply_to / accept / error / priority / deadline / issued_by…`（由 [messages_md.schema.yaml](../data/schemas/multiagent/messages_md.schema.yaml) 校验）。

**三层锁**：CAS 锁（status.json.version compare-and-swap + fencing_token）→ FileLock（portalocker 跨进程）→ 跨进程文件锁（per-collab，防并发 append）。

### 13.3 WorkerAdapter（[worker_adapter.py](../teage_liu/multiagent/worker_adapter.py)，~3000 行核心）

**生命周期**：`start()`（加载跨重启状态 → 注册 agent_card → 心跳 → Director 健康监测 → 协作轮询 + 空闲检查 + 健康监控 + LLM 重试 consumer）→ 运行 → `stop()`（取消任务 / 释放锁 / 置 offline / 持久化状态）。

**协作轮询循环**：
- active 态完整轮询；连续 N 次空轮询进入 **sleeping 休眠态**（仅 stat mtime 廉价探针）；watchdog 文件变更事件经 `_collab_interrupt` Event 唤醒（<100ms）。
- D11 优化：同轮询周期消息缓存 + watchdog 失效，prompt 稳定前缀 + 易变后缀（缓存友好）。
- **消息路由**：`request/directive` 紧急路径、`response/result` 双队列、`consensus/end` 归档、`extend` 扩容。

**关键状态**：幂等集（`_responded_request_seqs / _processed_msg_seqs` FIFO LRU 2000 / `_executed_op_ids`）、双队列（`_urgent_queue` 插队 + `_normal_queue`）、round 状态、休眠状态机、归档集合（入口硬阻断）。

**Round 回合机制**：一来一回计数，双方同回合共享 round 号；默认 `max_rounds=8`，`extend` 信号扩容，绝对硬上限 1000（防 bug）。

**AutonomousModeController**：Director 故障 → 时间片轮转（30s/片）+ FIFO 仲裁 → 拒绝 Director 写入 → 二次确认退出（防抖动）。

### 13.4 DirectorEngine（[director_engine.py](../teage_liu/multiagent/director_engine.py)）

- 启动：`locks/director.lock` 互斥锁，失败进入硬超时强抢（心跳 age > 2×timeout 时 emergency_release 接管）；每次启动递增 `director.md.current_epoch`。
- 主循环 6 子任务：`tick / heartbeat / turn_timeout / observe / flush_pending / arbitrate`；每子任务独立 try/except，连续失败 10 次触发 Director 重启。
- **信任分**（0-100）：单次 delta ≤5，阈值 `degraded=60 / rejected=30 / force_offline=10` 触发状态降级。
- 三种实现（[directors/](../teage_liu/multiagent/directors/)）：`agent_director`（LLM 驱动）/ `user_director`（用户工作台手动注入）/ `script_director`（规则自动，超时检测）。
- `director_manager.py`：`LocalDirectorManager` 子进程 spawn + watchdog 自动重启（滑动窗口限流）+ 僵尸锁清理。

### 13.5 选举与恢复

- [election.py](../teage_liu/multiagent/election.py)：A2A 并行查询远端 `read_director_md` → epoch 最高者胜 → epoch 相同字典序仲裁 → 心跳超时可抢占（epoch+1）。
- [recovery.py](../teage_liu/multiagent/recovery.py)：从 audit.jsonl 重放重建 status.json，恢复期 fence 拒绝旧 epoch 写入。

### 13.6 协作与主会话隔离

- **对话**：专用 session `multiagent_{agent_id}` + 协作专用 system prompt（`system_prompt_override`），每次触发前清空协作会话历史。
- **数据**：协作写 `collaboration.md / collabs/{cid}.md`，主对话 messages.md 不混用。
- **上下文**：黑板层保留全量消息；LLM 上下文层用滚动摘要（frontmatter `summary` 四段 schema：consensus/open/positions/decisions）+ 近期 `partner_context_window` 轮 verbatim。

### 13.7 关键模块一览

| 模块 | 职责 |
|------|------|
| `a2a_client / a2a_gateway / a2a_server` | JSON-RPC 2.0 异步客户端 / 工作台网关（11 方法）/ agent 侧点对点服务器 |
| `agent_registry` | `agents/{id}.md` 注册表 + 心跳 |
| `blackboard` | 原子写 / YAML frontmatter / CollabWriter（串行化+去重+防连发闸门+consensus 熔断） |
| `collaboration_routes` | `/api/multiagent/collab/*` REST + SSE |
| `collab_health` | 停滞检测（双信号+宽限期）+ error 死循环检测（连续 K 轮 error 归档） |
| `collab_sanitize` | 消息净化（剥离工具元语言/思考性开头） |
| `injection_isolator` | LLM 注入扫描（6 特征 → 标记 + audit，不拒绝写入） |
| `path_sandbox` | 拒绝绝对路径 / `..` 穿越（Unix + Windows） |
| `rate_limiter` | 滑动窗口限流（per-IP 100/s） |
| `signature / message_signature` | Director/消息级 ed25519 验签 + 阈值阻断 |
| `trust_score / turn_manager / watchdog_watcher / worker_state` | 信任分 / 轮次 + pending flush / 文件监听（watchdog 降级轮询）/ 跨重启幂等状态 |

### 13.8 工作台入口

- `/workbench`：独立协作工作台（Agent 列表 / 消息流 / 活动时间线 / Director 控制台）。
- chat 页 `chat-collab-bridge.js`：将涉及本 agent 的协作消息注入主对话气泡。

---

## 14. 外部 Agent SDK

> 文档：[docs/sdk/api-reference.md](sdk/api-reference.md)、[getting-started.md](sdk/getting-started.md)、[protocol.md](sdk/protocol.md)。

**面向**：希望接入 Teage-Liu 工作台的外部 Agent 进程（Python asyncio）。

| 模块 | 职责 |
|------|------|
| [agent.py](../teage_liu/sdk/agent.py) | `TeageAgent`：生命周期（start/stop）、`send_message`（A2A 点对点 + 自动归档）、`send_collab_response`（S4 协作轮次响应）、`query_agent`、`get_directive_context`（Director 搭便车注入）、回调 `on_message / on_collab_archived / on_collab_error` |
| [transport.py](../teage_liu/sdk/transport.py) | `A2ATransport`：出站 JSON-RPC + 工作台 Forward 归档（ed25519 签名）+ 协作消息推送 |
| [message_adapter.py](../teage_liu/sdk/message_adapter.py) | 统一 A2A 入站 + 广播轮询双通道，按 message_id 去重 |
| [exceptions.py](../teage_liu/sdk/exceptions.py) | `SDKError → AuthenticationError / TransportError / ProtocolError` |

A2A 错误码：`-32700/-32600/-32601/-32602/-32603/-32001(签名)/-32002(路径沙箱)/-32003(限流)/-32004(锁失败)`。

---

## 15. 前端

### 15.1 页面

| 路由 | 页面 | 核心功能 |
|------|------|----------|
| `/` | index.html | 能力矩阵 + ReAct 流程 + 记忆可视化 + 主题切换 |
| `/chat` | chat.html | 主对话（侧栏会话/记忆/文件、流式气泡、工具卡片、审批卡片、Todo 卡片、思考区、设置） |
| `/monitor` | monitor.html | 性能指标 / Canvas 直方图 / 审计 / 调度历史 / 信号池进度条 |
| `/scheduler` | scheduler.html | 调度管理（执行历史 / 三 Tab 管理 / 新建调度含 Workflow 多模式） |
| `/workflow` | workflow.html | Workflow 可视化编排（模板库 + YAML/表单 + 依赖预览 + 保存为调度） |
| `/workbench` | workbench.html | 协作工作台（Agent 列表 / 消息流 / 活动时间线 / Director 控制台） |

### 15.2 JS 模块（[web/static/js/](../web/static/js/)）

- **基础设施**：`utils.js`（api 封装/markdown/工具函数）、`theme.js`（Design Token 主题）。
- **对话**：`chat-core.js`（SSE 消费 + 气泡/卡片渲染 + 中断）、`chat-main.js`（事件绑定）、`chat-session.js`、`chat-memory.js`、`chat-files.js`（上传 + ETL 轮询）、`chat-settings.js`（5 分类设置 + 防护开关）、`chat-schedule-badge.js`。
- **协作**：`chat-collab-bridge.js`、`collab-sse.js`（协作 SSE）、`collab-workbench.js`（观察窗）、`workbench.js`（独立工作台增强）、`multiagent-sse.js / multiagent-render.js / multiagent-settings.js`（web/js/ 顶层，Director 状态通道）。
- **监控/调度**：`monitor.js`、`scheduler.js`、`workflow.js`、`workflow-presets.js`。

### 15.3 三条 SSE 通道

| 通道 | 端点 | 方式 | 事件 |
|------|------|------|------|
| Chat SSE | `POST /chat/stream` | Fetch + ReadableStream | session/status/reasoning/text/round_start/tool/todo_*/approval_*/done/error/output_filtered |
| Collab SSE | `GET /api/multiagent/collab/sse` | EventSource | collab_message_append |
| Multiagent SSE | `GET /api/multiagent/sse` | EventSource | director_state_change/agent_join/agent_leave/autonomous_enter/autonomous_exit |

### 15.4 CSS 体系

`tokens.css`（CSS 变量：背景/文字/语义色/毛玻璃/气泡/字体 Fraunces+Sora+JetBrains Mono/8px 网格/布局尺寸）→ `base.css` → `components.css` → 各页面样式。双主题 `[data-theme=dark|light]` × 4 强调色 `[data-accent=violet|blue|amber|teal]`。

---

## 16. API 路由总表

### 16.1 对话与杂项

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/chat` | 非流式对话 |
| POST | `/chat/stream` | 流式对话（SSE） |
| POST | `/chat/cancel` | 中断（immediate/graceful/force） |
| GET | `/health` `/metrics` `/metrics/history` `/metrics/reset` `/metrics/signals` | 健康与指标 |
| GET | `/tools` | 工具清单（core/deferred/loaded） |
| GET | `/audit/logs` `/audit/since` | 审计日志 / 增量游标 |
| GET/POST | `/reasoning/status` `/reasoning/toggle` | 推理模式 |
| GET | `/recall` | FTS5 消息检索 |
| POST | `/consolidation/flush` | 强制记忆沉淀 |
| POST | `/restart` | 软重启 |
| GET | `/` `/chat` `/monitor` `/scheduler` `/workflow` `/workbench` | 静态页面 |

### 16.2 会话 / 配置 / 审批 / 提议 / Skill / Cron 工具 / 文件 / 记忆 / 调度

| 域 | 端点 |
|----|------|
| 会话 | `GET /sessions`、`GET /sessions/{id}/messages`、`PATCH /sessions/{id}`、`DELETE /sessions/{id}` |
| 配置 | `GET /config`、`PUT /config` |
| 审批 | `GET /approvals`、`POST /approvals/{id}/resolve` |
| 提议 | `GET /proposals`、`GET /proposals/{id}`、`POST /proposals/{id}/{confirm,modify,reject}` |
| Skill | `GET /skills`、`GET /skills/{name}`、`POST /skills/{name}/{reload,toggle}`、`DELETE /skills/{name}` |
| Cron 工具 | `GET /cron_tools`、`GET /cron_tools/pending`、`POST /cron_tools/{name}/{activate,reject}`、`PUT|DELETE /cron_tools/{name}`、`/cron_tools/schedules/{id}/runs*`、`/cron_tools/runs/{recent,stats}` |
| 文件 | `POST /files/upload`、`GET /files`、`GET /files/{id}`、`GET /files/{id}/raw`、`GET /sessions/{id}/files`、`DELETE /admin/files/{id}` |
| 记忆 | `GET /memories`、`GET /memories/all`、`DELETE /memories/{id}`、`GET /profile` |
| 调度 | `GET/POST /schedules`、`GET/PUT/DELETE /schedules/{id}`、`POST /schedules/{id}/trigger`、`GET /schedules/{id}/{history,audit,audit/{run_id},memories}`、`GET /schedules/runs`、`GET /schedules/pending-count` |

### 16.3 Multiagent（`/api/multiagent/*`）

`GET status / agents / messages / audit / director / worker/config / agents/{id}/card`、`POST director/{start,stop,restart}`、`GET director/status`、`POST agents/{id}/heartbeat`、`GET sse`。

### 16.4 协作（`/api/multiagent/collab/*`）

`GET messages`、`POST append / forward(强制签名) / broadcast / announce / directive / collabs`、`GET agents / collabs / events / sse`。

---

## 17. 数据落盘布局

```
data/
├── sessions.db (+wal/shm)        # SQLite：sessions / messages / messages_fts
├── sessions2.db                  # 兼容双实例分离存储
├── memory.md                     # 用户画像（Markdown）
├── memory_backups/               # 画像写入前备份（保留 5 份）
├── chroma/                       # ChromaDB 向量库（+ chroma2 测试）
├── history/                      # 会话完整历史 JSONL + todo/ 子目录
├── schedules.yaml                # 调度项持久化
├── schedules/                    # {schedule_id}/runs.jsonl + snapshot.json
├── uploads/                      # 上传文件 + .parsed 缓存
├── audit.jsonl (+audit2)         # 主审计日志
├── profile_signal_pool.json      # 信号池
├── domain_state.json             # web_fetch 域名状态缓存
├── skills_state.json             # Skill 禁用/锁定状态
├── server.log (.err)             # 服务日志
├── worker1/worker2.log           # 双 Worker 日志
├── blackboard/ blackboard_a2a_1/2  # 多 Agent 黑板目录
├── schemas/multiagent/           # agent_card/messages_md/director_md 等 JSON Schema
└── _collab_*.json                # 协作状态快照
```

---

## 18. 监控与可观测性

- **MetricsCollector**（内存快照）→ **MetricsStore**（SQLite 每日合并，`metrics_persist_loop` 定时增量落盘）→ `/metrics` + `/metrics/history` 趋势。
- **HealthChecker**（`/health`）：聚合 orchestrator / session_logger / mcp / skill / metrics / proposal_store 状态。
- **审计**：`AuditLogger`（环形缓冲 + JSONL + 50000 行轮转保留 5 份）+ MultiAgent 版（append-only + hash 链）。
- 指标维度：LLM 调用/Token/缓存命中率/工具延迟/记忆命中率/意图分类/终止原因/探索率。

---

## 19. 部署形态

### 19.1 端口拓扑

```
生产（Linux）: [客户端] --HTTP:88--> [Nginx] --HTTP:7007--> [uvicorn, 127.0.0.1]
本地（Win/*nix）: [客户端] --HTTP:8000--> [uvicorn, 0.0.0.0:8000]
双 Worker（本地测试）: 8000 (teagent-lu) + 8001 (teagent-liu-2) 共享 blackboard
桌面（Tauri）: 内嵌 Python（pyembed）子进程 + WebView
```

### 19.2 一键部署（deploy.sh）

安装依赖 → 创建 deploy 用户 → rsync 到 `/opt/teage-liu` → 建 venv 装依赖 → 初始化 data + .env → 配置 systemd（`teage-liu.service`，127.0.0.1:7007，Restart=always，系统安全加固）→ 配置 nginx 反代（88→7007，SSE 关缓冲 `proxy_buffering off`、`proxy_read_timeout 300s`、静态资源 7d 缓存）。

### 19.3 桌面壳（desktop/）

Tauri（Rust）轻量包装 + `pyembed/python/python.exe` 嵌入式 Python 运行时，Sidecar 子进程方式启动后端，WebView 加载前端页面。核心逻辑全在 Python。

---

## 20. 测试体系

```
tests/
├── test_*.py            # 单元测试（~250 文件）
├── api/                 # multiagent 路由 + OpenAPI
├── multiagent/          # 协作全链路（e2e_self_talk / e2e_dual_instance / e2e_cross_device / collab_*）
├── e2e/                 # 端到端（含 Playwright UI）
├── integration/         # N-worker 长跑验证（--run-integration 启用）
├── sdk/                 # SDK 测试
├── conftest.py          # 夹具 + integration marker
└── performance/ scripts/collect_perf_metrics.py   # 性能采集
```

- 关键覆盖：工具注册、记忆巩固、PolicyEngine、ReAct 循环、配置热更新、SQLite/ChromaDB 持久化、MCP、Skill、Cron、Workflow、信号池、Guardrails、协作幂等/恢复/隔离契约。
- 验证脚本：`scripts/verify_multiagent_spec_coverage.py`（36 项 spec Grep 验收，支持 `--all`/单 Plan/JSON 输出）。

---

## 21. 演进脉络

| 阶段 | 内容 | 代表 Plan |
|------|------|-----------|
| 早期 | 单体对话 + 记忆 + 工具 | — |
| 2026-07 中 | 架构重构（DI 容器 / 路由拆分 / 前缀缓存） | docs/plans/2026-07-11-架构重构.md |
| 2026-07 中 | Workflow 引擎 + 信号池 + 监控页 + 结构化异常 | docs/plans/2026-07-17-调度执行强化.md |
| 2026-07 下 | Multi-Agent Phase 1-5（黑板/Director+Worker/A2A/前端） | docs/plans/2026-07-21-multiagent-*.md |
| 2026-07 末 | 自主协作重设计 + 协作链路四问题修复 | docs/plans/2026-07-23-*.md、2026-07-24-*.md |
| 2026-07 底 | Worker 幂等 / 重启恢复 | docs/plans/2026-07-27-*.md |
| 2026-07-29 | A2A 协议修复 + SDK | docs/plans/2026-07-29-A2A协议修复与SDK实施计划.md |
| 2026-07-30~08 | N-Worker 扩展（选举/嵌入式 Director/跨实例心跳/协作隔离契约） | docs/plans/2026-07-30-N-*、2026-07-31-* |

当前 HEAD（2026-08-02）：协作 SDK 回调完善（`send_collab_response` + `on_collab_archived/on_collab_error`）、协作/主会话完全隔离契约固化。

---

**文档版本**：v1.0　**生成日期**：2026-08-02　**覆盖**：源码级（读代码 + 全量测试清单）｜ 若有代码演进，请同步更新本文件。
