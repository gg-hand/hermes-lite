# 子系统接入规范(Subsystem SPI)

> **文档性质**:本文档描述**目标形态**的子系统接入契约——任一子系统(记忆 / 工具 / 护栏 / 调度 / 协作……)按本规范定义的接口实现后,即可接入主干并正常工作,主干代码零改动。
>
> ⚠️ **当前状态**:现有代码**尚未实现**本契约,采用的是"判空 + 异常降级"的退化接入(每个组件独立 try/except 降级为 None,管线中 `if orch.xxx is not None` 跳过)。从现状迁移到本契约的路径见 [§8 迁移路径](#8-迁移路径),与本仓库 `docs/ISSUES-2026-08-11-架构问题清单.md` 的 ISSUE-01/ISSUE-04 联动。
>
> **配套阅读**:[ARCHITECTURE.md](ARCHITECTURE.md)(现状总览)、[ISSUES-2026-08-11-架构问题清单.md](ISSUES-2026-08-11-架构问题清单.md)(问题依据)。

---

## 1. 概念模型

### 1.1 主干(Core)

主干 = **不可再减的对话动作** + **扩展机制**:

```
输入 → [钩子链 before] → LLM 调用 → [钩子链 after] → 输出
```

- **不可减部分**:一次 LLM 调用(`llm.chat(messages)`)+ 钩子链(扩展的本质)+ 会话历史落盘(对话的身份);
- **全可裁部分**:工具、记忆、护栏、意图分类、审计、指标、调度、协作——每一个都是挂在钩子链上的**枝干**。

### 1.2 枝干(Branch)

枝干 = 实现 `Branch` 接口的一个类 = 一个可插拔的子系统。枝干之间**互不可见**,只能通过 `BranchContext.extra` 交换数据,通过 `BranchContext` 与主干交互。

### 1.3 钩子点

主干在固定时机调用枝干的钩子方法(只调用枝干实现了的部分):

| 时机 | 钩子 | 典型用途 |
|------|------|----------|
| 启动 | `setup` | 加载依赖、预热资源(向量库 / MCP / 模型) |
| LLM 前 | `build_system` | 注入稳定前缀(用户画像) |
| LLM 前 | `build_injection` | 注入 messages[0] 动态区(检索记忆 / 环境 / 任务) |
| LLM 前 | `before` | 输入扫描 / 意图路由 / 工具 schema 填充 / 拦截 |
| 循环中 | `on_tool_call` | 执行工具(仅工具枝干实现) |
| LLM 后 | `after` | 输出过滤 / 审计 / 记忆巩固 / 指标 |
| 关闭 | `teardown` | 释放资源 |

---

## 2. 接口定义(正式版)

```python
# teage_liu/core/branch.py —— 目标文件(当前不存在)
"""枝干契约:子系统接入主干的唯一入口。"""
from __future__ import annotations

from abc import ABC
from typing import Any, Optional


class BranchContext:
    """主干与枝干间交换的数据包:一次对话的完整状态。

    - 主干**负责初始化**:session_id / user_input / history / system_text;
    - 枝干**只能写入**:messages 追加、tools 填充、extra、stop;
    - 枝干间共享数据一律走 extra,禁止直接引用其他枝干实例。
    """

    def __init__(self, session_id: str, user_input: str):
        self.session_id = session_id
        self.user_input = user_input
        self.history: list[dict] = []        # 主干就绪的会话历史(只读建议)
        self.system_text: str = ""           # 主干基础 system(build_system 可追加)
        self.messages: list[dict] = []       # 送往 LLM 的消息
        self.tools: list[dict] = []          # 工具 schema 列表(工具枝干填充)
        self.extra: dict[str, Any] = {}      # 枝干间共享的自定义数据
        self.stop: bool = False              # 枝干置 True → 主干跳过 LLM 直接返回


class Branch(ABC):
    """枝干基类:实现需要的钩子,其余继承默认空实现。"""

    #: 唯一标识,用于配置 / 日志 / 审计
    name: str = "branch"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def setup(self, config: dict, core: Any) -> None:
        """启动时初始化:加载依赖、预热资源。

        契约:配置开启但初始化失败 → **抛出异常使启动失败**(不再静默吞错);
        配置关闭 → 该枝干根本不注册,不调用本方法。
        """
        ...

    async def teardown(self) -> None:
        """关闭时释放资源(连接 / 锁 / 后台任务)。"""
        ...

    # ------------------------------------------------------------------
    # LLM 前的注入(可选)
    # ------------------------------------------------------------------
    async def build_system(self, ctx: BranchContext) -> None:
        """向 ctx.system_text 追加稳定前缀内容(如用户画像主体)。

        只放**稳定不变**的内容——该区域参与 LLM 前缀缓存,
        动态内容一律走 build_injection。
        """
        ...

    async def build_injection(self, ctx: BranchContext) -> str:
        """返回注入 messages[0] 的文本(检索记忆 / 环境 / 任务进度)。

        返回空串 = 不注入。主干负责汇总并按预算裁剪(复用现有
        ContextManager 的 8000 字符预算,见 memory/context_manager.py)。
        """
        return ""

    # ------------------------------------------------------------------
    # LLM 前后的钩子(可选)
    # ------------------------------------------------------------------
    async def before(self, ctx: BranchContext) -> None:
        """LLM 调用前:输入扫描 / 意图路由 / 工具 schema 填充。

        置 ctx.stop=True 可拦截本次对话(如护栏 deny)。
        """
        ...

    async def on_tool_call(self, ctx: BranchContext, tool_name: str,
                           tool_input: dict) -> Any:
        """执行工具(仅工具类枝干实现)。

        返回结果(字符串 / 结构)由主干包装为 tool_result 回喂 LLM。
        未实现的工具返回 NotImplemented 由主干跳过。
        """
        return NotImplemented

    async def after(self, ctx: BranchContext, response: Any) -> None:
        """LLM 返回后:输出过滤 / 审计 / 记忆巩固 / 指标。"""
        ...


# ----------------------------------------------------------------------
# 主干(目标形态,示意)
# ----------------------------------------------------------------------
class Core:
    """主干:固定管线 + 钩子链。枝干注册后,此处代码不再改动。"""

    def __init__(self):
        self.branches: list[Branch] = []

    def register(self, branch: Branch) -> None:
        """接入一个枝干(注册顺序 = before 调用顺序)。"""
        self.branches.append(branch)

    async def chat(self, session_id: str, user_input: str) -> str:
        ctx = BranchContext(session_id, user_input)
        # 主干装配基础上下文(历史 / 基础 system)
        ...
        # 1. LLM 前注入
        for b in self.branches:
            await b.build_system(ctx)
        injections = [t for t in [await b.build_injection(ctx) for b in self.branches] if t]
        # 2. before 钩子
        for b in self.branches:
            await b.before(ctx)
        if ctx.stop:
            return "对话已被枝干拦截"
        # 3. LLM 调用(有工具 schema 则进 React 循环,否则单轮)
        response = await self._llm_call(ctx)   # 内部:无 tools → 单轮
        # 4. after 钩子
        for b in reversed(self.branches):
            await b.after(ctx, response)
        return response.text
```

---

## 3. 顺序与生命周期契约

| 规则 | 内容 |
|------|------|
| **注册顺序 = 调用顺序** | `before` / `build_*` 按注册序正序;`after` / `teardown` **逆序**(洋葱模型,后注册的先收尾) |
| **枝干间隔离** | 禁止互相引用实例,只通过 `ctx.extra` 通信;违反者在 code review 拦截 |
| **只读 vs 写入** | `history` / `system_text` 由主干初始化,枝干只读;`messages` / `tools` / `extra` 枝干可写 |
| **钩子超时** | 主干以 `asyncio.wait_for` 包裹每个钩子(默认 5s,可配置),超时跳过该枝干并记日志——**枝干不得卡死主干** |
| **异常语义** | `setup` 失败 → 启动失败(配置开了却坏了,必须暴露);`before/after/build_*` 运行时异常 → 跳过该枝干 + `logger.error`(单枝干故障不影响对话) |
| **优雅关闭** | 主干停止时按逆序调 `teardown`,每个枝干独立 try/except |

---

## 4. 与现状代码的映射(迁移目标)

每个现有子系统迁到钩子后的形态:

| 现有子系统 | 现状(判空退化) | 迁移后(Branch) |
|-----------|----------------|----------------|
| Guardrails | `if orch.guardrail_engine is not None`(`orchestrator/chat_handler.py:164`);None→noop(`agent/react_loop.py:154-162`) | `before`(输入扫描)+ `after`(输出过滤);noop 形态消失,由"不注册"替代 |
| 记忆检索 | `if orch.memory_retriever is not None`(`orchestrator/enhanced_context.py:66`) | `build_injection` 返回检索文本 |
| 用户画像 | `context_manager.get_cache_stable_prefix()`(`enhanced_context.py:58`) | `build_system` 追加稳定前缀 |
| 记忆巩固 | `if orch.consolidation_engine is not None`(`chat_handler.py:358/1001`) | `after` 达阈值触发巩固 |
| 工具系统 | `tool_registry=None → 纯对话`(`agent/react_loop.py:92`) | `before` 填 `ctx.tools` + `on_tool_call` 执行;ReactLoop 收敛为"主干内置的循环策略",无工具时自动单轮 |
| 意图分类 | `if classify_intent is not None and orch.llm_client is not None`(`chat_handler.py:200`) | `before` 路由,结果写 `ctx.extra["intent"]` |
| 审计 | 分散调用 | `after` 统一记录 |
| 指标 | 分散调用 | `after` 统一上报 |
| Cron / Skill / MCP / Files | 条件注册 + 启动预热(`lifespan.py` 各 `_register_*`) | 各自 `Branch.setup` 承载预热,`build_*`/`before` 承载注入 |
| Multiagent / A2A | 条件注册(角色、开关) | 各自 `Branch`,`setup` 承载 adapter 启动,`after` 承载协作消息处理 |

**迁移的判别标准**:现状"组件可 None"由**初始化失败**决定(故障驱动,不可预测);目标"枝干不存在"由**配置关闭**决定(声明驱动,可测试)。`ISSUES-2026-08-11` ISSUE-01 收敛裸 `except` 是迁移的前置条件。

---

## 5. 接入三步法(给新子系统作者)

1. **实现**:继承 `Branch`,只实现需要的钩子(其余继承空实现),`name` 取唯一标识;
2. **注册**:一行 `core.register(MyBranch())`(配置系统按 `enabled` 过滤,关闭 = 不注册);
3. **验证**:跑 `pytest tests/core/` 的"枝干契约测试"——框架保证"枝干顺序正确、隔离有效、超时生效"。

新枝干**不得**改动 `Core.chat` / 钩子链 / 其他枝干——这是接入是否"合格"的唯一硬标准。

---

## 6. 最小完整示例:时间与环境信息枝干

展示三个钩子的完整用法(注入 + before + after),从零接入:

```python
# branches/environment_branch.py
from teage_liu.core.branch import Branch, BranchContext


class EnvironmentBranch(Branch):
    """注入当前时间/日期到 system,并在对话结束后记录一条审计。"""

    name = "environment"

    async def setup(self, config: dict, core) -> None:
        self.show_time = config.get("show_time", True)
        self.log_file = config.get("log_file")          # None 则不落盘
        # setup 只做资源准备,不做 try/except——配置开了就失败让启动暴露

    async def build_system(self, ctx: BranchContext) -> None:
        if self.show_time:
            import datetime
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            ctx.system_text += f"\n\n当前时间:{now}"   # 稳定前缀,参与缓存

    async def before(self, ctx: BranchContext) -> None:
        # 示例:凌晨时段提示,不拦截,仅记录到 extra 供其他枝干读取
        ctx.extra["environment.hour"] = datetime.datetime.now().hour

    async def after(self, ctx: BranchContext, response) -> None:
        if self.log_file:
            line = f"{ctx.session_id} | {ctx.user_input[:50]} | {response.text[:50]}\n"
            # 落盘(此处用同步写,生产应 aiofiles 或委托后台)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)


# ---- 接入(应用装配处,唯一改动点)----
core.register(EnvironmentBranch())
```

**要点**:
- `setup` 无 try/except —— 配置开但坏 → 启动失败,符合契约;
- `build_system` 只放稳定内容 —— 不污染前缀缓存;
- 枝干只依赖 `ctx` 与自己的配置 —— 与主干、其他枝干零耦合。

---

## 7. 工具枝干的接入(特殊形态)

工具系统是唯一需要"循环"的枝干,契约设计使其与其他枝干同构:

```python
class ToolBranch(Branch):
    name = "tools"

    async def setup(self, config, core) -> None:
        # 加载工具注册表(现有 tool_registry 原样复用)
        self.registry = build_registry(config)

    async def before(self, ctx: BranchContext) -> None:
        # 1. 填充 schema:Core Tier 全量注入,Deferred Tier 只放 stub
        ctx.tools = self.registry.build_schemas()

    async def on_tool_call(self, ctx, tool_name: str, tool_input: dict) -> Any:
        # 2. 执行:复用现有 PolicyEngine / ToolExecutor / GuardrailEngine
        return await self.registry.execute(tool_name, tool_input)

    async def after(self, ctx, response) -> None:
        # 3. 收尾:审计 / 卡死检测状态清理
        ...
```

主干 `_llm_call` 的循环策略:
- `ctx.tools` 为空 → **单轮纯对话**(现状 `react_loop.py:92` 的退化路径,行为不变);
- `ctx.tools` 非空 → 进入 React 循环:LLM 返回 `tool_use` → 遍历枝干调用 `on_tool_call`(首个非 NotImplemented 者负责)→ 回喂 `tool_result` → 再调 LLM,直到 `end_turn` / `max_loops` / `ctx.stop`。

这样"工具"只是多实现一个钩子点的枝干,ReactLoop 收敛为主干内部的循环策略,**不再是独立组件**。

---

## 8. 迁移路径(分阶段,与 ISSUE 清单联动)

> 原则:每一步都可独立合入、测试、回滚;兼容期保留现有 `chat()` 入口,新管线并行跑影子验证。

| 阶段 | 内容 | 前置 |
|------|------|------|
| **P0** | 收敛裸 `except`(ISSUE-01):工厂改为"配置关闭→不构造 / 配置开但坏→抛错+日志";HealthChecker 聚合组件缺失报告 | 无 |
| **P1** | 抽出 `Core` + 钩子链(`BranchContext` / `Branch` / 超时包裹 / 逆序 after),以**新文件新入口**实现,不拆现状 | P0 |
| **P2** | 逐枝干迁移,顺序按风险从低到高:guardrail(有 noop 先例,最简)→ environment/意图 → 记忆(检索/画像/巩固)→ 工具(重头,含 React 循环内化)→ 审计/指标 → cron/skill/mcp → multiagent/a2a | P1 |
| **P3** | 每迁完一个枝干:删除对应旧判空路径 + 补"该枝干关闭时对话照常"的测试(契约测试);全部迁完删除旧 `chat()` 入口 | P2 |
| **P4** | 配置格式定稿(见附录)+ 文档同步(ARCHITECTURE.md 增加"枝干注册表"章节) | P3 |

**验证基线**:现有 266 个测试文件全部保留,迁移不改变任何已固化的行为(纯对话、cron、协作、流式);新增"枝干契约测试"覆盖:顺序 / 隔离 / 超时 / 单枝干故障不影响对话 / 零枝干时主干可用。

---

## 9. 附录:配置格式草案

```yaml
core:
  branches:                 # 显式声明枝干,顺序即注册顺序
    environment:
      enabled: true
      show_time: true
    guardrails:
      enabled: true
      input_scan: block
    memory:
      enabled: true
      retrieval_top_k: 5
    tools:
      enabled: true
      max_react_loops: 50
    # 未声明或 enabled:false → 不注册,主干零感知
```

未声明的枝干 = 不存在(非"存在但坏")。`enabled: true` 但初始化失败 = 启动失败并给出可读错误——这彻底取代 ISSUE-01 的静默降级。

---

## 10. 文档维护

- 本规范是**契约**,接口变更需走"先改文档 → 评审 → 再改代码"的顺序;
- 新增枝干接入案例可追加到 §6(保持示例可运行);
- 迁移进度在 §8 表格逐阶段勾选,并在 ISSUE 清单对应条目注明。
