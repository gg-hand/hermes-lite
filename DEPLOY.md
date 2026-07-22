# Teage Liu 部署文档

本文档描述 Teage Liu 个人长期 AI Agent 的环境要求、安装步骤、本地运行、EC2 部署、API 接口、配置说明与故障排查。

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
git clone <your-repo-url> teage-liu
cd teage-liu
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
sudo mkdir -p /opt/teage-liu
sudo chown ec2-user:ec2-user /opt/teage-liu
cd /opt/teage-liu
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
sudo tee /etc/teage-liu/env > /dev/null <<EOF
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx
EOF
sudo chmod 600 /etc/teage-liu/env
sudo chown ec2-user:ec2-user /etc/teage-liu/env
```

### 4.6 配置 systemd 服务

将项目根目录下的 `teage-liu.service` 复制到 systemd 目录：

```bash
sudo cp /opt/teage-liu/teage-liu.service /etc/systemd/system/teage-liu.service
```

`teage-liu.service` 内容：

```ini
[Unit]
Description=Teage Liu Personal AI Agent
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/teage-liu
Environment=ANTHROPIC_API_KEY=your_api_key_here
ExecStart=/usr/bin/python3 -m uvicorn src.server:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> **安全建议**：生产环境建议使用 `EnvironmentFile=/etc/teage-liu/env` 替代 `Environment=ANTHROPIC_API_KEY=...`，避免 API Key 明文出现在 service 文件中：
>
> ```ini
> EnvironmentFile=/etc/teage-liu/env
> ExecStart=/opt/teage-liu/.venv/bin/python -m uvicorn src.server:app --host 0.0.0.0 --port 8000 --workers 1
> ```

### 4.7 启动服务

```bash
# 重新加载 systemd 配置
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start teage-liu

# 设置开机自启
sudo systemctl enable teage-liu

# 查看服务状态
sudo systemctl status teage-liu
```

### 4.8 查看日志

```bash
# 实时查看服务日志
sudo journalctl -u teage-liu -f

# 查看最近 100 行日志
sudo journalctl -u teage-liu -n 100
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
  "response": "你好！我是 Teage Liu...",
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

## 7. AI 护栏（AI Guardrails）

Teage Liu 在 Phase 9 引入 AI 护栏工程，构建多层防御以应对 Prompt 注入、工具滥用与 PII 泄漏。护栏与既有 L5 PolicyEngine（白名单/审批/路径门控）**并存**：PolicyEngine 守护「工具能否被调用」，护栏守护「输入是否可信、输出是否安全」。

### 7.1 护栏架构概述

护栏由 `GuardrailEngine`（`src/guardrails/engine.py`）统一编排，按 LLM 调用生命周期分层：

| 层级 | 组件 | 时机 | 职责 |
|------|------|------|------|
| **L1 输入扫描** | `InjectionGuard` | 接收用户输入后 | 检测 prompt 注入模式（越狱模板、角色扮演劫持、指令覆盖），按策略 `block / warn / off` 处置 |
| **L3 工具脱敏** | `ToolOutputSanitizer` | 工具返回结果回填 LLM 前 | 对非白名单工具输出执行 PII 脱敏 + 长度截断，阻止 PII 通过工具结果回流 |
| **L4 输出过滤** | `OutputFilter` | LLM 流式输出后 | 检测并脱敏手机号、邮箱、身份证、银行卡、IP 等 PII，向前端补发 `output_filtered` 事件 |
| **L5 PolicyEngine** | `PolicyEngine`（已有） | 工具调用前 | 路径白名单、危险操作审批（与护栏职责互补，不重叠） |

> L1 / L3 / L4 由 `GuardrailEngine.from_config(config)` 装配；L5 由 `SecurityManager` 装配，独立运行。

### 7.2 配置项说明

`config.yaml` 中的 `guardrails` 段：

```yaml
guardrails:
  input_scan:
    enabled: true
    action: warn  # block(拦截) / warn(放行+告警,默认) / off(跳过)
  sanitizer:
    enabled: true
    trusted_tools:
      - memory_search
      - search_memory
      - memory_query
    max_output_length: 20000
  output_filter:
    enabled: true
    enable_bank_card: true
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `input_scan.enabled` | bool | `true` | 是否启用 L1 输入扫描 |
| `input_scan.action` | enum | `warn` | 检测到注入时的处置：`block`（拒绝并返回安全提示）/ `warn`（放行但记录告警）/ `off`（仅跳过扫描） |
| `sanitizer.enabled` | bool | `true` | 是否启用 L3 工具脱敏 |
| `sanitizer.trusted_tools` | list | 见示例 | 信任工具列表，输出不脱敏（仅做长度截断）；其余工具输出经 PII 脱敏 |
| `sanitizer.max_output_length` | int | `20000` | 工具输出最大字符数，超出截断 |
| `output_filter.enabled` | bool | `true` | 是否启用 L4 输出过滤 |
| `output_filter.enable_bank_card` | bool | `true` | 是否启用银行卡 PII 检测（与 13~19 位长数字易混淆，按需关闭） |

### 7.3 PII 误报权衡

PII 检测器基于正则 + 校验位算法，遵循「**脱敏优于漏报**」原则：

- **11 位数字**：可能被误匹配为手机号（如订单号、流水号）。误报代价是脱敏为 `[PHONE]`，用户可见；漏报代价是真实手机号泄漏，不可逆。
- **18 位身份证**：含校验位，误报率低；但若与 18 位订单号冲突，可关闭对应检测器。
- **13~19 位银行卡**：依赖 Luhn 校验，误报率较低；如业务无卡号场景，可设 `enable_bank_card: false`。
- **邮箱 / IP**：纯正则匹配，存在少量边界误报，影响小。

> 误报是已知权衡，前端会通过 `output_filtered` 事件提示用户「输出已被脱敏」，便于人工复核。

### 7.4 流式中断限制

L4 输出过滤依赖「先缓冲完整段、再脱敏、再下发」的流式策略：

- **正常流**：分段下发，每段经过 PII 检测后补发 `output_filtered` 事件，前端可替换展示。
- **流式中断**（用户主动停止 / 异常断连）：未刷出的段不会触发 `output_filtered` 事件，前端可能展示原始片段。但 `ConsolidationEngine` 写入长期记忆的文本仍以**脱敏后版本**为准（护栏作用于 `react_loop` 与 `orchestrator` 写入路径），不会因前端展示差异导致 PII 落库。
- **建议**：前端收到中断事件后，主动向后端请求该消息的最终（脱敏）版本，避免展示残留原文。

### 7.5 注入 patterns 维护建议

L1 注入检测基于模式列表（关键词 + 结构模板），随攻击手法演化需定期更新：

- **更新频率**：建议每季度 review 一次，或重大模型升级（如 Claude / DeepSeek 版本切换）后立即评估。
- **来源**：参考 OWASP LLM Top 10、各厂商红队披露的越狱模板、社区公开 jailbreak 集合。
- **回滚机制**：模式变更只影响检测灵敏度，`action=warn` 模式下不会阻断业务，可灰度上线。
- **最终防线**：**工具调用门（L5 PolicyEngine + SYSTEM_PROMPT 工具策略）**是最终防线 —— 即使 L1 漏检注入，PolicyEngine 仍会拦截未授权工具调用，SYSTEM_PROMPT 仍会拒绝危险指令。护栏各层不互相依赖，单层失效不致整体失守。

### 7.6 热更新说明

`guardrails.*` 配置变更**需要重启服务**才能生效，原因：

- `GuardrailEngine.from_config(config)` 在启动时构建一次，运行时不重新装配（避免每次请求重建检测器的开销）。
- 与 `security.rules` / `security.enabled` 行为一致：均涉及检测器实例重建，统一走重启路径。

修改 `config.yaml` 中的 `guardrails` 段后：

```bash
sudo systemctl restart teage-liu
```

或通过 `/config/reload` 接口提交配置时，`_RESTART_REQUIRED_KEYS` 会判定 `guardrails` 变更需重启，返回 `needs_restart: true`，前端可提示用户重启。

---

## 8. 故障排查

### 8.1 服务无法启动

**现象**：`systemctl start teage-liu` 失败或进程立即退出。

**排查步骤**：

```bash
# 查看详细错误日志
sudo journalctl -u teage-liu -n 50 --no-pager

# 手动启动测试（绕过 systemd）
cd /opt/teage-liu
source .venv/bin/activate
python -m uvicorn src.server:app --host 0.0.0.0 --port 8000
```

**常见原因**：
- Python 路径错误：`ExecStart` 中的 Python 路径不对，改用虚拟环境的 `/opt/teage-liu/.venv/bin/python`
- 依赖未安装：重新执行 `pip install -r requirements.txt`
- 配置文件不存在：确认 `config.yaml` 在 `WorkingDirectory` 下

### 8.2 API Key 未配置

**现象**：日志中出现 `主对话 LLM API Key 未设置`。

**解决**：

```bash
# 确认环境变量已设置
echo $ANTHROPIC_API_KEY

# 或在 systemd service 中使用 EnvironmentFile
sudo tee -a /etc/teage-liu/env > /dev/null <<EOF
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
EOF
# 然后修改 teage-liu.service：
# EnvironmentFile=/etc/teage-liu/env
sudo systemctl daemon-reload
sudo systemctl restart teage-liu
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

### 8.4 ChromaDB 权限错误

**现象**：`写入 data/chroma 失败: Permission denied`。

**解决**：

```bash
sudo chown -R ec2-user:ec2-user /opt/teage-liu/data
sudo chmod -R 755 /opt/teage-liu/data
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
sudo journalctl -u teage-liu -n 100 --no-pager | grep -A 20 "Traceback"

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
cd /opt/teage-liu

# 语法验证
python -m py_compile src/*.py src/**/*.py

# 模块导入验证
python tests/test_imports.py

# 集成测试（mock 模式，不调真实 API）
python tests/test_integration.py
```

---

## 9. 目录结构

```
teage-liu/
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
├── teage-liu.service           # systemd 服务文件
├── DEPLOY.md                     # 本部署文档
└── README.md                     # 项目说明
```
