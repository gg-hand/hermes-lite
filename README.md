# Hermes Lite 🪶

> ⚠️ **个人 Demo 项目** — 本框架为个人学习和实验用途，非生产级产品。

一个自托管的个人 AI Agent，专注于长期对话、记忆沉淀与自主任务执行。

---

## 功能点

### 记忆系统
- **三层架构**：短期对话缓冲 → ChromaDB 向量长期记忆 → memory.md 持久化画像
- **记忆巩固**：N 轮自动提取事实，surprise-gating 双阈值过滤后沉淀
- **历史冷凝**：Masking + LLM Summary 双策略压缩超长上下文
- **记忆衰减**：近因 × 频率 × 重要性 三因子动态评分
- **JSONL 持久化**：会话历史落盘，重启可恢复

### 用户画像信号池
- **阈值沉淀**：同一信号累计 7 次才写入画像，过滤偶发表达
- **三层来源**：L1 用户主动告知 / L2 行为推断 / L3 情感信号
- **Jaccard 去重**：相似度 ≥ 0.25 视为同义，计数累加 + 续期
- **情感增强**：明确喜欢/讨厌等强情感 ×2 加权
- **原子化**：复合句拆分为原子事实，避免大段重复

### AI 护栏（Guardrails）
- **输入扫描**：Prompt 注入检测，命中 warn/block
- **工具脱敏**：工具返回值过滤（可信工具白名单豁免）
- **输出过滤**：PII 过滤（银行卡、手机号、身份证等）
- **fail-open 软护栏**：与 PolicyEngine fail-closed 硬拦截构成 defense in depth
- **前端开关**：iOS 风格 toggle，关闭触发二次确认

### ReAct 工具链
- **Core/Deferred 双层注册**：Core 字节级稳定缓存命中，Deferred 按需发现
- **HIL 审批**：高危操作拦截确认，超时自动拒绝
- **统一错误处理**：17 种结构化异常，三阶段分流
- **卡死检测**：两阶段处理，首次软警告 → 二次硬终止
- **结果缓存**：7 个读取类工具 per-run 缓存
- **路径安全**：read_paths 黑白名单，deny_first 保护源码/配置

### 通用 Workflow 引擎
- **拓扑执行**：自动按 depends_on 排序，含环检测
- **错误策略**：retry / fallback / skip / abort 四种
- **重试预算**：fixed / linear / exponential backoff
- **条件跳过**：condition 字段简化正则
- **执行追踪**：StepTrace 记录每步，注入 LLM 上下文
- **可视化面板**：访问 `/workflow` 可视化编排工作流，无需手写 YAML（详见 [Workflow 面板](#workflow-可视化面板)）

### Cron 调度
- **提议-确认协议**：LLM 提议 → 用户审查 → 创建调度
- **cron_tool 隔离**：独立 TOOL.md + run.py 子进程
- **工具快照锁定**：避免运行时竞态
- **会话隔离**：`cron:{schedule_id}` 与用户会话独立
- **独立页面**：从 chat 内嵌拆为 `/scheduler`

### 流式对话
- **SSE 实时推送**：文本增量 + 工具事件
- **立即/优雅中断**：断点检测在自然边界切出
- **per-token 超时**：60 秒可热更新
- **流式总超时**：300 秒
- **主动取消**：调用 stream.close()

### MCP 扩展
- **三种传输**：stdio / SSE / HTTP
- **命名规范**：`mcp__{server}__{tool}`，注册为 Core Tier
- **HIL 分级**：可信 server 直接放行，陌生 server 走审批
- **降级模式**：超过 30 个工具时摘要模式，需调用 mcp__list 查看

### Skill 系统
- **三层结构**：SKILL.md 元数据 / body 注入 / scripts 脚本
- **热重载**：运行时启用/禁用
- **降级模式**：超过 20 个时单 skill__load 元工具
- **B 站 Skill**：热门、搜索、视频详情、UP 主、分区榜

### 监控与可观测性
- **独立监控页**：`/monitor`，Canvas 直方图 + 健康检查 + 审计日志
- **信号池可视化**：攻略进度条按 section 分组、进度降序
- **指标持久化**：SQLite 增量写入，每日合并支持趋势查询
- **探索率指标**：跟踪工具调用方向

### 多模型协作
- **主/巩固分离**：强模型对话 + 轻量模型巩固
- **多提供商**：Anthropic / OpenAI / DeepSeek 兼容
- **全链路异步**：AsyncOpenAI / AsyncAnthropic，避免阻塞事件循环

### 配置与持久化
- **热更新**：timeout / enabled / rules 即时生效，结构性变更需重启
- **原子写入 + Schema 校验 + 备份回滚**
- **TodoList 持久化**：磁盘原子写 + 懒加载恢复
- **会话标题**：首轮异步生成 5-10 字精炼标题

### 文件 ETL 与知识库
- **格式支持**：PDF / DOCX / TXT / MD / PNG / JPG
- **自动分块**：chunk_size=512, overlap=64
- **OCR 识别**：chi_sim+eng
- **自动注入**：上传后下一轮注入 LLM 上下文

### 前缀缓存优化
- **Core Tier 字节级稳定**：100% 缓存命中
- **分层 system prompt**：稳定内容在前，动态内容在后
- **Anthropic 前缀缓存友好**：减少延迟与 token 成本

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| Web 框架 | FastAPI + Uvicorn |
| LLM 客户端 | AsyncAnthropic + AsyncOpenAI |
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
# 编辑 .env，设置 ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY
```

### 3. 启动服务

```bash
python src/server.py
# 或
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

服务默认监听 `http://localhost:8000`。

- 首页：`/`
- 对话：`/chat`
- 监控：`/monitor`
- 调度：`/scheduler`
- Workflow 编排：`/workflow`

---

## 配置说明

所有配置集中在 `config.yaml`，环境变量通过 `${VAR}` 占位注入。关键段：

- `llm`：模型提供商、模型名、API Key、超时
- `memory`：向量库路径、巩固阈值、检索 top_k、surprise_gate
- `storage`：SQLite 路径、会话 TTL
- `files`：上传目录、分块参数、OCR
- `tools`：max_react_loops、bash_timeout
- `guardrails`：input_scan / sanitizer / output_filter 三组件独立开关
- `security`：approval_timeout、read_paths 黑白名单
- `skills.mcp`：MCP server HIL 配置
- `tasks.schedules`：Cron 调度项

---

## Workflow 可视化面板

提供 `/workflow` 页面可视化编排工作流，无需手写 YAML。

### 三步上手

1. **选模板**：左侧模板库按难度分组（入门/进阶/高级），点击卡片一键加载
2. **配 step**：中栏结构化表单填写每个 step 的参数，或切"高级模式"直接编辑 YAML
3. **绑定 cron**：点"保存为调度"跳转 `/scheduler` 设置 cron 表达式

### 5 个预设模板

| 模板 | 难度 | 说明 |
|------|------|------|
| 每日新闻两步流 | 入门 | tool 抓取 → llm 总结 |
| 目录监控+邮件 | 入门 | deterministic 监控 → deterministic 通知 |
| 周报生成 | 进阶 | react 检索 → llm 起草 |
| 数据备份清理 | 进阶 | tool 备份 → deterministic 清理 |
| 代码 review | 高级 | react 自主审查 |

### 5 种 step 类型

- `deterministic`：调用内置模板（directory_watch / email_notify / cleanup_suggest 等）
- `llm`：单轮 LLM 调用（prompt + system）
- `tool`：直接调用单个工具（tool + input JSON）
- `react`：ReactLoop 多轮工具调用循环（task + max_loops + tool_whitelist）
- `subworkflow`：嵌套 workflow（P2 stub，暂未实装）

### YAML 示例

```yaml
name: daily_news
steps:
  - id: fetch
    name: 抓取新闻
    type: tool
    config:
      tool: web_search
      input:
        query: 今日热点新闻
  - id: summarize
    name: 生成简报
    type: llm
    depends_on: [fetch]
    config:
      prompt: 将上一步新闻整理成 300 字简报
```

### 配置项

`config.yaml` 的 `workflow` 段（可选，未配置时使用默认值）：

```yaml
workflow:
  default_timeout_seconds: 3600
  default_retry:
    max_attempts: 3
    backoff_strategy: fixed
    base_delay_ms: 1000
    max_delay_ms: 30000
  allowed_templates:
    - directory_watch
    - summary
    - email_notify
    - cleanup_suggest
    - research
    - custom
```

---

## 相关页面

| 路由 | 页面 |
|------|------|
| `/` | 首页 |
| `/chat` | 对话 |
| `/monitor` | 运行监控 |
| `/scheduler` | 调度管理 |
| `/workflow` | Workflow 可视化编排 |

---

## 开发

### 运行测试

```bash
pytest tests/ -v
```

100+ 测试用例，覆盖工具注册、记忆巩固、策略引擎、ReAct 循环、配置热更新、SQLite/ChromaDB 持久化、MCP 客户端、Skill 加载、Cron 调度、Workflow 引擎、信号池、Guardrails 等。
