# hermes-lite

个人长期 AI Agent —— 一个面向长期对话与记忆管理的轻量级 Agent 框架。

## 核心特性

- **三层记忆架构**：短期对话历史、长期记忆（向量检索）、持久化记忆文件，分层管理上下文。
- **记忆 Consolidation（巩固）**：当对话轮次达到阈值时，自动通过轻量模型对历史进行摘要与去重，沉淀为长期记忆，避免上下文膨胀。
- **前缀缓存优化**：通过稳定的系统提示与历史前缀设计，最大化利用 LLM 的前缀缓存（prefix caching），降低延迟与 token 成本。
- **多模型协作**：主对话模型与巩固模型分离，关键任务用强模型，巩固/摘要用轻量模型。
- **工具调用（ReAct）**：支持工具延迟加载与最大循环数限制，避免无限递归。

## 目录结构

```
hermes-lite/
├── src/
│   ├── agent/         # Agent 主体逻辑
│   ├── llm/           # LLM 客户端封装
│   ├── memory/        # 三层记忆与巩固
│   ├── storage/       # SQLite/Chroma 持久化
│   ├── orchestrator.py
│   └── server.py      # FastAPI 服务入口
├── data/              # 运行时数据（DB、向量库、记忆文件）
├── config.yaml        # 配置文件
├── requirements.txt
└── README.md
```

## 快速启动

1. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

2. 配置环境变量（API Key）：

   ```bash
   export ANTHROPIC_API_KEY=your_key_here
   ```

3. 启动服务：

   ```bash
   python -m uvicorn src.server:app --host 0.0.0.0 --port 8000
   ```

   或直接运行：

   ```bash
   python src/server.py
   ```

4. 服务默认监听 `http://localhost:8000`。

## 配置说明

所有可调参数集中在 `config.yaml`，包括模型选择、上下文阈值、记忆巩固阈值、检索 top_k、服务端口等。环境变量统一使用 `${VAR}` 语法在 YAML 中占位，运行时注入。

## License

MIT
