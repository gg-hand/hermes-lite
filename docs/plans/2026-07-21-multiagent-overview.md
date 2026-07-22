---
plan_id: 2026-07-21-multiagent-overview
title: Multi-Agent 改造总览（4 Plan 依赖图 + 统一验收方案）
created_at: 2026-07-21
depends_on:
  - 2026-07-21-multiagent-phase1-foundation
  - 2026-07-21-multiagent-phase2-collaboration
  - 2026-07-21-multiagent-phase3-4-cross-device
  - 2026-07-21-multiagent-phase5-frontend
spec_ref: docs/superpowers/specs/2026-07-20-多agent协作机制-design.md
revision: v1.0.3
---

# Multi-Agent 改造总览

本文档是 Hermes Multi-Agent Protocol v1.0.3 实施的**顶层规划入口**，统一管理 4 个分阶段 Plan 的依赖关系、产出边界、统一验收方案与子智能体委派策略。任何子智能体执行单个 Plan 前**必须先阅读本文档**确认依赖就绪。

---

## 1. 整体架构与定位

### 1.1 改造目标

将 Hermes 从单 agent 架构升级为多 agent 协作架构，支持：

- **Phase 1（基础层）**：单实例本地黑板 self-talk（一个进程扮演 Director + Worker）
- **Phase 2（协作层）**：双实例本地协作（两个 hermes-lite 进程通过文件黑板协作）
- **Phase 3-4（跨设备层）**：多设备跨网络协作（A2A Gateway + httpx + JSON-RPC）
- **Phase 5（前端层）**：用户可视化配置与状态监控（SSE + 状态指示器）

### 1.2 设计原则

| 原则 | 实施体现 |
|------|----------|
| 文件优先（File-First） | 所有协议文件存放 `${HERMES_BB_DIR}`，本地协作零外部依赖 |
| 全链路异步 | 所有 I/O 使用 async/await，禁止阻塞调用 |
| 路径沙箱 | 协议文件禁绝绝对路径，相对 `bb_root` + 正斜杠分隔 |
| 配置热更新 | `multiagent.enabled/role` 可热更新；`blackboard_dir` 需重启 |
| TDD 流程 | 每个 Task 严格 RED → GREEN → REFACTOR → commit |
| 渐进发布 | Phase 1→2→3→4→5 逐层交付，每层独立可验收 |
| 复用现有基础设施 | DI 容器 / Orchestrator / ReactLoop / SSE 通道全部复用 |

### 1.3 spec 与 design 对齐

| 文档 | 路径 | 状态 |
|------|------|------|
| 设计文档 | `docs/superpowers/specs/2026-07-20-多agent协作机制-design.md` | v1.0.3 修订完成（commit `5dbd445`） |
| 修订 spec | `docs/superpowers/specs/2026-07-21-多agent协作机制-v1.0.3修订-spec.md` | 已完善（15 处修正） |
| Plan 1 | `docs/plans/2026-07-21-multiagent-phase1-foundation.md` | 10 Task |
| Plan 2 | `docs/plans/2026-07-21-multiagent-phase2-collaboration.md` | 10 Task |
| Plan 3 | `docs/plans/2026-07-21-multiagent-phase3-4-cross-device.md` | 9 Task |
| Plan 4 | `docs/plans/2026-07-21-multiagent-phase5-frontend.md` | 6 Task |
| **本总览** | `docs/plans/2026-07-21-multiagent-overview.md` | 顶层入口 |

---

## 2. 4 Plan 依赖关系图

### 2.1 依赖拓扑

```
                    ┌─────────────────────────────┐
                    │  Plan 1: Phase 1 基础层      │
                    │  (10 Task, 7 模块 + 8 schema) │
                    │  产出：blackboard / 锁 /      │
                    │       audit / registry /     │
                    │       watchdog / recovery    │
                    └──────────────┬──────────────┘
                                   │
                                   │ depends_on
                                   ▼
                    ┌─────────────────────────────┐
                    │  Plan 2: Phase 2 协作层      │
                    │  (10 Task, Director + Worker) │
                    │  产出：DirectorEngine /       │
                    │       WorkerAdapter /         │
                    │       ReactLoop 7 集成点 /    │
                    │       自治模式 / 信任分       │
                    └──────────────┬──────────────┘
                                   │
                                   │ depends_on
                                   ▼
                    ┌─────────────────────────────┐
                    │  Plan 3: Phase 3-4 跨设备层  │
                    │  (9 Task, A2A Gateway)       │
                    │  产出：a2a_gateway /          │
                    │       a2a_client /            │
                    │       remote_agent_adapter /  │
                    │       election /              │
                    │       path_sandbox /          │
                    │       rate_limiter            │
                    └──────────────┬──────────────┘
                                   │
                                   │ depends_on
                                   ▼
                    ┌─────────────────────────────┐
                    │  Plan 4: Phase 5 前端适配    │
                    │  (6 Task, SSE + UI)          │
                    │  产出：multiagent_routes /    │
                    │       multiagent-settings.js │
                    │       multiagent-sse.js /     │
                    │       multiagent-render.js / │
                    │       multiagent.css          │
                    └─────────────────────────────┘
```

### 2.2 依赖矩阵

| Plan | 直接依赖 | 间接依赖 | 可并行 |
|------|----------|----------|--------|
| Plan 1 | 无 | 无 | — |
| Plan 2 | Plan 1 Task 1-9 | 无 | 否 |
| Plan 3 | Plan 1 + Plan 2 | 无 | 否 |
| Plan 4 | Plan 1 + Plan 2 + Plan 3 | 无 | 否 |

> **严格串行**：4 个 Plan 必须按顺序执行，不可并行。每个 Plan 的 Task 也是严格串行（前序 Task 的 GREEN 通过是后序 Task RED 的前置）。

### 2.3 跨 Plan 接口契约

| 接口 | 提供方 | 消费方 | 契约文件 |
|------|--------|--------|----------|
| `Blackboard` 类 | Plan 1 Task 2 | Plan 2 Task 1-10 / Plan 3 Task 1-7 / Plan 4 Task 1 | `hermes/multiagent/blackboard.py` |
| `LockManager` 单例 | Plan 1 Task 4 | Plan 2 Task 2 / Plan 3 Task 1-4 | `hermes/multiagent/file_lock.py` |
| `MultiAgentAuditLogger` | Plan 1 Task 5 | Plan 2 Task 3-6 / Plan 3 Task 1-4 | `hermes/multiagent/audit_logger.py` |
| `AgentRegistry` | Plan 1 Task 6 | Plan 2 Task 2 / Plan 3 Task 3 / Plan 4 Task 1 | `hermes/multiagent/agent_registry.py` |
| `RecoveryCoordinator` | Plan 1 Task 8 | Plan 2 Task 5 / Plan 3 Task 4 | `hermes/multiagent/recovery.py` |
| 异常类（8 个） | Plan 1 Task 1 | Plan 2 / Plan 3 / Plan 4 全部 Task | `hermes/multiagent/exceptions.py` |
| `DirectorEngine` | Plan 2 Task 1 | Plan 3 Task 4 / Plan 4 Task 1 | `hermes/multiagent/director_engine.py` |
| `WorkerAdapter` | Plan 2 Task 2 | Plan 3 Task 3 / Plan 4 Task 1 | `hermes/multiagent/worker_adapter.py` |
| `SignatureVerifier` | Plan 2 Task 5 | Plan 3 Task 2-4 | `hermes/multiagent/signature.py` |
| `AutonomousModeController` | Plan 2 Task 7 | Plan 4 Task 1（状态查询） | `hermes/multiagent/autonomous.py` |
| `A2AGateway` | Plan 3 Task 1 | Plan 4 Task 1（状态聚合） | `hermes/multiagent/a2a_gateway.py` |
| `RemoteAgentAdapter` | Plan 3 Task 3 | Plan 4 Task 1（远程 agent 显示） | `hermes/multiagent/remote_agent_adapter.py` |

---

## 3. 各 Plan 产出与边界

### 3.1 Plan 1: Phase 1 基础层

| 维度 | 内容 |
|------|------|
| **范围** | 单实例本地黑板 self-talk |
| **新增模块** | 7 个（blackboard / schema_validator / file_lock / audit_logger / agent_registry / watchdog_watcher / recovery） |
| **新增 schema** | 7 个（protocol_md / director_md / status_json / agent_card / messages_md / task_md / audit_record） |
| **新增异常类** | 8 个（CASConflictError / CASVersionMismatchError / FencingTokenMismatchError / LockAcquisitionError / NotMyTurnError / DirectorUnavailableError / GhostWriteAttemptError / CapabilityNotInCardError / A2AGatewayError） |
| **修改现有文件** | requirements.txt / config.py / config_helpers.py / container.py / lifespan.py / tool_error.py |
| **退出条件** | self-talk 端到端测试通过（单进程模拟 Director + Worker 对话） |
| **Task 数** | 10 |

### 3.2 Plan 2: Phase 2 协作层

| 维度 | 内容 |
|------|------|
| **范围** | 双实例本地协作（Director + Worker 进程隔离） |
| **新增模块** | 7 个（director_engine / worker_adapter / signature / injection_isolator / autonomous / director_cli / worker_cli） |
| **ReactLoop 集成点** | 7 个（system prompt / capabilities / SessionManager hook × 2 / 轮次校验 / 心跳监测 / LLM 上下文隔离） |
| **关键机制** | Director 双形态 / Epoch / 启动互斥锁 / 硬超时强抢 / ed25519 签名 / 信任分 / 自治模式 / 二次确认退出 |
| **派生文件** | messages.pending.md / messages.replay_candidates.md |
| **退出条件** | 双实例协作 30 分钟无 audit 损坏 |
| **Task 数** | 10 |

### 3.3 Plan 3: Phase 3-4 跨设备层

| 维度 | 内容 |
|------|------|
| **范围** | 多设备跨网络协作（A2A Gateway + httpx + JSON-RPC） |
| **新增模块** | 6 个（a2a_gateway / a2a_client / remote_agent_adapter / election / path_sandbox / rate_limiter） |
| **通信协议** | JSON-RPC 2.0（10 个错误码：-32700 / -32600 / -32601 / -32602 / -32603 / -32001 / -32002 / -32003 / -32004 / -32005） |
| **Director 选举** | epoch + fencing_token + 字典序仲裁 + 心跳超时抢占 |
| **限流策略** | 单 IP 每秒 100 次（滑动窗口 + 线程安全） |
| **TLS 策略** | 本地 http / 跨网络 https（配置 `a2a.tls.enabled`） |
| **退出条件** | 两设备端到端协作 + Director 故障切换成功 |
| **Task 数** | 9 |

### 3.4 Plan 4: Phase 5 前端适配

| 维度 | 内容 |
|------|------|
| **范围** | 用户可视化配置与状态监控 |
| **新增后端** | multiagent_routes.py（REST + SSE 端点） |
| **新增前端** | multiagent-settings.js / multiagent-sse.js / multiagent-render.js / multiagent.css |
| **SSE 通道** | `multiagent_alert`（事件类型：director_state_change / agent_join / agent_leave / autonomous_enter / autonomous_exit） |
| **状态指示器** | 三色（健康绿 / 降级黄 / 自治橙 / 故障红） |
| **配置入口** | 复用 chat.html 齿轮图标设置模态框（不新建独立配置页） |
| **退出条件** | Playwright E2E 测试通过（配置 → SSE 推送 → 状态显示） |
| **Task 数** | 6 |

---

## 4. 统一验收方案

### 4.1 验收层次

```
┌─────────────────────────────────────────────────────────┐
│  Layer 4: 跨 Plan 集成验收（本总览执行）                 │
│  - 全链路 self-talk → 双实例 → 跨设备 → 前端监控        │
├─────────────────────────────────────────────────────────┤
│  Layer 3: 单 Plan 端到端验收（各 Plan 末 Task）         │
│  - Plan 1: test_e2e_self_talk.py                        │
│  - Plan 2: test_e2e_dual_instance.py                   │
│  - Plan 3: test_e2e_cross_device.py                    │
│  - Plan 4: test_multiagent_ui.py (Playwright)          │
├─────────────────────────────────────────────────────────┤
│  Layer 2: 单 Task TDD 验收（每个 Task 内）              │
│  - RED: pytest 失败                                    │
│  - GREEN: pytest 通过                                  │
│  - REFACTOR: 测试仍通过 + 代码质量提升                 │
├─────────────────────────────────────────────────────────┤
│  Layer 1: spec coverage 静态验收（Grep 检查）           │
│  - 每个 spec §4 修订项对应代码事实存在                 │
└─────────────────────────────────────────────────────────┘
```

### 4.2 跨 Plan 集成验收清单

执行顺序：Plan 1 → Plan 2 → Plan 3 → Plan 4 → 本验收

#### 4.2.1 模块完整性验收

```bash
# 验收：所有新增模块文件存在
test -f hermes/multiagent/__init__.py
test -f hermes/multiagent/blackboard.py
test -f hermes/multiagent/schema_validator.py
test -f hermes/multiagent/file_lock.py
test -f hermes/multiagent/audit_logger.py
test -f hermes/multiagent/agent_registry.py
test -f hermes/multiagent/watchdog_watcher.py
test -f hermes/multiagent/recovery.py
test -f hermes/multiagent/exceptions.py
test -f hermes/multiagent/director_engine.py
test -f hermes/multiagent/worker_adapter.py
test -f hermes/multiagent/signature.py
test -f hermes/multiagent/injection_isolator.py
test -f hermes/multiagent/autonomous.py
test -f hermes/multiagent/director_cli.py
test -f hermes/multiagent/worker_cli.py
test -f hermes/multiagent/a2a_gateway.py
test -f hermes/multiagent/a2a_client.py
test -f hermes/multiagent/remote_agent_adapter.py
test -f hermes/multiagent/election.py
test -f hermes/multiagent/path_sandbox.py
test -f hermes/multiagent/rate_limiter.py
test -f hermes/api/multiagent_routes.py
test -f web/js/multiagent-settings.js
test -f web/js/multiagent-sse.js
test -f web/js/multiagent-render.js
test -f web/css/multiagent.css
```

#### 4.2.2 schema 文件完整性验收

```bash
test -f data/schemas/multiagent/protocol_md.schema.yaml
test -f data/schemas/multiagent/director_md.schema.yaml
test -f data/schemas/multiagent/status_json.schema.json
test -f data/schemas/multiagent/agent_card.schema.yaml
test -f data/schemas/multiagent/messages_md.schema.yaml
test -f data/schemas/multiagent/task_md.schema.yaml
test -f data/schemas/multiagent/audit_record.schema.json
```

#### 4.2.3 测试套件全量通过

```bash
# 在 hermes-lite 目录执行
pytest tests/multiagent/ -v --tb=short
pytest tests/api/test_multiagent_routes.py -v
pytest tests/e2e/test_multiagent_ui.py -v
```

**预期**：所有测试通过，无 skip（除非标注 `@pytest.mark.skip(reason="...")` 且 reason 合理）。

#### 4.2.4 spec coverage 全量检查（Grep 验收）

以下 36 项 Grep 检查对齐 v1.0.3 修订 spec §4 的 23 个修订项 + 13 个补充检查：

```bash
# === Plan 1 范围（spec §4.1-§4.4） ===

# §4.1 异常类完整（8 个）
grep -E "class (CASConflictError|CASVersionMismatchError|FencingTokenMismatchError|LockAcquisitionError|NotMyTurnError|DirectorUnavailableError|GhostWriteAttemptError|CapabilityNotInCardError|A2AGatewayError)" hermes/multiagent/exceptions.py

# §4.2 schema 文件（7 个）
grep -l "schema" data/schemas/multiagent/*.yaml data/schemas/multiagent/*.json

# §4.3 blackboard 路径沙箱（绝对路径拒绝）
grep -E "(is_absolute|sanitize_path|to_absolute)" hermes/multiagent/blackboard.py

# §4.4 file_lock CAS + fencing_token + grace_period
grep -E "(fencing_token|grace_period|acquire|release)" hermes/multiagent/file_lock.py

# === Plan 2 范围（spec §4.5-§4.11） ===

# §4.5 flush 流程（messages.pending.md 幂等）
grep -E "(messages\.pending\.md|flush|op_id)" hermes/multiagent/director_engine.py

# §4.6 append_audit append-only 语义（无 .tmp + os.replace）
grep -v "os.replace" hermes/multiagent/audit_logger.py
grep -E "(append_only|open.*mode.*a)" hermes/multiagent/audit_logger.py

# §4.7 Director 双形态（agent / script）
grep -E "(director_implementation|agent|script)" hermes/multiagent/director_engine.py

# §4.8 Epoch 机制
grep -E "(current_epoch|epoch)" hermes/multiagent/director_engine.py

# §4.9 启动互斥锁 + 硬超时强抢
grep -E "(director\.lock|emergency_release|F_SETLK|LockFileEx)" hermes/multiagent/director_engine.py

# §4.10 ed25519 签名
grep -E "(ed25519|director_signature|SignatureVerifier|VerifyResult)" hermes/multiagent/signature.py

# §4.11 自治模式 + 二次确认退出
grep -E "(autonomous|confirm_exit|rollback_exit|AutonomousModeController)" hermes/multiagent/autonomous.py

# 信任分管理
grep -E "(trust_score|degraded_threshold|rejected_threshold|force_offline_threshold)" hermes/multiagent/director_engine.py

# InjectionIsolator 全链路异步
grep -E "(async def scan_and_tag|async def build_llm_context)" hermes/multiagent/injection_isolator.py

# ReactLoop 7 集成点
grep -E "(_build_multiagent_prompt|_check_capabilities|_session_hook|_check_turn|_heartbeat|InjectionIsolator)" hermes/orchestrator.py

# === Plan 3 范围（spec §4.12-§4.18） ===

# §4.12 A2A Gateway JSON-RPC 2.0
grep -E "(jsonrpc.*2\.0|method|params|id)" hermes/multiagent/a2a_gateway.py

# §4.13 httpx 异步客户端
grep -E "(httpx\.AsyncClient|async with)" hermes/multiagent/a2a_client.py

# §4.14 远程 agent 适配器
grep -E "(RemoteAgentAdapter|register_remote)" hermes/multiagent/remote_agent_adapter.py

# §4.15 Director 跨设备选举
grep -E "(Election|ElectionResult|epoch|lexicographic)" hermes/multiagent/election.py

# §4.16 路径沙箱（sanitize_path + sanitize_dict_paths）
grep -E "(sanitize_path|to_absolute|sanitize_dict_paths)" hermes/multiagent/path_sandbox.py

# §4.17 限流器（滑动窗口）
grep -E "(RateLimiter|sliding_window|100)" hermes/multiagent/rate_limiter.py

# §4.18 JSON-RPC 错误码（10 个）
grep -E "(-32700|-32600|-32601|-32602|-32603|-32001|-32002|-32003|-32004|-32005)" hermes/multiagent/a2a_gateway.py

# === Plan 4 范围（spec §4.19-§4.23） ===

# §4.19 multiagent_alert SSE 通道
grep -E "(multiagent_alert|text/event-stream)" hermes/api/multiagent_routes.py

# §4.20 前端配置 UI
grep -E "(multiagent-settings|multiagent\.enabled|multiagent\.role)" web/js/multiagent-settings.js

# §4.21 前端 SSE 订阅
grep -E "(multiagent-sse|EventSource|multiagent_alert)" web/js/multiagent-sse.js

# §4.22 状态指示器（三色）
grep -E "(healthy|degraded|autonomous|fault)" web/js/multiagent-render.js

# §4.23 容器映射 + 热更新边界
grep -E "(multiagent.*:.*\[|_RESTART_REQUIRED_KEYS)" hermes/container.py hermes/config_helpers.py
```

#### 4.2.5 容器注册与热更新验收

```bash
# CONFIG_TO_COMPONENTS 新增 multiagent 段
grep -E '"multiagent":' hermes/container.py

# _RESTART_REQUIRED_KEYS 包含 multiagent.blackboard_dir
grep -E "multiagent\.blackboard_dir" hermes/config_helpers.py

# lifespan 注册 multiagent 组件
grep -E "multiagent" hermes/lifespan.py
```

#### 4.2.6 路径规范验收（project_memory 硬约束）

```bash
# 协议文件中禁绝绝对路径（运行时动态生成检查）
# 此项需通过单元测试覆盖：tests/multiagent/test_blackboard.py 中应包含
# test_protocol_files_no_absolute_path 测试用例
pytest tests/multiagent/test_blackboard.py::test_protocol_files_no_absolute_path -v

# 路径沙箱单元测试
pytest tests/multiagent/test_path_sandbox.py -v
```

#### 4.2.7 端到端场景验收

| 场景 | 测试文件 | 验收点 |
|------|----------|--------|
| 单进程 self-talk | `tests/multiagent/test_e2e_self_talk.py` | Director + Worker 同进程对话 5 轮 |
| 双实例本地协作 | `tests/multiagent/test_e2e_dual_instance.py` | 两进程协作 30 分钟无 audit 损坏 |
| Director 崩溃恢复 | `tests/multiagent/test_e2e_dual_instance.py::test_director_crash_recovery` | Director kill 后 Worker 自治，Director 重启后退出自治 |
| 跨设备协作 | `tests/multiagent/test_e2e_cross_device.py` | 两设备通过 A2A Gateway 协作 |
| Director 跨设备选举 | `tests/multiagent/test_e2e_cross_device.py::test_director_election` | 多设备 Director 选举 + 故障切换 |
| 前端配置 + 监控 | `tests/e2e/test_multiagent_ui.py` | Playwright 模拟用户配置 + SSE 推送 + 状态显示 |

### 4.3 验收执行流程

```bash
# 1. 在 hermes-lite 目录执行全量测试
cd hermes-lite
pytest tests/ -v --tb=short 2>&1 | tee test_output.log

# 2. 执行 spec coverage 检查（Python 脚本，封装 §4.2.4 的 36 项 Grep）
python scripts/verify_multiagent_spec_coverage.py --all 2>&1 | tee grep_output.log

# 3. 单 Plan 范围验收（可选）
python scripts/verify_multiagent_spec_coverage.py plan1  # 仅验收 Plan 1 范围
python scripts/verify_multiagent_spec_coverage.py plan2  # 仅验收 Plan 2 范围
python scripts/verify_multiagent_spec_coverage.py plan3  # 仅验收 Plan 3 范围
python scripts/verify_multiagent_spec_coverage.py plan4  # 仅验收 Plan 4 范围

# 4. 端到端场景测试
pytest tests/multiagent/test_e2e_self_talk.py -v
pytest tests/multiagent/test_e2e_dual_instance.py -v
pytest tests/multiagent/test_e2e_cross_device.py -v
pytest tests/e2e/test_multiagent_ui.py -v

# 5. 验收报告（Python 脚本输出 JSON，便于 CI 集成）
python scripts/verify_multiagent_spec_coverage.py --all --format json > verification_report.json
```

---

## 5. 子智能体委派策略

### 5.1 委派原则

1. **顺序委派**：严格按 Plan 1 → Plan 2 → Plan 3 → Plan 4 顺序，前序 Plan 验收通过后才能启动后序 Plan
2. **单 Plan 单智能体**：每个 Plan 由一个独立的 `general_purpose_task` 子智能体完整执行，避免中途切换
3. **TDD 强制**：子智能体必须严格遵循 RED → GREEN → REFACTOR → commit 流程，每个 Task 完成后提交一次
4. **验收阻断**：子智能体完成 Plan 后必须执行该 Plan 的端到端验收（Layer 3），通过后才能委派下一个 Plan
5. **总览验收**：4 个 Plan 全部完成后，由主智能体执行 Layer 4 跨 Plan 集成验收

### 5.2 委派任务模板

对每个 Plan，使用以下模板委派子智能体：

```
任务：执行 Plan N: <Plan 标题>

Plan 文档：e:\Java\webser\web_app\webme\hermes-lite\docs\plans\<plan-file>.md
工作目录：e:\Java\webser\web_app\webme\hermes-lite

执行要求：
1. 严格遵循 TDD 流程：每个 Task 先写失败测试（RED），验证失败后再写最小实现（GREEN），通过后重构（REFACTOR），最后 git commit
2. 完整执行 Plan 文档中的所有 Task（共 N 个），不可跳过任何 Task
3. 每个 Task 完成后立即 git commit，commit message 格式：`feat(multiagent): Plan N Task M - <task title>`
4. Plan 1 Task 1 额外创建 `scripts/verify_multiagent_spec_coverage.py`（Python 验收脚本，封装 §4.2.4 的 36 项 Grep 检查，支持 `--all` / 单 Plan 参数 / `--format json` 输出）
5. 全部 Task 完成后，执行 Plan 文档末尾的端到端验收测试 + `python scripts/verify_multiagent_spec_coverage.py plan<N>` 单 Plan 验收
6. 验收通过后，返回完整的执行报告：
   - 每个 Task 的 commit hash
   - 测试通过情况（pytest 输出摘要）
   - 端到端验收结果
   - Python 验收脚本输出（plan<N> 范围）
   - 遇到的问题与解决方案

前置依赖：<列出依赖的 Plan 与 Task>
参考文档：
- 设计文档：docs/superpowers/specs/2026-07-20-多agent协作机制-design.md
- 修订 spec：docs/superpowers/specs/2026-07-21-多agent协作机制-v1.0.3修订-spec.md
- 总览文档：docs/plans/2026-07-21-multiagent-overview.md

注意事项：
- 全链路异步，禁止阻塞调用
- 路径沙箱：协议文件禁绝绝对路径
- 配置热更新：可调整配置支持热更新
- 异常类风格：@dataclass(kw_only=True) + ErrorStage enum
- 容器注册键避免与现有组件冲突（如 multiagent_audit_logger 而非 audit_logger）
- commit 粒度：每个 Task 一次 commit（共 35 个），格式 `feat(multiagent): Plan N Task M - <task title>`
```

### 5.3 委派顺序与依赖检查

| 步骤 | 委派任务 | 前置检查 | 验收检查 |
|------|----------|----------|----------|
| 1 | Plan 1（Phase 1 基础层） | 无 | `pytest tests/multiagent/test_e2e_self_talk.py` 通过 |
| 2 | Plan 2（Phase 2 协作层） | Plan 1 端到端验收通过 | `pytest tests/multiagent/test_e2e_dual_instance.py` 通过 |
| 3 | Plan 3（Phase 3-4 跨设备层） | Plan 2 端到端验收通过 | `pytest tests/multiagent/test_e2e_cross_device.py` 通过 |
| 4 | Plan 4（Phase 5 前端适配） | Plan 3 端到端验收通过 | `pytest tests/e2e/test_multiagent_ui.py` 通过 |
| 5 | 总览验收（主智能体执行） | 4 个 Plan 全部通过 | §4.2 全部 36 项 Grep + 端到端场景 |

### 5.4 子智能体选择

使用 `Task` 工具，`subagent_type=general_purpose_task`，因为：

- 每个 Plan 涉及多文件创建/修改 + TDD 流程 + git commit
- 需要执行 RunCommand 运行 pytest
- 需要使用 Write/Edit/Grep 等工具
- 输出量大（每个 Plan 产出 6-10 个模块 + 测试），适合子智能体隔离上下文

### 5.5 失败处理

| 失败场景 | 处理策略 |
|----------|----------|
| 单 Task RED 失败（测试不报错） | 子智能体自查测试逻辑，确认测试正确触发待实现功能 |
| 单 Task GREEN 失败（实现不通过） | 子智能体修复实现，最多重试 3 次后回报主智能体 |
| 端到端验收失败 | 子智能体回报失败原因 + 相关日志，主智能体决策（修复 / 回滚 / 升级到人工） |
| 跨 Plan 依赖断裂 | 主智能体检查前序 Plan 产出，必要时回退到前序 Plan 修复 |

---

## 6. 整体执行顺序

### 6.1 串行执行（推荐）

```
主智能体: 创建 4 Plan + 总览（已完成）
    ↓
主智能体: 委派 Plan 1 子智能体
    ↓
Plan 1 子智能体: 执行 10 Task + 端到端验收
    ↓
主智能体: 检查 Plan 1 验收报告 → 委派 Plan 2 子智能体
    ↓
Plan 2 子智能体: 执行 10 Task + 端到端验收
    ↓
主智能体: 检查 Plan 2 验收报告 → 委派 Plan 3 子智能体
    ↓
Plan 3 子智能体: 执行 9 Task + 端到端验收
    ↓
主智能体: 检查 Plan 3 验收报告 → 委派 Plan 4 子智能体
    ↓
Plan 4 子智能体: 执行 6 Task + 端到端验收
    ↓
主智能体: 执行总览 Layer 4 跨 Plan 集成验收
    ↓
主智能体: 汇总验收报告 → 用户确认
```

### 6.2 工作量估算

> 以下不作为时间承诺，仅用于资源规划。

| Plan | Task 数 | 新增文件数 | 测试文件数 | 复杂度 |
|------|---------|-----------|-----------|--------|
| Plan 1 | 10 | 7 模块 + 8 测试 + 7 schema = 22 | 8 | 中（基础设施） |
| Plan 2 | 10 | 7 模块 + 7 测试 + 2 CLI = 16 | 7 | 高（Director + ReactLoop 集成） |
| Plan 3 | 9 | 6 模块 + 7 测试 = 13 | 7 | 高（跨设备 + 选举） |
| Plan 4 | 6 | 4 前端 + 1 后端 + 2 测试 = 7 | 2 | 低（UI 适配） |
| **总计** | **35** | **58** | **24** | — |

---

## 7. 风险与缓解

| 风险 | 概率 | 影响 | 缓解策略 |
|------|------|------|----------|
| Plan 2 ReactLoop 集成破坏现有单 agent 流程 | 中 | 高 | Plan 2 Task 6 必须包含现有 ReactLoop 测试回归 |
| Plan 3 跨设备锁 TTL 计算偏差导致死锁 | 中 | 高 | 单元测试覆盖不同 RTT 场景 + 超时兜底 |
| Plan 4 SSE 通道与现有 SSE 冲突 | 低 | 中 | 复用现有 SSE 框架，新增独立通道名 `multiagent_alert` |
| 路径沙箱在 Windows 上失效 | 中 | 高 | 跨平台测试（test_path_sandbox.py 覆盖 Windows 路径） |
| Director 选举脑裂 | 低 | 高 | epoch + 字典序仲裁 + 心跳超时三重保障 |
| 全链路异步被阻塞调用破坏 | 中 | 中 | CI 静态检查 + 单元测试检测阻塞调用 |

---

## 8. 用户决策点（已确认）

以下决策已于 2026-07-21 由用户确认：

### 8.1 执行启动方式 ✅ 已决策：先审核文档

- **用户选择**：先审核 4 个 Plan 文档与总览，确认无误后再执行
- **执行流程**：用户审核 → 确认 → 主智能体委派 Plan 1 子智能体 → 按 §6.1 串行执行
- **当前状态**：等待用户审核完成

### 8.2 验收脚本形式 ✅ 已决策：Python 脚本

- **用户选择**：编写 Python 验收脚本（更易维护和扩展）
- **实施位置**：在 Plan 1 Task 1 中创建 `scripts/verify_multiagent_spec_coverage.py`
- **脚本职责**：
  - 封装 §4.2 中的 36 项 Grep 检查为 Python 函数
  - 输出结构化验收报告（JSON + 控制台表格）
  - 支持单 Plan 验收 / 全量验收两种模式
  - 失败时返回非零退出码，便于 CI 集成
- **接口示例**：
  ```python
  # scripts/verify_multiagent_spec_coverage.py
  def verify_all() -> dict:
      """执行全部 36 项 Grep 检查，返回 {passed, failed, details}。"""
  
  def verify_plan(plan_id: str) -> dict:
      """仅执行指定 Plan 范围的 Grep 检查。"""
  
  if __name__ == "__main__":
      import sys, json
      result = verify_all() if "--all" in sys.argv else verify_plan(sys.argv[1])
      print(json.dumps(result, ensure_ascii=False, indent=2))
      sys.exit(0 if result["failed"] == 0 else 1)
  ```

### 8.3 commit 粒度 ✅ 已决策：每个 Task 一次 commit

- **用户选择**：每个 Task 一次 commit（共 35 个 commit）
- **commit message 格式**：`feat(multiagent): Plan N Task M - <task title>`
- **示例**：
  - `feat(multiagent): Plan 1 Task 1 - 项目脚手架 + 依赖升级`
  - `feat(multiagent): Plan 1 Task 2 - blackboard.py 黑板目录读写`
  - `feat(multiagent): Plan 2 Task 1 - DirectorEngine 核心引擎`

---

## 9. 后续维护

### 9.1 文档同步

- 代码变更必须同步更新 design.md（若设计调整）
- spec §4 修订项新增时，必须同步更新本总览 §4.2.4 Grep 验收清单
- 新增 Plan 时，必须更新 §2 依赖图与 §3 产出边界

### 9.2 回归测试

- 任何 Plan 的代码变更都必须重跑该 Plan 的端到端验收 + 总览 Layer 4 集成验收
- CI 中应包含 §4.2.3 测试套件全量通过检查

### 9.3 监控指标

- Plan 2 上线后，监控 Director 心跳超时频率
- Plan 3 上线后，监控 A2A Gateway 请求延迟与错误率
- Plan 4 上线后，监控 SSE 连接数与重连频率

---

## 10. 附录

### 10.1 术语表

| 术语 | 含义 |
|------|------|
| BB_ROOT | Blackboard 根目录（环境变量 `HERMES_BB_DIR`） |
| Director | 协调者角色，负责轮次推进与冲突仲裁 |
| Worker | 工作者角色，执行具体任务 |
| Epoch | Director 任期编号，单调递增 |
| Fencing Token | 锁版本号，防止旧 Director 写入 |
| CAS | Compare-And-Swap，乐观并发控制 |
| A2A | Agent-to-Agent，跨设备通信协议 |
| SSE | Server-Sent Events，前端实时推送 |
| self-talk | 单进程模拟 Director + Worker 对话 |

### 10.2 相关文档索引

- [设计文档 v1.0.3](file:///e:/Java/webser/web_app/webme/hermes-lite/docs/superpowers/specs/2026-07-20-多agent协作机制-design.md)
- [修订 spec v1.0.3](file:///e:/Java/webser/web_app/webme/hermes-lite/docs/superpowers/specs/2026-07-21-多agent协作机制-v1.0.3修订-spec.md)
- [Plan 1: Phase 1 基础层](file:///e:/Java/webser/web_app/webme/hermes-lite/docs/plans/2026-07-21-multiagent-phase1-foundation.md)
- [Plan 2: Phase 2 协作层](file:///e:/Java/webser/web_app/webme/hermes-lite/docs/plans/2026-07-21-multiagent-phase2-collaboration.md)
- [Plan 3: Phase 3-4 跨设备层](file:///e:/Java/webser/web_app/webme/hermes-lite/docs/plans/2026-07-21-multiagent-phase3-4-cross-device.md)
- [Plan 4: Phase 5 前端适配](file:///e:/Java/webser/web_app/webme/hermes-lite/docs/plans/2026-07-21-multiagent-phase5-frontend.md)

### 10.3 project_memory 硬约束对齐

本总览与 4 个 Plan 严格遵守 project_memory 中的硬约束，关键对齐项：

| 硬约束 | 实施位置 |
|--------|----------|
| 协议文件禁绝绝对路径 | Plan 1 Task 2 blackboard.py + Plan 3 Task 5 path_sandbox.py |
| 路径运行时动态获取 | Plan 1 Task 9 配置容器集成（`HERMES_BB_DIR` 环境变量） |
| 全链路异步 | 所有 Plan 的 Global Constraints |
| 配置热更新 | Plan 1 Task 9 + Plan 2 Task 9 + Plan 3 Task 8 + Plan 4 Task 5 |
| LLM 客户端异步 SDK | Plan 2 Task 1 DirectorEngine 复用 Orchestrator 的 AsyncOpenAI/AsyncAnthropic |
| 取消 LLM 流主动 close | Plan 2 Task 1 DirectorEngine |
| 工具调用失败重试预算 2 次 | Plan 1 Task 4 CAS 重试上限 |
| symlink 路径 deny | Plan 1 Task 2 blackboard.py + Plan 3 Task 5 path_sandbox.py |
| 路径沙箱 deny_first 模式 | Plan 1 Task 2 + Plan 3 Task 5 |
| 资源打包保留目录结构 | Plan 4 Task 4 前端资源打包 |
| TDD 流程 | 所有 Plan 的 Global Constraints |
| 容器映射 CONFIG_TO_COMPONENTS | Plan 1 Task 9 + Plan 2 Task 9 + Plan 3 Task 8 |
| _RESTART_REQUIRED_KEYS 边界 | Plan 1 Task 9（`multiagent.blackboard_dir`） |

---

**文档版本**：v1.1
**最后更新**：2026-07-21
**状态**：✅ 4 Plan + 总览全部完成，用户决策已固化（先审核文档 / Python 验收脚本 / 每 Task 一 commit），等待用户审核完成
