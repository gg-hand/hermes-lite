# 多 Agent 中间人功能补全设计

> 日期：2026-07-23
> 状态：待审核
> 范围：teage-liu 前端面板重构后的功能补全——让 Director 中间人真正可用

## 1. 背景与问题

前端面板重构（[2026-07-23-多agent前端面板重构](../superpowers/specs/2026-07-23-多agent前端面板重构-design.md)）已完成 UI 外壳，但中间人功能未接线。用户看到的工作台只有静态信息，无法实际发起协作。

### 1.1 现有后端基础设施（已实现）

| 模块 | 文件 | 功能 |
|------|------|------|
| Director 引擎 | `teage_liu/multiagent/director_engine.py` | 独立进程，1秒 tick 循环：更新心跳→检查 worker 心跳→检查轮次超时→flush pending 消息→仲裁冲突 |
| 黑板 | `teage_liu/multiagent/blackboard.py` | 文件系统协调：`append_message()` 写消息、`messages.pending.md`→`messages.md` flush 机制 |
| Agent 注册 | `teage_liu/multiagent/agent_registry.py` | `list_active_agents()` 返回 agents/{id}.md frontmatter，字段含 agent_id/status/last_heartbeat/role |
| API 端点 | `teage_liu/api/multiagent_routes.py` | 6 个 GET 端点（status/agents/messages/audit/director/sse），无 POST |
| SSE | `multiagent-sse.js` | 监听 6 种事件，`message_append` 时 dispatch `CustomEvent("multiagent-message")` 但无人监听 |

### 1.2 缺失的 5 个关键环节

| # | 缺失 | 根因 |
|---|------|------|
| 1 | 无 POST 端点提交任务 | 所有 multiagent API 都是 GET 只读 |
| 2 | @director 指令未处理 | `chat-core.js:sendMessage()` 无前缀检测，消息走普通 `/chat/stream` |
| 3 | "分派任务"按钮是死的 | `#wbDispatchBtn` 无 click 事件 |
| 4 | 协作消息不在聊天流展示 | SSE `message_append` dispatch 了事件但无代码监听 |
| 5 | 消息/审计记录不展示 | 后端有 `/messages` `/audit` 端点，工作台未调用 |

## 2. Director 引擎工作链路（研究结论）

### 2.1 消息流转机制

```
写入方 → messages.pending.md → Director tick flush → messages.md → /api/multiagent/messages
                                                    ↓
                                              SSE message_append 事件
```

- `append_message(bb_root, message)` 写入 `messages.md`（直接写，不等 Director）
- `messages.pending.md` 是待 flush 区，Director 每 tick 读取并 append 到 `messages.md`，然后清空 pending
- flush 幂等：通过 `op_id` 去重，重复 flush 不会重复写入

### 2.2 消息格式

`append_message` 接收 dict，序列化为 YAML frontmatter + body：

```yaml
---
op_id: <uuid>
from: user
type: task
content: "帮我对比这三个方案"
target_agents: ["researcher-01", "analyst-02"]
mode: dispatch
ts: 2026-07-23T10:30:00Z
---
帮我对比这三个方案
```

### 2.3 Director tick 循环（L304-320）

```python
async def _run_loop(self):
    while self._running:
        await asyncio.sleep(self._tick_interval)  # 1秒
        await self._update_director_tick()         # 更新 director.md last_director_tick
        await self._check_worker_heartbeats()      # 检查 worker 心跳
        await self._check_turn_timeout()           # 检查轮次超时
        await self._flush_pending_messages()       # flush pending → permanent
        await self._arbitrate_conflicts()          # 仲裁冲突
```

### 2.4 聊天发送流程（chat-core.js L1462-1491）

```javascript
async function sendMessage(textOverride) {
  const text = textOverride || messageInputEl.value.trim();
  if (!text) return;
  // ... 状态检查 ...
  appendMessage('user', text);          // L1477 添加用户消息
  // ... 设置流式 ...
  fetch(API_BASE + '/chat/stream', {     // L170 发送到 LLM
    method: 'POST',
    body: JSON.stringify({ session_id, message: userText }),
  });
}
```

**@director hook 点**：L1464 之后（验证非空）、L1477 之前（添加用户消息之前）。检测 `@director ` 前缀，走 dispatch 路径。

## 3. 设计方案

### 3.1 后端：POST /api/multiagent/dispatch 端点

在 `multiagent_routes.py` 的 `create_multiagent_router` 中新增：

```python
@router.post("/dispatch")
async def dispatch_task(payload: dict) -> dict:
    """提交任务到黑板，Director 引擎自动 pickup。

    请求体：
    {
        "task": "任务描述",
        "target_agents": ["agent_id_1"],  // 可选，空则广播
        "mode": "dispatch"                // 可选，dispatch/relay/debate
    }

    返回：
    {
        "ok": true,
        "op_id": "uuid",
        "message": "任务已提交到黑板"
    }
    """
```

**实现逻辑**：
1. 生成 `op_id = str(uuid.uuid4())`
2. 构造消息 dict：`{op_id, from: "user", type: "task", content: task, target_agents, mode, ts: now_iso}`
3. 调用 `append_message(bb_root, message)` 写入 `messages.md`（直接写，不等 Director flush）
4. 同时写入 `messages.pending.md`（让 Director 的 flush 机制也能感知）
5. 返回 `{ok: true, op_id}`

**为什么不直接写 pending 而是同时写两个**：`append_message` 写 `messages.md`（立即可见），`messages.pending.md` 的写入让 Director 的 flush + audit 链路完整记录。但为避免重复，只写 `messages.md` 即可——Director flush 是把 pending 移到 permanent，如果已经在 permanent 里就不需要走 pending。

**最终决策**：只调 `append_message()` 写入 `messages.md`。Director 的 `_flush_pending_messages` 只处理 pending 文件，不会重复处理已在 messages.md 中的消息。SSE 的轮询会在下次 `get_status()` 时检测到 messages 变化并推送 `message_append` 事件。

### 3.2 前端：@director 指令检测

在 `chat-core.js` 的 `sendMessage()` 函数中，L1464 之后插入检测：

```javascript
// @director 指令检测
if (text.startsWith('@director ')) {
  const task = text.slice('@director '.length).trim();
  if (!task) {
    showToast('请在 @director 后输入任务描述', 'warning');
    return;
  }
  return dispatchToDirector(task);
}
```

`dispatchToDirector(task)` 函数：
1. `appendMessage('user', text)` — 显示用户原始消息
2. 显示 collab 气泡："🎯 Director · 已接收任务"
3. `fetch('/api/multiagent/dispatch', { method: 'POST', body: { task } })`
4. 成功：更新 collab 气泡为"🎯 Director · 已分派，等待 agent 响应…"
5. 失败：更新 collab 气泡为"❌ 分派失败: <error>"

### 3.3 前端："分派任务"按钮

点击 `#wbDispatchBtn` → 弹出分派对话框（复用现有 modal 机制）：

```
┌─────────────────────────────────────┐
│  分派任务                       [×] │
├─────────────────────────────────────┤
│  任务描述                            │
│  ┌─────────────────────────────────┐ │
│  │  (textarea)                     │ │
│  └─────────────────────────────────┘ │
│                                      │
│  目标 Agent（可多选，空则广播）       │
│  ☐ researcher-01  ☐ analyst-02      │
│  ☐ writer-01      ☐ coder-01        │
│                                      │
│  协作模式                            │
│  ○ 分派（默认）  ○ 接力  ○ 辩论     │
│                                      │
│  [取消]              [提交分派]      │
└─────────────────────────────────────┘
```

- Agent 列表从 `/api/multiagent/agents` 获取
- 提交 → `POST /api/multiagent/dispatch` → 关闭对话框 → 在聊天流显示 collab 气泡

### 3.4 前端：协作消息展示

监听 SSE 的 `multiagent-message` 自定义事件，在聊天流插入紫色 collab 气泡：

```javascript
window.addEventListener('multiagent-message', function(e) {
  var data = e.detail || {};
  var messages = data.messages || (data.message ? [data.message] : []);
  messages.forEach(function(msg) {
    if (msg.type === 'task' || msg.type === 'result' || msg.type === 'relay') {
      appendCollabMessage(msg);
    }
  });
});
```

`appendCollabMessage(msg)` 渲染：
- `msg.from + " · " + msg.type` 作为 role 标签
- `msg.content` 作为气泡内容
- 紫色 `.msg.collab` 样式（已在 multiagent.css 中定义）

### 3.5 前端：消息记录展示

在 Agent 名册 tab 下方增加"消息记录"折叠区：
- 点击展开 → `fetch('/api/multiagent/messages?limit=50')` → 渲染消息时间线
- 每条消息显示：时间、from、type、content 摘要
- 默认折叠，避免信息过载

## 4. 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `teage_liu/api/multiagent_routes.py` | 改 | 新增 `POST /dispatch` 端点 |
| `web/static/js/chat-core.js` | 改 | `sendMessage()` 插入 @director 检测 + `dispatchToDirector()` 函数 |
| `web/static/js/chat-main.js` | 改 | 绑定 `#wbDispatchBtn` click → 打开分派对话框；监听 `multiagent-message` 事件 |
| `web/chat.html` | 改 | 新增分派对话框 modal 骨架 |
| `web/css/multiagent.css` | 改 | 新增分派对话框样式、消息记录样式 |
| `web/js/multiagent-render.js` | 改 | 新增 `renderMessageLog()` 渲染消息记录 |

## 5. 数据流

### 5.1 @director 指令流

```
用户输入 "@director 帮我对比方案"
  → chat-core.js sendMessage() 检测 @director 前缀
  → dispatchToDirector("帮我对比方案")
  → appendMessage('user', "@director 帮我对比方案")  // 显示用户消息
  → appendCollabMessage("🎯 Director · 已接收任务")
  → POST /api/multiagent/dispatch { task: "帮我对比方案" }
  → 后端 append_message() 写入 messages.md
  → SSE 轮询检测到消息变化 → message_append 事件
  → 前端 multiagent-message 监听器 → appendCollabMessage(分派结果)
```

### 5.2 分派按钮流

```
用户点击 #wbDispatchBtn
  → 打开分派对话框
  → fetch /api/multiagent/agents → 填充 agent 多选列表
  → 用户填写任务 + 选择 agent + 选择模式
  → 点击"提交分派"
  → POST /api/multiagent/dispatch { task, target_agents, mode }
  → 关闭对话框
  → appendCollabMessage("🎯 Director · 已分派给 researcher-01, analyst-02")
```

### 5.3 协作消息展示流

```
Director/Worker 写入 messages.md
  → SSE get_status() 检测到消息变化
  → SSE 推送 message_append 事件
  → multiagent-sse.js dispatch CustomEvent("multiagent-message")
  → chat-main.js 监听器接收
  → appendCollabMessage(msg) → 紫色气泡插入聊天流
```

## 6. 交互细节

### 6.1 @director 指令
- 前缀检测：`text.startsWith('@director ')`（注意空格）
- 大小写不敏感：也检测 `@Director` `@DIRECTOR`
- 无任务内容时：toast 提示"请在 @director 后输入任务描述"
- 分派成功：collab 气泡显示"🎯 Director · 已接收任务，等待 agent 响应…"
- 分派失败：collab 气泡显示"❌ 分派失败: <error>"，toast 告警

### 6.2 分派对话框
- Agent 列表为空时：显示"暂无可用 agent"提示，但仍可提交（广播模式）
- 提交时任务为空：禁用提交按钮
- 提交后自动关闭对话框
- 对话框复用现有 `.modal-overlay` 样式

### 6.3 协作消息气泡
- 不同 type 用不同前缀图标：
  - task: 🎯 Director · 分派
  - result: ✅ Agent · 结果
  - relay: 🔁 Agent · 接力
  - info: ℹ️ 系统
- 气泡点击可展开完整内容（长消息默认截断）

### 6.4 消息记录
- 默认折叠（"消息记录 ▸"）
- 展开后显示最近 50 条
- 每条：`[时间] from · type: content摘要`
- 可滚动

## 7. 边界与错误处理

1. **multiagent 未启用**：POST /dispatch 返回 404（现有逻辑：enabled=false 时所有端点返回 404）；前端 @director 检测到 404 时 toast 提示"多 agent 未启用，请在设置中开启"
2. **Director 未运行**：消息仍写入 messages.md（append_message 不依赖 Director），Director 启动后会看到历史消息。但实时分派不会发生——collab 气泡提示"Director 未运行，任务已暂存"
3. **无 agent 在线**：POST /dispatch 仍成功（消息写入黑板），但 collab 气泡提示"当前无在线 agent，任务已广播"
4. **网络错误**：fetch 失败时 collab 气泡显示错误，不阻塞聊天
5. **SSE 断连**：复用现有重连机制，断连期间不更新协作消息，重连后自动刷新

## 8. 不做的事（YAGNI）

- 不实现 Director 的任务拆解逻辑（那是 Director 引擎的职责，已由后端实现）
- 不实现 worker 执行逻辑（worker_adapter.py 已有）
- 不做实时协作进度条（SSE message_append 足够）
- 不做消息编辑/删除（黑板消息不可变）
- 不重构现有 multiagent-sse.js 和 multiagent-settings.js
- 不改 Director 引擎的 tick 间隔或 flush 逻辑

## 9. 测试要点

1. **POST /dispatch**：提交任务 → messages.md 出现新消息 → op_id 返回
2. **@director 检测**：输入 `@director 对比方案` → 走 dispatch 路径，不走 /chat/stream
3. **非 @director 消息**：普通消息仍走 /chat/stream，不受影响
4. **分派按钮**：点击 → 对话框打开 → 选 agent → 提交 → 对话框关闭 → collab 气泡出现
5. **协作消息**：SSE message_append → 紫色气泡插入聊天流
6. **消息记录**：点击展开 → 显示消息列表 → 可滚动
7. **未启用态**：multiagent 未启用时 @director 提示"未启用"
8. **Director 未运行**：任务仍写入黑板，气泡提示"Director 未运行"
