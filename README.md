# Hermes Lite 🪶

> ⚠️ **个人 Demo 项目** — 本框架为个人学习和实验用途，非生产级产品。

个人长期 AI Agent 框架——面向长期对话与记忆管理的轻量级 Agent 系统。

Hermes Lite 是一个自托管的个人 AI Agent，专注于**长期记忆管理**和**自主工具调用**。它通过三层记忆架构、定期记忆巩固（Consolidation）、前缀缓存优化等技术，实现了低成本、可持续的长期对话能力。

---

## 核心特性

### 三层记忆架构

```
短期对话 ──→ 历史缓冲 ──→ 长期记忆 ──→ 持久化画像
HistoryBuffer   Condenser   ChromaDB向量库   memory.md
```

- **短期记忆**：会话内的对话历史，FIFO 环形缓冲区，超限触发归档
- **长期记忆**：ChromaDB 向量存储 + ONNX MiniLM 本地嵌入，语义检索
- **持久化画像**：`memory.md` 文件存储用户偏好、背景等结构化信息，自动注入系统提示

### 记忆巩固（Consolidation）

对话轮次达到阈值时，自动通过轻量模型提取事实，经 surprise-gating 双重阈值过滤后沉淀为长期记忆。采用 **延迟合并写入** 策略，减少向量库写放大。

### 前缀缓存优化

- **Core/Deferred 双层工具注册**：Core Tier 工具字节级稳定，100% 缓存命中
- **system prompt 分层设计**：稳定内容（画像、工具 schema）在前，动态内容（检索记忆、历史）在后
- **Anthropic 前缀缓存友好**：减少延迟与 token 成本

### 工具调用（ReAct）

- 支持工具延迟加载（Deferred Tier），按需发现
- HIL 审批（Human-in-the-Loop）：高危操作经 PolicyEngine 评估后需用户确认
- 工具卡死检测（滑动窗口 + 重试阈值）
- 审计日志（JSONL 记录所有工具调用，支持按调度/批次追溯）

### 多模型协作

- 主对话模型 + 巩固模型分离
- 关键任务用强模型，巩固/摘要用轻量模型
- 多 LLM 提供商支持（Anthropic、OpenAI 兼容）

### MCP 扩展

支持 MCP Server 工具注册，三种传输方式：
- **stdio**：子进程通信
- **SSE**：服务端推送
- **HTTP**：标准 REST

### Skill 系统

动态加载本地 Skill 扩展工具，支持热重载、启用/禁用管理。每个 Skill 通过 `SKILL.md` + `tools.py` 定义。

### Cron 调度

- 定时任务调度（标准 cron 表达式）
- **提议-确认协议**：LLM 提议调度项 → 用户审查确认 → 创建调度
- **cron_tool 子进程隔离**：独立 TOOL.md 描述 + run.py 执行

### 流式对话

- SSE 实时推送 LLM 文本增量与工具调用事件
- 支持立即中断与优雅中断（等待自然断点）
- 断点检测：在输出边界自动中断

### 配置热更新

运行时更新多数配置项无需重启（`PUT /config`），原子写入 + Schema 校验 + 备份回滚。

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| Web 框架 | FastAPI + Uvicorn |
| LLM 客户端 | Anthropic SDK + OpenAI SDK |
| 向量存储 | ChromaDB + ONNX MiniLM-L6-v2 |
| 结构化存储 | SQLite + FTS5 全文搜索 |
| 前端 | 纯 HTML/CSS/JS（暗色主题，无框架依赖） |
| 流式 | Server-Sent Events (SSE) |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入 API Key：

```bash
cp .env.example .env
# 编辑 .env，设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY
```

### 3. 启动服务

```bash
python src/server.py
# 或
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

服务默认监听 `http://localhost:7007`，打开浏览器即可开始对话。

---

## 配置说明

所有配置集中在 `config.yaml`：

```yaml
llm:
  main_provider: anthropic           # 主对话模型提供商
  main_model: claude-sonnet-5        # 主对话模型
  consolidation_provider: anthropic  # 巩固模型提供商
  consolidation_model: claude-haiku-4-5-20251001  # 巩固模型（轻量）

memory:
  chroma_path: data/chroma           # 向量库持久化路径
  consolidation_threshold: 15        # 多少轮对话后触发巩固
  retrieval_top_k: 5                 # 记忆检索返回条数
  surprise_gate_enabled: true        # surprise-gating 开关

server:
  host: 0.0.0.0
  port: 7007
```

环境变量使用 `${VAR}` 语法在 YAML 中占位，运行时自动注入。

---

## 项目结构

```
hermes-lite/
├── src/
│   ├── agent/           # Agent 核心（工具注册、ReactLoop、策略、审计、审批）
│   │   ├── tool_registry.py    # Core/Deferred 双层工具注册
│   │   ├── react_loop.py       # ReAct 循环（同步 + 流式）
│   │   ├── policy.py           # 策略引擎（文件权限、命令分类）
│   │   ├── approval.py         # HIL 审批管理器
│   │   ├── audit.py            # 审计日志（JSONL 环形缓冲区）
│   │   ├── builtin_tools.py    # 内置工具（文件、HTTP、命令）
│   │   ├── file_registry.py    # 会话级文件操作记录
│   │   ├── cron_tools.py       # Cron 调度工具
│   │   ├── cron_proposals.py   # 提议-确认协议
│   │   ├── cron_tool_registry.py  # cron_tool 独立注册
│   │   └── skill_tools.py      # Skill 管理工具
│   ├── llm/             # LLM 客户端
│   ├── memory/          # 三层记忆管理
│   │   ├── consolidation.py    # 记忆巩固引擎
│   │   ├── context_manager.py  # 提示词构建（缓存优化）
│   │   ├── retriever.py        # 记忆检索（bucket 排序、可选 LLM 重排）
│   │   ├── condenser.py        # 历史冷凝（Masking + LLM Summary）
│   │   ├── decay.py            # 记忆衰减（近因×频率×重要性）
│   │   └── memory_md.py        # 持久化画像文件管理
│   ├── storage/         # 持久化存储
│   │   ├── chroma_store.py     # ChromaDB + ONNX 嵌入
│   │   ├── sqlite_log.py       # SQLite + FTS5 会话日志
│   │   └── history_buffer.py   # 短期历史缓冲（FIFO + JSONL 持久化）
│   ├── mcp/             # MCP 协议支持
│   ├── tasks/           # 任务编排
│   │   ├── scheduler.py        # Cron 调度器
│   │   ├── cron_tool_loader.py # cron_tool 加载器
│   │   └── workflow/           # 工作流模板
│   ├── skill/           # Skill 加载器
│   ├── monitoring/      # 监控（指标、健康检查）
│   ├── orchestrator.py  # 全局编排器
│   ├── server.py        # FastAPI HTTP 服务
│   └── config.py        # 配置加载（YAML + 环境变量注入）
├── web/
│   └── index.html       # 前端 SPA（暗色主题）
├── skills/              # 本地 Skill 扩展
├── cron_tool/           # cron_tool 子进程工具
├── tests/               # 100+ 单元与集成测试
├── config.yaml          # 主配置文件
└── requirements.txt     # Python 依赖
```

---

## API 概览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/chat` | POST | 同步对话 |
| `/chat/stream` | POST | SSE 流式对话 |
| `/chat/cancel` | POST | 中断流式对话 |
| `/consolidation/flush` | POST | 手动触发记忆巩固 |
| `/sessions` | GET/POST | 会话管理 |
| `/sessions/{id}/messages` | GET | 会话消息历史 |
| `/memories` | GET | 向量记忆搜索 |
| `/memories/all` | GET | 列出所有记忆 |
| `/memories/{id}` | DELETE | 删除单条记忆 |
| `/profile` | GET | 用户画像 |
| `/health` | GET | 深度健康检查 |
| `/tools` | GET | 工具清单 |
| `/audit/logs` | GET | 审计日志 |
| `/approvals` | GET | 待审批请求 |
| `/approvals/{id}/resolve` | POST | 提交审批决定 |
| `/config` | GET/PUT | 配置读写 |
| `/schedules` | GET/POST | Cron 调度 |
| `/schedules/{id}/trigger` | POST | 立即触发调度 |
| `/proposals` | GET | 调度提议 |
| `/skills` | GET | Skill 管理 |
| `/metrics` | GET | 指标快照 |

---

## 开发

### 运行测试

```bash
pytest tests/ -v
```

### 测试覆盖

100+ 测试用例，覆盖：
- 工具注册与执行
- 记忆巩固与检索
- 策略引擎与权限
- ReAct 循环与流式
- 配置热更新
- SQLite + ChromaDB 持久化
- MCP 客户端
- Skill 加载
- Cron 调度与 cron_tool

---

## 许可证

MIT
