开发日志 —— 本次变更摘要

对比基准：origin/dev @ 3420269 → 当前工作树
统计：80 文件修改（+9228/-3025），73 新增文件，1 删除文件

---

新功能

- 通用 Workflow 引擎：调度从单模板升级为通用多步引擎，支持 retry/fallback/skip/abort 四种错误策略、拓扑排序、条件跳过。6 个新模块：engine/spec/adapter/retry/step_executor/step_trace/validator
- 画像信号池：三层信号摘取统一入口，信号达阈值 7 次才写入画像。解决"一次提及即写入"导致的画像噪音。持久化到 data/profile_signal_pool.json
- 监控 + 调度独立页面：调度从 chat 内嵌面板拆为独立 /scheduler 页；新增实时监控页 /monitor（Canvas 直方图、健康检查、审计日志）
- Metrics 持久化：监控指标增量持久化到 SQLite，每日合并，支持趋势查询
- 统一工具错误处理：17 种结构化异常替代字符串错误，按 pre_execution/execution/protocol 三阶段分流处理
- B站 Skill：热门视频、搜索、视频详情、UP主信息、分区排行榜

优化

- ReactLoop 重构：弱引用防 GC 泄漏；下线过敏感卡死检测规则；中断提示跨轮注入
- 前端拆分：删除 chat-schedule.js（895 行），拆为独立调度页 + 轻量徽章轮询
- 审批卡片增强：按工具类别（文件/Skill/MCP/Shell/记忆）差异化渲染
- TodoList 持久化：从纯内存升级为磁盘原子写 + 懒加载恢复
- 会话标题：cron 会话标题取自 schedule.name，用户会话首轮异步生成
- 移动端适配：侧栏遮罩层、响应式 CSS
- 安全：新增 read_paths 黑白名单，保护 src/、config.yaml、.git/ 等敏感路径

Bug 修复

- 定时清理遗漏 todo/ 子目录（已补）
- 审批缺少拒绝原因字段（已加）
- WorkflowResult 异常路径返回 None 导致空指针（已修复）
- 调度 API 缺少 workflow 字段（已补）

测试

- 新增 24 个测试文件，修改 21 个
- 核心覆盖：Workflow 引擎全套、信号池、MetricsStore、ToolError、Todo 持久化、监控页面

---

## 2026-07-30 N-Worker 协作修复（阶段 0/1/2 + 阶段 5 验证 + 阶段 3 研究）

> 配套文档：
> - [docs/plans/2026-07-30-N-worker协作问题修复与可扩展架构探讨.md](docs/plans/2026-07-30-N-worker协作问题修复与可扩展架构探讨.md)
> - [docs/plans/2026-07-30-N-worker修复执行清单.md](docs/plans/2026-07-30-N-worker修复执行清单.md)
> - [docs/plans/2026-07-31-嵌入式Director设计.md](docs/plans/2026-07-31-嵌入式Director设计.md)
> - [docs/plans/2026-07-31-跨实例Director心跳查询设计.md](docs/plans/2026-07-31-跨实例Director心跳查询设计.md)
> - [docs/plans/2026-07-31-N-Worker扩展性验证方案.md](docs/plans/2026-07-31-N-Worker扩展性验证方案.md)

### 用户决策
1. 共享 Blackboard 方案：**Y A2A 同步**
2. Director 部署模式：**A 嵌入式 + 选举**
3. N 扩展性目标：**中规模 N≤10**
4. 实施优先级：**渐进**
5. 外部依赖：**暂不引入 Redis/etcd**
6. 部署形态：**同机/多机都可能**

### 阶段 0：现场止血（无代码改动）
- 杀掉 worker2 僵尸 director 子进程 PID 6420
- 删除 worker2 锁文件 `data/blackboard_a2a_2/locks/{director,audit}.lock`
- 重置 worker2 agent_card：`status: offline→active`、`trust_score: 89→100`
- 核查 worker1 audit.jsonl 尾部：无 trust_score_update 雪崩，判为健康（PID 12120 未处理；但 API 返回 state=degraded 待代码修复改善）
- 两实例 director/status 端点 200 OK

### 阶段 1：根因修复（任务 1.1–1.5）

#### 任务 1.1 Election 接入（类别 A1）
- 文件：`teage_liu/multiagent/worker_adapter.py`
- 改动：`_check_director_health` 检测到 `age > timeout` 时优先调 `Election.run()`，胜出则启动新 director（暂用 LocalDirectorManager.start()），失败则等待远程端点接管；旧"直接进入自治"逻辑保留为 fallback
- `__init__` 新增 `self._director_manager = None`（默认 None，由外部注入）
- 改动用 `if self._config.get("director_v2_enabled", True):` 包住便于回滚
- 测试：`tests/multiagent/test_election_integration.py`（3 个测试，全通过）

#### 任务 1.2 跨进程文件锁（类别 B4 + A3 扩展）
- 文件：`teage_liu/multiagent/file_lock.py`（新增 FileLock 类）
  - 基于 `portalocker.Lock`，async 上下文管理器，timeout 默认 5s
  - 锁文件路径 `{target}.lock`（与目标同目录，区别于现有 `locks/{name}.lock` 进程内 CAS 锁）
- 文件：`teage_liu/multiagent/agent_registry.py`
  - `update_heartbeat` / `update_agent_status` 用 `async with FileLock(agent_file):` 包住读-改-写
- 文件：`teage_liu/multiagent/director_engine.py`
  - `_update_trust_score` 用 `if self._config.get("director_v2_enabled", True):` 包住 FileLock 调用，旧路径作 fallback
  - 防死锁：FileLock 仅包住 trust_score 直接写，不嵌套 update_agent_status 调用
- 测试：`tests/multiagent/test_file_lock.py`（2 个测试，全通过）

#### 任务 1.3 director_engine._run_loop 异常容错（类别 B1）
- 文件：`teage_liu/multiagent/director_engine.py`
- 改动：`_run_loop` 每个子任务独立 try/except + 失败计数；连续失败 10 次触发 director 重启；成功一轮仅衰减本轮未失败任务的计数（修正原骨架"每轮衰减所有任务"的缺陷——否则连续失败计数永远不累积）
- `__init__` 新增 `self._task_failure_counts: dict[str, int] = {}`，keys 在 `_run_loop` 启动时按 tasks 列表初始化
- 测试：`tests/multiagent/test_director_engine.py`（2 个测试，全通过）

#### 任务 1.4 director_cli 僵尸修复（类别 B2）
- 文件：`teage_liu/multiagent/director_cli.py`
- 改动：`main_async` 新增 `_watch_loop_task` 协程监听 `director._loop_task` 退出，触发 `stop_event.set()`；watcher 在 `director.start()` 之后创建避免 AttributeError
- 用 `if config.get("director_v2_enabled", True):` 包住新逻辑
- 测试：`tests/multiagent/test_director_cli.py`（2 个测试，全通过）

#### 任务 1.5 LocalDirectorManager 自动重启（类别 B3）
- 文件：`teage_liu/multiagent/director_manager.py`
- `__init__` 新增 6 个 watchdog 字段：`_watchdog_task / _watchdog_running / _restart_count / _max_restarts(=3) / _restart_window(=300s) / _restart_times`
- 新增 `_watchdog` 协程：每 10s 检查子进程，崩溃自动重启；窗口内最多 3 次重启；崩溃时调 `_cleanup_stale_state` 清理锁文件
- 新增 `_cleanup_stale_state`：删除 `locks/director.lock` + `locks/audit.lock` + `director.pid`
- `stop()` 方法开头先关闭 watchdog（防止 stop 期间触发自动重启）
- 测试：`tests/multiagent/test_director_manager.py`（3 个测试，全通过）

### 阶段 2：性能优化（任务 2.1–2.3）

#### 任务 2.1 接入 WatchdogWatcher（类别 A2）
- 文件：`teage_liu/multiagent/worker_adapter.py`
  - `__init__` 新增 `self._collab_interrupt = asyncio.Event()`
  - 新增 `_on_collab_file_changed(event)`：watchdog 文件变更时 `set()`，仅对 collaboration.md / agent_card 触发避免噪声
  - `_collab_poll_loop` 改为 `await asyncio.wait_for(self._collab_interrupt.wait(), timeout=interval)`，事件触发或超时都执行 `_poll_collab_once`，延迟从 0-2s 降到 < 100ms
- 文件：`teage_liu/lifespan.py`
  - watchdog_watcher 注册用延迟绑定模式（`worker_adapter_holder: list = []`），在 multiagent_adapter 实例化后填充 holder，激活 callback
  - 用 `director_v2_enabled` 开关，关闭时保留旧 lambda
- 测试：`tests/multiagent/test_worker_adapter.py`（3 个新测试，全通过）

#### 任务 2.2 A2A 重试参数激进调整（类别 B5）
- 文件：`teage_liu/multiagent/a2a_client.py`
  - `__init__`：timeout 10→3，retry_count 2→1
  - `call_method` 重试 sleep 改为指数退避：`min(0.5 * (2 ** attempt), 2.0)`（0.5 → 1.0 → 2.0，上限 2.0s）
- 测试：`tests/multiagent/test_a2a_client.py`（2 个测试，全通过）

#### 任务 2.3 LLM 调用串行化（类别 B6）
- 文件：`teage_liu/multiagent/worker_adapter.py`
  - `__init__` 新增 `self._llm_lock = asyncio.Lock()`
  - `_trigger_urgent_llm` 用 `async with self._llm_lock:` 包住 `orchestrator.chat` 调用
  - 异常 catch 不阻塞 `_collab_poll_loop`
  - 用 `if self._config.get("director_v2_enabled", True):` 包住新逻辑
- 测试：`tests/multiagent/test_worker_adapter.py`（1 个新测试，通过）

### 阶段 5：统一验证

#### 单元测试
- 全量 `tests/multiagent/`：437 passed / 4 failed / 9 skipped
  - 4 failed 全部为**预先存在 WIP 失败**（untracked 测试文件依赖未完成的 `append_collab_message` 函数），经 git stash 验证与阶段 1+2 改动无关
  - 失败用例：`test_broadcast_dedup_on_duplicate_submission` / `test_worker_polls_request` / `test_already_processed_normal_request_skipped` / `test_normal_request_marked_processed_after_enqueue`
- 全量 `tests/api/test_multiagent_routes.py`：27 passed / 0 failed
- 新增测试总数：17 个（任务 1.1: 3 + 1.2: 2 + 1.3: 2 + 1.4: 2 + 1.5: 3 + 2.1: 3 + 2.2: 2）

#### 集成测试脚本（用户长跑验证用）
- 新增：`tests/integration/test_n_worker_collab.py`
  - 3 个测试：`test_director_auto_restart_on_kill` / `test_audit_growth_rate_under_threshold` / `test_e2e_collab_message_latency`
  - 默认 skip，需 `--run-integration` 启用
- 新增：`tests/conftest.py` 注册 `integration` marker + `--run-integration` 选项

#### 性能采集脚本
- 新增：`scripts/collect_perf_metrics.py`
  - 采集 a2a_p50/p95/p99/max、audit_growth_per_min、director_unreachable_count、sample_success_rate
  - 输出 JSON 含阈值判定 + `all_pass` 总判定
  - 用法：`python scripts/collect_perf_metrics.py --duration 1800 --output metrics.json`

#### 用户长跑验证步骤
```powershell
cd e:\Java\webser\web_app\webme\teage-liu
.\start_dual_workers.ps1
python scripts\collect_perf_metrics.py --duration 1800 --output metrics.json
python -m pytest tests\integration\test_n_worker_collab.py --run-integration -v
type metrics.json | findstr /C:"all_pass"
```

### 阶段 3：架构研究文档（待用户决策后启动实施）

#### 嵌入式 Director 设计（[docs/plans/2026-07-31-嵌入式Director设计.md](docs/plans/2026-07-31-嵌入式Director设计.md)）
- 设计 `EmbeddedDirector` 类（standby→active→standby 状态机 + asyncio.Lock 串行化 activate/deactivate）
- 与 `DirectorEngine` 组合（不继承，复用阶段 1+2 成果）
- 与 `WorkerAdapter` 协作（`_director_manager` 字段从 None 改为 EmbeddedDirector 实例）
- 新文件 `teage_liu/multiagent/embedded_director.py` + lifespan DI 改造 + multiagent_routes 改造
- 待用户决策 6 项（D1 默认启用 / D2 保留 LDM / D3 status 同步策略 / D4 组合关系 / D5 lease 续约 / D6 faulted 自愈）

#### 跨实例 Director 心跳查询设计（[docs/plans/2026-07-31-跨实例Director心跳查询设计.md](docs/plans/2026-07-31-跨实例Director心跳查询设计.md)）
- A2A 新增 JSON-RPC 方法 `read_director_md`（注册到 `A2AServer._handlers`）
- 三阶段查询算法：本地判定 → 远程并发查询 → Election 兜底
- 新增 `healthy_remote` 健康级别（远程有健康 director 时跟随，不进自治）
- 防风暴：`_remote_query_lock` 限每实例 1 个在飞查询；防无限递归：`_recheck_depth` 上限 2
- 待用户决策 4 项（D-1 查询频率 / D-2 端点列表来源 / D-3 重试策略 / D-4 healthy_remote 是否同步）

#### N-Worker 扩展性验证方案（[docs/plans/2026-07-31-N-Worker扩展性验证方案.md](docs/plans/2026-07-31-N-Worker扩展性验证方案.md)）
- 验证矩阵：N=2（已通过）/ N=3（P0）/ N=5（P1）/ N=10（P2）/ 跨机 N=3（P2）
- 5 个验证场景：选举 failover / 100 条广播无丢失 / LLM 限流 / A2A 调用风暴 / 跨机网络抖动
- 性能瓶颈预测表：LLM 风暴（N>3）/ A2A HTTP（N>5）/ 文件锁争用（N>3 同机）/ audit I/O（N>5）/ watchdog fd 耗尽（N>10）/ Director 主循环（N>8）
- 部署模板：3 份 config 模板 + `generate_n_configs.ps1` + `start_n_workers.ps1/.sh`
- 待用户决策 6 项

### 阶段 1 遗留潜在 bug（H2 研究发现，已纳入阶段 3 修复范围）
1. **GAP-1**：`a2a_server.py` 未注册 `read_director_md` handler → Election 远程候选收集实际失效（待 3.R2.1 修复）
2. **GAP-2**：Election 期望远程返回 `status.director` 子结构，但实际 `read_director_md` 应返回 director.md 格式 → 字段名不匹配（待 3.R2.5 修复）
3. **GAP-4**：`_check_director_health` 选举失败后递归调用自身无深度上限 → 远程持续超时时栈风险（待 3.R2.4 修复）

### 回滚预案
1. **代码级**：所有改动用 `if config.get("director_v2_enabled", True):` 包住，配置一键回退
2. **配置级**：保留旧 config 模板，可恢复 10s timeout / 3 retry
3. **进程级**：`auto_restart_enabled` 配置开关，可关闭回归手动管理
4. **架构级**：保留 `LocalDirectorManager` 作为永久 fallback（嵌入式 Director 失败时降级）

### 改动文件清单
- 代码改动（8 个文件）：
  - `teage_liu/multiagent/worker_adapter.py`（__init__ + _check_director_health + _on_collab_file_changed + _collab_poll_loop + _trigger_urgent_llm）
  - `teage_liu/multiagent/file_lock.py`（新增 FileLock 类）
  - `teage_liu/multiagent/agent_registry.py`（update_heartbeat + update_agent_status 加锁）
  - `teage_liu/multiagent/director_engine.py`（__init__ + _run_loop + _update_trust_score）
  - `teage_liu/multiagent/director_cli.py`（main_async watcher）
  - `teage_liu/multiagent/director_manager.py`（__init__ + _watchdog + _cleanup_stale_state + stop）
  - `teage_liu/lifespan.py`（watchdog callback 延迟绑定）
  - `teage_liu/multiagent/a2a_client.py`（__init__ + call_method 重试参数）
- 数据文件改动（1 个文件）：
  - `data/blackboard_a2a_2/agents/teagent-liu-2.md`（status + trust_score）
- 测试文件新增/扩展（7 个文件）：
  - `tests/multiagent/test_election_integration.py`（新建）
  - `tests/multiagent/test_file_lock.py`（扩展）
  - `tests/multiagent/test_director_engine.py`（扩展）
  - `tests/multiagent/test_director_cli.py`（新建）
  - `tests/multiagent/test_director_manager.py`（新建）
  - `tests/multiagent/test_worker_adapter.py`（扩展）
  - `tests/multiagent/test_a2a_client.py`（扩展）
  - `tests/conftest.py`（追加 integration marker）
- 集成测试（1 个文件）：
  - `tests/integration/__init__.py` + `tests/integration/test_n_worker_collab.py`（新建）
- 性能采集脚本（1 个文件）：
  - `scripts/collect_perf_metrics.py`（新建）
- 研究文档（3 份，共 3415 行）：
  - `docs/plans/2026-07-31-嵌入式Director设计.md`（1394 行）
  - `docs/plans/2026-07-31-跨实例Director心跳查询设计.md`（1044 行）
  - `docs/plans/2026-07-31-N-Worker扩展性验证方案.md`（977 行）
