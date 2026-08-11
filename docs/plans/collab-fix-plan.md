# teage-liu 多 Agent 协作修复计划 v5

> 本文档为已定稿的修复计划，涵盖 A2A 路径修复、文件锁、前端排序、Subagent/讨论模式、稳定性增强与工作台增强，并补充 Agent 基础设施与工具模型章节。

---

## 一、问题背景

### 1.1 工作台消息排序错乱

**现象**：刷新工作台后，旧消息（22:15）堆叠在新消息（22:56）下方。

**根因**：
- 前端 `init()` 中 `loadRecentMessages()`（HTTP）与 `connectCollabSSE()`（SSE）并发执行。
- SSE 先到达的新消息被 append 到 DOM 后，HTTP 后到达的旧消息也被 append 在后面。
- `renderCollabMessage` 不按 seq 排序，仅按到达顺序追加。

### 1.2 Agent 协作中断

**现象**：两个 agent 完成一轮协作后即无后续消息。

**根因（复合）**：
- (a) 跨进程写入 `collaboration.md` 无文件锁，导致 seq 重复，消息被消费方跳过。
- (b) peer response 分支（worker_adapter.py L1047）缺少伙伴上下文（Director 广播分支 L1118 调用了 `_build_collab_partner_context`，peer response 没有）。
- (c) A2A 工具被 prompt 主动屏蔽，响应不走 A2A 转发，远程 agent 收不到响应。

---

## 二、关键架构发现

- `send_remote_message` 工具已存在（a2a_tools.py），承担两件事：写入 `collaboration.md` + A2A HTTP 转发。但 worker_adapter 的 prompt 在 4 处屏蔽它（"不要调用 send_remote_message"）。
- system prompt（prompts.py L437-440）说明"通过 send_remote_message 通信"，与 user prompt 矛盾。
- `_trigger_urgent_llm`（worker_adapter.py L1311-1408）直接 `append_collab_message` 写 response，绕过 A2A 转发。
- Director 有干预机制：DirectorInjector 轮询 directive 并 piggyback 到 LLM system prompt（rule_type: intervention/ordering/guidance），需保留。

---

## 三、用户确认的所有决策

1. 本机 agent 用共享黑板，远程用 A2A 协议，两者并存。
2. 前端排序用方案 C（await 历史加载 + 按 seq 插入位置）。
3. 文件锁优先稳定性，锁整个 append。
4. `to` 字段由 LLM 自主选择，维护协作名单（roster）让 agent 知道参与者。
5. 协作 ID（`collab_id`）由系统生成，每次协作不同，不继承其他协作记忆。
6. LLM 结构化输出改为方案 Y：LLM 驱动 `send_remote_message` 工具调用（非系统自动写入）。
7. `initiate_collaboration` 工具全局注册。
8. 协作正常结束不用通知 Director，但工作台要明显显示协作结束。
9. 主会话发起协作用 subagent 阻塞模式（wait=True），Director 广播用讨论模式（wait=False，多轮协商）。
10. 工作台按 `collab_id` 归档分组 + 时间排序。
11. 协作 agent 拥有正常基础设施（react loop 等），agent 工具通常自带（本系统可在 card 预置工具，其他 agent 可能拥有所有工具）。

---

## 四、两种协作模式

### 4.1 Subagent 模式

- 主会话 agent 发起。
- `wait_for_response=True`，阻塞等待结果返回。
- 减少主会话上下文压力。

### 4.2 讨论模式

- Director 广播触发。
- `wait_for_response=False`，多轮协商至 consensus/end。

---

## 五、修复阶段

### 阶段 0：A2A 路径修复 + 工具增强

#### 0.1 解除 A2A 工具屏蔽

- 文件：`teage_liu/multiagent/worker_adapter.py`（4 处屏蔽语句）
- 文件：`teage_liu/llm/prompts.py`
- 操作：移除/改写"不要调用 send_remote_message"等屏蔽语句，统一 prompt 与 system prompt 一致性（prompts.py L437-440 与 user prompt 对齐）。

#### 0.2 增强 send_remote_message

- 文件：`teage_liu/agent/tools/a2a_tools.py`
- 新增参数：
  - `collab_id`：协作会话 ID
  - `msg_type`：消息类型（request/response/consensus/end 等）
  - `wait_for_response`：是否阻塞等待对方响应
  - `timeout`：等待超时（秒）
- 引入 contextvar：
  - `_current_collab_id`：当前协作上下文
  - `_collab_tool_called`：标记本次 LLM 调用是否触发了工具
- 实现 subagent 阻塞等待逻辑（wait_for_response=True 时挂起当前流程，等待对方 response 到达同 collab_id）。

#### 0.3 _trigger_urgent_llm 改造

- 文件：`teage_liu/multiagent/worker_adapter.py`（L1311-1408）
- 流程：
  1. 设置 contextvar（`_current_collab_id`、`_collab_tool_called=False`）。
  2. 调用 LLM，由 LLM 驱动 `send_remote_message` 工具调用。
  3. Fallback 检测：若 `_collab_tool_called=False`（LLM 未调用工具），系统代写一条 response 消息保证不卡死。

#### 0.4 peer response 伙伴上下文修复 + roster 复用

- 文件：`teage_liu/multiagent/worker_adapter.py`
- 在 peer response 分支（L1047-1059）补齐 `_build_collab_partner_context` 调用，与 Director 广播分支（L1118）对齐。
- **roster 复用而非重写**：`_build_collab_partner_context`（L1238）已包含 roster 功能（在线伙伴 agent_id + capabilities + 最近表态）。新增 `_build_collab_roster(collab_id)` 作为其**轻量版**（只列参与者 id+能力，不含最近表态），供 LLM 决策 `to` 字段时减少 token 消耗；或在 `_build_collab_partner_context` 上加 `include_history: bool = True` 参数控制是否带表态。
- 给 `_build_collab_partner_context` 增加 `collab_id` 参数，按 collab_id 读取对应协作文件的消息（当前 L1277 读全局 collaboration.md）。

---

### 阶段 1：核心 Bug 修复

#### 1.1 跨进程文件锁（补充 asyncio.Lock 已存在）

- 文件：`teage_liu/multiagent/blackboard.py`（CollabWriter L559-632）
- **现状**：CollabWriter 已有 `asyncio.Lock`（L568），但**仅在单进程内串行化**。多 worker 进程并发写入时仍会导致 seq 重复。
- 实现（在 asyncio.Lock 外层叠加跨进程文件锁）：
  - 锁文件：`<bb_root>/.collab.lock`
  - Windows：`msvcrt.locking`（`LK_LOCK` 阻塞获取）
  - Unix：`fcntl.flock`（`LOCK_EX` 阻塞获取）
  - 锁整个 append 流程（读取最新 seq → 写入新消息 → flush → fsync）。
  - 锁粒度：按 collab_id 对应文件单独加锁（避免全局串行），锁文件名 `<bb_root>/.collab.<collab_id_or_global>.lock`。
- `append_collab_message` 写路径需在跨进程锁内；`read_collab_messages` 为只读可不强加锁（容忍瞬时不一致），但 `_read_last_collab_seq` 在 append 内调用时已在锁内。

#### 1.2 前端排序（去重已按 seq，重点是排序插入 + init 串行化）

- 文件：`web/static/js/collab-workbench.js`
- **现状**：`renderCollabMessage`（L216）已用 `_renderedSeqs` 按 `seq` 去重（L222），但渲染是 `stream.appendChild(item)`（L240）纯追加，不按 seq 排序。`loadRecentMessages`（L809）与 `connectCollabSSE` 若并发，SSE 先到的新消息先 append，历史旧消息后 append → 旧消息堆在新消息下方。
- 改动：
  - `init()`：`await loadRecentMessages()` 完成后再 `connectCollabSSE()`（串行化）。
  - `renderCollabMessage`：改为按 seq **二分插入**到正确位置（维护 DOM 子节点 seq 升序），而非简单 append。插入后仍受 `MAX_MESSAGES=200` 上限约束（超限时移除最早节点）。
  - 去重 key 维持 `seq`（已正确），无需改为 `seq+from`。
  - `_streamMinSeq` 更新逻辑保留（用于古早记录分页游标）。

#### 1.3 Director 广播携带 collab_id

- 文件：`teage_liu/multiagent/a2a_gateway.py`
- `_director_broadcast`（L460）：广播消息体携带 `collab_id`，确保被广播发起的协作可被工作台归档分组。

---

### 阶段 2：Subagent + 讨论模式

#### 2.1 两种模式 prompt 引导

- 在 collab system prompt 中明确：
  - subagent 模式：等待单次 response 后结束。
  - 讨论模式：多轮协商，由 consensus/end 消息终止。

#### 2.2 死锁防护

- subagent 模式中，被调用方（B）必须以 `wait_for_response=False` 回复，避免双向阻塞。

#### 2.3 协作结束标识

- `msg_type=consensus` 或 `end` 触发协作结束。
- 工作台显示结束徽章（详见阶段 4）。

---

### 阶段 3：稳定性增强

#### 3.1 list_active_agents 心跳新鲜度校验

- 文件：`teage_liu/multiagent/agent_registry.py`（L115-122）
- 心跳超过 90s 视为离线，不返回给调用方。

#### 3.2 Worker 轮询容错

- 用 `processed_seqs` 集合替代 `seq > last_seq` 判断。
- LRU 上限 2000，避免内存膨胀。
- 防止 seq 回绕或重复消费。

---

### 阶段 4：工作台增强

#### 4.1 按 collab_id 分组归档

- 文件：`web/static/js/collab-workbench.js`
- 消息按 `collab_id` 分组展示，组内按 seq/时间排序。

#### 4.2 状态徽章

- 进行中 / 已共识 / 已结束 / 超时 四种状态徽章。

#### 4.3 消息流展示

- 每条消息展示 `from → to` 与时间戳。

---

## 六、补充章节：Agent 基础设施与工具模型

### 6.1 协作 Agent 的基础设施

协作 agent 拥有完整基础设施，与其他 agent 一致：
- React loop（推理-行动循环）
- 工具系统（注册、调度、权限）
- LLM 调用链路
- 上下文管理
- 错误处理与重试

协作流程不绕过上述基础设施，而是在其上叠加协作语义（collab_id、msg_type、roster 等）。

### 6.2 工具自带模型

每个 agent 的工具集由其实现决定：
- **本系统**：可在 `agent_card` 中预置工具列表，注册时声明。
- **外部 agent**：可能拥有全部工具，由对方实现决定。

本系统不应假设对方工具集，仅通过 A2A 协议交互。

### 6.3 send_remote_message 作为协作通信统一入口

- 注册到 ToolRegistry Core Tier（全局可用）。
- 所有协作消息（request/response/consensus/end）均通过该工具发出。
- 该工具同时负责：
  - 写入共享黑板 `collaboration.md`（本机）
  - A2A HTTP 转发（远程）
- 由此统一入口保证 seq 连续、collab_id 一致、可被工作台归档。

---

## 七、关键文件清单

| 模块 | 文件 | 关键位置 |
|------|------|----------|
| A2A 工具 | `teage_liu/agent/tools/a2a_tools.py` | send_remote_message 实现 |
| Worker 协作核心 | `teage_liu/multiagent/worker_adapter.py` | `_trigger_urgent_llm` L1311、`_handle_request` L1068、`_handle_collab_message` L1017、`_build_collab_partner_context` L1264 |
| 共享黑板 | `teage_liu/multiagent/blackboard.py` | `CollabWriter` L580+、`append_collab_message`、`read_collab_messages` |
| A2A 网关 | `teage_liu/multiagent/a2a_gateway.py` | `_director_broadcast` L460、`_agent_message` L477、`_register_remote_agent` L331 |
| Agent 注册表 | `teage_liu/multiagent/agent_registry.py` | `list_active_agents` L115 |
| Director 注入 | `teage_liu/multiagent/director_injection.py` | `DirectorInjector` |
| Director 引擎 | `teage_liu/multiagent/director_engine.py` | `observe_collab` L377 |
| Prompt 构建 | `teage_liu/llm/prompts.py` | `build_collab_system_prompt` L470、`_COLLAB_SYSTEM_PROMPT_TEMPLATE` L394 |
| 前端工作台 | `web/static/js/collab-workbench.js` | `init`、`loadRecentMessages` L809、`renderCollabMessage` L216 |

---

## 八、实施顺序建议

1. 阶段 0（A2A 路径 + 工具增强）→ 解锁后续所有协作能力。
2. 阶段 1（文件锁 + 前端排序 + 广播 collab_id）→ 修复可见 Bug。
3. 阶段 2（Subagent + 讨论模式）→ 完善协作语义。
4. 阶段 3（稳定性）→ 提升鲁棒性。
5. 阶段 4（工作台）→ 体验打磨。

每个阶段完成后需回归验证：消息排序、协作连续性、Director 干预不破坏。

---

## 附录 A：代码审查验证结果（2026-08-01）

| # | 计划假设 | 验证结果 | 实际情况 |
|---|---------|---------|---------|
| A1 | CollabWriter 无锁 | ⚠️ 部分一致 | 已有 `asyncio.Lock`（blackboard.py L568），单进程内串行，**跨进程**仍冲突 |
| A2 | collab_id 需新建机制 | ⚠️ 部分存在 | blackboard 层已支持（`_get_collab_file` L486、`read_collab_messages` L501、`CollabWriter.append` L570 均有 collab_id 参数，文件分离 `collabs/{collab_id}.md`）；**调用层未传入**（send_remote_message、_trigger_urgent_llm、_director_broadcast、_handle_request 都没传 collab_id） |
| A3 | send_remote_message 只有 target_agent_id+content | ✓ 一致 | a2a_tools.py L168，已做本地写入+A2A转发，已注册 Core Tier |
| A4 | 屏蔽语句 4 处 | ✓ 一致 | worker_adapter.py L1052、L1125、L1306、L1560 |
| A5 | _trigger_urgent_llm 直接 append 绕过 A2A | ✓ 一致 | L1311-1408，L1383-1401 直接 append_collab_message，不调用 a2a_client |
| A6 | peer response L1047 缺 partner_context | ✓ 一致 | L1047-1059 无 `_build_collab_partner_context` 调用 |
| A7 | Director 广播 L1118 有 partner_context | ✓ 一致 | L1118 有调用 |
| A8 | _director_broadcast 不携带 collab_id | ✓ 一致 | a2a_gateway.py L466-472 消息体无 collab_id，写入全局 collaboration.md |
| A9 | list_active_agents 无心跳校验 | ✓ 一致 | agent_registry.py L115-122，心跳字段 `last_heartbeat` |
| A10 | 前端去重 key 是 from | ❌ 不一致 | 已是 `seq`（`_renderedSeqs` L222），问题是排序而非去重 |
| A11 | prompts.py L437-440 与 user prompt 矛盾 | ✓ 一致 | L438-441 说"通过 send_remote_message 通信"，与 worker 屏蔽语句矛盾 |

## 附录 B：collab_id 已有基础设施（可复用）

blackboard.py 已实现 collab_id 文件分离机制，**无需新建**，只需在调用层传入：

- `_get_collab_file(bb_root, collab_id)` L486：collab_id=None → `collaboration.md`；否则 → `collabs/{collab_id}.md`
- `read_collab_messages(bb_root, collab_id, ...)` L501：已支持按 collab_id 读取
- `CollabWriter.append(message, collab_id)` L570：已支持按 collab_id 写入
- `_read_last_collab_seq(bb_root, collab_id)` L546：已支持

**待接入点**（需修改以传入 collab_id）：
- `send_remote_message` 工具（a2a_tools.py L168）→ 新增 collab_id 参数
- `_trigger_urgent_llm`（worker_adapter.py L1383）→ 从 context_msg 取 collab_id 传入 append
- `_director_broadcast`（a2a_gateway.py L466）→ 消息体加 collab_id
- `_handle_request` / `_handle_collab_message` → 透传 collab_id

## 附录 C：新增风险与注意事项

1. **_build_collab_partner_context 调用 list_active_agents（L1270）**：阶段 3.1 加心跳校验后，若所有伙伴心跳过期，partner_context 返回"无在线伙伴"，协作会卡住。需保留兜底：心跳过期但仍在本机进程内的 agent 应视为在线（本机 agent 心跳可能未及时更新）。

2. **file-first 与 A2A 并存策略**：当前 file-first 是本机 agent 通信主路径（轮询 collaboration.md）。_trigger_urgent_llm 改造为驱动 send_remote_message 工具后，工具内部同时做本地写入+A2A转发，本机和远程都能收到。需保留本机轮询作为 fallback（LLM 未调用工具时系统代写仍走 file-first）。

3. **Director 干预必须保留**：_trigger_urgent_llm 中 `_director_injector.poll_and_enqueue_new_directives`（L1330）和 `drain_pending_directives`（L1331）在改造时不能破坏。DirectorInjector 的 rule_type（intervention/ordering/guidance）piggyback 机制需完整保留。

4. **幂等集已存在**：_handle_request 已用 `_responded_request_seqs`（L1105）和 `_processed_msg_seqs`（L1150）集合幂等。阶段 3.2 主要是确认 `_poll_collab_once` 推进逻辑是否用 `seq > last_seq`，若是则改为集合 + LRU 上限防回绕。

5. **_build_collab_partner_context 当前读全局 collaboration.md（L1277）**：接入 collab_id 后需改为按 collab_id 读取，否则跨协作消息会混在一起。

6. **跨进程锁与 asyncio.Lock 叠加顺序**：跨进程文件锁应在 asyncio.Lock **外层**（先抢跨进程锁，再抢进程内锁），避免持锁顺序不一致导致死锁。
