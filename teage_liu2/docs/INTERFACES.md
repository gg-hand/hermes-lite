# teage_liu2 接口示例文档(Interfaces)

> **定位**:与 [CORE.md](./CORE.md) 配套的**可运行示例**。展示一个完整枝干从实现到注册的每一步,以及 core 各接口的典型用法。示例可直接跑(`python 或 pytest`),也可作为新模块的模板。
>
> 示例中 `TODO(你)` 处为需要替换的实际逻辑。

---

## 1. 完整枝干示例:天气查询枝干(覆盖 5 个钩子)

```python
# branches/weather.py
from __future__ import annotations

import logging
from typing import Any

from teage_liu2.core.hooks import Branch, BranchContext
from teage_liu2.core.injection import (
    Injection,
    L_STABLE_SYSTEM,
    L_PREFIX,
)

logger = logging.getLogger(__name__)


class WeatherBranch(Branch):
    """天气查询枝干:注入工具说明 + 声明工具 + 执行工具 + 审计。"""

    name = "weather"

    async def setup(self, config: dict, core: Any) -> None:
        """setup 只做资源准备与配置自校验;配置开但坏 → 抛错 = 启动失败。"""
        self.api_key = config.get("api_key", "")
        self.audit = core.storage_provider          # 持久态通道
        if not self.api_key:
            raise ValueError("weather.api_key 未配置(配置开了却坏了必须暴露)")

    async def build_injections(self, ctx: BranchContext) -> list[Injection]:
        """声明注入:工具说明放稳定区(参与前缀缓存)。"""
        return [
            Injection(L_STABLE_SYSTEM, "你可以调用天气查询工具获取实时天气。", priority=10),
        ]

    async def before(self, ctx: BranchContext) -> None:
        """填充工具 schema(ctx.tools);LLM 据此决定是否 tool_use。"""
        ctx.tools = [{
            "name": "get_weather",
            "description": "查询城市实时天气",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }]

    async def on_tool_call(self, ctx: BranchContext, tool_name: str, tool_input: dict) -> Any:
        """执行工具;未实现的工具返回 NotImplemented(主干跳过)。"""
        if tool_name != "get_weather":
            return NotImplemented
        # TODO(你):实际天气 API 调用
        return f"{tool_input.get('city', '?')} 晴,25°C"

    async def after(self, ctx: BranchContext, response: Any) -> None:
        """正常完成后审计(持久态经 StorageProvider 落盘)。"""
        self.audit.write("weather.audit", {
            "session_id": ctx.session_id,
            "round": ctx.round,
            "response": response.text[:100],
        })
```

**要点**:
- `setup` 无 try/except——配置开但坏 → 启动失败;
- `build_injections` 按稳定度选层(稳定内容 → STABLE_SYSTEM,动态 → PREFIX/BEFORE_INPUT);
- `on_tool_call` 只处理自己的工具,其余返回 `NotImplemented`;
- `after` 用 `core.storage_provider` 落盘(持久态三态模型)。

---

## 2. 注册与配置(装配点)

```python
# server/app.py 内(唯一改动点)
from teage_liu2.branches.weather import WeatherBranch

registry.register_factory("weather", lambda cfg: WeatherBranch(cfg))
```

```yaml
# config.yaml
core:
  branches:
    weather:
      enabled: true
      api_key: ${WEATHER_API_KEY}   # 敏感值走 .env 占位符
```

**铁律**:添加/移除枝干只需改装配点 + 配置,**core/ 零改动**(M2 guardrails 已实测)。

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
class SensitiveGuard(Branch):
    name = "sensitive_guard"

    async def before(self, ctx: BranchContext) -> None:
        for word in self.denylist:
            if word in ctx.user_input:
                ctx.stop = True                      # 置 stop → 主干跳过 LLM 直接 done
                ctx.extra["sensitive_guard.denied"] = word
                return
```

- 拦截后事件流:单个 `done`(`termination_reason="intercepted"`),无 step 事件;
- 完整实现见现有枝干 `branches/guardrails.py`。

---

## 5. 轮次间注入示例(inject_round,I2)

```python
class RoundMemoBranch(Branch):
    """每轮注入当前工具执行进度(记忆枝干雏形)。"""

    name = "round_memo"

    async def inject_round(self, ctx: BranchContext) -> str | None:
        if ctx.round == 1:
            return "进度:任务刚开始"
        return f"进度:第 {ctx.round} 轮,已完成步骤见上文"
```

- `inject_round` 在 loop 每轮 step 前调用,返回文本注入当前输入/工具结果前(自动合并相邻 user 消息,保持交替);
- 返回 `None` = 本轮不注入。

---

## 6. 状态存储示例(三态模型)

```python
class MemoryBranch(Branch):
    """会话态 + 持久态演示。"""

    async def setup(self, config, core):
        self.session_store = core.session_store      # 会话态(内存,重启即失)
        self.storage = core.storage_provider         # 持久态(重启保留)

    async def before(self, ctx):
        # 会话态:同会话跨轮计数
        state = self.session_store.get(ctx.session_id)
        state.setdefault("turns", 0)
        state["turns"] += 1
        ctx.extra["memory.turns"] = state["turns"]   # 对话态:本轮临时

    async def teardown(self):
        # 持久态经 StorageProvider(重启 setup 时自恢复);core 不替你冲刷
        ...
```

| 态 | 放哪 | 重启后 |
|----|------|--------|
| 对话态 | `ctx.extra["{branch}.{key}"]` | 消失 |
| 会话态 | `core.session_store.get(session_id)` | 消失(内存) |
| 持久态 | `core.storage_provider` 或自持 | 保留 |

---

## 7. 后台任务示例(TaskRegistry)

```python
class PollBranch(Branch):
    async def setup(self, config, core):
        self.tasks = core.task_registry

        async def poll():
            while True:
                await asyncio.sleep(30)
                # TODO(你):轮询逻辑(注意取消时清理)

        self.tasks.create_task(poll())   # 注册后台任务

    async def teardown(self):
        pass  # 冲刷由 shutdown 编排:①TaskRegistry.cancel_all 统一取消
```

- 枝干后台任务一律经 `core.task_registry.create_task`,关闭时统一取消,不泄漏。

---

## 8. 钩子内调 LLM(防递归约定)

```python
class ConsolidationBranch(Branch):
    """after 里做记忆巩固 —— 必须直调 core.llm_client,禁止调 pipeline。"""

    async def setup(self, config, core):
        self.llm = core.llm_client          # ✅ 正确来源

    async def after(self, ctx, response):
        summary = await self.llm.chat_main(  # 直调 backend,不重入钩子链
            messages=[{"role": "user", "content": f"总结:{response.text}"}],
        )
        ...
```

- **禁止** `core.pipeline.chat_stream(...)`(重入钩子链 → 递归)。

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
# registry.register_factory("weather", lambda c: WeatherBranch(c))  # 注册枝干
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

# 新增枝干自己的测试(推荐:放 tests_branches/test_<name>.py)
# 至少覆盖:enabled:false 照常 / setup 失败启动失败 / 钩子被正确调用
```

新枝干**不得**改动 `core/` 任何文件——这是接入是否"合格"的唯一硬标准。
