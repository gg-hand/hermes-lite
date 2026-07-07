# hermes-lite

个人 agent 项目，含 cron 调度、workflow 引擎、监控面板、意图分类器。

## Workflow 编排

访问 `http://127.0.0.1:8000/workflow` 可视化编排工作流，无需手写 YAML。

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

### 相关页面

- `/workflow`：可视化编排
- `/scheduler`：调度管理（含 workflow 字段配置）
- `/monitor`：运行监控
- `/chat`：对话
