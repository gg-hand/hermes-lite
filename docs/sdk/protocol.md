# Teage Multi-Agent Protocol 摘要

## 协议版本
v1.0.3

## 核心概念

### 黑板（Blackboard）
- 文件系统作为消息中介
- 路径：`data/blackboard/`
- 关键文件：
  - `messages.md`：主对话消息
  - `collaboration.md`：全局协作消息
  - `collabs/{collab_id}.md`：单协作消息
  - `agents/{agent_id}.md`：agent 注册信息
  - `director.md`：Director 状态
  - `status.json`：全局状态

### 路径规范
- 所有协议文件中路径必须为相对路径
- 外部 agent 通过 `BB_ROOT` 变量拼接路径
- 禁止绝对路径（如 `/`、`C:\`）

## 消息格式

### 必填字段
```yaml
seq: 1                    # 自增序号（服务端自动分配）
from: agent_alice         # 发送者 agent_id
type: relay               # 消息类型
content: "消息内容"        # 内容（announce/status 外必填）
```

### from 字段规则
- 正则：`^[a-zA-Z0-9_]{3,32}$|^(user|director)$`
- 特殊值：`user`（用户消息）、`director`（Director 消息）

### 消息类型枚举
`chat` / `system` / `broadcast` / `action` / `error` / `task` / `assign` / `status` / `result` / `relay` / `request` / `response` / `directive` / `announce`

### 任务相关字段
```yaml
task_op_id: "uuid"        # 任务幂等 ID
assigned_to: "agent_id"   # 被分派的 agent
status: "processing"      # 任务状态
target_agents: ["a", "b"] # 目标 agent 列表
mode: "dispatch"          # 协作模式
```

## 安全机制

### ed25519 签名
- 所有写操作必须签名
- 公钥位置：`agents/keys/{agent_id}.pem`
- 签名内容：消息 dict（移除 signature 字段）的 canonical JSON
- 签名算法：ed25519

### 路径沙箱
- 拒绝绝对路径
- 拒绝路径穿越（`..`）
- read_file 受白名单限制

### 限流
- 默认 100 req/s per IP
- 超限返回 -32003

## 完整协议规范
详见 `docs/superpowers/specs/2026-07-20-多agent协作机制-design.md`
