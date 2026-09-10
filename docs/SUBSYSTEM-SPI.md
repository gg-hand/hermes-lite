# 子系统接入规范(Subsystem SPI)

> **稳定面冻结声明(2026-08-21)**:协议版本 `v1.0.0`,`teage_liu2/PROTOCOL/` 为**唯一契约源**,本文档为实现视图,与 `teage_liu2/core/` 代码同步(发现不一致时**以代码为准**)。

> **文档性质**:本文档描述 teage_liu2 主干-枝干架构的**现行接入契约**——任一子系统(记忆 / 工具 / 护栏 / 调度 / 协作……)按本规范定义实现后,即可经配置接入主干,主干代码零改动。
>
> **配套阅读**:[ARCHITECTURE.md](ARCHITECTURE.md)(老系统现状总览)、[teage_liu2/docs/CORE.md](../teage_liu2/docs/CORE.md)(主干接口契约)。老系统(teage_liu/)已冻结,本文档 §14 为历史迁移记录。

---

## 1. 概念模型

### 1.1 主干(Core)

主干 = **不可再减的对话动作** + **扩展机制**:

```
输入 → [① build_injections 注入声明收集]
     → [② before(注册序,action 立即应用;SetStop 短路)]
     → [③ 组装收口(注入分层叠加 → merge 相邻 user → 语义级校验)]
     → [④ 送 LLM(loop/bare 形态)]
     → 事件流(step_start/text_delta/reasoning_delta/step_end/tool_use/tool_result/done/error)
     → [after(逆序) / on_error] → 输出
```

- **不可减部分**:一次 LLM 调用(事件流基建)+ 11 钩子链(扩展的本质)+ 会话历史消息级落盘(对话的身份)+ 多层级注入组装 + 协议桥(transport 消息);
- **全可裁部分**:工具、记忆、护栏、意图分类、审计、指标、调度、协作——每一个都是挂在钩子链上的**枝干/扩展**;
- **形态可插拔**:loop(React 循环,默认)/ bare(单轮)经**形态实例字典**选择,新形态 = 新类(实现 `run_stream`)+ 一行注册(`core/modes.py`)。

### 1.2 枝干(Branch / 扩展)

枝干 = 实现 `Branch` 接口的一个类(或经协议接入的异语言进程)。枝干之间**互不可见**,只能通过 `snapshot.extra`(命名空间 `{branch}.{key}`)交换数据,通过 **Snapshot(不可变)+ Action(变更请求)** 与主干交互。

### 1.3 钩子点(11 钩子)

| 时机 | 钩子 | 典型用途 |
|------|------|----------|
| 启动(一次) | `setup(config, host)` | 加载依赖、预热资源(向量库 / MCP / 模型) |
| 关闭(一次) | `teardown()` | 释放资源、冲刷自己的状态 |
| 每次对话组装前 | `build_injections(snapshot)` | 声明多层级注入项(L0 稳定区 ~ L4 输入前) |
| loop 每轮 step 前 | `inject_round(snapshot)` | 轮次间注入(全收集合并,层强制 BEFORE_INPUT) |
| LLM 前 | `before(snapshot)` | 输入扫描 / 意图路由 / 工具 schema 填充 / 拦截(SetStop) |
| 工具执行前 | `pre_tool_call(snapshot, name, input)` | 策略决策(reject 短路 / modify 叠加) |
| 工具执行 | `on_tool_call(snapshot, name, input)` | 执行工具(仅工具类枝干实现) |
| 工具执行后 | `post_tool_call(snapshot, ...)` | 结果审计 / 状态更新(action) |
| 每轮 step 后 | `after_step(snapshot, summary)` | 轮摘要(action) |
| 正常完成 | `after(snapshot, response)` | 输出过滤 / 审计 / 记忆巩固 / 指标(逆序) |
| 失败/断连/拦截 | `on_error(snapshot, error)` | 失败通知(逆序) |

---

## 2. 接口定义(正式版,`core/hooks.py`)

```python
class Branch(ABC):
    name: str = "branch"                 # extension_name,^[a-z0-9_]+$
    capabilities: list[str] = []         # observe / tool_executor / llm / self_hosted_storage
    host_port: Any = None                # 进程内能力端口(装配注入)

    # 生命周期(setup 失败 = 启动失败,禁止 try/except 吞错)
    async def setup(self, config: dict, host: Any) -> None: ...
    async def teardown(self) -> None: ...

    # 多层级注入(I1):声明注入项(位置 + 内容 + 优先级,自主控制)
    async def build_injections(self, snapshot) -> list[Injection]: return []
    async def inject_round(self, snapshot) -> Injection | None: return None

    # LLM 前后钩子(不可变 Snapshot 只读,经 Action 变更)
    async def before(self, snapshot) -> list[Action]: return []
    async def pre_tool_call(self, snapshot, name, input) -> ToolDecision:
        return ToolDecision(decision="allow")
    async def on_tool_call(self, snapshot, tool_name, tool_input) -> Any:
        return NotImplemented                        # 未实现返回 NotImplemented
    async def post_tool_call(self, snapshot, name, input, result, duration) -> list[Action]: return []
    async def after_step(self, snapshot, summary) -> list[Action]: return []
    async def after(self, snapshot, response) -> list[Action]: return []   # response 为 AfterResponse
    async def on_error(self, snapshot, error) -> list[Action]: return []
```

**Snapshot(不可变,`core/types.py`)**:`session_id / user_input / round / started_at / history / system_text / messages / tools / extra / stop / revision`;`revision` 由 host 独占递增(每次 action 批次 +1),扩展只读。

**Action 6 种(`core/actions.py`)**:`AppendMessage`(仅 role=user)/ `SetTools` / `SetExtra`(key 白名单 `^[a-z0-9_]+\.[a-z0-9_.]+$`)/ `SetStop`(短路)/ `SetSystem` / `ModifyToolSchema`。

**Injection(`core/injection.py`)**:`{layer, content, priority, key}` —— 五层:

```
L0 STABLE_SYSTEM   system 稳定区(前缀缓存命中)     ← 画像主体/全局规则/工具说明
L1 SYSTEM          system 末位(缓存失效仍有效)     ← 会话级指令/临时全局上下文
L2 PREFIX          messages[0] 前置(缓存失效点)    ← 检索记忆/环境信息/任务状态
L3 MID             历史中间(按位插入)              ← 对话背景/长期上下文说明
L4 BEFORE_INPUT    当前输入前(最动态)              ← 意图引导/瞬时指令
```

分层预算(默认可配置):stable_system 4000 / system 2000 / prefix 8000 / mid 2000 / before_input 2000;层内 `priority` 降序 + 注册序稳定,**整段丢弃不截半**;同 `key` 后声明覆盖先声明。**注入内容永不落盘**(瞬时上下文,只进 effective_messages)。

---

## 3. 顺序与生命周期契约

| 规则 | 内容 |
|------|------|
| **注册顺序 = 调用顺序** | `build_injections` / `before` / `pre_tool_call` 按注册序正序;`after` / `on_error` / `teardown` **逆序**(洋葱模型) |
| **枝干间隔离** | 禁止互相引用实例,只通过 `snapshot.extra` 通信;违反者在 code review 拦截 |
| **立即应用 + 原子批次** | 每个扩展的 action 批次在调用下一个扩展前**原子推进快照**(revision+1,COW 浅拷贝);SetStop 短路后续同名钩子 |
| **只读 vs 变更** | Snapshot 全部字段只读;变更一律经 Action(AppendMessage 仅 user;SetExtra 白名单 key) |
| **before 职责边界(E5)** | 只允许追加消息 / SetStop / 改 tools;**禁止删改历史与已组装注入**(防 400);违反由 C1 校验拦截 |
| **钩子超时** | 主干以 `asyncio.wait_for` 包裹每个钩子(默认 5s,可配置 `core.hook_timeout`),超时跳过该枝干并记日志;`on_tool_call` 不加超时(工具执行由工具自身负责超时) |
| **异常语义** | `setup` 失败 → 启动失败(配置开了却坏了,必须暴露);其余运行时异常 → 跳过该枝干 + `logger.error`(单枝干故障不影响对话) |
| **setup 原子性(E1)** | `setup_all` 任一失败 → **逆序 teardown 已成功枝干** → 再抛错(启动失败) |
| **终态钩子** | `after` / `on_error` 返回 action 一律忽略 + 记录(HOOK_TERMINAL_ACTION_IGNORED) |
| **observe 只读** | 声明 `observe` 的扩展,钩子返回 action 一律忽略 + 记录(§15-A4③) |
| **并发契约(E8)** | 枝干实例全局共享,必须**无状态或只读共享**;可变状态只能放 `snapshot.extra`(对话态)/ `SessionStore`(会话态);同 session 并发对话由**外壳** session 级 `asyncio.Lock` 串行化(§18.7) |
| **优雅关闭(L1)** | `registry.shutdown`:① TaskRegistry.cancel_all → ② teardown 逆序 → ③ message_store.close → ④ storage_provider.close;随后 supervisor.shutdown(扩展进程)+ storage_writer.close;幂等可多次调用 |

**新增契约(X1)**:
1. **钩子内调 LLM 防递归**:枝干(after 记忆巩固等)必须经 `invoke_llm` 消息(直调 `core.llm_client`,不进 pipeline/钩子链 → 递归,§15-A5);
2. **setup/teardown 可重建契约**:枝干必须幂等可重入(L2 热重载反复调用);
3. **宿主访问走消息通道**:扩展访问宿主能力(storage_*/invoke_llm/task_*)一律经 transport 消息(同语言 host_port / 异语言 stdio),不得以对象引用访问(§storage S-2)。

---

## 4. 错误责任矩阵(§8,错误码全集见 `core/errors.py`)

| 错误码 | 捕获层 | 上报 | 兜底 |
|--------|--------|------|------|
| LLM_TIMEOUT / LLM_CANCELED / LLM_STREAM_FAILED / LLM_API_ERROR(主对话) | step 层 | error 事件 | pipeline finally 落盘 |
| LLM_TIMEOUT / LLM_API_ERROR(invoke_llm) | **扩展自身** | `{error}` 响应 | 扩展自降级(记忆巩固失败仅告警) |
| LOOP_MAX_REACHED | loop 层 | logger.warning | done(max_loops) |
| HOOK_TIMEOUT / HOOK_EXCEPTION | HookChain | logger.error | 跳过该扩展 |
| HOOK_INVALID_ACTION | HookChain 应用层 | logger.error + 跳过该条 | 对话继续 |
| HOOK_TERMINAL_ACTION_IGNORED | HookChain 终态 | logger.error | 忽略 |
| TOOL_NO_EXECUTOR | dispatch 层 | logger.warning | done(no_tool_executor) |
| TOOL_EXEC_FAILED | dispatch 层 | tool_result is_error 回喂 | loop 回喂继续 |
| TOOL_REJECTED_BY_POLICY / TOOL_MODIFY_INVALID | pre_tool_call | tool_result is_error 回喂 | loop 回喂继续 |
| STORAGE_WRITE_FAILED / STORAGE_READ_FAILED | persist/storage 层 | logger.error | 对话继续(旁路)/ 降级 |
| CONFIG_UNKNOWN_KEY / CONFIG_INVALID_VALUE / CONFIG_MISSING_KEY | 配置加载 | 抛错(启动失败) | 可读错误 |

**7 终止原因**:normal / max_loops / user_cancel / no_tool_executor / llm_error / intercepted / tool_rejected(行为套件用例 16 全覆盖锚定)。

**禁止裸 `except Exception` 静默吞错**——所有捕获点必须带日志;`setup` 失败 = 启动失败(配置开了却坏了必须暴露)。

---

## 5. 状态三态模型(S1)

| 态 | 容器 | 生命周期 | 重启后 |
|----|------|----------|--------|
| 对话态 | `snapshot.extra`(`{extension_name}.{key}`) | 一次对话 | 消失(契约) |
| 会话态 | `snapshot.extra` 会话内延续(构建时从 SessionStore 恢复、结束写回,§5 L-10) | 一个会话 | **消失**(契约) |
| 持久态 | `StorageProvider`(自选 kind)或自持 | 枝干自管 | 保留(setup 时自恢复) |

- core **不为枝干状态提供持久化魔法**:SessionStore 是内存态,跨重启状态必须经 StorageProvider/自持并在 setup 恢复;
- **会话态协议化通道(§5 L-10)**:扩展访问会话态的**唯一通道 = 快照 extra 会话内延续**(构建时从 SessionStore 恢复 extra 基座、结束写回最终 extra,pipeline 自动接线);**不经 SessionStore 直访**,扩展经 SetExtra 读写;
- SessionStore 清理由调用方负责:`session_store.drop(session_id)`,core 不自动清理。

---

## 6. 消息合法性(C1)—— core 的保证

- **文档契约**:扩展只经 Action 变更快照(AppendMessage 仅 user);禁止删改历史与已组装注入;
- **运行时双保险校验**(core 行为):组装收口后 + 每轮增量收口(`incremental_finalize`),`validate_messages()`(`core/types.py`)校验消息序列——角色白名单(user/assistant)+ **交替**;M2 扩展 tool_result 配对 tool_use;
- 不合法 → `logger.error` + 拒绝消息序列(发 `error` 事件,**不发送给 LLM**);
- 注入平台配套保证交替:组装末尾合并相邻 user 消息(`merge_consecutive_user_messages`)。

---

## 7. 存储平台(D1,`core/storage.py`)

```
StorageProvider(ABC)          # 通用持久化通道 —— 扩展接口
│     write(kind, doc | docs[]) -> doc_id | doc_id[]   # 支持批量(§18.4)
│     read(kind, doc_id)            # kind 命名空间:"messages"/"audit"/
│     query(kind, limit=None, **filters)  # limit 防全量加载
│     delete(kind, doc_id)
│     close()
├── SQLiteStorageProvider     # 唯一实现:单库多 kind,每 kind 一张 doc 表
└── MessageStore(ABC)         # 消息级落盘契约(实现:SQLiteHistoryStore)
                              #   messages 专表:content 纯文本保 FTS +
                              #   content_blocks JSON(LLM 重建/工具配对)
                              #   + token_count/reasoning/message_type(D3)
```

- **扩展访问宿主存储的唯一通道 = transport `storage_*` 消息**(§storage S-2 / §15-A3):kind 必须带 `{extension_name}.` 前缀(读写都隔离,跨前缀拒绝);同语言实现经 `host_port` 亦走消息冷路径;
- kind 白名单 `^[a-z0-9_.]+$`(防表注入,非法名抛 ValueError);
- **全部 SQLite 写经 StorageWriter 异步单写者**(§18.1):user 前置 flush / 其余 background,事件循环零同步写;
- **落盘时机(事件驱动)**:user 前置 → step_end 落盘 assistant(content_blocks)→ tool_result 缓冲聚合落盘 user(配对 tool_use_id)→ finally 兜底(断连不丢);**注入永不落盘**;
- 历史读取窗口:最近 `history_window_messages`(默认 100)条;M2 condenser 做 token 预算 + 配对安全截断。

---

## 8. 观测(事件流三层 + L3 旁路)—— core 只供原材料

core **不内置指标/审计/事件总线**,只提供观测原材料:

| 原材料 | 形态 |
|--------|------|
| L1 热路径 | text_delta/reasoning_delta(宿主内直传,不进 transport) |
| L2 结构化摘要 | StepSummary / AfterResponse(经钩子快照+action) |
| L3 观测通知 | tool_use/tool_result/step_end 原始事件(异步批处理 50ms/64 条,经 event 消息投递 observe 扩展) |
| 外壳直通 | step_start/done/error(宿主内直传,不投 L3) |
| `snapshot.started_at` | 对话开始时间 |
| `snapshot.round` | 轮次号 |
| AfterResponse | 完成摘要(text/content_blocks/usage/done_event) |
| on_error | 失败通知 |

观测枝干 = **声明 `observe` 能力**的只读枝干(注册 after/on_tool_call,不干预对话;L3 订阅自动生效,每观测扩展有界队列 1024 满丢最旧+计数)。M2 先做轻量观测枝干验证原材料够用;不够则"补原材料",而非 core 内置指标。

**L3 投递形态(2026-08-21 缺口修复)**:observe 扩展的 L3 订阅在装配时接线——异语言(stdio)扩展经 `event` 消息投递;同语言扩展由外壳 `subscribe_inprocess_l3` 订阅,经 `Branch.on_l3_events(events)` 进程内异步投递(旁路,异常隔离,不阻断主对话流);热重载后自动重新订阅。同语言观测枝干实现 `on_l3_events` 即接收 L3 原始事件。

---

## 9. 配置契约(F1/F2,`core/config.py`)

```yaml
core:
  mode: loop                    # bare / loop(未知值启动失败)
  max_loops: 50                 # 1-200
  system_prompt: ...            # 字符串(默认内置)
  hook_timeout: 5.0             # 0.1-60 秒
  history_window_messages: 100  # 1-10000
  injection_budget_chars:       # 分层预算(层名白名单)
    PREFIX: 8000
  max_snapshot_bytes: 2097152            # §15-A6 资源上限(独立配置项,防 DoS)
  max_message_bytes: 524288
  max_messages_per_conversation: 2000
  branches:                     # 枝干声明,顺序 = 注册顺序
    guardrails: { enabled: true }
    audit:                      # 异语言扩展(transport: stdio)
      transport: stdio
      command: ["python", "path/to/ext.py"]
      protocol_version: v1.0.0
      hooks_implemented: [setup, before, after]
      capabilities: [observe, llm]
    # 未声明或 enabled:false → 不注册,主干零感知
```

- **core 段严格校验**(`core_config_from(cfg) -> CoreConfig`):类型 + 范围 + **未知键拒绝**(core 段未定义键 → 启动失败,可读错误列未知键)——防 typo 静默失效;
- **枝干配置枝干自校验**(setup 里,失败 = 启动失败;setup 收到**该枝干自己的配置段** + host 纯数据声明);
- 未知名枝干名(工厂未注册)→ 启动失败;声明 transport 但无扩展启动器 → 启动失败。

---

## 10. 工具枝干的接入(特殊形态)

工具系统是唯一需要"循环"的枝干,契约设计使其与其他枝干同构:

```python
class ToolBranch(Branch):
    name = "tools"
    capabilities = ["tool_executor"]      # 声明工具执行能力(§12)

    async def setup(self, config, host) -> None:
        self.registry = build_registry(config)  # 加载工具注册表

    async def build_injections(self, snapshot):
        return [Injection(layer="STABLE_SYSTEM", content=工具说明, priority=10)]

    async def before(self, snapshot) -> list[Action]:
        return [SetTools(tools=self.registry.build_schemas())]  # 填充 schema

    async def on_tool_call(self, snapshot, tool_name: str, tool_input: dict) -> Any:
        return await self.registry.execute(tool_name, tool_input)
        # 未实现返回 NotImplemented;抛异常由 dispatch 层捕获
        # 转 tool_result is_error 回喂 LLM
```

循环策略:`snapshot.tools` 为空 → 单轮纯对话;非空 → loop 形态:LLM 返回 `tool_use` → 工具路径四连(`pre_tool_call` 链 → `tool_use` 事件 → 执行 → `tool_result` 回喂)→ 再调 LLM,直到 `end_turn` / `max_loops` / 无执行者友好终止。**工具只是多实现一个钩子点的枝干,ReactLoop 收敛为主干内置形态,不再是独立组件**。

> 异语言工具扩展:声明 `tool_executor` 且经 RemoteBranchAdapter 接入时,core 派发走 `invoke_tool` 轻量消息(仅 name+input,免快照序列化,§transport T-5)。

---

## 11. 协议桥与生命周期(transport / 异语言扩展)

**transport 消息全集**(`core/transport.py`,与 PROTOCOL/transport.schema.json 一致):

`invoke_hook / invoke_tool / invoke_llm / storage_write / storage_read / storage_query / storage_delete / task_register / task_cancel / event / heartbeat / shutdown`

- **帧格式**:`{type, payload, encoding: full|delta, protocol_version}`,JSON 行编码;序列化边界(帧长 4MiB / JSON 深度 64 / 非法帧拒绝,§15-A7);
- **异语言扩展接入**:配置 `transport: stdio + command`,Supervisor spawn 进程 → 握手互报 `protocol_version`(版本协商 §evolution V-2:major 拒绝启动 / minor 降级)→ RemoteBranchAdapter 对 core 是普通扩展,11 钩子经 invoke_hook 转发;
- **热重载(L2/L-3)**:`registry.rebuild`(build 新链 → setup 成功 → teardown 旧链 → 替换;失败回滚保旧链)+ `supervisor.reload`(新进程 spawn → rebuild → 替换/回滚);外壳 `POST /reload` 端点;
- **L3 观测**:BatchBuffer 50ms/64 条先到触发,每观测扩展有界队列(1024),丢弃计数随心跳上报(§15-A6);
- **invoke_llm 并发上限**:信号量硬边界(INVOKE_LLM_MAX_CONCURRENCY=4,§15-A6)。

---

## 12. 接入三步法(给新子系统作者)

1. **实现**:在 `extensions_root` 下建 `<name>/manifest.yaml + main.py`(统一扩展目录树,§13.2),`main.py` 导出 `create_branch(config) -> Branch`;继承 `Branch`,只实现需要的钩子(其余继承空实现),`name` 取唯一标识(extension_name `^[a-z0-9_]+$`,须与目录名一致);
   - 同语言需访问宿主能力(存储/LLM/任务):经装配注入的 `host_port`(`storage_write/query/invoke_llm` 等消息方法);
   - 异语言:manifest 声明 `language: other + transport: stdio + command`(相对路径相对 manifest 目录解析),按协议实现 stdio JSON 行进程;
2. **启用**:config.yaml 的 `core.branches.<name>` 声明 `enabled: true` + 运行配置(安装 ≠ 激活;关闭 = 不注册;异语言扩展由 supervisor.launcher 装配);
3. **验证**:跑 `pytest tests_core/` 的契约测试——"枝干顺序正确、隔离有效、超时生效、setup 失败回滚、注入不落盘"。

**register_factory 定位(§2.1 定案,2026-09-08)**:测试/行为套件/编程式嵌入的内存注入通道,**非生产装载方式**。生产扩展一律走 extensions_root 目录发现(manifest.yaml 为安装态唯一事实源)。当前唯二合法使用方 = `PROTOCOL/behavior-suite/runner.py`(注入 ScriptedBranch)与 `tests_core` / `tests_branches`(注入测试枝干)。"外壳不知道任何枝干名"由代码事实保证:`server/` 无 import branches、生产路径零 register_factory 调用。

新枝干**不得**改动 `pipeline` / 钩子链 / 其他枝干——这是接入是否"合格"的唯一硬标准。

---

## 13. 最小完整示例:时间与环境信息枝干

```python
from teage_liu2.core.actions import SetExtra
from teage_liu2.core.hooks import Branch
from teage_liu2.core.injection import Injection, L_STABLE_SYSTEM


class EnvironmentBranch(Branch):
    """注入当前时间到 system 稳定区,并在对话结束后记录一条审计。"""

    name = "environment"

    async def setup(self, config, host) -> None:
        self.show_time = config.get("show_time", True)
        self.log_file = config.get("log_file")

    async def build_injections(self, snapshot):
        if not self.show_time:
            return []
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        return [Injection(L_STABLE_SYSTEM, f"当前时间:{now}")]

    async def before(self, snapshot) -> list:
        import datetime
        return [SetExtra(key="environment.hour", value=datetime.datetime.now().hour)]

    async def after(self, snapshot, response) -> list:
        # 持久化经宿主消息通道(storage_*):同语言用 host_port,异语言经 stdio
        if self.log_file and self.host_port is not None:
            await self.host_port.storage_write("environment.audit", [{
                "session_id": snapshot.session_id,
                "round": snapshot.round,
                "text": response.text[:50],
            }])
        return []
```

**要点**:`setup` 无 try/except(配置开但坏 → 启动失败);`build_injections` 按稳定度选层;枝干只依赖 `snapshot` / `host` / `host_port` 与自己的配置——与主干、其他枝干零耦合。

### 13.1 观测枝干示例:audit(observe 只读,M2 首个实验性扩展,2026-08-21 已落地)

```python
# branches/audit.py(完整实现见仓库;此处展示关键形态)
class AuditBranch(Branch):
    name = "audit"
    capabilities = ["observe"]          # 只读约束:钩子返回 action 一律忽略

    async def after(self, snapshot, response) -> list:
        # 终态钩子:action 忽略,数据经 host_port 消息通道落盘(kind 带前缀)
        # snapshot 为终态快照(round/messages 为对话结束后状态)
        await self.host_port.storage_write("audit.conversations", [{
            "session_id": snapshot.session_id,
            "response_text": response.text[:200],
            "rounds": snapshot.round,
        }])
        return []
```

要点:
- `capabilities = ["observe"]`:只读;L3 订阅声明;钩子返回 action 一律忽略 + 记录;
- 所有写经 `host_port.storage_write`,kind 必须带 `audit.` 前缀(§15-A3 跨前缀拒绝);
- 钩子内 await 全部 try/except 降级(best-effort,失败不影响对话);
- `after` / `on_error` 收到的是**终态快照**(round 为实际轮次,2026-08-21 修复);
- 同语言 observe 扩展目前仅收 L2 摘要(钩子),L3 原始事件投递仅对 stdio 异语言扩展接线(见 CORE-缺口记录.md)。

---

### 13.2 统一扩展目录树(2026-09-08 已落地)

**形态**(VSCode 式):目录 = 安装单位,`manifest.yaml` = 安装态唯一事实源;运行态(enabled 开关 + 配置覆盖)在 config.yaml 的 `core.branches.<name>`。根目录由 `core.extensions_root` 指定(默认 `data2/extensions/`,仓库外不入库;相对路径相对 cwd,同 storage 语义)。

```yaml
# <extensions_root>/<name>/manifest.yaml(启动/热重载时严格校验,坏 manifest = 该扩展装配失败)
name: audit                # 必填,^[a-z0-9_]+$,必须与目录名一致
version: 0.1.0             # 必填,扩展自身版本(与宿主/协议版本无关)
language: python           # 必填,python(进程内) | other(必须配 transport)
entry: main.py             # language=python 必填,相对 manifest 目录
# language=other 时必填:transport: stdio / command: [...](相对路径相对 manifest 目录解析)
#                        protocol_version 可选;entry 不支持
capabilities: [observe]    # 必填(可空列表),值域 = observe | tool_executor | llm | self_hosted_storage
description: ...           # 可选
# requirements: [...]     # 可选,同语言第三方依赖声明(仅文档,宿主不自动安装)
# kind: branch(缺省) | host-component   # P-6(2026-09-09,experimental)可选新增字段
# slots: [storage, history]             # host-component 必填:可接管插槽(⊆ SLOTS 白名单)
```

**P-6 宿主组件 backend 目录发现**(2026-09-09):manifest 新增可选 `kind` 字段(缺省
`branch`,存量扩展零影响)。`kind: host-component` = 宿主组件 backend(如 Rust 存储
后端 storage_rust),不作为枝干装载、不计入"已装未启用"统计;其 `slots` 必填且须覆盖
请求插槽、`capabilities` 必须为空(钩子授权面不适用)、强制 `language: other` +
`transport: stdio`;**core.branches 声明 host-component 扩展名 = 启动失败**。引用方式
= host_components 的 `options.extension`(与 `options.command` 二选一),宿主按
extensions_root 读 manifest 取 command(相对 manifest 目录解析),`options.args`
追加启动参数(如 `--db`);首个用方见 `docs/plans/2026-09-09-rust存储后端扩展-设计.md`。

**main.py 约定**(language=python):导出 `def create_branch(config: dict) -> Branch`;扩展只允许 import `teage_liu2.core` 的接口(hooks/actions/types 等纯数据契约),禁止 import core 实现内部模块与 `teage_liu2.server`;子模块/资源经 `__file__` 相对定位,装载器不污染 `sys.path`。

**装载语义**(`core/extension_loader.py`):
- python → `importlib` 进程内动态装载,模块名 `teage_liu2_ext_<name>_<manifest_hash>`:manifest 变更 → 全新模块对象(热重载隔离),未变 → 命中 `sys.modules` 缓存。**注意:只改 main.py 不触碰 manifest.yaml → hash 不变 → /reload 仍用旧缓存模块;改代码须同步 bump manifest version 才能热重载生效**
- **授权一致性**:代码类声明的 `capabilities` 必须 ⊆ manifest 授权面,超出 = 启动失败(manifest 为授权声明面唯一事实源,防声明面被代码架空)
- other → manifest 的 transport/command/protocol_version 由 `wire_extensions` 合并进 effective config,走 supervisor launcher(stdio,现状不变)
- config 声明且启用的扩展缺失/manifest 非法 → **启动失败**;已安装未声明 = 不启用(安装 ≠ 激活,启动日志提示)
- 解析优先级:`register_factory`(测试/嵌入注入,见 §12 定位)> 目录装载器 > ValueError

**信任边界**:进程内 = 信任执行(扩展与宿主同进程);兜底 = `hook_timeout` + 钩子异常隔离 + L3 旁路投递超时(5s)。需要强隔离的扩展用 `language: other`(stdio 进程)。guardrails 外迁后,安全策略的完整性 = 扩展目录完整性(个人自托管单用户场景接受,见设计文档 §8)。

## 14. 迁移历史(老系统 → teage_liu2)

- teage_liu2 为同仓库提取式重写(M1 纯对话内核已交付),老系统 `teage_liu/` **冻结**(只修致命 bug);
- 老系统的"判空 + 异常降级"退化接入(组件可 None 由**初始化失败**决定)已被"配置开关 + 钩子注册"显式形态取代(枝干不存在由**配置关闭**决定,声明驱动、可测试);
- 里程碑:M1(纯对话内核)→ 基础夯实(落盘注入接入)→ 阶段 0(协议族立根)→ 阶段 1(宿主数据层)→ 阶段 2(交互模型迁移,Snapshot+Action)→ 阶段 3(协议桥与生命周期,transport/stdio/supervisor/L3)→ **阶段 4(稳定面冻结 v1.0)**;切换完成后删除老系统(一次性仪式)。

## 15. 文档维护

- 本规范是**契约**,接口变更需走"先改计划文档 → 评审 → 再改代码"的顺序;
- **唯一契约源 = `teage_liu2/PROTOCOL/`**(9 域 spec + schema + 行为套件);本文档与 CORE.md 为实现视图,变更时以 PROTOCOL/ 与代码为准回写;
- 新增枝干接入案例可追加到 §13(保持示例可运行)。
