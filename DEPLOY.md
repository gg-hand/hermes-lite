# Hermes Lite 部署文档

本文档描述 Hermes Lite 个人长期 AI Agent 的环境要求、安装步骤、本地运行、EC2 部署、API 接口、配置说明与故障排查。

---

## 1. 环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.11 及以上 |
| 操作系统 | Linux（推荐 Ubuntu 22.04 / Amazon Linux 2023）、macOS、Windows |
| API Key | Anthropic API Key（`ANTHROPIC_API_KEY` 环境变量） |
| 网络 | 需访问 Anthropic API（`api.anthropic.com`）；首次运行需下载 `all-MiniLM-L6-v2` 模型权重 |

### Python 依赖

所有依赖列于 `requirements.txt`：

```
anthropic>=0.40.0
openai>=1.50.0
chromadb>=0.5.0
fastapi>=0.115.0
uvicorn>=0.32.0
tiktoken>=0.8.0
pydantic>=2.9.0
pyyaml>=6.0
httpx>=0.27.0
numpy>=1.26.0
sentence-transformers>=3.0.0
```

> **注意**：`sentence-transformers` 首次运行时会自动下载 `all-MiniLM-L6-v2` 模型（约 90MB），需确保网络畅通。

---

## 2. 安装步骤

### 2.1 克隆项目

```bash
git clone <your-repo-url> hermes-lite
cd hermes-lite
```

### 2.2 创建虚拟环境（推荐）

```bash
python3 -m venv .venv
source .venv/bin/activate    # Linux / macOS
# .venv\Scripts\activate     # Windows PowerShell
```

### 2.3 安装依赖

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.4 配置环境变量

```bash
export ANTHROPIC_API_KEY="sk-ant-xxxxxxxxxxxxxxxxxxxx"
```

> 也可写入 `.env` 文件或 systemd 服务配置（见 EC2 部署部分）。

---

## 3. 本地运行

### 3.1 使用 uvicorn 启动（开发模式，支持热重载）

```bash
python -m uvicorn src.server:app --reload --host 0.0.0.0 --port 8000
```

### 3.2 直接运行 server.py

```bash
python src/server.py
```

### 3.3 使用启动脚本

```bash
chmod +x start.sh
./start.sh
```

`start.sh` 内容：

```bash
#!/bin/bash
cd "$(dirname "$0")"
export ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-your_api_key_here}
python -m uvicorn src.server:app --host 0.0.0.0 --port 8000 --workers 1
```

### 3.4 验证服务

服务启动后，访问健康检查接口：

```bash
curl http://localhost:8000/health
```

预期返回：

```json
{"status": "healthy", "timestamp": "2026-01-01T00:00:00.000000", "version": "0.1.0"}
```

---

## 4. EC2 部署步骤

### 4.1 SSH 到 EC2 实例

```bash
ssh -i your-key.pem ec2-user@<EC2_PUBLIC_IP>
```

### 4.2 安装系统依赖

```bash
# Amazon Linux 2023
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip git

# Ubuntu 22.04
# sudo apt update -y
# sudo apt install -y python3.11 python3.11-venv python3.11-dev git
```

### 4.3 克隆项目

```bash
sudo mkdir -p /opt/hermes-lite
sudo chown ec2-user:ec2-user /opt/hermes-lite
cd /opt/hermes-lite
git clone <your-repo-url> .
```

### 4.4 安装 Python 依赖

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4.5 配置环境变量

将 API Key 写入环境变量文件（避免明文暴露在命令历史中）：

```bash
# 创建环境变量文件（仅 root/ec2-user 可读）
sudo tee /etc/hermes-lite/env > /dev/null <<EOF
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx
EOF
sudo chmod 600 /etc/hermes-lite/env
sudo chown ec2-user:ec2-user /etc/hermes-lite/env
```

### 4.6 配置 systemd 服务

将项目根目录下的 `hermes-lite.service` 复制到 systemd 目录：

```bash
sudo cp /opt/hermes-lite/hermes-lite.service /etc/systemd/system/hermes-lite.service
```

`hermes-lite.service` 内容：

```ini
[Unit]
Description=Hermes Lite Personal AI Agent
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/hermes-lite
Environment=ANTHROPIC_API_KEY=your_api_key_here
ExecStart=/usr/bin/python3 -m uvicorn src.server:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> **安全建议**：生产环境建议使用 `EnvironmentFile=/etc/hermes-lite/env` 替代 `Environment=ANTHROPIC_API_KEY=...`，避免 API Key 明文出现在 service 文件中：
>
> ```ini
> EnvironmentFile=/etc/hermes-lite/env
> ExecStart=/opt/hermes-lite/.venv/bin/python -m uvicorn src.server:app --host 0.0.0.0 --port 8000 --workers 1
> ```

### 4.7 启动服务

```bash
# 重新加载 systemd 配置
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start hermes-lite

# 设置开机自启
sudo systemctl enable hermes-lite

# 查看服务状态
sudo systemctl status hermes-lite
```

### 4.8 查看日志

```bash
# 实时查看服务日志
sudo journalctl -u hermes-lite -f

# 查看最近 100 行日志
sudo journalctl -u hermes-lite -n 100
```

### 4.9 配置安全组（EC2 控制台）

在 EC2 安全组中开放 8000 端口（入站规则）：

| 类型 | 协议 | 端口 | 来源 |
|------|------|------|------|
| 自定义 TCP | TCP | 8000 | 你的 IP / 0.0.0.0/0 |

> **安全建议**：生产环境建议仅允许特定 IP 访问，或在前端加 Nginx 反向代理 + HTTPS。

---

## 5. API 接口文档

服务默认监听 `http://<host>:8000`。

### 5.1 健康检查

```bash
curl http://localhost:8000/health
```

**响应**：

```json
{
  "status": "healthy",
  "timestamp": "2026-01-01T00:00:00.000000",
  "version": "0.1.0"
}
```

### 5.2 对话

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，请介绍一下你自己"}'
```

**请求体**：

```json
{
  "session_id": null,
  "message": "你好，请介绍一下你自己"
}
```

- `session_id`（可选）：会话 ID，不传则自动新建。

**响应**：

```json
{
  "session_id": "abc-123-def",
  "response": "你好！我是 Hermes Lite...",
  "timestamp": "2026-01-01T00:00:00.000000"
}
```

**带会话 ID 继续对话**：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "abc-123-def", "message": "我刚才说了什么？"}'
```

### 5.3 列出所有会话

```bash
curl http://localhost:8000/sessions
```

**响应**：

```json
{
  "sessions": [
    {
      "id": "abc-123-def",
      "created_at": "2026-01-01T00:00:00.000000",
      "updated_at": "2026-01-01T00:05:00.000000"
    }
  ]
}
```

### 5.4 获取会话消息历史

```bash
curl http://localhost:8000/sessions/abc-123-def/messages
```

**可选参数** `limit`（限制返回条数）：

```bash
curl "http://localhost:8000/sessions/abc-123-def/messages?limit=10"
```

**响应**：

```json
{
  "messages": [
    {
      "role": "user",
      "content": "你好",
      "created_at": "2026-01-01T00:00:00.000000"
    },
    {
      "role": "assistant",
      "content": "你好！有什么可以帮你的？",
      "created_at": "2026-01-01T00:00:01.000000"
    }
  ]
}
```

### 5.5 删除会话

```bash
curl -X DELETE http://localhost:8000/sessions/abc-123-def
```

> **注意**：当前 `SessionLogger` 暂不支持删除会话，返回 `501 Not Implemented`。

---

## 6. 配置说明

所有可调参数集中在 `config.yaml`，环境变量使用 `${VAR}` 语法在 YAML 中占位，运行时由 `load_config` 解析注入。

```yaml
llm:
  main_model: claude-sonnet-4-5-20250929        # 主对话模型
  main_api_key: ${ANTHROPIC_API_KEY}             # 主对话 API Key（从环境变量读取）
  consolidation_model: claude-haiku-4-5-20251001 # 记忆沉淀模型（轻量）
  consolidation_api_key: ${ANTHROPIC_API_KEY}    # 沉淀 API Key
  max_context_tokens: 200000                     # 上下文窗口大小（token 数）
  context_threshold: 0.8                         # 上下文溢出阈值（占比 80%）

memory:
  history_max_turns: 20                          # 短期历史最大保留条数
  consolidation_threshold: 15                    # 记忆沉淀触发阈值（信息计数）
  retrieval_top_k: 5                             # 向量检索返回数量
  dedup_similarity_threshold: 0.85               # 去重相似度阈值
  memory_md_path: data/memory.md                 # 用户画像 memory.md 路径
  chroma_path: data/chroma                       # ChromaDB 持久化路径

server:
  host: 0.0.0.0                                  # 监听地址
  port: 8000                                     # 监听端口

storage:
  sqlite_path: data/sessions.db                  # SQLite 会话日志路径

tools:
  defer_loading_threshold: 20                    # 工具延迟加载阈值（超过则启用 stub）
  max_react_loops: 10                            # React 循环最大次数
```

### 配置项详解

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `llm.main_model` | `claude-sonnet-4-5-20250929` | 主对话使用的 Claude 模型 |
| `llm.consolidation_model` | `claude-haiku-4-5-20251001` | 记忆沉淀使用的轻量模型 |
| `llm.max_context_tokens` | `200000` | 上下文窗口大小，超出则触发裁剪 |
| `llm.context_threshold` | `0.8` | 上下文占比阈值（LLM 客户端 / Condenser 使用） |
| `memory.history_max_turns` | `20` | 短期历史 FIFO 上限 |
| `memory.consolidation_threshold` | `15` | 信息计数达到 15 条触发记忆沉淀 |
| `memory.retrieval_top_k` | `5` | 向量检索返回 Top-K 记忆 |
| `memory.dedup_similarity_threshold` | `0.85` | 相似度 >0.85 判定为重复记忆 |
| `tools.defer_loading_threshold` | `20` | 注册工具数 >20 时启用延迟加载 |
| `tools.max_react_loops` | `10` | React 循环最大迭代次数 |

---

## 7. 故障排查

### 7.1 服务无法启动

**现象**：`systemctl start hermes-lite` 失败或进程立即退出。

**排查步骤**：

```bash
# 查看详细错误日志
sudo journalctl -u hermes-lite -n 50 --no-pager

# 手动启动测试（绕过 systemd）
cd /opt/hermes-lite
source .venv/bin/activate
python -m uvicorn src.server:app --host 0.0.0.0 --port 8000
```

**常见原因**：
- Python 路径错误：`ExecStart` 中的 Python 路径不对，改用虚拟环境的 `/opt/hermes-lite/.venv/bin/python`
- 依赖未安装：重新执行 `pip install -r requirements.txt`
- 配置文件不存在：确认 `config.yaml` 在 `WorkingDirectory` 下

### 7.2 API Key 未配置

**现象**：日志中出现 `主对话 LLM API Key 未设置`。

**解决**：

```bash
# 确认环境变量已设置
echo $ANTHROPIC_API_KEY

# 或在 systemd service 中使用 EnvironmentFile
sudo tee -a /etc/hermes-lite/env > /dev/null <<EOF
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
EOF
# 然后修改 hermes-lite.service：
# EnvironmentFile=/etc/hermes-lite/env
sudo systemctl daemon-reload
sudo systemctl restart hermes-lite
```

### 7.3 sentence-transformers 模型下载失败

**现象**：`加载 sentence-transformers 模型失败`。

**解决**：

```bash
# 手动下载模型
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# 若网络受限，使用代理
export HTTPS_PROXY=http://your-proxy:port
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

### 7.4 ChromaDB 权限错误

**现象**：`写入 data/chroma 失败: Permission denied`。

**解决**：

```bash
sudo chown -R ec2-user:ec2-user /opt/hermes-lite/data
sudo chmod -R 755 /opt/hermes-lite/data
```

### 7.5 端口被占用

**现象**：`Address already in use: port 8000`。

**解决**：

```bash
# 查看占用端口的进程
sudo lsof -i :8000

# 终止占用进程
sudo kill -9 <PID>

# 或修改 config.yaml 中的 server.port
```

### 7.6 对话返回 500 错误

**现象**：`POST /chat` 返回 HTTP 500。

**排查**：

```bash
# 查看服务日志中的异常堆栈
sudo journalctl -u hermes-lite -n 100 --no-pager | grep -A 20 "Traceback"

# 常见原因：
# 1. Anthropic API 限流（429）→ 等待后重试
# 2. API Key 鉴权失败（401）→ 检查 ANTHROPIC_API_KEY
# 3. 模型名称错误 → 检查 config.yaml 中的 main_model
```

### 7.7 记忆沉淀未触发

**现象**：对话多次但长期记忆未增长。

**排查**：
- 确认对话轮次是否达到 `consolidation_threshold`（默认 15 条信息）
- 查看 ChromaDB 是否有写入权限
- 检查日志中是否有 `consolidation 执行失败` 的警告

### 7.8 测试验证

部署完成后，运行测试确认环境正常：

```bash
cd /opt/hermes-lite

# 语法验证
python -m py_compile src/*.py src/**/*.py

# 模块导入验证
python tests/test_imports.py

# 集成测试（mock 模式，不调真实 API）
python tests/test_integration.py
```

---

## 8. 目录结构

```
hermes-lite/
├── src/
│   ├── __init__.py
│   ├── config.py                 # 配置加载（解析 ${ENV_VAR}）
│   ├── orchestrator.py           # 主编排器
│   ├── server.py                 # FastAPI HTTP 服务
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── react_loop.py         # React 循环引擎
│   │   ├── tool_registry.py      # 工具注册中心（defer_loading）
│   │   └── builtin_tools.py      # 内置工具（文件/命令/HTTP/Plan 模式）
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py             # LLM 客户端（主对话 + consolidation）
│   │   └── prompts.py            # 系统提示词与沉淀 prompt
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── consolidation.py      # 记忆沉淀引擎
│   │   ├── memory_md.py          # 用户画像 memory.md 管理
│   │   ├── retrieval.py          # 记忆检索与注入
│   │   └── context_manager.py    # Prompt 上下文管理（前缀缓存优化）
│   └── storage/
│       ├── __init__.py
│       ├── chroma_store.py       # ChromaDB 长期记忆向量库
│       ├── history_buffer.py     # 短期对话历史（FIFO + 溢出降级）
│       └── sqlite_log.py         # SQLite 会话日志
├── tests/
│   ├── __init__.py
│   ├── _mock_deps.py             # 缺失依赖的 mock
│   ├── test_imports.py           # 模块导入验证
│   └── test_integration.py       # 端到端集成测试
├── data/                         # 运行时数据（自动创建）
├── config.yaml                   # 配置文件
├── requirements.txt              # Python 依赖
├── start.sh                      # 启动脚本
├── hermes-lite.service           # systemd 服务文件
├── DEPLOY.md                     # 本部署文档
└── README.md                     # 项目说明
```
