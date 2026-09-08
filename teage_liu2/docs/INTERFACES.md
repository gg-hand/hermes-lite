# teage_liu2 接口示例文档(Interfaces)

> **定位**:与 [CORE.md](./CORE.md) 配套的**可运行示例**。展示一个完整枝干从实现到注册的每一步,以及 core 各接口的典型用法。
>
> **契约源**:`teage_liu2/PROTOCOL/`(协议 v1.0.0)为唯一契约源;本文档与 `core/` 代码同步(发现不一致时以代码为准)。示例签名基于 **Snapshot + Action 交互模型**(阶段 2 落地),**不再使用 BranchContext 可变对象**。
>
> 示例中 `TODO(你)` 处为需要替换的实际逻辑。

---

## 0. 新签名速查(Snapshot + Action)

### Branch 11 钩子(`core/hooks.py`)

```python
class Branch(ABC):
    name: str = "branch"                 # extension_name,^[a-z0-9_]+$(禁点)
    capabilities: List[str] = []         # observe / tool_executor / llm / self_hosted_storage
    host_port: Any = None                # 进程内能力端口(装配自动注入,见下)

    async def setup(self, config: dict, host: Any) -> None        # host = 宿主能力声明(纯数据)
    async def teardown(self) -> None                              # 幂等可重入
    async def build_injections(self, snapshot: Snapshot) -> List[Injection]
    async def inject_round(self, snapshot: Snapshot) -> Optional[Injection]
    async def before(self, snapshot: Snapshot) -> List[Action]
    async def pre_tool_call(self, snapshot, name, input) -> ToolDecision
    async def on_tool_call(self, snapshot, name, input) -> Any    # 未实现返回 NotImplemented
    async def post_tool_call(self, snapshot, name, input, result, duration) -> List[Action]
    async def after_step(self, snapshot, summary: StepSummary) -> List[Action]
    async def after(self, snapshot: Snapshot, response: AfterResponse) -> List[Action]
    async def on_error(self, snapshot: Snapshot, error: Any) -> List[Action]
    async def on_l3_events(self, events: List[dict]) -> None    # L3 观测通知(observe 同语言扩展)
```

**交互模型**:Snapshot **不可变只读**;变更一律经返回 Action[](core 立即应用 + 原子批次)。终态钩子(`after`/`on_error`)返回的 action 一律忽略 + 记录(HOOK_TERMINAL_ACTION_IGNORED),数据写入走 `host_port.storage_write`。

### Action 6 种(`core/actions.py`)

| Action | 用途 |
|--------|------|
| `AppendMessage(message=...)` | 追加一条消息(**仅 role=user**;叠加) |
| `SetTools(tools=[...])` | 整体覆盖工具 schema 列表(后注册覆盖先注册) |
| `SetExtra(key="branch.name", value=...)` | 写入枝干间共享数据(**key 白名单 `^[a-z0-9_]+\.[a-z0-9_.]+$`**) |
| `SetStop(reason="...")` | 拦截整个对话(短路后续同名钩子,跳过 LLM → done(intercepted)) |
| `SetSystem(text="...")` | 覆盖基础 system |
| `ModifyToolSchema(name=..., description=..., input_schema=...)` | 按工具名定向修改单个工具 schema |

### host_port 接口(`core/transport.py` InProcessHostPort,装配自动注入)

同语言扩展访问宿主能力的**唯一通道** = host_port 消息(storage_*/invoke_llm/task_*),**不得以对象引用访问宿主存储**。kind 必须带 `{extension_name}.` 前缀(跨前缀拒绝)。

```python
await self.host_port.storage_write(kind, docs)              # kind 必须带 "branch_name." 前缀
await self.host_port.storage_read(kind, doc_id)
await self.host_port.storage_query(kind, limit=None, **filters)
await self.host_port.storage_delete(kind, doc_id)
await self.host_port.invoke_llm(role="main", messages=[...], system=None, max_tokens=None)
self.host_port.register_task(task_id, description="")       # 宿主登记扩展侧任务(可观测)
self.host_port.cancel_task(task_id)
```

`host_port` 在装配时由 registry 注入(可能为 None 当未配置通道时),使用前判空。

---

## 1. 完整枝干示例:天气查询枝干(覆盖 5 个钩子)

```python
# data2/extensions/weather/main.py(统一扩展目录树,安装规范见 §2/SPI §13.2)
from __future__ import annotations

import logging
from typing import Any, Dict, List

from teage_liu2.core.actions import Action, SetExtra, SetTools
from teage_liu2.core.hooks import Branch
from teage_liu2.core.injection import Injection, L_STABLE_SYSTEM
from teage_liu2.core.types import Snapshot

logger = logging.getLogger(__name__)


class WeatherBranch(Branch):
    """天气查询枝干:注入工具说明 + 声明工具 + 执行工具 + 审计。"""

    name = "weather"
    capabilities: List[str] = ["tool_executor"]   # 声明工具执行能力(§12)

    async def setup(self, config: dict, host: Any) -> None:
        """setup 只做资源准备与配置自校验;配置开但坏 → 抛错 = 启动失败。
        host = 宿主能力声明(纯数据,含 storage 通道与 kind 前缀)。
        """
        self.api_key = config.get("api_key", "")
        self.kind_prefix = host["storage"]["kind_prefix"]   # = "weather"
        if not self.api_key:
            raise ValueError("weather.api_key 未配置(配置开了却坏了必须暴露)")

    async def build_injections(self, snapshot: Snapshot) -> List[Injection]:
        """声明注入:工具说明放稳定区(参与前缀缓存)。"""
        return [
            Injection(L_STABLE_SYSTEM, "你可以调用天气查询工具获取实时天气。", priority=10),
        ]

    async def before(self, snapshot: Snapshot) -> List[Action]:
        """填充工具 schema(SetTools);LLM 据此决定是否 tool_use。"""
        return [SetTools(tools=[{
            "name": "get_weather",
            "description": "查询城市实时天气",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }])]

    async def on_tool_call(self, snapshot: Snapshot, name: str, input: dict) -> Any:
        """执行工具;未实现的工具返回 NotImplemented(主干跳过)。"""
        if name != "get_weather":
            return NotImplemented
        # TODO(你):实际天气 API 调用
        return f"{input.get('city', '?')} 晴,25°C"

    async def after(self, snapshot: Snapshot, response: Any) -> List[Action]:
        """正常完成后审计(终态钩子:返回 action 一律忽略,落盘走 host_port 消息)。"""
        if self.host_port is not None:
            await self.host_port.storage_write("weather.audit", [{
                "session_id": snapshot.session_id,
                "round": snapshot.round,
                "text": response.text[:100],
            }])
        return []
```

**要点**:
- `setup` 无 try/except——配置开但坏 → 启动失败;`host` 是**纯数据声明**(非对象引用);
- `build_injections` 按稳定度选层(稳定内容 → STABLE_SYSTEM,动态 → PREFIX/BEFORE_INPUT);
- `on_tool_call` 只处理自己的工具,其余返回 `NotImplemented`;
- 变更快照一律经 Action(此处 `SetTools`),绝不直接改 `snapshot`;
- `after` 是终态钩子,数据写入走 `host_port.storage_write`(kind 带 `weather.` 前缀),不返回 action。

---

## 2. 安装与配置(统一扩展目录树,2026-09-08)

**安装** = 在 `extensions_root`(默认 `data2/extensions/`)下建目录(规范全文见 SPI §13.2):

```text
data2/extensions/weather/
├── manifest.yaml      # name: weather / version / language: python / entry: main.py / capabilities: [tool_executor]
└── main.py            # 代码(§1 示例)+ 文末导出 create_branch(config) -> Branch
```

**启用与运行配置**(config.yaml;安装 ≠ 激活):

```yaml
core:
  branches:
    weather:
      enabled: true
      api_key: ${WEATHER_API_KEY}   # 敏感值走 .env 占位符
```

**铁律**:添加/移除枝干只需建/删扩展目录 + 改 config,**core/ 与 server/ 零改动**。装载由 `registry.set_directory_loader`(extensions_root 目录发现)完成;`register_factory` = 测试/行为套件/嵌入注入通道(设计 §2.1),非生产装载方式。注册后 `host_port` 由 registry 自动注入(`registry.set_host_port_factory` 已在 app.py 装配)。

---

## 3. 关闭与启用(可拔插)

```yaml
core:
  branches:
    weather:
      enabled: false     # 不注册 → 主干零感知,对话照常(可拔插)
```

- 未声明 或 `enabled: false` → 枝干**不存在**(非"存在但坏");
- 关闭状态下 LLM 请求 `get_weather` → 无执行者 → 友好终止(no_tool_executor)。

---

## 4. 拦截示例(护栏模式)

```python
from teage_liu2.core.actions import Action, SetExtra, SetStop
from teage_liu2.core.hooks import Branch
from teage_liu2.core.types import Snapshot


class SensitiveGuard(Branch):
    name = "sensitive_guard"

    async def before(self, snapshot: Snapshot) -> List[Action]:
        for word in self.denylist:
            if word in snapshot.user_input:
                # SetStop → 短路后续 before → 跳过收口与 LLM → done(intercepted)
                return [SetExtra(key="sensitive_guard.denied", value=word),
                        SetStop(reason="blocked")]
        return []
```

- 拦截后事件流:单个 `done`(`termination_reason="intercepted"`),无 step 事件;
- 完整实现见现有枝干 `branches/guardrails.py`(block/warn 双模式)。

---

## 5. 轮次间注入示例(inject_round,I2)

```python
from teage_liu2.core.injection import Injection, L_BEFORE_INPUT
from teage_liu2.core.hooks import Branch
from teage_liu2.core.types import Snapshot
from typing import Optional


class RoundMemoBranch(Branch):
    """每轮注入当前工具执行进度(记忆枝干雏形)。"""

    name = "round_memo"

    async def inject_round(self, snapshot: Snapshot) -> Optional[Injection]:
        if snapshot.round == 1:
            return Injection(L_BEFORE_INPUT, "进度:任务刚开始")
        return Injection(L_BEFORE_INPUT, f"进度:第 {snapshot.round} 轮,已完成步骤见上文")
```

- `inject_round` 在 loop 每轮 step 前调用,**layer 强制 BEFORE_INPUT**;返回 `None` = 本轮不注入;
- 全收集合并(注册序拼接,同 key 去重),自动合并相邻 user 消息,保持交替。

---

## 6. 状态存储示例(三态模型)

```python
from teage_liu2.core.actions import Action, SetExtra
from teage_liu2.core.hooks import Branch
from teage_liu2.core.types import Snapshot


class MemoryBranch(Branch):
    """会话态 + 持久态演示。"""

    name = "memory"

    async def before(self, snapshot: Snapshot) -> List[Action]:
        # 会话态:经 snapshot.extra 会话内延续(core 构建快照时已从
        # SessionStore 恢复同 session 的 extra 基座,结束自动写回)。
        # 不经 SessionStore 直访(§5 L-10 唯一通道 = extra)。
        turns = snapshot.extra.get("memory.turns", 0) + 1
        return [SetExtra(key="memory.turns", value=turns)]

    async def after(self, snapshot: Snapshot, response: Any) -> List[Action]:
        # 持久态:经 host_port.storage_write(kind 带 "memory." 前缀);
        # 重启后 setup 时自恢复。
        if self.host_port is not None:
            await self.host_port.storage_write("memory.facts", [{
                "session_id": snapshot.session_id,
                "text": response.text[:200],
            }])
        return []

    async def teardown(self) -> None:
        # 持久态经 StorageProvider(重启 setup 时自恢复);core 不替你冲刷
        ...
```

| 态 | 放哪 | 重启后 |
|----|------|--------|
| 对话态 | `snapshot.extra["{branch}.{key}"]`(SetExtra 写入) | 消失 |
| 会话态 | `snapshot.extra` 会话内延续(构建恢复 / 结束写回) | 消失(契约) |
| 持久态 | `host_port.storage_write`(宿主存储)或自持 | 保留 |

---

## 7. 后台任务示例

```python
import asyncio

from teage_liu2.core.hooks import Branch


class PollBranch(Branch):
    async def setup(self, config, host) -> None:
        self._task = asyncio.create_task(self._poll())   # 自身进程内协程
        if self.host_port is not None:
            # 宿主登记(可观测/协调取消);任务归属扩展进程,宿主不承载执行(T-4)
            self.host_port.register_task("poll", description="poll loop")

    async def _poll(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                # TODO(你):轮询逻辑(注意取消时清理)
        except asyncio.CancelledError:
            pass  # teardown 取消

    async def teardown(self) -> None:
        self._task.cancel()   # 扩展自取消;宿主 shutdown cancel_all 兜底
```

- 同语言扩展后台任务直接 `asyncio.create_task`(宿主只经 `host_port.register_task` 登记);
- `teardown` 自取消;宿主 shutdown 与热重载重建时 `cancel_all` 兜底,不泄漏。

---

## 8. 钩子内调 LLM(invoke_llm 消息,防递归约定)

```python
from teage_liu2.core.hooks import Branch
from teage_liu2.core.types import Snapshot


class ConsolidationBranch(Branch):
    """after 里做记忆巩固 —— 必须经 host_port.invoke_llm,禁止调 pipeline。"""

    name = "consolidation"
    capabilities = ["llm"]          # 声明 llm 能力(未声明 → 宿主拒绝,capability_not_declared)

    async def after(self, snapshot: Snapshot, response: Any) -> list:
        if self.host_port is not None:
            result = await self.host_port.invoke_llm(
                role="main",   # 多角色路由:main / consolidation(LLMClient.chat_role)
                messages=[{"role": "user", "content": f"总结:{response.text}"}],
            )
            # result = {"content_blocks": [...], "stop_reason": ..., "usage": ...}
            # TODO(你):提取事实后经 host_port.storage_write 落盘
        return []
```

- **禁止** `core.pipeline.chat_stream(...)`(重入钩子链 → 递归,§15-A5);
- `invoke_llm` 走宿主 `LLMClient.chat_role` 直调(协议级防重入)+ 并发信号量硬边界(§15-A6);
- **冷路径通道**:用于 after / on_error / setup / 后台任务;**禁止用于 before / inject_round**(直接加首 token 延迟,违反 §18.6 预算);
- 失败 → 抛异常(`{code}: {message}`),扩展自行降级(记忆巩固失败仅告警)。

---

## 9. 最小调用方示例(外壳/CLI 用法)

```python
from teage_liu2.core.config import core_config_from, load_config
from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import HookChain
from teage_liu2.core.llm import LLMClient
from teage_liu2.core.pipeline import ChatPipeline
from teage_liu2.core.registry import BranchRegistry

cfg = load_config("config.yaml")
core_config = core_config_from(cfg)            # 严格校验,失败 = 启动失败

registry = BranchRegistry()
# registry.set_directory_loader(make_directory_loader(specs))  # 生产装载 = 目录发现
# registry.register_factory(...)  # 仅测试/嵌入注入通道(设计 §2.1)
hooks = registry.build(cfg)                    # 配置驱动装配

llm = LLMClient(config=cfg)
store = SQLiteHistoryStore("data2/sessions.db")
pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks,
                        mode=core_config.mode, max_loops=core_config.max_loops)

# 流式消费(事件流是基建)
async for ev in pipeline.chat_stream("session-1", "你好"):
    if ev["type"] == "text_delta":
        print(ev["text"], end="")
    elif ev["type"] == "done":
        print(f"\n[完成:{ev['termination_reason']}]")
```

---

## 10. 事件流手动驱动(测试/调试用)

```python
import asyncio

async def drain(agen):
    return [ev async for ev in agen]

events = asyncio.run(drain(pipeline.chat_stream("s", "查天气")))
done = events[-1]
assert done["type"] == "done"
assert done["termination_reason"] == "normal"
```

事件流**必以 done 或 error 收尾**;`done` 9 键齐整(见 CORE.md §5)。

---

## 11. 验证清单(新枝干接入后必跑)

```bash
# 全部契约测试(含枝干测试)
pytest tests_core/ tests_branches/

# 行为套件(协议黄金用例,17/17)
python PROTOCOL/behavior-suite/runner.py

# 新增枝干自己的测试(推荐:放 tests_branches/test_<name>.py)
# 至少覆盖:enabled:false 照常 / setup 失败启动失败 / 钩子被正确调用
```

新枝干**不得**改动 `core/` 任何文件——这是接入是否"合格"的唯一硬标准。
