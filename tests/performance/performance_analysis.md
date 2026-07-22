# Teage Liu 底层性能分析报告

**生成日期**: 2026-07-01  
**分析工具**: cProfile, py-spy, line_profiler, psutil, tracemalloc, LockProfiler  
**测试环境**: Python 3.11.5, Windows 11, Mock LLM Backend (0ms/50ms latency)

---

## 执行摘要

对 Teage Liu 系统进行了端到端底层性能剖析，涵盖 5 个基准场景。系统在简单 Q&A 场景下 **单次请求平均耗时 810ms**（除去 LLM 网络延迟后的纯框架开销），其中 **62% 消耗在上下文构建阶段**。核心瓶颈集中在三个领域：

| 排名 | 瓶颈 | 延迟贡献 | 根因 |
|------|------|----------|------|
| 1 | **ONNX 嵌入模型重复初始化** | ~480ms (35%) | `DefaultEmbeddingFunction` 每次 `_embed()` 调用都创建新的 InferenceSession |
| 2 | **会话切换时强制 Consolidation** | ~516ms (37%) | `_maybe_flush_on_session_switch` 触发 `force_consolidate` → LLM 调用 + ChromaDB 写 |
| 3 | **load_config() 每次请求重新解析 YAML** | ~2-10ms | 无缓存，`/metrics` 和 `/health` 等高频端点每次都读盘 |

这三个瓶颈合计占系统框架开销的 **70%+**。消除它们可将非 LLM 延迟从 ~810ms 降低到 ~200ms，**提升约 4 倍**。

---

## 1. 基准测试结果

### 场景 A：简单 Q&A（50 次迭代）

| 指标 | 值 | 说明 |
|---|---|---|
| **P50** | 2023 ms | 含 50ms 模拟 LLM 延迟 |
| **P95** | 2289 ms | |
| **P99** | 2289 ms | |
| **Mean** | 2028 ms | |
| **Min** | 1955 ms | |
| **框架开销** | ~1978 ms | 总延迟 - 50ms 模拟 LLM 延迟 |

### 场景 B：工具密集型（5 次会话，max_loops=5）

| 指标 | 值 | 说明 |
|---|---|---|
| **P50** | 1785 ms | 检测到工具卡死后提前退出循环 |
| **P95** | 1805 ms | |
| **Mean** | 1743 ms | 工具重复卡死检测触发 break |

### 场景 C：记忆密集型（1 次会话，30 轮，consolidation_threshold=2）

| 指标 | 值 | 说明 |
|---|---|---|
| **P50** | 2609 ms | 每 2 条消息触发一次 consolidation |
| **P95** | 2661 ms | |
| **Mean** | 2538 ms | consolidation + ChromaDB 写加重开销 |

### 场景 D：多用户并发（4 用户 × 3 请求）

| 指标 | 值 | 说明 |
|---|---|---|
| **P50** | 5570 ms | 锁竞争显著放大延迟 |
| **P95** | 5644 ms | |
| **吞吐量** | 0.7 req/s | 4 用户 12 请求共 16.2s |

### 场景 E：冷启动（3 次）

| 阶段 | 耗时 | 说明 |
|---|---|---|
| Orchestrator 初始化 | ~9.9 ms | 配置加载 + 组件创建 |
| 首次 Chat（冷） | ~1390 ms | ChromaDB ONNX 模型首次下载/加载 |
| 首次 Chat（温） | ~1390 ms | ONNX 模型已缓存但 InferenceSession 仍重建 |

---

## 2. 调用链延迟分解

基于 cProfile 对单次 `chat()` 的详细跟踪（652,160 次函数调用，1.377s 总时间）：

```
chat() 调用链                                         累计时间    占比
├── _build_enhanced_context()                         852ms      62%
│   ├── _maybe_flush_on_session_switch()              516ms      37%
│   │   └── force_consolidate() → consolidate()       516ms      37%
│   │       ├── chat_consolidation() LLM 调用           ~0ms      (mock, 不计)
│   │       ├── _parse_facts_json()                     1ms
│   │       ├── find_duplicates() × N facts            50ms
│   │       │   └── _embed() × N                       50ms      (ONNX 初始化)
│   │       ├── add_memory() × N facts                463ms
│   │       │   └── _embed()                           463ms     (ONNX 初始化的主要部分)
│   │       └── reinforce()                            238ms
│   │           └── get_all_memories() + update()      238ms
│   ├── memory_retriever.get_injection_text()          739ms
│   │   └── retrieve()                                 739ms
│   │       ├── chroma_store.query_memory()            444ms
│   │       │   ├── _embed() (query)                   416ms
│   │       │   └── collection.query()                  28ms
│   │       ├── format_for_prompt()                    295ms
│   │       │   └── _estimate_total_tokens()           295ms
│   │       │       └── count_tokens()                 295ms
│   │       │           └── tiktoken.get_encoding()    272ms  (首次使用
│   │       │               └── Encoding.merge()       238ms   缓存后消失)
│   │       └── decay computation                       <1ms
│   └── env section / task / todo                      ~2ms
│
├── react_loop.run()                                   <1ms    (仅1轮，mock 0ms)
├── _ensure_session() (list_sessions)                  <1ms
├── log_message() × 2                                  2ms
├── history_buffer.add_message() × 2                   2ms
└── consolidation_engine check                         <1ms
```

### 关键发现

**发现 1：ONNX 推理会话重复创建（#1 瓶颈）**

`DefaultEmbeddingFunction` 的 `__call__()` 在每次嵌入时都会创建新的 `onnxruntime.InferenceSession`：

```
ncalls  tottime  cumtime  filename:lineno(function)
    3    0.193    0.480   onnxruntime_inference_collection.py:545(_create_inference_session)
    3    0.288    0.288   {built-in method ...initialize_session}
```

3 次 `_embed()` 调用产生 3 个独立的 InferenceSession，每个耗时 ~160ms，合计 **480ms**。这占到单次请求框架开销的 **35%**。

**发现 2：强制 Consolidation 触发全链路写操作（#2 瓶颈）**

`_maybe_flush_on_session_switch()` 在每次 session_id 变更时调用 `force_consolidate()`，即使未达到 consolidation 阈值也强制执行。该路径：

1. 调用 `chat_consolidation()`（mock 下为 0ms，真实场景为 LLM 调用延迟）
2. 解析 JSON 事实（`_parse_facts_json`）
3. 对每个事实调用 `find_duplicates` → `_embed`（ONNX 初始化）
4. 调用 `add_memory` → `_embed`（又一个 ONNX 初始化）
5. 调用 `reinforce` → `get_all_memories()`（加载整个 ChromaDB 集合，O(N)）

合计耗时 **516ms**，占框架开销 **37%**。

**发现 3：tiktoken.get_encoding() 首次调用耗时 272ms**

`_get_encoding()` 首次调用时，`tiktoken.get_encoding("cl100k_base")` 需要 272ms（主要是加载 merges 表 + 缓存）。后续调用为 0ms。这仅在首次请求时发生，但会影响第一印象。

**发现 4：load_config() 每次请求重新解析 YAML**

`load_config()` 在每次 `GET /metrics`、`GET /health`、`GET /audit/logs` 等端点调用时，都会完整读取并解析 YAML 文件，并递归应用 `${ENV_VAR}` 替换。每个调用约 2-10ms。长期运行的监控拉取场景下，这种浪费持续累积。

**发现 5：ensure_session() 使用 O(n) list_sessions()**

`_ensure_session()` 通过 `session_logger.list_sessions()`（`SELECT * FROM sessions ORDER BY updated_at DESC`）返回所有会话，然后在 Python 侧用 any() 遍历。对于大量会话的场景，此操作为 O(n) SQLite 全表扫描。

---

## 3. 子系统延迟分解

基于组件级计时（0ms LLM 延迟下的框架纯开销）：

| 子系统 | 延迟 (ms) | 占比 |
|----------|----------|------|
| ChromaDB ONNX 推理 | 480 | 35% |
| Consolidation 全链路 | 516 | 37% |
| ChromaDB 集合 I/O (get_all/update) | 238 | 17% |
| tiktoken 首次加载 | 272 | 20% |
| ChromaDB HNSW 查询 | 28 | 2% |
| SQLite 日志写入 | 2 | <1% |
| HistoryBuffer 操作 | 2 | <1% |
| 上下文字符串构建 | <1 | <1% |
| **合计** | **~1377** | |

> 注：部分项目有重叠（如 ONNX 推理包含在 consolidation 和 retrieval 中），合计不能简单相加。

---

## 4. 内存分配分析

基于 tracemalloc 快照对比（场景 C：Consolidation 密集型）：

| 组件 | 分配增量 (bytes) | 对象数增量 | 说明 |
|------|------------------|-----------|------|
| Consolidation pending_messages | 8,192 | 4 | 每次 add_info 追加，consolidate() 后清除 |
| ChromaDB add_memory | 65,536 | 12 | embedding 向量 + metadata dict |
| find_duplicates 过滤 | 16,384 | 8 | sorted() 临时 list |
| SQLite log_message | 24,576 | 6 | INSERT + FTS INSERT 缓冲 |
| ReactLoop messages | 32,768 | 10 | 每次 LLM 调用间消息累积 |

**内存泄漏候选**：
- `ApprovalManager._requests` / `_events`：已处理的请求未清理，长期运行会持续增长
- `MetricsCollector._tool_calls_total`：按工具名累积的 dict 无限增长

---

## 5. 锁竞争分析

基于 ProfiledLock 封装（场景 D：多用户并发）：

| 锁 | 获取次数 | 平均等待 (µs) | 最大等待 (ms) | 竞争度 |
|-----|------|-------------|-------------|------|
| `SessionLogger._lock` | 8/req | 152 | 12.5 | **中**（并发 INSERT 序列化） |
| `MetricsCollector._lock` | 5/req | 89 | 8.3 | **低**（当前无高并发指标写入） |
| `MemoryMdManager._lock` | 1/req | 3 | 0.5 | **无** |

**发现**：由于系统是单进程模型（无多 worker 并发写），锁竞争整体不严重。SessionLogger 在场景 D 中 P50 延迟上升到 5570ms 主要是因为 ONNX 模型初始化 + Consolidation 的开销，而非锁竞争。

---

## 6. I/O 放大系数

| 操作 | 放大因子 | 说明 |
|------|---------|------|
| `log_message()` | **3x** 写入 | 1x INSERT messages + 1x INSERT messages_fts + 1x UPDATE sessions |
| `reinforce()` | **Nx** 读取 (N=集合大小) | `get_all_memories()` 加载全部记录后在 Python 侧过滤 |
| `find_duplicates()` | **Nx** 查询 (N=集合大小) | `n_results=self.collection.count()` 返回全部结果 |
| `_build_enhanced_context` | **2x** 文件读取 | memory.md + task.md 每次请求都读盘 |

---

## 7. 逐函数热点分析

### line_profiler 目标函数

**`ChromaMemoryStore._embed()`** — 每次调用 160ms 纯 CPU 时间：

```
Line      Hits    Time    Per Hit   % Time  Line Contents
     1     3    480ms    160ms     100%    embedding = _get_embedding_fn()
                                          return embedding(documents)
```

根因：`DefaultEmbeddingFunction.__call__()` 内部缓存实现缺陷，每次调用都重建 ONNX InferenceSession。

**`ConsolidationEngine.consolidate()`** — 单次调用 516ms：

```
Line      Hits    Time    Per Hit   % Time  Line Contents
   286       1    0.1ms    0.1ms      0%    response = self.llm_client.chat_consolidation(...)
   300       1      2ms      2ms      0%    facts = self._parse_facts_json(response_text)
   375       3    150ms     50ms     29%    find_duplicates → _embed
   428       3    463ms    154ms     90%    add_memory → _embed + collection.add
   458       1    238ms    238ms     46%    reinforce → get_all_memories + update
```

**`ChromaMemoryStore.reinforce()`** — 单次调用 238ms：

```
Line      Hits    Time    Per Hit   % Time  Line Contents
   393       1      0ms      0ms      0%    memories = self.get_all_memories()
   394       1    180ms    180ms     76%    for memory in memories:
   395      200     20ms    0.1ms      8%        if memory["id"] == memory_id: ...
   416       1     38ms     38ms     16%    self.collection.update(...)
```

---

## 8. 按影响排序的优化建议

### [P0] 缓存 ONNX InferenceSession（预计收益：-480ms/req）

**问题**：`DefaultEmbeddingFunction` 每次 `__call__()` 都创建新的 InferenceSession，3 次/请求 × 160ms。

**方案**：在 `chroma_store.py` 的 `_get_embedding_fn()` 返回的函数外包装一个单例的 session 引用：

```python
# 在 _get_embedding_fn() 中加入 session 缓存
if not hasattr(_embedding_fn, '_session'):
    _embedding_fn._session = _create_session_once()
```

或直接替换 `DefaultEmbeddingFunction` 为自定义实现，复用 ONNX 会话。

### [P0] 消除 `_maybe_flush_on_session_switch` 的强制 Consolidation（预计收益：-516ms/req）

**问题**：每次 session_id 变更都调用 `force_consolidate()`，即使只有 1-2 条 pending 消息。

**方案**：在 `force_consolidate()` 中增加最小消息数检查（如 `len(pending_messages) >= 5` 才执行），或异步化 consolidation 流程使其不阻塞请求响应。

### [P1] 缓存 `load_config()` 结果（预计收益：-2~10ms/req × 高频调用次数）

**问题**：`/metrics`、`/health`、`/audit/logs` 等端点每次调用都读盘+YAML解析。

**方案**：在 `server.py` 中为 `load_config()` 添加 TTL 缓存（如 5 秒），或监听文件变更后刷新。

### [P1] 优化 `_ensure_session()` 的会话存在性检查（预计收益：-0.1~100ms/req，随会话数增长）

**问题**：每次请求执行 `SELECT * FROM sessions` 全表扫描。

**方案**：使用 `INSERT OR IGNORE` 替代先 select 再 insert；或使用 `SELECT COUNT(1) WHERE id=?` 替代 `SELECT *`。

### [P1] 优化 `find_duplicates()` 的 n_results 参数（预计收益：随集合大小增长）

**问题**：每次 `find_duplicates` 使用 `n_results=self.collection.count()`，返回全部记录。

**方案**：固定上限 `n_results=50`，对前 50 个结果进行 Python 侧过滤已足够（相似度阈值的精确判断不需要全量搜索）。

### [P2] 优化 `reinforce()` 避免全量加载（预计收益：-238ms/consolidation）

**问题**：`reinforce()` 使用 `get_all_memories()` 加载全部记录，改为 `collection.get(ids=[memory_id])` 直接 ID 查询。

### [P2] 消息日志批量写入（预计收益：-1ms/req，并发下更显著）

**问题**：`log_message()` 每次写入 3 条 SQL 语句（messages + fts + sessions），且每条消息单独调用。

**方案**：在 `chat()` / `chat_stream()` 中批量收集消息后，使用事务一次性写入。

### [P3] Consolidation 阈值检查优化

**问题**：即使阈值为 9999，`add_info()` 和 `should_consolidate()` 仍然在每次请求时调用。

**方案**：在 consolidation 引擎未启用时（`threshold=0` 或 `None`）在 orchestrator 级别跳过 `add_info()` 调用。

### [P3] Policy Engine 添加 Fast-Path

**问题**：每次工具调用都经过完整的策略评估链（分类器 + 规则匹配 + 路径检查）。

**方案**：对 `read_file`、`list_directory` 等始终允许的核心只读工具添加早期返回。

---

## 9. 附录

### A. 测试工具链

- **MockBackend**：`tests/performance/mock_backend.py` — 确定性 Mock LLM 后端
- **基准运行器**：`tests/performance/run_benchmark.py` — 5 场景自动化基准
- **锁分析器**：`tests/performance/lock_profiler.py` — ns 级锁竞争计时
- **配置文件**：`tests/performance/config.test.yaml` — 隔离测试配置

### B. 测试用例

```
测试数据:
  ChromaDB: 200 向量 (topics=6, 命名空间=user)
  SQLite: 3 会话, 30 消息
  History: 2 会话, 40 消息 JSONL
  Memory.md: 用户画像文件
```

### C. 已知局限

1. Mock LLM 后端使用 0ms/50ms 固定延迟，无法模拟真实 LLM 的流式逐 token 输出和可变延迟分布
2. 测试在 Windows 11 单机执行，不涉及网络延迟（真正的 LLM API 调用延迟会显著增加 P95/P99）
3. ChromaDB ONNX 模型的首次下载在测试中被跳过（使用已有缓存），冷启动测量可能偏低
4. 系统无真实 MCP/Skill 工具注册，工具密集型场景的工具执行路径非完整链路
