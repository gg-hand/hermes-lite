# 多 Agent 完善交流链路优化设计

> 日期：2026-07-23
> 状态：待审核
> 范围：让 Director 中间人协作链路端到端打通 + 用户可见全链路反馈

## 1. 背景与问题

前端面板重构和功能补全已完成 @director 指令检测和 POST /dispatch 端点，但协作链路断裂：

- **Director 引擎未运行**：任务写入 messages.md 后无人处理，任务消失在黑洞中
- **无 worker 活跃**：worker_001.md 心跳已过期，无 agent 实际执行任务
- **反馈缺失**：collab 气泡说"已分派"但永远没有后续，用户无法知道任务是否被处理
- **无任务状态追踪**：用户无法看到任务的 pending → assigned → processing → completed 流转

## 2. 整体架构

核心原则：**Director 是独立实体，teage-liu 只是控制面板**。通过 DirectorManager 抽象层实现"现在本地托管、未来独立部署"的扩展能力。

```
┌─────────────────────────────────────────────────────────┐
│  teage-liu 主服务器                                      │
│                                                         │
│  前端(控制面板) ──→ API 层 ──→ DirectorManager(抽象层)    │
│       ↑                          ├─ LocalManager(当前)   │
│       │                          └─ RemoteManager(扩展)  │
│  SSE 时间线 ←── Blackboard ←──────┘                      │
│                    (文件系统)                             │
└────────────────────────┬────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          │                             │
   ┌──────┴──────┐              ┌───────┴───────┐
   │  Director   │              │   Workers     │
   │  独立进程    │              │  外部 agent    │
   │  调度/指挥   │              │  执行任务      │
   └─────────────┘              └───────────────┘
```

### 2.1 DirectorManager 抽象层

```python
class DirectorManager(ABC):
    """Director 生命周期管理抽象层。"""

    @abstractmethod
    async def start(self) -> dict: ...      # 启动 Director

    @abstractmethod
    async def stop(self) -> dict: ...       # 停止 Director

    @abstractmethod
    async def status(self) -> dict: ...     # 查询运行状态

    @abstractmethod
    async def restart(self) -> dict: ...    # 重启 Director
```

**LocalDirectorManager**（当前实现）：
- `start()`：spawn `python -m teage_liu.multiagent.director_cli` 子进程，记录 PID
- `stop()`：发 SIGTERM，等 5 秒，未退出则 SIGKILL
- `status()`：检查进程存活 + 读 director.md 心跳时间戳
- `restart()`：stop() + start()

**RemoteDirectorManager**（未来扩展，本次不实现）：
- 通过 HTTP 连接独立部署的 Director 服务
- 配置：`multiagent.director_mode: remote`，`multiagent.director_endpoint: http://director-host:port`

**配置开关**：
```yaml
multiagent:
  enabled: true
  blackboard_dir: data/blackboard
  director_mode: local  # local | remote（remote 本次预留不实现）
  director_endpoint: "" # 仅 remote 模式使用
```

### 2.2 新增 API 端点

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/multiagent/director/start` | POST | 启动 Director 进程 |
| `/api/multiagent/director/stop` | POST | 停止 Director 进程 |
| `/api/multiagent/director/status` | GET | Director 运行状态 + 心跳 |
| `/api/multiagent/tasks/{op_id}` | GET | 查询指定任务的状态流转 |
| `/api/multiagent/agents/register` | POST | 远程 agent 注册（写 agent_card.md） |
| `/api/multiagent/agents/{id}/heartbeat` | POST | 远程 agent 心跳更新 |

### 2.3 数据流

```
1. 前端点"启动 Director" → POST /director/start → LocalDirectorManager.start() → spawn 子进程
2. 用户 @director 任务 → POST /dispatch → 写入 blackboard messages.md (status: pending)
3. Director tick 读取 messages.md → 分派给 worker → 写 messages.md (status: assigned)
4. Worker 执行任务 → 写 messages.md (status: processing → completed)
5. SSE 轮询 blackboard 变化 → 推送 task_status / agent_message 事件
6. 前端收到事件 → 更新折叠时间线 → 用户看到完整链路
```

## 3. Director 生命周期管理

### 3.1 LocalDirectorManager 实现

**进程启动**：
```python
import subprocess, sys, os

class LocalDirectorManager(DirectorManager):
    def __init__(self, bb_root: str, config: dict):
        self._bb_root = bb_root
        self._config = config
        self._process: subprocess.Popen | None = None
        self._pid_file = os.path.join(bb_root, "director.pid")

    async def start(self) -> dict:
        # 已在运行则返回
        if await self._is_running():
            return {"ok": True, "message": "Director 已在运行", "pid": self._process.pid}

        # spawn 子进程
        cmd = [sys.executable, "-m", "teage_liu.multiagent.director_cli"]
        env = {**os.environ, "HERMES_BB_DIR": self._bb_root}
        self._process = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000  # Windows CREATE_NO_WINDOW
        )
        # 写 PID 文件
        with open(self._pid_file, "w") as f:
            f.write(str(self._process.pid))

        return {"ok": True, "message": "Director 已启动", "pid": self._process.pid}

    async def stop(self) -> dict:
        if not await self._is_running():
            return {"ok": True, "message": "Director 未运行"}

        pid = self._process.pid if self._process else self._read_pid_file()
        if pid:
            # SIGTERM → 等5秒 → SIGKILL
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not await self._is_running():
                    break
                await asyncio.sleep(0.1)
            if await self._is_running():
                os.kill(pid, signal.SIGKILL)

        self._process = None
        if os.path.exists(self._pid_file):
            os.remove(self._pid_file)

        return {"ok": True, "message": "Director 已停止"}

    async def status(self) -> dict:
        running = await self._is_running()
        # 读 director.md 心跳
        heartbeat = await self._read_director_heartbeat()
        return {
            "running": running,
            "pid": self._process.pid if self._process else None,
            "last_heartbeat": heartbeat,
            "healthy": running and heartbeat is not None,
        }
```

**崩溃恢复**：
- `status()` 检测到进程不在但 PID 文件存在 → 标记为 crashed
- 前端显示"Director 已崩溃"，提供"重启"按钮
- 不自动重启（避免崩溃循环），由用户决定

**服务器关闭时清理**：
- 主服务 lifespan shutdown 时调用 `director_manager.stop()`
- 避免僵尸 Director 进程

### 3.2 前端控制

工作台 header 增加 Director 控制区：

```
┌─────────────────────────────────────────┐
│  Director 工作台                        │
│  ● 运行中 (PID: 12345)  [停止] [重启]   │
│  ── 或 ──                               │
│  ○ 未运行  [启动 Director]              │
└─────────────────────────────────────────┘
```

- 绿色 ● = 运行中且心跳正常
- 黄色 ● = 运行中但心跳过期（degraded）
- 灰色 ○ = 未运行
- 红色 ● = 已崩溃（PID 文件存在但进程不在）

**@director 时的状态检查**：
- dispatchToDirector() 发送前先查 `/api/multiagent/director/status`
- 如果 Director 未运行：collab 气泡提示"Director 未运行，任务已暂存到黑板。是否启动 Director？"并提供"启动"按钮
- 如果 Director 运行中：collab 气泡正常显示"已分派，等待 agent 响应…"

## 4. 任务生命周期与状态追踪

### 4.1 任务状态机

```
pending → assigned → processing → completed
                                    → failed
                                    → timeout
```

| 状态 | 含义 | 谁写入 |
|------|------|--------|
| pending | 任务已提交，等待 Director 分派 | 用户（dispatch API） |
| assigned | Director 已分派给指定 worker | Director 引擎 |
| processing | Worker 开始执行 | Worker |
| completed | Worker 完成并返回结果 | Worker |
| failed | Worker 执行失败 | Worker |
| timeout | 任务超时未完成 | Director 引擎 |

### 4.2 状态存储与推断

状态通过 messages.md 中的消息记录追踪。**不修改 director_engine.py**，由后端 API 层根据消息类型和内容推断状态：

**推断规则**（在 `GET /tasks/{op_id}` 和 SSE 事件中应用）：
- 消息 `type: task` + `from: user` → 状态 `pending`
- 消息 `type: status` + `from: director` → 状态 `assigned`
- 消息 `type: status` + `from: worker-*` → 状态 `processing`
- 消息 `type: result` + `from: worker-*` → 状态 `completed`（content 含"失败"/"error"则 `failed`）
- 无新消息超过 timeout 阈值 → 状态 `timeout`

如果现有 Director/worker 未写 `status` 字段，后端按上述规则从 `type` + `from` 推断。未来 Director/worker 可主动写 `status` 字段以支持更精确的状态。

消息示例（理想格式，含 status 字段）：

```yaml
# 任务提交（用户）
---
op_id: <uuid>
from: user
type: task
status: pending
content: "帮我对比三个方案"
target_agents: []
mode: dispatch
ts: 2026-07-23T10:30:00Z
---

# Director 分派
---
op_id: <uuid>  # 同一 op_id
from: director
type: status
status: assigned
assigned_to: researcher-01
ts: 2026-07-23T10:30:05Z
---
分派给 researcher-01

# Worker 处理中
---
op_id: <uuid>
from: researcher-01
type: status
status: processing
ts: 2026-07-23T10:30:10Z
---
开始分析...

# Worker 完成
---
op_id: <uuid>
from: researcher-01
type: result
status: completed
ts: 2026-07-23T10:31:00Z
---
方案A最优，因为...
```

### 4.3 状态查询 API

`GET /api/multiagent/tasks/{op_id}`：

```python
@router.get("/tasks/{op_id}")
async def get_task_status(op_id: str) -> dict:
    """查询指定任务的状态流转历史。"""
    messages = await read_messages(bb_root)
    task_msgs = [m for m in messages if m.get("op_id") == op_id]
    if not task_msgs:
        raise HTTPException(404, "任务不存在")

    latest = task_msgs[-1]
    return {
        "op_id": op_id,
        "status": latest.get("status", "unknown"),
        "assigned_to": latest.get("assigned_to"),
        "timeline": [
            {
                "ts": m.get("ts"),
                "from": m.get("from"),
                "type": m.get("type"),
                "status": m.get("status"),
                "content": (m.get("content") or "")[:200],
            }
            for m in task_msgs
        ],
    }
```

返回示例：
```json
{
  "op_id": "a21f9c81...",
  "status": "completed",
  "assigned_to": "researcher-01",
  "timeline": [
    {"ts": "10:30:00", "from": "user", "type": "task", "status": "pending", "content": "帮我对比三个方案"},
    {"ts": "10:30:05", "from": "director", "type": "status", "status": "assigned", "content": "分派给 researcher-01"},
    {"ts": "10:30:10", "from": "researcher-01", "type": "status", "status": "processing", "content": "开始分析..."},
    {"ts": "10:31:00", "from": "researcher-01", "type": "result", "status": "completed", "content": "方案A最优..."}
  ]
}
```

## 5. SSE 时间线与折叠消息

### 5.1 SSE 事件增强

现有 SSE 轮询 `/api/multiagent/status`，检测到变化时推送事件。新增两种事件类型：

**task_status 事件**（任务状态变更）：
```json
{
  "type": "task_status",
  "op_id": "a21f9c81...",
  "status": "assigned",
  "from": "director",
  "assigned_to": "researcher-01",
  "ts": "2026-07-23T10:30:05Z",
  "content": "分派给 researcher-01"
}
```

**agent_message 事件**（agent 消息）：
```json
{
  "type": "agent_message",
  "op_id": "a21f9c81...",
  "from": "researcher-01",
  "msg_type": "result",
  "status": "completed",
  "content": "方案A最优，因为...",
  "ts": "2026-07-23T10:31:00Z"
}
```

**SSE 检测逻辑**（在现有 `get_status()` 轮询中）：
- 记录上次轮询时 messages.md 的消息数量
- 新增消息时，对每条新消息 dispatch 对应事件
- task 类型 + status 字段 → `task_status` 事件
- result/status 类型 → `agent_message` 事件

### 5.2 前端折叠时间线

每个 @director 任务在聊天流中创建一个**任务块**（而非多条独立气泡）：

```
┌──────────────────────────────────────────────────┐
│  🎯 Director · 帮我对比三个方案  [✅ 已完成]  ▸  │  ← 折叠态
└──────────────────────────────────────────────────┘

点击展开后：
┌──────────────────────────────────────────────────┐
│  🎯 Director · 帮我对比三个方案  [✅ 已完成]  ▾  │
│  ┌────────────────────────────────────────────┐  │
│  │ 10:30:00 · user · 提交任务 (pending)        │  │
│  │ 10:30:05 · director · 分派给 researcher-01  │  │
│  │ 10:30:10 · researcher-01 · 处理中...        │  │
│  │ 10:31:00 · researcher-01 · 方案A最优...     │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

**状态徽章颜色**：
- ⏳ pending — 灰色
- 📤 assigned — 蓝色
- ⚙️ processing — 橙色（带脉冲动画）
- ✅ completed — 绿色
- ❌ failed — 红色
- ⏱️ timeout — 红色

**渲染逻辑**：
1. `dispatchToDirector()` 成功后创建任务块（op_id 关联），初始状态 pending
2. 监听 `task_status` 事件 → 更新对应任务块的状态徽章 + 添加时间线条目
3. 监听 `agent_message` 事件 → 添加时间线条目
4. 默认折叠，状态变更时徽章闪烁高亮提示，用户手动点击展开查看详情

**数据结构**（前端）：
```javascript
// 任务块注册表
var taskBlocks = {};  // { op_id: { el, status, timeline: [] } }

function createTaskBlock(op_id, taskText) {
  // 创建折叠任务块 DOM 元素
  // 存入 taskBlocks[op_id]
}

function updateTaskBlock(op_id, event) {
  // 根据 event.type 更新状态徽章和时间线
}
```

## 6. Worker 接入

### 6.1 现有机制（保留）

- Worker 将 `agent_card.md` 放入 `blackboard/agents/` 目录
- Director 检测新 agent，开始心跳监控
- Worker 定期更新 agent_card.md 的 `last_heartbeat` 字段

### 6.2 新增 API 接入（远程 worker）

对于无法直接访问文件系统的远程 agent，提供 API 代理：

**POST /api/multiagent/agents/register**：
```json
// 请求
{
  "agent_id": "remote-researcher",
  "role": "worker",
  "capabilities": ["research", "analysis"],
  "endpoint": "http://remote-server:9000",
  "auth_method": "local"
}

// 响应
{ "ok": true, "message": "已注册" }
```
后端将信息写入 `blackboard/agents/{agent_id}.md`。

**POST /api/multiagent/agents/{id}/heartbeat**：
```json
// 请求
{ "status": "active", "current_task": "a21f9c81..." }

// 响应
{ "ok": true, "next_heartbeat_due": 10 }
```
后端更新 `blackboard/agents/{id}.md` 的 `last_heartbeat` 字段。

### 6.3 Agent 名册增强

工作台 Agent 名册 tab 显示：
- agent_id、role、status（active/degraded/offline）
- 最后心跳时间
- capabilities
- endpoint（远程 agent 显示 URL，本地 agent 显示 PID）
- 当前执行的任务（如果有）

## 7. 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `teage_liu/multiagent/director_manager.py` | 新建 | DirectorManager 抽象层 + LocalDirectorManager |
| `teage_liu/api/multiagent_routes.py` | 改 | 新增 director/start/stop/status、tasks/{op_id}、agents/register、agents/{id}/heartbeat 端点 |
| `teage_liu/server.py` | 改 | lifespan shutdown 时调用 director_manager.stop() |
| `config.yaml` | 改 | 新增 director_mode、director_endpoint 配置项 |
| `web/static/js/chat-core.js` | 改 | dispatchToDirector 增加 Director 状态检查；createTaskBlock/updateTaskBlock 折叠时间线 |
| `web/static/js/chat-main.js` | 改 | Director 启停按钮绑定；task_status/agent_message 事件监听 |
| `web/chat.html` | 改 | 工作台 header 增加 Director 控制区 |
| `web/css/multiagent.css` | 改 | 任务块样式、状态徽章、折叠时间线样式 |
| `web/js/multiagent-sse.js` | 改 | SSE 增加 task_status、agent_message 事件 dispatch |

## 8. 不做的事（YAGNI）

- 不实现 RemoteDirectorManager（本次只预留接口和配置开关）
- 不实现 worker 的任务执行逻辑（worker_adapter.py 已有，由 worker 自身运行）
- 不实现 Director 引擎的内部调度逻辑修改（director_engine.py 不改）
- 不做任务优先级队列（YAGNI，当前需求是基本链路打通）
- 不做消息加密/签名验证（signature.py 已有，本次不集成）
- 不做跨设备黑板同步（当前是本地文件系统，网络共享是部署层面的事）

## 9. 边界与错误处理

1. **Director 启动失败**：返回 500 + 错误信息；前端 toast 提示"Director 启动失败: <error>"
2. **Director 崩溃**：status() 检测进程不在 → 前端红色 ● + "重启"按钮
3. **Director 已在运行**：start() 返回 `{ok: true, message: "Director 已在运行"}`
4. **任务无 worker 响应**：Director 超时机制处理（现有 turn_manager.py 有超时逻辑），前端时间线显示 timeout 状态
5. **SSE 断连**：复用现有重连机制；重连后重新查询所有进行中任务的状态
6. **远程 agent 注册冲突**：agent_id 已存在返回 409 Conflict
7. **服务器关闭**：lifespan shutdown 调用 director_manager.stop()，避免僵尸进程

## 10. 测试要点

1. **Director 启停**：POST /director/start → 进程启动 → status 返回 running:true → POST /director/stop → 进程退出
2. **Director 崩溃恢复**：手动 kill 进程 → status 返回 crashed → 前端显示重启按钮
3. **任务全链路**：dispatch → pending → assigned → processing → completed → 前端时间线完整展示
4. **@director 状态检查**：Director 未运行时 @director → 提示"未运行" + 启动按钮
5. **折叠时间线**：任务块默认折叠 → 点击展开 → 显示完整时间线 → 状态徽章颜色正确
6. **远程 agent 注册**：POST /agents/register → agent_card.md 创建 → 名册显示新 agent
7. **SSE 事件**：任务状态变更 → SSE 推送 task_status → 前端更新任务块
8. **服务器关闭清理**：关闭服务器 → Director 进程被停止 → 无僵尸进程
