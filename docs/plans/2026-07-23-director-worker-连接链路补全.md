# Director-Worker 连接链路补全 Implementation Plan (v3)

> **For agentic workers:** Use the java-dev-skills workflow to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Director 能自动发现本地 worker 并分派任务，让 Worker 能拾取任务并通过复用现有 Orchestrator 执行，最终打通端到端的协作链路。所有消息严格遵守 messages_md schema，支持本地/远程 agent 通用通信。v3 修复 v2 遗漏的 get_task_status 端点字段不匹配、A2A Gateway 未启用 schema 验证、Worker 重启后 _executed_op_ids 丢失三个严重问题。

**Architecture:** 分 8 个任务串行实施。Task 0 扩展 messages_md schema 枚举 + 改进 append_message（自动 seq + 可选验证）。Task 1 修复 dispatch_task 和 get_task_status 两个端点（字段名统一为 task_op_id/timestamp）。Task 2 WorkerAdapter 注入 Orchestrator + 状态推进到 active。Task 3 Director 任务分派。Task 4 Worker 任务执行循环 + 重启恢复 _executed_op_ids（扫描已有 result 消息）。Task 5 任务状态推断扩展。Task 6 A2A Gateway 启用 schema 验证（确保远程 agent 写入也合规）。Task 7 端到端集成测试。

**设计说明（v3 新增）：**
- `op_id` vs `task_op_id` 命名区分：audit 记录用 `op_id`（每次操作唯一 ID），任务消息用 `task_op_id`（任务消息链关联 ID），现有 system/broadcast 消息保留 `op_id` 不改名。两者属于不同 schema 命名空间，不冲突。
- `director_implementation` 双形态：当前 `_dispatch_tasks` 是确定性脚本逻辑（选第一个 active worker），符合 `script` 形态。`agent` 形态（LLM 决定分派策略）留作扩展，本 plan 不实现。
- 任务 `timeout` 状态：本 plan 不实现（worker 拾取后崩溃的场景），留作扩展。当前状态机为 pending → assigned → processing → completed/failed。
- 现有不合规消息清理：data/blackboard/messages.md 中的旧消息（op_id/ts 字段）保留不动，read_messages 容错读取（不验证），新消息全部使用 task_op_id/timestamp 合规字段。get_task_status 端点同时兼容旧消息（fallback 查 op_id）。

**Tech Stack:** Python 3.11+, asyncio, FastAPI, pytest, pytest-asyncio, portalocker, PyYAML, jsonschema

## Global Constraints

- 所有协议消息必须遵守 `data/schemas/multiagent/messages_md.schema.yaml` 规范
- 消息必填字段：`[seq, from, to, timestamp, type, content]`，`seq` 由 blackboard.append_message 自动分配
- 消息 `from` 字段必须匹配 `^[a-z0-9_]{3,32}$`（小写字母+数字+下划线，3-32 字符）
- 任务幂等去重通过额外字段 `task_op_id`（UUID 字符串）实现，schema 允许额外字段
- 消息关联通过 `reply_to`（integer, nullable，指向原消息 seq）实现
- Worker 复用现有 Orchestrator（`orchestrator.chat(session_id, user_input) -> str`），不引入新 LLM 客户端
- 不得破坏现有 WorkerAdapter 的心跳循环、自治模式、轮次校验逻辑
- 不得修改 director_engine.py 的 `_acquire_mutex_lock` / `_increment_epoch` / `_check_worker_heartbeats` 等已有方法签名
- Worker 执行任务必须使用独立 `session_id`（格式 `{agent_id}_{task_op_id}`），避免污染用户会话历史
- 本地 agent 直接读写 blackboard 文件系统；远程 agent 通过 A2A Gateway HTTP 端点访问（已有基础设施，本 plan 不修改）
- 测试使用 `pytest tests/multiagent/test_xxx.py -v` 运行
- 已存在 fixture：`bb_root`（conftest.py L29-45）、`sample_status_json`（conftest.py L48-71）

## 协议事实（基于代码研究）

### 消息 schema 现状（messages_md.schema.yaml）
- 必填字段：`[seq, from, to, timestamp, type, content]`
- 当前 type 枚举：`[chat, system, broadcast, action, error]` — **本 plan 将扩展**
- `from` 模式：`^[a-z0-9_]{3,32}$`
- `to`：string 或 array
- `reply_to`：integer 或 null
- `trust_score`：integer 或 null
- schema 默认允许额外字段（`additionalProperties` 未限制）

### blackboard.append_message 现状（blackboard.py:286-298）
- **不调用 SchemaValidator**，仅 yaml.safe_dump + 追加文件
- **不自动分配 seq**，调用方需自行提供
- 本 plan 将改进：自动分配 seq + 可选 schema 验证

### 现有 dispatch_task 端点问题（multiagent_routes.py:186-194）
```python
message = {
    "op_id": op_id,      # ❌ 不在 schema，应改名 task_op_id
    "from": "user",      # ❌ "user" 是 4 字符，符合模式但建议用 user_dispatch
    "type": "task",      # ❌ 不在枚举（本 plan 将扩展）
    "content": task,
    "target_agents": target_agents,  # 额外字段（允许）
    "mode": mode,        # 额外字段（允许）
    "ts": ts,            # ❌ 应为 timestamp
}
# 缺少必填：seq, to
```

### agent_card auth_method 取值（agent_card.schema.yaml:46-48）
- `local`：本地文件系统信任（当前 worker_001）
- `signed`：ed25519 签名（远程 agent）
- `api_key`/`oauth2`/`mtls`：其他认证方式

### SignatureVerifier 现状（signature.py）
- **只验证 Director 写 status.json 的签名**（director_signature 字段）
- **不验证普通消息签名**
- 软约束：无签名字段返回 degraded（不拒绝）
- 本 plan 不修改签名机制

### A2A 协议（a2a_gateway.py / a2a_client.py）
- HTTP + JSON-RPC 2.0，用于**远程 agent 跨设备访问黑板**
- 本地 agent 直接读写文件系统，不走 A2A
- **v3 修复**：a2a_gateway.py:250 的 `_append_message` 调用 `append_message(bb_root, message)` 未传 `validate=True`，远程 agent 可写入不合规消息。Task 6 修复此问题。

### 现有 messages.md 不合规消息（data/blackboard/messages.md）
- 旧消息使用 `op_id`/`ts` 字段，缺 `seq`/`to`，`from=user`（4 字符合法但不规范）
- **清理策略**：旧消息保留不动（read_messages 容错读取，不验证），新消息全部使用合规字段
- get_task_status 端点兼容旧消息：优先查 `task_op_id`，fallback 查 `op_id`

### turn_manager._read_last_message_seq（turn_manager.py:225-239）
- 读取 messages.md 最后一条消息的 seq
- 返回 0 如果文件为空或不存在
- 本 plan 的 blackboard.append_message 改进将复用此逻辑

---

## File Structure

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `data/schemas/multiagent/messages_md.schema.yaml` | 消息 schema：扩展 type 枚举 + 添加 task_op_id 字段 | 修改 |
| `teage_liu/multiagent/blackboard.py` | append_message 自动分配 seq + 可选 schema 验证 | 修改 |
| `teage_liu/api/multiagent_routes.py` | 修复 dispatch_task + get_task_status 端点（字段名统一） | 修改 |
| `teage_liu/multiagent/worker_adapter.py` | Worker：注册、心跳、自治、任务接收与执行、重启恢复 | 修改 |
| `teage_liu/multiagent/director_engine.py` | Director：心跳监督、轮次推进、任务分派 | 修改 |
| `teage_liu/multiagent/a2a_gateway.py` | A2A Gateway 的 _append_message 启用 schema 验证 | 修改 |
| `teage_liu/lifespan.py` | DI 容器注册（注入 orchestrator 到 WorkerAdapter） | 修改 |
| `tests/multiagent/test_blackboard.py` | append_message 自动 seq + schema 验证测试 | 修改 |
| `tests/multiagent/test_worker_adapter.py` | Worker 单元测试扩展（含重启恢复） | 修改 |
| `tests/multiagent/test_director_engine.py` | Director 单元测试扩展 | 修改 |
| `tests/multiagent/test_a2a_gateway.py` | A2A Gateway schema 验证测试 | 修改 |
| `tests/api/test_multiagent_routes.py` | dispatch_task + get_task_status 端点测试 | 修改 |
| `tests/multiagent/test_task_dispatch_e2e.py` | 端到端集成测试 | 新建 |

设计原则：
- `append_message` 自动分配 seq（如果 message 缺失 seq 字段），集中管理避免并发冲突
- `append_message` 新增 `validate` 参数（默认 False 兼容现有，新代码用 True）
- `WorkerAdapter` 持有可选 `orchestrator` 引用（None 时保持原有协议执行者行为）
- `DirectorEngine._dispatch_tasks` 是无状态纯消费方法
- 任务消息流转：`type=task`（user 投递）→ `type=assign`（Director 分派）→ `type=status`（worker processing）→ `type=result`（worker 完成）

---

## Task 0: 扩展消息 schema + 改进 append_message

**Files:**
- Modify: `data/schemas/multiagent/messages_md.schema.yaml`
- Modify: `teage_liu/multiagent/blackboard.py:286-329`（append_message + read_messages）
- Test: `tests/multiagent/test_blackboard.py`

**Interfaces:**
- Consumes: `turn_manager._read_last_message_seq` 的逻辑（读取最后 seq）
- Produces: `append_message(bb_root, message, validate=False)` 新签名（自动分配 seq + 可选验证）；`messages_md.schema.yaml` 新增 type 枚举值 `task`/`assign`/`status`/`result` 和 `task_op_id` 字段

- [ ] **Step 1: 在 test_blackboard.py 末尾追加失败测试**

打开 `tests/multiagent/test_blackboard.py`，在文件末尾追加：

```python
@pytest.mark.asyncio
async def test_append_message_auto_assigns_seq(bb_root):
    """append_message 在 message 缺失 seq 时自动分配（last_seq + 1）。"""
    from teage_liu.multiagent.blackboard import append_message, read_messages

    # 投递第一条消息（不提供 seq）
    await append_message(bb_root, {
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task",
        "content": "第一条任务",
        "task_op_id": "task-001",
    })

    # 投递第二条消息（不提供 seq）
    await append_message(bb_root, {
        "from": "director_001",
        "to": "worker_001",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "assign",
        "content": "分派任务",
        "reply_to": 1,
        "task_op_id": "task-001",
    })

    messages = await read_messages(bb_root)
    assert len(messages) == 2
    assert messages[0]["seq"] == 1
    assert messages[1]["seq"] == 2
    assert messages[1]["reply_to"] == 1


@pytest.mark.asyncio
async def test_append_message_preserves_explicit_seq(bb_root):
    """append_message 在 message 已有 seq 时保留原值。"""
    from teage_liu.multiagent.blackboard import append_message, read_messages

    await append_message(bb_root, {
        "seq": 100,
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task",
        "content": "显式 seq",
    })

    messages = await read_messages(bb_root)
    assert messages[0]["seq"] == 100


@pytest.mark.asyncio
async def test_append_message_with_validate_passes_compliant(bb_root):
    """validate=True 时合规消息通过验证。"""
    from teage_liu.multiagent.blackboard import append_message, read_messages

    await append_message(bb_root, {
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task",
        "content": "合规消息",
    }, validate=True)

    messages = await read_messages(bb_root)
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_append_message_with_validate_rejects_invalid(bb_root):
    """validate=True 时非法 type 被拒绝。"""
    from teage_liu.multiagent.blackboard import append_message
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        await append_message(bb_root, {
            "from": "user_dispatch",
            "to": "*",
            "timestamp": "2026-07-23T10:00:00+00:00",
            "type": "invalid_type",  # 不在枚举内
            "content": "非法消息",
        }, validate=True)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/multiagent/test_blackboard.py::test_append_message_auto_assigns_seq tests/multiagent/test_blackboard.py::test_append_message_with_validate_rejects_invalid -v`

Expected: FAIL（`seq` 自动分配未实现；`task` type 不在枚举内）

- [ ] **Step 3: 扩展 messages_md.schema.yaml**

打开 `data/schemas/multiagent/messages_md.schema.yaml`，将完整内容替换为：

```yaml
$schema: http://json-schema.org/draft-07/schema#
title: messages_md_record
type: object
required: [seq, from, to, timestamp, type, content]
properties:
  seq:
    type: integer
    minimum: 1
  from:
    type: string
    pattern: '^[a-z0-9_]{3,32}$'
  to:
    oneOf:
      - type: string
      - type: array
        items: {type: string}
  timestamp:
    type: string
  type:
    type: string
    enum: [chat, system, broadcast, action, error, task, assign, status, result]
  content:
    type: string
  reply_to:
    type: [integer, "null"]
  trust_score:
    type: [integer, "null"]
  task_op_id:
    type: [string, "null"]
    description: 任务幂等 ID（UUID），用于 task/assign/status/result 消息链关联
  assigned_to:
    type: [string, "null"]
    description: 被分派的 worker agent_id（assign 消息使用）
  status:
    type: [string, "null"]
    description: 任务状态（processing/completed/failed），status 和 result 消息使用
  target_agents:
    type: [array, "null"]
    items: {type: string}
    description: 任务指定的目标 agent_id 列表（task 消息使用）
  mode:
    type: [string, "null"]
    description: 协作模式（dispatch/relay/debate），task 消息使用
  epoch:
    type: [integer, "null"]
```

- [ ] **Step 4: 改进 blackboard.append_message 自动分配 seq + 可选验证**

打开 `teage_liu/multiagent/blackboard.py`，定位 L286-298 的 `append_message` 函数，将其完整替换为：

```python
async def append_message(bb_root: Path, message: dict, validate: bool = False) -> None:
    """追加消息到 messages.md（YAML frontmatter 格式）。

    每条消息写为独立 frontmatter 块，便于后续按 frontmatter 解析。

    Args:
        bb_root: 黑板根目录
        message: 消息字典。若缺失 seq 字段，自动分配（last_seq + 1）
        validate: 是否调用 SchemaValidator 验证消息格式（默认 False 兼容现有）
    """
    # 自动分配 seq（如果缺失）
    if "seq" not in message or message.get("seq") is None:
        last_seq = await _read_last_message_seq(bb_root)
        message = {**message, "seq": last_seq + 1}

    # 可选 schema 验证
    if validate:
        from teage_liu.multiagent.schema_validator import SchemaValidator

        validator = SchemaValidator(enabled=True)
        validator.validate_messages_record(message)

    messages_path = bb_root / "messages.md"
    messages_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_str = yaml.safe_dump(message, sort_keys=False, allow_unicode=True)
    content = f"---\n{yaml_str}---\n\n{message.get('content', '')}\n\n"
    async with aiofiles.open(messages_path, "a", encoding="utf-8") as f:
        await f.write(content)
        await f.flush()
        os.fsync(f.fileno())


async def _read_last_message_seq(bb_root: Path) -> int:
    """读取 messages.md 最后一条消息的 seq。

    Returns:
        最后一条消息的 seq；若文件为空或不存在返回 0
    """
    messages_path = bb_root / "messages.md"
    if not messages_path.exists():
        return 0

    content = messages_path.read_text(encoding="utf-8")
    if not content.strip():
        return 0

    parts = content.split("---\n")
    last_seq = 0
    for i in range(1, len(parts), 2):
        if i >= len(parts):
            break
        frontmatter_str = parts[i]
        if not frontmatter_str.strip():
            continue
        try:
            frontmatter = yaml.safe_load(frontmatter_str)
            if isinstance(frontmatter, dict):
                seq = frontmatter.get("seq", 0)
                if isinstance(seq, int) and seq > last_seq:
                    last_seq = seq
        except yaml.YAMLError:
            continue
    return last_seq
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `pytest tests/multiagent/test_blackboard.py::test_append_message_auto_assigns_seq tests/multiagent/test_blackboard.py::test_append_message_preserves_explicit_seq tests/multiagent/test_blackboard.py::test_append_message_with_validate_passes_compliant tests/multiagent/test_blackboard.py::test_append_message_with_validate_rejects_invalid -v`

Expected: PASS

- [ ] **Step 6: 运行现有 blackboard 测试确保无回归**

Run: `pytest tests/multiagent/test_blackboard.py -v`

Expected: 所有现有测试 PASS

- [ ] **Step 7: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add data/schemas/multiagent/messages_md.schema.yaml teage_liu/multiagent/blackboard.py tests/multiagent/test_blackboard.py
git commit -m "feat(blackboard): extend message schema + auto-assign seq + optional validation"
```

---

## Task 1: 修复 dispatch_task + get_task_status 端点（字段名统一）

**Files:**
- Modify: `teage_liu/api/multiagent_routes.py:158-221`（dispatch_task 端点）
- Modify: `teage_liu/api/multiagent_routes.py:241-269`（get_task_status 端点）
- Test: `tests/api/test_multiagent_routes.py`

**Interfaces:**
- Consumes: Task 0 的 `append_message(validate=True)` 和扩展后的 schema
- Produces: dispatch_task 端点写入合规消息（`type=task`, `from=user_dispatch`, `to=*`, 含 `task_op_id`/`target_agents`/`mode`）；get_task_status 端点使用 `task_op_id` 查询、`timestamp` 字段返回（兼容旧消息 fallback 查 `op_id`）

- [ ] **Step 1: 在 test_multiagent_routes.py 中追加失败测试**

打开 `tests/api/test_multiagent_routes.py`，在文件末尾追加：

```python
@pytest.mark.asyncio
async def test_dispatch_task_writes_compliant_message(tmp_path):
    """dispatch_task 端点写入的消息符合 messages_md schema。"""
    from teage_liu.api.multiagent_routes import create_multiagent_router
    from teage_liu.multiagent.blackboard import read_messages
    from teage_liu.container import Container

    # 初始化黑板
    _init_blackboard_sync(tmp_path)

    # 构造最小容器
    class FakeContainer:
        def __init__(self, bb, cfg):
            self._bb = bb
            self._cfg = cfg
            self.config = cfg

        def get(self, name):
            if name == "bb_root":
                return self._bb
            return None

    config = {
        "multiagent": {
            "enabled": True,
            "blackboard_dir": str(tmp_path),
        }
    }
    container = FakeContainer(tmp_path, config)
    router = create_multiagent_router(container)

    from fastapi import FastAPI
    from httpx import AsyncClient

    app = FastAPI()
    app.include_router(router, prefix="/api/multiagent")
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post("/api/multiagent/dispatch", json={
            "task": "测试任务",
            "target_agents": [],
            "mode": "dispatch",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "op_id" in data

    # 验证消息格式合规
    messages = await read_messages(tmp_path)
    assert len(messages) == 1
    msg = messages[0]
    assert msg["seq"] == 1  # 自动分配
    assert msg["from"] == "user_dispatch"  # 符合 ^[a-z0-9_]{3,32}$
    assert msg["to"] == "*"
    assert msg["type"] == "task"
    assert msg["content"] == "测试任务"
    assert "timestamp" in msg  # 必须有 timestamp（不是 ts）
    assert msg["task_op_id"] == data["op_id"]
    assert msg["target_agents"] == []
    assert msg["mode"] == "dispatch"


@pytest.mark.asyncio
async def test_get_task_status_uses_task_op_id(tmp_path):
    """get_task_status 端点使用 task_op_id 查询消息（v3 修复）。"""
    from teage_liu.api.multiagent_routes import create_multiagent_router
    from teage_liu.multiagent.blackboard import append_message, read_messages
    from fastapi import FastAPI
    from httpx import AsyncClient

    _init_blackboard_sync(tmp_path)

    class FakeContainer:
        def __init__(self, bb, cfg):
            self._bb = bb
            self._cfg = cfg
            self.config = cfg

        def get(self, name):
            if name == "bb_root":
                return self._bb
            return None

    config = {"multiagent": {"enabled": True, "blackboard_dir": str(tmp_path)}}
    container = FakeContainer(tmp_path, config)
    router = create_multiagent_router(container)

    app = FastAPI()
    app.include_router(router, prefix="/api/multiagent")

    # 写入合规的 task 消息
    await append_message(tmp_path, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task", "content": "查询测试任务",
        "task_op_id": "query-task-001", "target_agents": [], "mode": "dispatch",
    }, validate=True)

    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.get("/api/multiagent/tasks/query-task-001")

    assert resp.status_code == 200
    data = resp.json()
    assert data["op_id"] == "query-task-001"
    assert data["status"] == "pending"
    assert len(data["timeline"]) == 1
    # timeline 使用 timestamp 字段（不是 ts）
    assert data["timeline"][0]["ts"] == "2026-07-23T10:00:00+00:00"


@pytest.mark.asyncio
async def test_get_task_status_fallback_to_op_id_for_legacy(tmp_path):
    """get_task_status 端点兼容旧消息（fallback 查 op_id）。"""
    from teage_liu.api.multiagent_routes import create_multiagent_router
    from teage_liu.multiagent.blackboard import append_message
    from fastapi import FastAPI
    from httpx import AsyncClient

    _init_blackboard_sync(tmp_path)

    class FakeContainer:
        def __init__(self, bb, cfg):
            self._bb = bb
            self._cfg = cfg
            self.config = cfg

        def get(self, name):
            if name == "bb_root":
                return self._bb
            return None

    config = {"multiagent": {"enabled": True, "blackboard_dir": str(tmp_path)}}
    container = FakeContainer(tmp_path, config)
    router = create_multiagent_router(container)

    app = FastAPI()
    app.include_router(router, prefix="/api/multiagent")

    # 写入旧格式消息（op_id 而非 task_op_id，ts 而非 timestamp）
    await append_message(tmp_path, {
        "op_id": "legacy-task-001",
        "from": "user_dispatch",
        "to": "*",
        "timestamp": "2026-07-23T10:00:00+00:00",
        "type": "task", "content": "旧格式任务",
    })

    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.get("/api/multiagent/tasks/legacy-task-001")

    assert resp.status_code == 200
    data = resp.json()
    assert data["op_id"] == "legacy-task-001"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/api/test_multiagent_routes.py::test_dispatch_task_writes_compliant_message tests/api/test_multiagent_routes.py::test_get_task_status_uses_task_op_id tests/api/test_multiagent_routes.py::test_get_task_status_fallback_to_op_id_for_legacy -v`

Expected: FAIL（`from=user` 不等于 `user_dispatch`；get_task_status 用 `op_id` 查不到 `task_op_id` 的消息）

- [ ] **Step 3: 修复 dispatch_task 端点**

打开 `teage_liu/api/multiagent_routes.py`，定位 L158-221 的 `dispatch_task` 函数，将其中的 `message` 字典构造部分（L186-194）替换为：

```python
        message = {
            "from": "user_dispatch",
            "to": "*",
            "timestamp": ts,
            "type": "task",
            "content": task,
            "task_op_id": op_id,
            "target_agents": target_agents,
            "mode": mode,
        }

        await append_message(bb_root, message, validate=True)
```

注意：保留 `op_id` 变量名用于返回给调用者，但消息中改名为 `task_op_id`。

- [ ] **Step 4: 修复 get_task_status 端点（字段名统一 + 兼容旧消息）**

打开 `teage_liu/api/multiagent_routes.py`，定位 L241-269 的 `get_task_status` 函数，将其完整替换为：

```python
    @router.get("/tasks/{op_id}")
    async def get_task_status(op_id: str) -> dict:
        """查询指定任务的状态流转历史。

        v3 修复：使用 task_op_id 查询消息（兼容旧消息 fallback 查 op_id），
        timeline 使用 timestamp 字段（兼容旧消息 fallback 用 ts）。
        """
        from teage_liu.multiagent.blackboard import read_messages

        messages = await read_messages(bb_root)
        # v3: 优先查 task_op_id，fallback 查 op_id（兼容旧消息）
        task_msgs = [
            m for m in messages
            if m.get("task_op_id") == op_id or m.get("op_id") == op_id
        ]
        if not task_msgs:
            raise HTTPException(status_code=404, detail="任务不存在")

        # 推断状态
        latest = task_msgs[-1]
        inferred_status = _infer_task_status(latest)

        return {
            "op_id": op_id,
            "status": inferred_status,
            "assigned_to": latest.get("assigned_to"),
            "timeline": [
                {
                    "ts": m.get("timestamp") or m.get("ts", ""),  # 兼容旧消息
                    "from": m.get("from", ""),
                    "type": m.get("type", ""),
                    "status": m.get("status") or _infer_task_status(m),
                    "content": (m.get("content") or "")[:200],
                }
                for m in task_msgs
            ],
        }
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `pytest tests/api/test_multiagent_routes.py::test_dispatch_task_writes_compliant_message tests/api/test_multiagent_routes.py::test_get_task_status_uses_task_op_id tests/api/test_multiagent_routes.py::test_get_task_status_fallback_to_op_id_for_legacy -v`

Expected: PASS

- [ ] **Step 6: 运行现有 API 测试确保无回归**

Run: `pytest tests/api/test_multiagent_routes.py -v`

Expected: 所有现有测试 PASS（如有失败，可能是旧测试断言 `from=user`，需更新为 `user_dispatch`）

- [ ] **Step 7: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add teage_liu/api/multiagent_routes.py tests/api/test_multiagent_routes.py
git commit -m "fix(api): dispatch_task + get_task_status use task_op_id/timestamp (schema-compliant, legacy fallback)"
```

---

## Task 2: WorkerAdapter 注入 Orchestrator + 状态推进到 active

**Files:**
- Modify: `teage_liu/multiagent/worker_adapter.py:210-245`（__init__ + start）
- Modify: `teage_liu/lifespan.py:140-148`（DI 注册）
- Modify: `teage_liu/multiagent/worker_adapter.py:247-298`（stop 增加任务循环取消）
- Test: `tests/multiagent/test_worker_adapter.py`

**Interfaces:**
- Consumes: `Orchestrator`（来自 `teage_liu.orchestrator`），其方法 `async def chat(session_id: str, user_input: str, cancel_event=None, reasoning_cfg=None, is_cron: bool = False) -> str`
- Produces: `WorkerAdapter.__init__(bb_root, config, agent_id, orchestrator=None)` 新签名；`WorkerAdapter.start()` 完成后 agent_card.status == "active"

- [ ] **Step 1: 在 test_worker_adapter.py 末尾追加失败测试**

打开 `tests/multiagent/test_worker_adapter.py`，在文件末尾追加：

```python
class FakeOrchestrator:
    """测试用 Orchestrator 替身。"""
    def __init__(self):
        self.calls = []

    async def chat(self, session_id: str, user_input: str,
                   cancel_event=None, reasoning_cfg=None, is_cron: bool = False) -> str:
        self.calls.append({"session_id": session_id, "user_input": user_input})
        return f"executed: {user_input[:30]}"


@pytest.mark.asyncio
async def test_worker_init_accepts_orchestrator(bb_root, worker_config):
    """WorkerAdapter __init__ 接受 orchestrator 参数并保存。"""
    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root,
        config=worker_config,
        agent_id="worker_001",
        orchestrator=fake_orch,
    )
    assert worker._orchestrator is fake_orch


@pytest.mark.asyncio
async def test_worker_start_promotes_status_to_active(bb_root, worker_config):
    """start() 完成后 agent_card.status 从 registering 推进到 active。"""
    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root,
        config=worker_config,
        agent_id="worker_001",
        orchestrator=fake_orch,
    )
    await worker.start()
    try:
        card_path = bb_root / "agents" / "worker_001.md"
        frontmatter, _ = read_yaml_frontmatter(card_path)
        assert frontmatter["status"] == "active"
    finally:
        await worker.stop()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/multiagent/test_worker_adapter.py::test_worker_init_accepts_orchestrator tests/multiagent/test_worker_adapter.py::test_worker_start_promotes_status_to_active -v`

Expected: FAIL（`__init__() got an unexpected keyword argument 'orchestrator'`）

- [ ] **Step 3: 修改 WorkerAdapter.__init__ 接受 orchestrator 参数**

打开 `teage_liu/multiagent/worker_adapter.py`，定位 L210-225 的 `__init__` 方法，将其完整替换为：

```python
    def __init__(
        self,
        bb_root: Path,
        config: dict,
        agent_id: str = "worker_001",
        orchestrator=None,
    ):
        self._bb_root = bb_root
        self._config = config.get("multiagent", {}).get("worker", {})
        self._director_config = config.get("multiagent", {}).get("director", {})
        self._agent_id = agent_id
        self._orchestrator = orchestrator
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._director_monitor_task: asyncio.Task | None = None
        self._task_poll_task: asyncio.Task | None = None
        self._executed_op_ids: set[str] = set()
        self._autonomous = AutonomousModeController(bb_root, agent_id)
        self._blackboard = Blackboard(bb_root)

        # 简单属性供测试直接设置（兼容测试的 _autonomous_mode / _autonomous_epoch）
        self._autonomous_mode = False
        self._autonomous_epoch = 0
```

- [ ] **Step 4: 修改 WorkerAdapter.start() 推进状态到 active**

定位 L230-245 的 `start()` 方法，将其完整替换为：

```python
    async def start(self) -> None:
        """启动 Worker。"""
        # 1. 注册 agent_card（status=registering）
        await self._register()

        # 2. 状态推进：registering → active
        await self._update_agent_card_status("active")

        # 3. 启动心跳循环
        self._running = True
        interval = self._config.get("heartbeat_interval_seconds", 10)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))

        # 4. 启动 Director 健康监测
        self._director_monitor_task = asyncio.create_task(
            self._director_monitor_loop()
        )

        # 5. 启动任务轮询循环（仅当 orchestrator 可用时）
        if self._orchestrator is not None:
            poll_interval = self._config.get("task_poll_interval_seconds", 2)
            self._task_poll_task = asyncio.create_task(
                self._task_poll_loop(poll_interval)
            )
            logger.info("Worker %s 任务轮询已启动（interval=%ss）", self._agent_id, poll_interval)

        logger.info("Worker %s 启动（status=active, orchestrator=%s）",
                    self._agent_id, "enabled" if self._orchestrator else "disabled")
```

- [ ] **Step 5: 修改 WorkerAdapter.stop() 取消任务轮询**

定位 L247-298 的 `stop()` 方法，将其中的"取消后台任务"部分（L252-260）替换为：

```python
        # 取消后台任务
        for task in [self._heartbeat_task, self._director_monitor_task, self._task_poll_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._heartbeat_task = None
        self._director_monitor_task = None
        self._task_poll_task = None
```

- [ ] **Step 6: 修改 lifespan.py 注入 orchestrator 依赖**

打开 `teage_liu/lifespan.py`，定位 L140-148 的 WorkerAdapter 注册代码，将其替换为：

```python
            container.register(
                "multiagent_adapter",
                lambda c: WorkerAdapter(
                    bb_root=bb_root,
                    config=config,
                    agent_id=worker_agent_id,
                    orchestrator=c.get("orchestrator"),
                ),
                deps=["orchestrator"], hot_reloadable=True,
            )
```

- [ ] **Step 7: 运行测试，确认通过**

Run: `pytest tests/multiagent/test_worker_adapter.py::test_worker_init_accepts_orchestrator tests/multiagent/test_worker_adapter.py::test_worker_start_promotes_status_to_active -v`

Expected: PASS

- [ ] **Step 8: 运行现有 worker 测试确保无回归**

Run: `pytest tests/multiagent/test_worker_adapter.py -v`

Expected: 所有现有测试 PASS

- [ ] **Step 9: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add teage_liu/multiagent/worker_adapter.py teage_liu/lifespan.py tests/multiagent/test_worker_adapter.py
git commit -m "feat(worker): inject orchestrator + promote status to active on start"
```

---

## Task 3: Director 任务分派（_dispatch_tasks）

**Files:**
- Modify: `teage_liu/multiagent/director_engine.py:304-320`（_run_loop 增加 _dispatch_tasks 调用）
- Modify: `teage_liu/multiagent/director_engine.py`（新增 _dispatch_tasks 方法）
- Test: `tests/multiagent/test_director_engine.py`

**Interfaces:**
- Consumes: `read_messages(bb_root) -> list[dict]`；`AgentRegistry.list_active_agents() -> list[dict]`（返回 `[{"agent_id": "...", "role": "worker", "status": "active", ...}, ...]`）；`append_message(bb_root, message, validate=True)`
- Produces: `DirectorEngine._dispatch_tasks()` 方法

**消息流转约定：**
- 用户投递任务消息（Task 1 修复后）：`{"seq": N, "from": "user_dispatch", "to": "*", "type": "task", "content": "...", "task_op_id": "uuid", "target_agents": [], "mode": "dispatch"}`
- Director 分派消息：`{"seq": M, "from": "director_001", "to": "worker_001", "type": "assign", "content": "<原task内容>", "reply_to": N, "task_op_id": "<同上>", "assigned_to": "worker_001", "epoch": E}`

- [ ] **Step 1: 在 test_director_engine.py 末尾追加失败测试**

打开 `tests/multiagent/test_director_engine.py`，在文件末尾追加：

```python
@pytest.mark.asyncio
async def test_director_dispatches_task_to_active_worker(bb_root):
    """Director._dispatch_tasks 扫描 type=task 消息并写入 type=assign 消息。"""
    from teage_liu.multiagent.blackboard import (
        Blackboard, append_message, read_messages,
    )
    from teage_liu.multiagent.director_engine import DirectorEngine
    import yaml

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    # 注册一个 active worker
    card = {
        "agent_id": "worker_001", "role": "worker", "status": "active",
        "protocol_version": "1.0.0", "agent_version": "1.0.0",
        "capabilities": ["file_read"],
        "last_heartbeat": "2026-07-23T10:00:00+00:00",
        "heartbeat_interval_seconds": 10,
        "trust_score": 100,
    }
    (bb_root / "agents" / "worker_001.md").write_text(
        f"---\n{yaml.safe_dump(card, sort_keys=False, allow_unicode=True)}---\n\n# Agent Card\n",
        encoding="utf-8",
    )

    # 初始化 director.md
    director_md = {
        "protocol_version": "1.0.0", "director_version": "1.0.0",
        "current_epoch": 1, "epoch_started_at": "2026-07-23T10:00:00+00:00",
        "last_director_tick": "2026-07-23T10:00:00+00:00",
        "director_id": "director_001", "director_implementation": "script",
        "heartbeat": {"interval_seconds": 10, "timeout_seconds": 30},
        "turn_policy": {"mode": "freeform", "order": []},
    }
    (bb_root / "director.md").write_text(
        f"---\n{yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)}---\n\n# Director Protocol\n",
        encoding="utf-8",
    )

    # 投递合规 task 消息
    await append_message(bb_root, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "task", "content": "帮我读取 data/test.txt 文件",
        "task_op_id": "task-001", "target_agents": [], "mode": "dispatch",
    })

    # 调用 _dispatch_tasks
    config = {"multiagent": {"director": {"turn_timeout_seconds": 30}}}
    director = DirectorEngine(bb_root=bb_root, config=config, agent_id="director_001")
    await director._dispatch_tasks()

    # 验证：应有 type=assign 消息
    messages = await read_messages(bb_root)
    assign_msgs = [m for m in messages if m.get("type") == "assign"]
    assert len(assign_msgs) == 1, f"期望 1 条 assign 消息，实际 {len(assign_msgs)}"
    assign_msg = assign_msgs[0]
    assert assign_msg["from"] == "director_001"
    assert assign_msg["to"] == "worker_001"
    assert assign_msg["assigned_to"] == "worker_001"
    assert assign_msg["task_op_id"] == "task-001"
    assert assign_msg["reply_to"] == 1  # 原 task 消息的 seq
    assert assign_msg["content"] == "帮我读取 data/test.txt 文件"


@pytest.mark.asyncio
async def test_director_dispatch_respects_target_agents(bb_root):
    """当 task.target_agents 指定时，Director 分派给指定 agent。"""
    from teage_liu.multiagent.blackboard import (
        Blackboard, append_message, read_messages,
    )
    from teage_liu.multiagent.director_engine import DirectorEngine
    import yaml

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    for aid in ["worker_001", "worker_002"]:
        card = {
            "agent_id": aid, "role": "worker", "status": "active",
            "protocol_version": "1.0.0", "agent_version": "1.0.0",
            "capabilities": [], "last_heartbeat": "2026-07-23T10:00:00+00:00",
            "heartbeat_interval_seconds": 10, "trust_score": 100,
        }
        (bb_root / "agents" / f"{aid}.md").write_text(
            f"---\n{yaml.safe_dump(card, sort_keys=False, allow_unicode=True)}---\n\n# Agent Card\n",
            encoding="utf-8",
        )

    director_md = {
        "protocol_version": "1.0.0", "director_version": "1.0.0",
        "current_epoch": 1, "epoch_started_at": "2026-07-23T10:00:00+00:00",
        "last_director_tick": "2026-07-23T10:00:00+00:00",
        "director_id": "director_001", "director_implementation": "script",
        "heartbeat": {"interval_seconds": 10, "timeout_seconds": 30},
        "turn_policy": {"mode": "freeform", "order": []},
    }
    (bb_root / "director.md").write_text(
        f"---\n{yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)}---\n\n# Director Protocol\n",
        encoding="utf-8",
    )

    await append_message(bb_root, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "task", "content": "专门给 worker_002 的任务",
        "task_op_id": "task-002", "target_agents": ["worker_002"], "mode": "dispatch",
    })

    config = {"multiagent": {"director": {"turn_timeout_seconds": 30}}}
    director = DirectorEngine(bb_root=bb_root, config=config, agent_id="director_001")
    await director._dispatch_tasks()

    messages = await read_messages(bb_root)
    assign_msgs = [m for m in messages if m.get("type") == "assign"]
    assert len(assign_msgs) == 1
    assert assign_msgs[0]["assigned_to"] == "worker_002"


@pytest.mark.asyncio
async def test_director_dispatch_is_idempotent(bb_root):
    """同一 task_op_id 的 task 不会被分派两次。"""
    from teage_liu.multiagent.blackboard import (
        Blackboard, append_message, read_messages,
    )
    from teage_liu.multiagent.director_engine import DirectorEngine
    import yaml

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    card = {
        "agent_id": "worker_001", "role": "worker", "status": "active",
        "protocol_version": "1.0.0", "agent_version": "1.0.0",
        "capabilities": [], "last_heartbeat": "2026-07-23T10:00:00+00:00",
        "heartbeat_interval_seconds": 10, "trust_score": 100,
    }
    (bb_root / "agents" / "worker_001.md").write_text(
        f"---\n{yaml.safe_dump(card, sort_keys=False, allow_unicode=True)}---\n\n# Agent Card\n",
        encoding="utf-8",
    )

    director_md = {
        "protocol_version": "1.0.0", "director_version": "1.0.0",
        "current_epoch": 1, "epoch_started_at": "2026-07-23T10:00:00+00:00",
        "last_director_tick": "2026-07-23T10:00:00+00:00",
        "director_id": "director_001", "director_implementation": "script",
        "heartbeat": {"interval_seconds": 10, "timeout_seconds": 30},
        "turn_policy": {"mode": "freeform", "order": []},
    }
    (bb_root / "director.md").write_text(
        f"---\n{yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)}---\n\n# Director Protocol\n",
        encoding="utf-8",
    )

    await append_message(bb_root, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "task", "content": "幂等测试",
        "task_op_id": "task-003", "target_agents": [], "mode": "dispatch",
    })

    config = {"multiagent": {"director": {"turn_timeout_seconds": 30}}}
    director = DirectorEngine(bb_root=bb_root, config=config, agent_id="director_001")

    await director._dispatch_tasks()
    await director._dispatch_tasks()

    messages = await read_messages(bb_root)
    assign_msgs = [m for m in messages if m.get("type") == "assign"]
    assert len(assign_msgs) == 1, f"期望 1 条 assign（幂等），实际 {len(assign_msgs)}"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/multiagent/test_director_engine.py::test_director_dispatches_task_to_active_worker tests/multiagent/test_director_engine.py::test_director_dispatch_respects_target_agents tests/multiagent/test_director_engine.py::test_director_dispatch_is_idempotent -v`

Expected: FAIL（`AttributeError: 'DirectorEngine' object has no attribute '_dispatch_tasks'`）

- [ ] **Step 3: 在 DirectorEngine._run_loop 中加入 _dispatch_tasks 调用**

打开 `teage_liu/multiagent/director_engine.py`，定位 L304-320 的 `_run_loop` 方法，将其完整替换为：

```python
    async def _run_loop(self) -> None:
        """Director 主循环。

        首次迭代前先 sleep tick_interval，避免覆盖 _increment_epoch 刚写入的
        last_director_tick（测试需要在 start() 后立即写入自定义 tick）。
        """
        try:
            while self._running:
                await asyncio.sleep(self._tick_interval)
                await self._update_director_tick()
                await self._check_worker_heartbeats()
                await self._check_turn_timeout()
                await self._dispatch_tasks()
                await self._flush_pending_messages()
                await self._arbitrate_conflicts()
        except asyncio.CancelledError:
            logger.info("Director 主循环被取消")
            raise
```

- [ ] **Step 4: 新增 _dispatch_tasks 方法**

在 `director_engine.py` 中找到 `_check_turn_timeout` 方法定义之前（约 L547 之前），插入以下新方法：

```python
    async def _dispatch_tasks(self) -> None:
        """扫描 messages.md 中未分派的 task 消息，分派给 active worker。

        流程：
        1. 读取所有消息，找出 type=task 且 task_op_id 未出现在 assign 消息中的
        2. 收集已存在的 assign 消息的 task_op_id（幂等去重）
        3. 对每个未分派 task：
           - 若 target_agents 非空，选第一个 active 的目标 agent
           - 否则选第一个 active worker（按 agent_id 字典序）
           - 写入 type=assign 消息（reply_to 指向原 task seq）
        4. 无 active worker 时跳过（任务保持 pending）
        """
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.blackboard import append_message, read_messages
        from teage_liu.multiagent.schema_validator import SchemaValidator

        messages = await read_messages(self._bb_root)

        # 已分派的 task_op_id 集合（幂等去重）
        assigned_op_ids = {
            m.get("task_op_id") for m in messages
            if m.get("type") == "assign" and m.get("task_op_id")
        }

        # 待分派的 task 消息
        pending_tasks = [
            m for m in messages
            if m.get("type") == "task"
            and m.get("task_op_id") not in assigned_op_ids
        ]

        if not pending_tasks:
            return

        # 获取 active worker 列表
        registry = AgentRegistry(self._bb_root, SchemaValidator(enabled=False))
        active_agents = await registry.list_active_agents()
        active_workers = [
            a for a in active_agents
            if a.get("role") == "worker" and a.get("status") == "active"
        ]
        if not active_workers:
            logger.warning("Director: 无 active worker，%d 个任务等待分派", len(pending_tasks))
            return

        # 按 agent_id 字典序排序，保证选 worker 的确定性
        active_workers.sort(key=lambda a: a.get("agent_id", ""))
        active_worker_ids = [a["agent_id"] for a in active_workers]

        for task_msg in pending_tasks:
            task_op_id = task_msg.get("task_op_id", "")
            target_agents = task_msg.get("target_agents") or []

            # 选 agent：优先 target_agents 中第一个 active 的，否则第一个 active worker
            chosen = None
            for tid in target_agents:
                if tid in active_worker_ids:
                    chosen = tid
                    break
            if chosen is None and not target_agents:
                chosen = active_worker_ids[0]
            if chosen is None:
                logger.warning(
                    "Director: task task_op_id=%s 的 target_agents=%s 均不在线，跳过",
                    task_op_id, target_agents,
                )
                continue

            assign_msg = {
                "from": self._agent_id,
                "to": chosen,
                "timestamp": _now_iso(),
                "type": "assign",
                "content": task_msg.get("content", ""),
                "reply_to": task_msg.get("seq"),
                "task_op_id": task_op_id,
                "assigned_to": chosen,
                "epoch": self._current_epoch,
            }
            await append_message(self._bb_root, assign_msg, validate=True)
            logger.info("Director: 任务 task_op_id=%s 已分派给 %s", task_op_id, chosen)
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `pytest tests/multiagent/test_director_engine.py::test_director_dispatches_task_to_active_worker tests/multiagent/test_director_engine.py::test_director_dispatch_respects_target_agents tests/multiagent/test_director_engine.py::test_director_dispatch_is_idempotent -v`

Expected: PASS

- [ ] **Step 6: 运行现有 director 测试确保无回归**

Run: `pytest tests/multiagent/test_director_engine.py -v`

Expected: 所有现有测试 PASS

- [ ] **Step 7: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add teage_liu/multiagent/director_engine.py tests/multiagent/test_director_engine.py
git commit -m "feat(director): add _dispatch_tasks to assign tasks to active workers (schema-compliant)"
```

---

## Task 4: Worker 任务执行循环 + 重启恢复 _executed_op_ids（_task_poll_loop + _execute_task + _recover_executed_op_ids）

**Files:**
- Modify: `teage_liu/multiagent/worker_adapter.py`（新增 _task_poll_loop、_poll_once、_execute_task、_recover_executed_op_ids 方法；修改 start() 调用恢复逻辑）
- Test: `tests/multiagent/test_worker_adapter.py`

**Interfaces:**
- Consumes: `read_messages(bb_root) -> list[dict]`；`append_message(bb_root, msg, validate=True)`；`self._orchestrator.chat(session_id, user_input) -> str`（Task 2 注入）
- Produces: `WorkerAdapter._task_poll_loop(interval)` 周期任务；`WorkerAdapter._poll_once()` 单次轮询；`WorkerAdapter._execute_task(task_msg) -> None` 执行单个任务；`WorkerAdapter._recover_executed_op_ids()` 重启后扫描已有 result 消息恢复已执行集合

**消息流转约定：**
- Worker 接收 `type=assign, to=worker_001, assigned_to=worker_001` 消息后：
  1. 写入 `type=status, from=worker_001, status=processing, task_op_id=<同上>, reply_to=<assign seq>` 消息
  2. 调用 `orchestrator.chat(session_id=f"worker_001_{task_op_id}", user_input=task_content)` 执行
  3. 写入 `type=result, from=worker_001, to=director_001, status=completed/failed, content=<结果>, task_op_id=<同上>, reply_to=<assign seq>` 消息
- 幂等：`_executed_op_ids` 集合记录已处理的 task_op_id，防止重复执行

- [ ] **Step 1: 在 test_worker_adapter.py 末尾追加失败测试**

```python
@pytest.mark.asyncio
async def test_worker_polls_and_executes_assign_message(bb_root, worker_config):
    """Worker _poll_once 拾取 type=assign 消息并调用 orchestrator.chat 执行。"""
    from teage_liu.multiagent.blackboard import (
        Blackboard, append_message, read_messages,
    )

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root, config=worker_config,
        agent_id="worker_001", orchestrator=fake_orch,
    )
    await worker.start()
    try:
        # 投递一个 assign 消息（自动分配 seq）
        await append_message(bb_root, {
            "from": "director_001", "to": "worker_001",
            "timestamp": "2026-07-23T10:00:01+00:00",
            "type": "assign", "content": "读取 data/test.txt",
            "reply_to": 1, "task_op_id": "task-100",
            "assigned_to": "worker_001", "epoch": 1,
        })

        await worker._poll_once()

        # 验证：orchestrator.chat 被调用
        assert len(fake_orch.calls) == 1
        assert fake_orch.calls[0]["user_input"] == "读取 data/test.txt"
        assert fake_orch.calls[0]["session_id"] == "worker_001_task-100"

        # 验证：写入 type=status, status=processing 消息
        messages = await read_messages(bb_root)
        status_msgs = [m for m in messages if m.get("type") == "status"]
        assert any(
            m.get("status") == "processing" and m.get("task_op_id") == "task-100"
            for m in status_msgs
        )

        # 验证：写入 type=result 消息
        result_msgs = [m for m in messages if m.get("type") == "result"]
        assert len(result_msgs) == 1
        assert result_msgs[0]["task_op_id"] == "task-100"
        assert result_msgs[0]["from"] == "worker_001"
        assert "executed" in result_msgs[0]["content"]
        assert result_msgs[0]["status"] == "completed"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_executes_task_only_for_self(bb_root, worker_config):
    """Worker 只处理 to == self._agent_id 的 assign 消息。"""
    from teage_liu.multiagent.blackboard import (
        Blackboard, append_message,
    )

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root, config=worker_config,
        agent_id="worker_001", orchestrator=fake_orch,
    )
    await worker.start()
    try:
        await append_message(bb_root, {
            "from": "director_001", "to": "worker_002",
            "timestamp": "2026-07-23T10:00:01+00:00",
            "type": "assign", "content": "给 worker_002 的任务",
            "reply_to": 1, "task_op_id": "task-200",
            "assigned_to": "worker_002", "epoch": 1,
        })

        await worker._poll_once()

        assert len(fake_orch.calls) == 0
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_task_execution_is_idempotent(bb_root, worker_config):
    """同一 task_op_id 的 assign 消息不会被 worker 执行两次。"""
    from teage_liu.multiagent.blackboard import (
        Blackboard, append_message,
    )

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root, config=worker_config,
        agent_id="worker_001", orchestrator=fake_orch,
    )
    await worker.start()
    try:
        await append_message(bb_root, {
            "from": "director_001", "to": "worker_001",
            "timestamp": "2026-07-23T10:00:01+00:00",
            "type": "assign", "content": "幂等测试",
            "reply_to": 1, "task_op_id": "task-300",
            "assigned_to": "worker_001", "epoch": 1,
        })

        await worker._poll_once()
        await worker._poll_once()

        assert len(fake_orch.calls) == 1
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_recovers_executed_op_ids_on_restart(bb_root, worker_config):
    """Worker 重启后通过扫描已有 result 消息恢复 _executed_op_ids，避免重复执行。"""
    from teage_liu.multiagent.blackboard import (
        Blackboard, append_message,
    )

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    # 第一次启动：执行一个任务
    fake_orch_1 = FakeOrchestrator()
    worker_1 = WorkerAdapter(
        bb_root=bb_root, config=worker_config,
        agent_id="worker_001", orchestrator=fake_orch_1,
    )
    await worker_1.start()
    try:
        await append_message(bb_root, {
            "from": "director_001", "to": "worker_001",
            "timestamp": "2026-07-23T10:00:01+00:00",
            "type": "assign", "content": "重启恢复测试",
            "reply_to": 1, "task_op_id": "task-restart-001",
            "assigned_to": "worker_001", "epoch": 1,
        })
        await worker_1._poll_once()
        assert len(fake_orch_1.calls) == 1
    finally:
        await worker_1.stop()

    # 第二次启动（模拟重启）：_executed_op_ids 应从 messages.md 恢复
    fake_orch_2 = FakeOrchestrator()
    worker_2 = WorkerAdapter(
        bb_root=bb_root, config=worker_config,
        agent_id="worker_001", orchestrator=fake_orch_2,
    )
    await worker_2.start()
    try:
        # 重启后 _executed_op_ids 应包含 "task-restart-001"
        assert "task-restart-001" in worker_2._executed_op_ids

        # 再次 poll 不应重复执行
        await worker_2._poll_once()
        assert len(fake_orch_2.calls) == 0
    finally:
        await worker_2.stop()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/multiagent/test_worker_adapter.py::test_worker_polls_and_executes_assign_message tests/multiagent/test_worker_adapter.py::test_worker_executes_task_only_for_self tests/multiagent/test_worker_adapter.py::test_worker_task_execution_is_idempotent tests/multiagent/test_worker_adapter.py::test_worker_recovers_executed_op_ids_on_restart -v`

Expected: FAIL（`AttributeError: 'WorkerAdapter' object has no attribute '_poll_once'`；重启恢复测试也失败）

- [ ] **Step 3: 在 WorkerAdapter 中新增 _task_poll_loop、_poll_once、_execute_task 方法**

打开 `teage_liu/multiagent/worker_adapter.py`，在 `_director_monitor_loop` 方法之后（约 L385 附近，`_check_director_health` 之前），插入以下新方法：

```python
    async def _task_poll_loop(self, interval: float) -> None:
        """任务轮询循环。"""
        try:
            while self._running:
                try:
                    await self._poll_once()
                except Exception as e:
                    logger.exception("Worker %s 任务轮询异常: %s", self._agent_id, e)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Worker %s 任务轮询循环被取消", self._agent_id)
            raise

    async def _poll_once(self) -> None:
        """执行一次任务轮询：扫描给自己的 assign 消息并执行。"""
        from teage_liu.multiagent.blackboard import read_messages

        messages = await read_messages(self._bb_root)

        for msg in messages:
            if msg.get("type") != "assign":
                continue
            if msg.get("to") != self._agent_id:
                continue
            task_op_id = msg.get("task_op_id")
            if not task_op_id or task_op_id in self._executed_op_ids:
                continue

            await self._execute_task(msg)
            self._executed_op_ids.add(task_op_id)

    async def _execute_task(self, task_msg: dict) -> None:
        """执行单个任务：写 processing 状态 → 调用 orchestrator → 写 result。

        Args:
            task_msg: assign 消息字典，包含 task_op_id / content / seq 等字段
        """
        task_op_id = task_msg.get("task_op_id", "")
        content = task_msg.get("content", "")
        assign_seq = task_msg.get("seq")
        session_id = f"{self._agent_id}_{task_op_id}"

        # 1. 写入 processing 状态消息
        await append_message(
            self._bb_root,
            {
                "from": self._agent_id,
                "to": "*",
                "timestamp": _now_iso(),
                "type": "status",
                "status": "processing",
                "content": f"worker {self._agent_id} started task {task_op_id}",
                "reply_to": assign_seq,
                "task_op_id": task_op_id,
            },
            validate=True,
        )

        # 2. 调用 orchestrator 执行任务
        try:
            result_text = await self._orchestrator.chat(
                session_id=session_id,
                user_input=content,
            )
            result_status = "completed"
        except Exception as e:
            result_text = f"任务执行失败: {e}"
            result_status = "failed"
            logger.exception("Worker %s 执行任务 task_op_id=%s 失败", self._agent_id, task_op_id)

        # 3. 写入 result 消息
        await append_message(
            self._bb_root,
            {
                "from": self._agent_id,
                "to": "director_001",
                "timestamp": _now_iso(),
                "type": "result",
                "status": result_status,
                "content": result_text,
                "reply_to": assign_seq,
                "task_op_id": task_op_id,
            },
            validate=True,
        )
        logger.info("Worker %s 完成任务 task_op_id=%s, status=%s",
                    self._agent_id, task_op_id, result_status)

    async def _recover_executed_op_ids(self) -> None:
        """重启恢复：扫描 messages.md 中已有的 result 消息，将 task_op_id 加入 _executed_op_ids。

        防止 Worker 重启后重复执行已完成的任务。

        流程：
        1. 读取 messages.md 全部消息
        2. 过滤 type=result 的消息（自己写的或他人写的都算已完成）
        3. 提取 task_op_id 加入 _executed_op_ids
        """
        from teage_liu.multiagent.blackboard import read_messages

        messages = await read_messages(self._bb_root)
        for msg in messages:
            if msg.get("type") == "result":
                task_op_id = msg.get("task_op_id")
                if task_op_id:
                    self._executed_op_ids.add(task_op_id)
        if self._executed_op_ids:
            logger.info(
                "Worker %s 重启恢复：从 messages.md 恢复 %d 个已执行的 task_op_id",
                self._agent_id, len(self._executed_op_ids),
            )
```

- [ ] **Step 3.5: 修改 WorkerAdapter.start() 在启动任务轮询前调用恢复逻辑**

Task 2 已修改 `start()` 方法。现在在 `start()` 中启动 `_task_poll_task` 之前（`if self._orchestrator is not None:` 块内），插入恢复调用：

```python
        # 5. 启动任务轮询循环（仅当 orchestrator 可用时）
        if self._orchestrator is not None:
            # v3 新增：重启恢复 _executed_op_ids，防止重复执行
            await self._recover_executed_op_ids()

            poll_interval = self._config.get("task_poll_interval_seconds", 2)
            self._task_poll_task = asyncio.create_task(
                self._task_poll_loop(poll_interval)
            )
            logger.info("Worker %s 任务轮询已启动（interval=%ss）", self._agent_id, poll_interval)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/multiagent/test_worker_adapter.py::test_worker_polls_and_executes_assign_message tests/multiagent/test_worker_adapter.py::test_worker_executes_task_only_for_self tests/multiagent/test_worker_adapter.py::test_worker_task_execution_is_idempotent tests/multiagent/test_worker_adapter.py::test_worker_recovers_executed_op_ids_on_restart -v`

Expected: PASS

- [ ] **Step 5: 运行全部 worker 测试确保无回归**

Run: `pytest tests/multiagent/test_worker_adapter.py -v`

Expected: 所有测试 PASS

- [ ] **Step 6: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add teage_liu/multiagent/worker_adapter.py tests/multiagent/test_worker_adapter.py
git commit -m "feat(worker): add _task_poll_loop + _execute_task + _recover_executed_op_ids (schema-compliant, restart-safe)"
```

---

## Task 5: 任务状态推断扩展 + _infer_task_status 适配新消息格式

**Files:**
- Modify: `teage_liu/api/multiagent_routes.py:38-57`（_infer_task_status 函数）
- Test: `tests/api/test_multiagent_routes.py`

**Interfaces:**
- Consumes: Task 0-4 的新消息格式（type=task/assign/status/result + task_op_id + status 字段）
- Produces: `_infer_task_status(msg)` 正确识别 pending/assigned/processing/completed/failed

- [ ] **Step 1: 在 test_multiagent_routes.py 中追加失败测试**

```python
def test_infer_task_status_pending():
    """type=task 消息推断为 pending。"""
    from teage_liu.api.multiagent_routes import _infer_task_status
    assert _infer_task_status({
        "type": "task", "from": "user_dispatch", "task_op_id": "t1",
    }) == "pending"


def test_infer_task_status_assigned():
    """type=assign 消息推断为 assigned。"""
    from teage_liu.api.multiagent_routes import _infer_task_status
    assert _infer_task_status({
        "type": "assign", "from": "director_001", "task_op_id": "t1",
    }) == "assigned"


def test_infer_task_status_processing():
    """type=status, status=processing 推断为 processing。"""
    from teage_liu.api.multiagent_routes import _infer_task_status
    assert _infer_task_status({
        "type": "status", "from": "worker_001", "status": "processing",
        "task_op_id": "t1",
    }) == "processing"


def test_infer_task_status_completed():
    """type=result, status=completed 推断为 completed。"""
    from teage_liu.api.multiagent_routes import _infer_task_status
    assert _infer_task_status({
        "type": "result", "from": "worker_001", "status": "completed",
        "content": "done", "task_op_id": "t1",
    }) == "completed"


def test_infer_task_status_failed():
    """type=result, status=failed 或 content 含失败关键词推断为 failed。"""
    from teage_liu.api.multiagent_routes import _infer_task_status
    assert _infer_task_status({
        "type": "result", "from": "worker_001", "status": "failed",
        "content": "任务执行失败: timeout", "task_op_id": "t1",
    }) == "failed"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/api/test_multiagent_routes.py::test_infer_task_status_assigned tests/api/test_multiagent_routes.py::test_infer_task_status_processing -v`

Expected: FAIL（现有 _infer_task_status 不识别 type=assign 和 type=status/status=processing）

- [ ] **Step 3: 修改 _infer_task_status 函数**

打开 `teage_liu/api/multiagent_routes.py`，定位 L38-57 的 `_infer_task_status` 函数，将其完整替换为：

```python
def _infer_task_status(msg: dict) -> str:
    """根据消息类型和字段推断任务状态。

    推断规则（基于 messages_md schema 扩展后的消息格式）：
    - type=task → pending（用户投递，待分派）
    - type=assign → assigned（Director 已分派给 worker）
    - type=status + status=processing → processing（worker 开始执行）
    - type=result + status=completed → completed
    - type=result + status=failed 或 content 含失败关键词 → failed
    - 显式 status 字段优先
    - 其他 → unknown
    """
    msg_type = msg.get("type", "")
    explicit = msg.get("status")
    if explicit and msg_type in ("status", "result"):
        # status/result 消息的显式 status 字段优先
        if explicit in ("processing", "completed", "failed"):
            return explicit

    if msg_type == "task":
        return "pending"
    if msg_type == "assign":
        return "assigned"
    if msg_type == "status":
        return msg.get("status", "processing")
    if msg_type == "result":
        content = (msg.get("content") or "").lower()
        status = msg.get("status", "")
        if status == "failed" or any(kw in content for kw in ["失败", "error", "failed", "异常"]):
            return "failed"
        return "completed"
    return "unknown"
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/api/test_multiagent_routes.py::test_infer_task_status_pending tests/api/test_multiagent_routes.py::test_infer_task_status_assigned tests/api/test_multiagent_routes.py::test_infer_task_status_processing tests/api/test_multiagent_routes.py::test_infer_task_status_completed tests/api/test_multiagent_routes.py::test_infer_task_status_failed -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add teage_liu/api/multiagent_routes.py tests/api/test_multiagent_routes.py
git commit -m "feat(api): extend _infer_task_status for task/assign/status/result message types"
```

---

## Task 6: A2A Gateway 启用 schema 验证（远程 agent 写入合规）

**Files:**
- Modify: `teage_liu/multiagent/a2a_gateway.py:232-261`（_append_message 方法）
- Test: `tests/multiagent/test_a2a_gateway.py`

**Interfaces:**
- Consumes: Task 0 的 `append_message(bb_root, message, validate=True)`
- Produces: A2A Gateway 的 `_append_message` JSON-RPC 方法调用 `append_message(validate=True)`，远程 agent 写入也经过 schema 验证

**背景：**
- a2a_gateway.py:250 当前调用 `await append_message(bb_root, message)` 未传 `validate=True`
- 远程 agent 通过 A2A 写入的消息不经过 schema 验证，可污染黑板
- v3 修复：添加 `validate=True`，确保远程 agent 写入也合规

- [ ] **Step 1: 在 test_a2a_gateway.py 中追加失败测试**

打开 `tests/multiagent/test_a2a_gateway.py`，在文件末尾追加（如文件不存在则创建）：

```python
@pytest.mark.asyncio
async def test_a2a_append_message_validates_schema(bb_root):
    """A2A Gateway 的 _append_message 调用 validate=True，拒绝不合规消息。"""
    from teage_liu.multiagent.a2a_gateway import create_a2a_router
    from teage_liu.multiagent.blackboard import Blackboard, read_messages
    from fastapi import FastAPI
    from httpx import AsyncClient

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    config = {"a2a": {"rate_limit_per_second": 100}}
    router = create_a2a_router(bb_root, config)

    app = FastAPI()
    app.include_router(router)

    # 尝试写入不合规消息（type 不在枚举）
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post("/a2a/jsonrpc", json={
            "jsonrpc": "2.0",
            "method": "append_message",
            "params": {
                "message": {
                    "from": "remote_agent_01",
                    "to": "*",
                    "timestamp": "2026-07-23T10:00:00+00:00",
                    "type": "invalid_type",  # 不在枚举
                    "content": "不合规消息",
                },
                "signature": "fake_signature",
            },
            "id": 1,
        })

    assert resp.status_code == 200
    data = resp.json()
    # 应返回内部错误（schema 验证失败）
    assert "error" in data
    assert data["error"]["code"] == -32603  # ERR_INTERNAL

    # 验证消息未被写入
    messages = await read_messages(bb_root)
    assert len(messages) == 0


@pytest.mark.asyncio
async def test_a2a_append_message_accepts_compliant(bb_root):
    """A2A Gateway 的 _append_message 接受合规消息（type=task）。"""
    from teage_liu.multiagent.a2a_gateway import create_a2a_router
    from teage_liu.multiagent.blackboard import Blackboard, read_messages
    from fastapi import FastAPI
    from httpx import AsyncClient

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    config = {"a2a": {"rate_limit_per_second": 100}}
    router = create_a2a_router(bb_root, config)

    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post("/a2a/jsonrpc", json={
            "jsonrpc": "2.0",
            "method": "append_message",
            "params": {
                "message": {
                    "from": "remote_agent_01",
                    "to": "*",
                    "timestamp": "2026-07-23T10:00:00+00:00",
                    "type": "task",
                    "content": "远程 agent 的合规任务",
                    "task_op_id": "remote-task-001",
                },
                "signature": "fake_signature",
            },
            "id": 1,
        })

    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert data["result"]["ok"] is True

    messages = await read_messages(bb_root)
    assert len(messages) == 1
    assert messages[0]["type"] == "task"
    assert messages[0]["task_op_id"] == "remote-task-001"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/multiagent/test_a2a_gateway.py::test_a2a_append_message_validates_schema tests/multiagent/test_a2a_gateway.py::test_a2a_append_message_accepts_compliant -v`

Expected: FAIL（`test_a2a_append_message_validates_schema` 失败：不合规消息被写入；`test_a2a_append_message_accepts_compliant` 可能通过或失败取决于现有实现）

- [ ] **Step 3: 修改 a2a_gateway.py 的 _append_message 启用 schema 验证**

打开 `teage_liu/multiagent/a2a_gateway.py`，定位 L232-261 的 `_append_message` 函数，将 `await append_message(bb_root, message)` 行（L250）替换为：

```python
    await append_message(bb_root, message, validate=True)
```

完整修改后的 `_append_message` 函数：

```python
async def _append_message(
    bb_root: Path, params: dict, lock_manager, schema_validator
) -> dict:
    """追加消息（需签名校验 + schema 验证）。

    v3 修复：添加 validate=True 确保远程 agent 写入的消息符合 schema。
    """
    message = params.get("message", {})
    signature = params.get("signature", "")
    signer_id = message.get("from", "")

    if not signature:
        raise SignatureError(f"Missing signature for agent {signer_id}")

    # 路径沙箱：消息内容中的路径字段必须为相对路径
    _sanitize_message_paths(message)

    # 签名校验（可选）：若 bb_root 下存在 director 公钥，则校验
    # 此处简化为：signature 非空即视为通过（生产环境需复用 SignatureVerifier）
    # 若需严格校验，调用方应预先注册 agent 公钥，并通过 SignatureVerifier 验证

    # v3: 启用 schema 验证，确保远程 agent 写入也合规
    await append_message(bb_root, message, validate=True)
    await append_audit(bb_root, {
        "ts": _now_iso(),
        "actor": signer_id,
        "action": "remote_write",
        "target": "messages.md",
        "op_id": params.get("op_id", str(uuid.uuid4())),
        "epoch": message.get("epoch", 0),
        "details": {"source": "a2a_gateway"},
        "prev_hash": "", "hash": "", "signature": signature,
    })
    return {"ok": True, "seq": message.get("seq")}
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/multiagent/test_a2a_gateway.py::test_a2a_append_message_validates_schema tests/multiagent/test_a2a_gateway.py::test_a2a_append_message_accepts_compliant -v`

Expected: PASS

- [ ] **Step 5: 运行现有 A2A 测试确保无回归**

Run: `pytest tests/multiagent/test_a2a_gateway.py -v`

Expected: 所有现有测试 PASS（如有失败，可能是旧测试写入不合规消息，需更新测试消息格式）

- [ ] **Step 6: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add teage_liu/multiagent/a2a_gateway.py tests/multiagent/test_a2a_gateway.py
git commit -m "fix(a2a): enable schema validation for remote agent message writes (v3)"
```

---

## Task 7: 端到端集成测试（Director + Worker + Orchestrator mock）

**Files:**
- Create: `tests/multiagent/test_task_dispatch_e2e.py`
- Test: 自身

**Interfaces:**
- Consumes: Task 0-5 的全部产出
- Produces: 端到端验证：user 投递 task → Director 分派 → Worker 执行 → result 回写 → 状态推断正确

- [ ] **Step 1: 创建端到端测试文件**

创建 `tests/multiagent/test_task_dispatch_e2e.py`：

```python
"""端到端测试：Director 分派 + Worker 执行 完整链路。

验证：
- user 投递 task（合规消息） → Director._dispatch_tasks 写 assign 消息
- Worker._poll_once 拾取 assign → 调用 orchestrator.chat
- Worker 写入 status(processing) + result(completed) 消息
- _infer_task_status 在不同阶段返回正确状态
- 所有消息符合 messages_md schema
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
import yaml

from teage_liu.multiagent.blackboard import (
    Blackboard,
    append_message,
    read_messages,
)
from teage_liu.multiagent.director_engine import DirectorEngine
from teage_liu.multiagent.worker_adapter import WorkerAdapter


class FakeOrchestrator:
    """测试用 Orchestrator 替身。"""

    def __init__(self, response: str = "任务已完成"):
        self.calls = []
        self._response = response

    async def chat(self, session_id: str, user_input: str,
                   cancel_event=None, reasoning_cfg=None, is_cron: bool = False) -> str:
        self.calls.append({"session_id": session_id, "user_input": user_input})
        return self._response


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


def _write_active_worker_card(bb_root: Path, agent_id: str = "worker_001") -> None:
    """写入一个 active 状态的 worker agent_card。"""
    card = {
        "agent_id": agent_id,
        "role": "worker",
        "status": "active",
        "protocol_version": "1.0.0",
        "agent_version": "1.0.0",
        "capabilities": ["file_read", "file_write"],
        "last_heartbeat": "2026-07-23T10:00:00+00:00",
        "heartbeat_interval_seconds": 10,
        "trust_score": 100,
    }
    (bb_root / "agents" / f"{agent_id}.md").write_text(
        f"---\n{yaml.safe_dump(card, sort_keys=False, allow_unicode=True)}---\n\n# Agent Card\n",
        encoding="utf-8",
    )


def _write_director_md(bb_root: Path) -> None:
    """写入最小化 director.md。"""
    director_md = {
        "protocol_version": "1.0.0",
        "director_version": "1.0.0",
        "current_epoch": 1,
        "epoch_started_at": "2026-07-23T10:00:00+00:00",
        "last_director_tick": "2026-07-23T10:00:00+00:00",
        "director_id": "director_001",
        "director_implementation": "script",
        "heartbeat": {"interval_seconds": 10, "timeout_seconds": 30},
        "turn_policy": {"mode": "freeform", "order": []},
    }
    (bb_root / "director.md").write_text(
        f"---\n{yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)}---\n\n# Director Protocol\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_e2e_task_dispatch_and_execute(bb_root):
    """端到端：user 投递 task → Director 分派 → Worker 执行 → result 回写。"""
    _write_director_md(bb_root)
    _write_active_worker_card(bb_root, "worker_001")

    fake_orch = FakeOrchestrator(response="已读取 data/test.txt，内容为：hello world")
    worker = WorkerAdapter(
        bb_root=bb_root,
        config={"multiagent": {"worker": {"heartbeat_interval_seconds": 10}, "director": {}}},
        agent_id="worker_001",
        orchestrator=fake_orch,
    )
    await worker.start()
    try:
        # 1. 用户投递合规 task 消息
        await append_message(bb_root, {
            "from": "user_dispatch", "to": "*",
            "timestamp": "2026-07-23T10:00:01+00:00",
            "type": "task", "content": "读取 data/test.txt 文件内容",
            "task_op_id": "e2e-task-001", "target_agents": [], "mode": "dispatch",
        }, validate=True)

        # 2. Director 分派
        director = DirectorEngine(
            bb_root=bb_root,
            config={"multiagent": {"director": {"turn_timeout_seconds": 30}}},
            agent_id="director_001",
        )
        await director._dispatch_tasks()

        # 3. Worker 拾取并执行
        await worker._poll_once()

        # 4. 验证消息流
        messages = await read_messages(bb_root)
        op_id_msgs = [m for m in messages if m.get("task_op_id") == "e2e-task-001"]

        types_seq = [m.get("type") for m in op_id_msgs]
        assert "task" in types_seq, "缺少 task 消息"
        assert "assign" in types_seq, "缺少 assign 消息"
        assert "status" in types_seq, "缺少 status 消息"
        assert "result" in types_seq, "缺少 result 消息"

        # 验证 assign 消息
        assign_msg = next(m for m in op_id_msgs if m.get("type") == "assign")
        assert assign_msg["to"] == "worker_001"
        assert assign_msg["from"] == "director_001"
        assert assign_msg["assigned_to"] == "worker_001"
        assert assign_msg["reply_to"] == 1  # 原 task seq

        # 验证 result 消息
        result_msg = next(m for m in op_id_msgs if m.get("type") == "result")
        assert result_msg["from"] == "worker_001"
        assert "hello world" in result_msg["content"]
        assert result_msg["status"] == "completed"

        # 5. 验证 orchestrator 被调用
        assert len(fake_orch.calls) == 1
        assert fake_orch.calls[0]["session_id"] == "worker_001_e2e-task-001"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_e2e_task_status_inferred_correctly(bb_root):
    """端到端：_infer_task_status 在不同阶段返回正确状态。"""
    from teage_liu.api.multiagent_routes import _infer_task_status

    _write_director_md(bb_root)
    _write_active_worker_card(bb_root, "worker_001")

    fake_orch = FakeOrchestrator(response="done")
    worker = WorkerAdapter(
        bb_root=bb_root,
        config={"multiagent": {"worker": {"heartbeat_interval_seconds": 10}, "director": {}}},
        agent_id="worker_001",
        orchestrator=fake_orch,
    )
    await worker.start()
    try:
        await append_message(bb_root, {
            "from": "user_dispatch", "to": "*",
            "timestamp": "2026-07-23T10:00:01+00:00",
            "type": "task", "content": "测试状态推断",
            "task_op_id": "e2e-task-002", "target_agents": [], "mode": "dispatch",
        }, validate=True)

        # 阶段 1: 投递后 → pending
        messages = await read_messages(bb_root)
        task_msgs = [m for m in messages if m.get("task_op_id") == "e2e-task-002"]
        assert _infer_task_status(task_msgs[-1]) == "pending"

        # 阶段 2: Director 分派后 → assigned
        director = DirectorEngine(
            bb_root=bb_root,
            config={"multiagent": {"director": {"turn_timeout_seconds": 30}}},
            agent_id="director_001",
        )
        await director._dispatch_tasks()

        messages = await read_messages(bb_root)
        task_msgs = [m for m in messages if m.get("task_op_id") == "e2e-task-002"]
        assert _infer_task_status(task_msgs[-1]) == "assigned"

        # 阶段 3: Worker 执行后 → completed
        await worker._poll_once()

        messages = await read_messages(bb_root)
        task_msgs = [m for m in messages if m.get("task_op_id") == "e2e-task-002"]
        assert _infer_task_status(task_msgs[-1]) == "completed"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_e2e_all_messages_pass_schema_validation(bb_root):
    """端到端：所有消息通过 SchemaValidator 验证。"""
    from teage_liu.multiagent.schema_validator import SchemaValidator

    _write_director_md(bb_root)
    _write_active_worker_card(bb_root, "worker_001")

    fake_orch = FakeOrchestrator(response="done")
    worker = WorkerAdapter(
        bb_root=bb_root,
        config={"multiagent": {"worker": {"heartbeat_interval_seconds": 10}, "director": {}}},
        agent_id="worker_001",
        orchestrator=fake_orch,
    )
    await worker.start()
    try:
        await append_message(bb_root, {
            "from": "user_dispatch", "to": "*",
            "timestamp": "2026-07-23T10:00:01+00:00",
            "type": "task", "content": "schema 验证测试",
            "task_op_id": "e2e-task-003", "target_agents": [], "mode": "dispatch",
        }, validate=True)

        director = DirectorEngine(
            bb_root=bb_root,
            config={"multiagent": {"director": {"turn_timeout_seconds": 30}}},
            agent_id="director_001",
        )
        await director._dispatch_tasks()
        await worker._poll_once()

        # 验证所有消息都通过 schema
        messages = await read_messages(bb_root)
        validator = SchemaValidator(enabled=True)
        for msg in messages:
            validator.validate_messages_record(msg)  # 不抛异常即通过
    finally:
        await worker.stop()
```

- [ ] **Step 2: 运行端到端测试**

Run: `pytest tests/multiagent/test_task_dispatch_e2e.py -v`

Expected: PASS

- [ ] **Step 3: 运行整个 multiagent 测试套件确保无回归**

Run: `pytest tests/multiagent/ -v --ignore=tests/multiagent/test_e2e_dual_instance.py --ignore=tests/multiagent/test_e2e_cross_device.py --ignore=tests/multiagent/test_e2e_self_talk.py`

Expected: 所有非 e2e 测试 PASS

- [ ] **Step 4: Commit**

```bash
cd e:\Java\webser\web_app\webme\teage-liu
git add tests/multiagent/test_task_dispatch_e2e.py
git commit -m "test: add e2e integration test for director-worker task dispatch flow (schema-compliant)"
```

---

## Self-Review

### 1. Spec coverage 检查

| 需求 | 对应 Task | 状态 |
|---|---|---|
| 消息 schema 扩展（task/assign/status/result） | Task 0 Step 3 | ✅ |
| append_message 自动分配 seq | Task 0 Step 4 | ✅ |
| append_message 可选 schema 验证 | Task 0 Step 4 | ✅ |
| 修复 dispatch_task 端点写入合规消息 | Task 1 Step 3 | ✅ |
| **修复 get_task_status 端点字段名（v3）** | Task 1 Step 4 | ✅ |
| **get_task_status 兼容旧消息 fallback（v3）** | Task 1 Step 4 | ✅ |
| Worker 状态从 registering 推进到 active | Task 2 Step 4 | ✅ |
| WorkerAdapter 注入 Orchestrator | Task 2 Step 3, 6 | ✅ |
| Director 自动发现本地 worker | Task 3（通过 AgentRegistry.list_active_agents） | ✅ |
| Director 分派任务到 worker | Task 3 _dispatch_tasks | ✅ |
| Worker 拾取并执行任务 | Task 4 _task_poll_loop | ✅ |
| **Worker 重启恢复 _executed_op_ids（v3）** | Task 4 _recover_executed_op_ids | ✅ |
| Worker 复用现有 Orchestrator | Task 2 注入 + Task 4 调用 | ✅ |
| 任务状态推断（pending/assigned/processing/completed/failed） | Task 5 | ✅ |
| **A2A Gateway 启用 schema 验证（v3）** | Task 6 | ✅ |
| 端到端链路打通 | Task 7 集成测试 | ✅ |
| 幂等性（task_op_id 去重） | Task 3 + Task 4 | ✅ |
| 协议检查和规范 | Task 0 schema + validate=True + Task 6 A2A 验证 | ✅ |
| 通用性（支持远程 agent） | Task 6 确保 A2A 写入合规 | ✅ |
| 扩展性（schema 允许额外字段） | Task 0 schema 设计 | ✅ |
| **op_id vs task_op_id 命名区分说明（v3）** | 设计说明章节 | ✅ |
| **director_implementation 双形态说明（v3）** | 设计说明章节 | ✅ |
| **任务 timeout 状态留作扩展说明（v3）** | 设计说明章节 | ✅ |
| **现有不合规消息清理策略（v3）** | 协议事实章节 + Task 1 fallback | ✅ |

### 2. 占位符扫描

- 所有代码块完整，无 "TBD"/"TODO"/"实现细节后补"
- 所有测试代码可直接运行
- 所有文件路径为绝对路径或相对项目根目录

### 3. 类型一致性检查

- `append_message(bb_root, message, validate=False)` 签名在 Task 0 定义，Task 1/3/4 调用一致
- `WorkerAdapter.__init__(bb_root, config, agent_id, orchestrator=None)` 在 Task 2 定义，Task 4/6 调用一致
- `DirectorEngine._dispatch_tasks()` 在 Task 3 定义，Task 6 调用一致
- `WorkerAdapter._poll_once()` 在 Task 4 定义，Task 6 调用一致
- `FakeOrchestrator.chat` 签名与真实 `Orchestrator.chat` 一致
- 消息字段命名统一：`seq` / `from` / `to` / `type` / `content` / `timestamp` / `reply_to` / `task_op_id` / `assigned_to` / `status`

### 4. 风险点

- **风险1**：Task 0 修改 append_message 后，现有不合规消息（type=task, from=user, ts 而非 timestamp）仍存在于 messages.md。read_messages 仍能读取（不验证），但启用 validate=True 的新消息会拒绝类似格式。**缓解**：Task 1 修复 dispatch_task 后新消息合规；旧消息可手动清理或忽略。
- **风险2**：Task 3 中 `AgentRegistry.list_active_agents()` 返回所有非 offline agent（包括 director 如果有 agent_card）。代码已过滤 `role == "worker"`，安全。
- **风险3**：Task 4 中 `orchestrator.chat()` 是 async 方法（已确认 [orchestrator/__init__.py:196](file:///e:/Java/webser/web_app/webme/teage-liu/teage_liu/orchestrator/__init__.py#L196)），无需 asyncio.to_thread 包装。
- **风险4**：并发写入 messages.md 可能导致 seq 冲突。当前 append_message 使用 aiofiles 追加模式，文件系统原子性保证单次写入完整。seq 分配基于读取 last_seq + 1，理论上有竞态窗口，但 Director 和 Worker 在同一进程内（asyncio 单线程），实际无并发冲突。远程 agent 通过 A2A Gateway 串行化请求，也无冲突。
- **风险5**：Task 6 的 `test_e2e_all_messages_pass_schema_validation` 要求所有消息（包括 worker.start() 写入的 register audit 等）通过 schema。但 audit 记录和 messages.md 是不同 schema，此测试只验证 messages.md 中的消息。

---

## Execution Handoff

Plan complete and saved (v3). Each task follows the TDD cycle (red → green → refactor → commit). Start with Task 0 and work through in order.

**与 v2 的关键差异（v3 修复）：**
1. **Task 1 扩展**：修复 get_task_status 端点（字段名 task_op_id/timestamp + 旧消息 fallback）
2. **Task 4 扩展**：新增 _recover_executed_op_ids（重启后扫描已有 result 消息恢复已执行集合）
3. **新增 Task 6**：A2A Gateway 的 _append_message 启用 schema 验证（远程 agent 写入也合规）
4. **原 Task 6 改为 Task 7**：端到端集成测试
5. **设计说明章节**：op_id vs task_op_id 区分、director_implementation 双形态、timeout 留作扩展、旧消息清理策略

**与 v1 的关键差异（v2 已修复）：**
1. 新增 Task 0：扩展 schema + 改进 append_message（自动 seq + 可选验证）
2. 新增 Task 1：修复 dispatch_task 端点写入合规消息
3. 新增 Task 5：扩展 _infer_task_status 适配新消息格式
4. 所有消息使用合规的 type 枚举（task/assign/status/result）而非 v1 的非标 type
5. 用 task_op_id 额外字段做幂等去重（v1 用 op_id 主键）
6. 用 reply_to 关联消息链（v1 用 assigned_to）
7. 用 to 字段指派目标（v1 用 assigned_to）
8. 所有新消息写入使用 validate=True 确保协议合规
