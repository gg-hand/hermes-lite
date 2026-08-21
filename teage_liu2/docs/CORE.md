# teage_liu2 核心文档(Core)

> **稳定面冻结声明(2026-08-21)**:协议版本 `v1.0.0`(`teage_liu2/PROTOCOL/VERSION`),`teage_liu2/PROTOCOL/` 为**唯一契约源**。本文档为实现视图,与 `teage_liu2/core/` 代码同步;发现不一致时**以代码为准**并更新本文档。

> **本文档定位**:teage_liu2 主干的**权威接口契约**。任何新模块(枝干 / 形态 / 存储实现 / 外壳)依据本文档 + [INTERFACES.md](./INTERFACES.md)(示例)即可自行实现扩展,**无需依赖老系统 `teage_liu/`**。
>
> **配套**:子系统接入规范 `docs/SUBSYSTEM-SPI.md`;协议族(语言无关) `teage_liu2/PROTOCOL/`(9 域 spec + schema + 行为套件)。

---

## 1. 架构总览

```
┌─ 外壳 server/ ──────────────────────────────────────────────┐
│   传输层(FastAPI):只依赖 core,不感知 branches                │
│   装配点例外:composition root 注册枝干工厂(见 §8)            │
│   会话并发互斥:session 级 asyncio.Lock(B3 根治,§12)          │
├─ 主干 core/ ────────────────────────────────────────────────┤
│   不可再减的对话动作 + 扩展机制                              │
│   pipeline(装配/落盘/校验) · modes(形态字典) · loop(循环)    │
│   step(单次 LLM 调用) · hooks(11 钩子链) · assembler(收口)  │
│   snapshot(Snapshot+Action 推进) · actions(6 Action)        │
│   injection(多层级注入) · event_stream(L1/L2/L3)            │
│   transport(TransportFrame/TransportBus) · stdio(跨进程)    │
│   remote_adapter(异语言扩展) · supervisor(进程监管/热重载)   │
│   storage(存储平台) · registry(装配) · session(会话态)      │
│   tasks(后台任务) · llm(多 provider 多角色) · config(校验)  │
│   types(值对象/事件/快照) · errors(错误码/终止原因)          │
├─ 枝干 branches/ ────────────────────────────────────────────┤
│   可插拔子系统,互不可见,只依赖 core 接口                    │
└──────────────────────────────────────────────────────────────┘
```

**依赖铁律**(违反即打回):
1. 主干**不知道任何枝干的名字**(不 import branches);
2. 枝干依赖 core 的接口,**不依赖彼此**(枝干间只经 `Snapshot.extra` 通信,命名空间 `{branch}.{key}`);
3. 外壳只依赖 core(唯一例外:装配点注册枝干工厂)。

**对话流程**(收口四连 + 事件流,§4.4):

```
输入 → [① build_injections 注入声明收集]
     → [② before(注册序,action 立即应用;SetStop 短路 → done(intercepted))]
     → [③ 组装收口(注入分层叠加 → merge 相邻 user → 语义级校验)]
     → [④ 送 LLM(裸形态单步 / 循环形态)]
     → 事件流(step_start/text_delta/reasoning_delta/step_end/tool_use/tool_result/done/error)
     → [after(逆序,终态钩子 action 一律忽略)] / [on_error]
```

---

## 2. Branch 接口(11 钩子 + Snapshot + Action)

```python
from abc import ABC
from teage_liu2.core.actions import Action, ToolDecision
from teage_liu2.core.injection import Injection
from teage_liu2.core.types import Snapshot, StepSummary

class Branch(ABC):
    name: str = "branch"                 # 唯一标识(^[a-z0-9_]+$,extension_name)
    capabilities: list[str] = []         # observe / tool_executor / llm / self_hosted_storage
    host_port: Any = None                # 进程内能力端口(装配注入;storage_*/invoke_llm 消息通道)

    # ---- 生命周期(setup 失败 = 启动失败,禁止 try/except 吞错)----
    async def setup(self, config: dict, host: Any) -> None: ...   # host = 宿主能力声明(纯数据)
    async def teardown(self) -> None: ...                         # 幂等可重入

    # ---- 注入(每次对话组装前 / loop 每轮 step 前)----
    async def build_injections(self, snapshot: Snapshot) -> list[Injection]: return []
    async def inject_round(self, snapshot: Snapshot) -> Injection | None: return None

    # ---- LLM 前后(不可变 Snapshot 只读,经 Action 变更)----
    async def before(self, snapshot: Snapshot) -> list[Action]: return []
    async def pre_tool_call(self, snapshot, name: str, input: dict) -> ToolDecision:
        return ToolDecision(decision="allow")
    async def on_tool_call(self, snapshot, tool_name: str, tool_input: dict) -> Any:
        return NotImplemented                    # 未实现返回 NotImplemented
    async def post_tool_call(self, snapshot, name, input, result, duration) -> list[Action]: return []
    async def after_step(self, snapshot, summary: StepSummary) -> list[Action]: return []
    async def after(self, snapshot: Snapshot, response: Any) -> list[Action]: return []
    async def on_error(self, snapshot: Snapshot, error: Any) -> list[Action]: return []
```

**交互模型(§4.2/§4.4)**:core ── Invocation{hook, snapshot, args} ──▶ 扩展;扩展 ── Action[] ──▶ core 立即应用。
- Snapshot **不可变**:`revision` 由 host 独占递增(每次 action 批次 +1),扩展只读;
- Action 6 种:`AppendMessage`(仅 role=user)/ `SetTools` / `SetExtra`(key 白名单 `^[a-z0-9_]+\.[a-z0-9_.]+$`)/ `SetStop`(短路)/ `SetSystem` / `ModifyToolSchema`;
- 立即应用 + 原子批次:每个扩展的 action 批次在调用下一个扩展前**原子推进快照**(O(n) 浅拷贝 + 结构共享,零 deepcopy);
- 终态钩子(`after`/`on_error`)返回 action 一律忽略(HOOK_TERMINAL_ACTION_IGNORED)。

**钩子契约表**:

| 钩子 | 时机 | 语义 | 超时 | 失败行为 | 约束 |
|------|------|------|------|----------|------|
| `setup(config, host)` | 启动一次 | 初始化资源 + 枝干配置自校验 | 无 | 抛错 = 启动失败(逆序回滚) | 收到**自己**的配置段 + host 纯数据声明;幂等可重入 |
| `teardown()` | 关闭/重建 | 释放资源 + 冲刷自己的状态 | 无 | 仅告警(逆序继续) | **幂等可重入** |
| `build_injections(snapshot)` | 每次对话组装前 | 声明多层级注入项 | 5s | 隔离跳过 | 注入永不落盘;快照含完整消息列表 |
| `inject_round(snapshot)` | loop 每轮 step 前 | 全收集合并(§4.2) | 5s | 隔离跳过 | layer 强制 BEFORE_INPUT |
| `before(snapshot)` | 组装后 LLM 前 | 最后干预(action) | 5s | 隔离跳过 | SetStop 短路后续扩展;observe 扩展 action 忽略 |
| `pre_tool_call(snapshot,name,input)` | 工具执行前 | 策略决策 | 5s | 隔离跳过 | reject 短路 / modify 叠加(§8) |
| `on_tool_call(snapshot,name,input)` | 工具请求 | 执行工具 | 无(工具自控) | 返回异常实例 → 转 `tool_result is_error` 回喂 | 未实现返回 NotImplemented |
| `post_tool_call(snapshot,...)` | 工具执行后 | action 立即应用 | 5s | 隔离跳过 | 增量收口校验 |
| `after_step(snapshot,summary)` | 每轮 step 后 | action 立即应用 | 5s | 隔离跳过 | 增量收口校验 |
| `after(snapshot,response)` | 正常完成 | 收尾(记忆/审计/观测) | 5s | 隔离跳过 | **逆序**;action 一律忽略 |
| `on_error(snapshot,error)` | 失败/断连/拦截 | 失败通知 | 5s | 隔离跳过 | **逆序**;action 一律忽略 |

**两条硬契约**:
1. **钩子内调 LLM 防递归**:必须直调 `core.llm_client`(或经 `invoke_llm` 消息),禁止调 `core.pipeline.chat_stream`(重入钩子链 → 递归,§15-A5);
2. **setup/teardown 可重建**:必须幂等可重入(L2 热重载反复调用)。

---

## 3. Snapshot(不可变快照,替换 BranchContext)

```python
@dataclass(frozen=True)
class Snapshot:
    session_id: str        # 会话 ID(只读)
    user_input: str        # 本次用户输入(只读)
    round: int             # 轮次号(loop 每轮递增,启动 0)
    started_at: str        # 对话开始时间(ISO,只读)
    history: list          # 会话历史(只读)
    system_text: str       # 主干基础 system(SetSystem 可覆盖)
    messages: list         # 对话消息(读;追加经 AppendMessage action)
    tools: list            # 工具 schema 列表(SetTools / ModifyToolSchema)
    extra: dict            # 枝干间共享数据(SetExtra,命名空间 `{branch}.{key}`)
    stop: bool             # SetStop → 主干跳过 LLM 直接 done(拦截)
    revision: int          # 快照版本(host 独占递增,扩展只读)
```

- 扩展**只能经 Action 变更快照**,不得直接修改;
- 枝干间共享数据一律走 `extra`(`{branch}.{key}`),禁止引用其他枝干实例;
- 消息合法性由 core 保证(C1 双保险):每轮组装后 + 增量收口校验角色交替,非法 → `error` 事件,**不发送给 LLM**。

---

## 4. 多层级注入(Injection)

```python
from teage_liu2.core.injection import Injection, L_STABLE_SYSTEM, L_SYSTEM, \
    L_PREFIX, L_MID, L_BEFORE_INPUT

Injection(layer, content, priority=0, key=None)
```

| 层 | 常量 | 位置 | 典型内容 |
|----|------|------|----------|
| L0 | `STABLE_SYSTEM` | system 稳定区(前缀缓存命中) | 画像主体/全局规则/工具说明 |
| L1 | `SYSTEM` | system 末位 | 会话级指令/临时全局上下文 |
| L2 | `PREFIX` | messages[0] 前置 | 检索记忆/环境信息/任务状态 |
| L3 | `MID` | 历史中间(按位插入) | 对话背景/长期上下文说明 |
| L4 | `BEFORE_INPUT` | 当前输入前 | 意图引导/瞬时指令 |

- 分层预算(默认可配置):`STABLE_SYSTEM 4000 / SYSTEM 2000 / PREFIX 8000 / MID 2000 / BEFORE_INPUT 2000`;
- 层内按 `priority` 降序 + 注册序稳定裁剪,**超预算整段丢弃不截半**;同 `key` 后声明覆盖先声明;
- **注入内容永不落盘**(瞬时上下文);
- 组装末尾自动合并相邻 user 消息(API 交替要求,防 400)。

---

## 5. 事件流契约(流是基建)

主干以 **async generator 事件流**输出,外壳编码为 SSE,非流式 = 收集 `text_delta` 拼字符串。8 种事件:

| 事件 | 时机 | 关键字段 |
|------|------|----------|
| `step_start` | 一次 LLM 调用开始 | step |
| `text_delta` | 文本增量 | text |
| `reasoning_delta` | 推理增量(可选) | text, signature |
| `step_end` | 一次 LLM 调用结束 | content_blocks, stop_reason, usage(循环形态**逐轮透传**) |
| `tool_use` | LLM 请求工具 | name, input, original_input, effective_input |
| `tool_result` | 工具结果回传 | name, **tool_use_id**, result, is_error, modified |
| `done` | 对话结束 | 见下(9 键) |
| `error` | 对话失败(不产生 done) | message |

**事件流三层(§3.2)**:
- **L1 热路径**(text_delta/reasoning_delta):宿主内部直传,绝不进 transport;
- **L2 结构化摘要**(StepSummary/AfterResponse):经钩子快照+action 传递;
- **L3 观测通知**(tool_use/tool_result/step_end 原始事件):异步批处理旁路(50ms/64 条先到触发),经 `event` 消息投递声明 `observe` 的扩展;每观测扩展有界队列(1024)满丢最旧+计数,绝不阻断主对话流;
- 外壳直通事件(step_start/done/error):宿主内部直传,不投 L3。

**done 事件统一 9 键**(所有终止路径一致,观测枝干可依赖):

```python
{"type": "done", "session_id": str, "response": str,
 "messages": list, "is_complete": bool, "termination_reason": str,
 "usage": dict|None, "content_blocks": list, "stop_reason": str}
```

**7 终止原因**:normal / max_loops / user_cancel / no_tool_executor / llm_error / intercepted / tool_rejected。
**事件源统一契约**:必以 `done` 或 `error` 收尾。

---

## 6. 生命周期与编排

```
启动: registry.build(cfg) → supervisor.launch_all(cfg)(spawn stdio 扩展 + 握手协商)
     → lifespan setup_all(cfg, host_builder)(host = 纯数据声明,kind 前缀按扩展名)
     (任一 setup 失败 → 逆序 teardown 已成功枝干 → 抛错 = 启动失败)  [E1]
对话: chat_stream(...) → 事件流(route_l3 旁路)→ after(正常)/ on_error(失败/断连/拦截)
热重载: POST /reload → supervisor.reload(新进程 spawn → registry.rebuild → 替换/回滚)
关闭: registry.shutdown(task_registry, message_store, storage_provider)
     = ①TaskRegistry.cancel_all → ②teardown_all(逆序,枝干各自冲刷)
       → ③message_store.close → ④storage_provider.close(幂等)      [L1]
     → supervisor.shutdown(扩展进程 shutdown 帧 + 终止兜底)
     → storage_writer.close(排空写队列) → llm_client.close
```

**宿主能力声明(host,§5 L-8)**:`setup(config, host)` 的 `host` 为**纯数据**(非对象引用):

```python
{
    "protocol_domains": ["types", "events", "hooks", "lifecycle", "storage",
                         "config", "transport", "errors", "evolution"],
    "storage": {"channel": "transport.storage_*", "kind_prefix": extension_name},
}
```

**协议桥(阶段 3/4 落地)**:扩展访问宿主能力的唯一通道 = transport 消息(`core/transport.py`):
- `storage_*`:kind 前缀隔离(§15-A3,`{extension_name}.` 强制);
- `invoke_llm`:LLMClient.chat_role 直调(防重入)+ 并发信号量硬边界(§15-A6);
- `task_register/cancel`:宿主登记扩展侧任务(T-4);
- 同语言扩展经 `host_port`(InProcessHostPort,消息语义零成本);异语言经 stdio 通道;
- **版本协商(§evolution V-2)**:握手互验 `protocol_version`,major 不匹配 → 拒绝启动,minor 降级兼容。

**状态三态模型**:

| 态 | 容器 | 生命周期 | 重启后 |
|----|------|----------|--------|
| 对话态 | `snapshot.extra`(`{extension_name}.{key}`) | 一次对话 | 消失(契约) |
| 会话态 | `snapshot.extra` 会话内延续(构建时从 SessionStore 恢复、结束写回,§5 L-10) | 一个会话 | 消失(契约) |
| 持久态 | `core.storage_provider`(自选 kind)或自持 | 枝干自管 | 保留(setup 时自恢复) |

> **会话态协议化通道(§5 L-10)**:扩展访问会话态的**唯一通道 = 快照 extra 会话内延续**——core 构建快照时从 SessionStore 恢复同 session 的 extra 基座,对话结束(done/error)写回最终 extra(经 pipeline 自动接线);**不经 `core.session_store.get` 直访**(那是宿主容器内部实现,扩展经 SetExtra 读写)。

**并发契约**:枝干实例全局共享,必须**无状态或只读共享**;可变状态只能放 `snapshot.extra` / `SessionStore`。同 session 并发对话由**外壳**以 session 级 `asyncio.Lock` 串行化(§18.7,core 单快照无共享)。

---

## 7. 配置契约(F1/F2)

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

- `core_config_from(cfg) -> CoreConfig`:类型 + 范围 + **未知键拒绝**(core 段未定义键 → 启动失败,可读错误列未知键);
- 枝干配置**枝干自校验**(setup 里,失败 = 启动失败);
- 未知名枝干名(工厂未注册)→ 启动失败;声明 transport 但无扩展启动器 → 启动失败。

---

## 8. 接入三步法(新枝干)

1. **实现**:继承 `Branch`,只实现需要的钩子,`name` 取唯一标识(extension_name `^[a-z0-9_]+$`;示例见 [INTERFACES.md](./INTERFACES.md));
2. **注册**:装配层(`server/app.py`)`registry.register_factory(name, factory)` 一行注册;配置声明 `enabled: true`;
   - 异语言扩展:配置 `transport: stdio + command`,由 `supervisor.launcher` 装配;
   - 需访问宿主能力(存储/LLM/任务):同语言经注入的 `host_port`,异语言经 stdio 消息;
3. **验证**:跑 `pytest tests_core/ tests_branches/` 契约测试(顺序/隔离/超时/回滚/注入不落盘)。

**硬标准**:新枝干**不得**改动 `core/` 任何文件(接入 = 零改动铁律)。

---

## 9. 错误责任矩阵(§8,错误码全集见 `core/errors.py`)

| 错误码 | 捕获层 | 上报 | 兜底 |
|--------|--------|------|------|
| LLM_TIMEOUT / LLM_CANCELED / LLM_STREAM_FAILED / LLM_API_ERROR(主对话) | step 层 | error 事件 | pipeline finally 落盘 |
| LLM_TIMEOUT / LLM_API_ERROR(invoke_llm) | **扩展自身**(经 transport 透传) | `{error}` 响应 | 扩展自降级 |
| LOOP_MAX_REACHED | loop 层 | logger.warning | done(max_loops) |
| HOOK_TIMEOUT / HOOK_EXCEPTION | HookChain | logger.error | 跳过该扩展 |
| HOOK_INVALID_ACTION | HookChain 应用层 | logger.error + 跳过该条 | 对话继续 |
| HOOK_TERMINAL_ACTION_IGNORED | HookChain 终态 | logger.error | 忽略 |
| TOOL_NO_EXECUTOR | dispatch 层 | logger.warning | done(no_tool_executor) |
| TOOL_EXEC_FAILED | dispatch 层 | tool_result is_error 回喂 | loop 回喂继续 |
| TOOL_REJECTED_BY_POLICY / TOOL_MODIFY_INVALID | pre_tool_call | tool_result is_error 回喂 | loop 回喂继续 |
| STORAGE_WRITE_FAILED / STORAGE_READ_FAILED | persist/storage 层 | logger.error | 对话继续(旁路)/ 降级 |
| CONFIG_UNKNOWN_KEY / CONFIG_INVALID_VALUE / CONFIG_MISSING_KEY | 配置加载 | 抛错(启动失败) | 可读错误 |

**禁止裸 `except Exception` 静默吞错**——所有捕获点必须带日志(§R-1)。

---

## 10. 存储平台

```python
core.storage_provider.write(kind, doc | docs[]) -> doc_id | list[doc_id]  # 支持批量(§18.4)
core.storage_provider.read(kind, doc_id) -> dict|None
core.storage_provider.query(kind, limit=None, **filters) -> list[dict]    # limit 防全量加载
core.storage_provider.delete(kind, doc_id)
core.storage_provider.close()
```

- kind 白名单 `^[a-z0-9_.]+$`(防表注入,非法名抛 ValueError);
- **扩展访问宿主存储的唯一通道 = transport `storage_*` 消息**(§storage S-2):kind 必须带 `{extension_name}.` 前缀(§15-A3 读写隔离,跨前缀拒绝);同语言实现亦走消息冷路径(host_port),不得以对象引用访问;
- 默认实现 `SQLiteStorageProvider`(单库多 kind,每 kind 一张 doc 表);
- `MessageStore` = 消息级落盘契约(content_blocks JSON / token_count / reasoning / message_type),实现 `SQLiteHistoryStore`;
- **落盘时机(事件驱动,全部 SQLite 写经 StorageWriter 异步单写者,§18.1)**:user 前置(flush,断连不丢)→ step_end 落盘 assistant → tool_result 缓冲聚合落盘 user(配对 tool_use_id)→ finally 兜底;注入永不落盘。

---

## 11. 观测(事件流三层 + L3 旁路)

core **不内置指标/审计**,只供原材料:

| 原材料 | 形态 |
|--------|------|
| L1 热路径 | text_delta/reasoning_delta(宿主内直传) |
| L2 结构化摘要 | StepSummary / AfterResponse |
| L3 观测通知 | tool_use/tool_result/step_end 原始事件(异步批处理,经 event 消息投递 observe 扩展) |
| 外壳直通 | step_start/done/error |
| `snapshot.started_at` / `snapshot.round` | 时间 / 轮次 |
| `on_error` | 失败通知 |

观测枝干 = **声明 `observe` 能力**的只读枝干:钩子返回 action 一律忽略 + 记录;L3 订阅自动生效(observe capability 即订阅)。

---

## 12. 形态扩展(E6)

统一接口:`run_stream(snapshot, messages, system, cancel_event, session_id) -> 事件流`(必以 done/error 收尾)。

```python
from teage_liu2.core.modes import MODE_BARE, MODE_LOOP   # 现有形态
# 新形态 = 新类(实现 run_stream)+ 在 pipeline 形态字典一行注册
```

`ChatPipeline` 构造参数:`llm_client / history_store / hooks / mode / max_loops / base_system_prompt / injection_budget / history_window_messages / storage_writer / event_stream`;入口 `chat_stream(session_id, user_input, system=None, cancel_event=None)`。
