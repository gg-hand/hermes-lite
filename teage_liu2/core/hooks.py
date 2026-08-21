"""枝干契约(SPI,阶段 2 协议化):Extension(Branch) / Snapshot+Action / HookChainEngine。

对齐设计文档 §4(交互模型)与 §16 阶段 2:
- 交互模型:core ── Invocation{hook, snapshot, args} ──▶ 扩展
              扩展 ── ActionResult{actions[]} ────────▶ core 应用
- 推翻 BranchContext 可变对象 → Snapshot(不可变)+ Action(变更请求)
- 11 钩子:setup/teardown/build_injections/inject_round/before/pre_tool_call/
  on_tool_call/post_tool_call/after_step/after/on_error
- 每钩子 Action 权限(§4.2):
  * 非终态(before/post_tool_call/after_step)允许全部 6 种 Action
  * 终态(after/on_error)返回 Action[] 但 core 一律忽略(HOOK_TERMINAL_ACTION_IGNORED)
  * build_injections/inject_round 输出 Injection;pre_tool_call 输出 ToolDecision;
    on_tool_call 输出 result;teardown 无 action
- 应用时序(§4.4):立即应用 + 原子批次 + 覆盖规则(仅非终态)+ SetStop 短路
- observe 只读(§3.2/§15-A4③):声明 observe 的扩展,钩子返回 action 一律忽略 + 记录
- 隔离:单扩展异常/超时 → 跳过该扩展 + logger.error,不中断对话
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC
from typing import Any, Dict, List, Optional, Tuple

from .actions import Action, ActionResult, ToolDecision, validate_action
from .injection import Injection, _dedupe_by_key
from .snapshot import apply_action_batch
from .types import Snapshot, StepSummary

logger = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_SECONDS = 5.0

# capabilities 枚举(§12,授权面声明)
CAP_OBSERVE = "observe"
CAP_TOOL_EXECUTOR = "tool_executor"
CAP_LLM = "llm"
CAP_SELF_HOSTED_STORAGE = "self_hosted_storage"

# on_tool_call 未实现的哨兵
_NO_EXECUTOR = object()


def no_executor(result: Any) -> bool:
    """判断工具派发结果是否为"无执行者"。"""
    return result is _NO_EXECUTOR


class Branch(ABC):
    """枝干/扩展基类:实现需要的钩子,其余继承默认空实现。

    新协议签名(阶段 2):钩子收到不可变 Snapshot,通过返回 Action 变更对话状态;
    不得直接修改快照本身。
    """

    #: 唯一标识(extension_name,^[a-z0-9_]+$)
    name: str = "branch"

    #: 能力声明集合(§12):observe / tool_executor / llm / self_hosted_storage
    capabilities: List[str] = []

    #: 宿主能力端口(阶段 3,§18.2 进程内绑定):装配注入;同语言扩展经此走
    #: storage_*/invoke_llm/task_* 消息语义访问宿主能力(非对象引用注入)。
    #: 异语言扩展经 stdio 通道,不使用本端口。
    host_port: Any = None

    # ------------------------------------------------------------------
    # 生命周期(setup 失败 = 启动失败,禁止 try/except 吞错)
    # ------------------------------------------------------------------
    async def setup(self, config: dict, host: Any) -> None:
        """启动时初始化:加载依赖、预热资源。host = 宿主能力声明(纯数据)。

        配置关闭 = 不注册,不调用本方法。
        """
        ...

    async def teardown(self) -> None:
        """关闭时释放资源(幂等;无 action)。"""
        ...

    # ------------------------------------------------------------------
    # LLM 前注入
    # ------------------------------------------------------------------
    async def build_injections(self, snapshot: Snapshot) -> List[Injection]:
        """声明多层级注入项(对话级,每次对话组装前调用)。空列表 = 不注入。"""
        return []

    async def inject_round(self, snapshot: Snapshot) -> Optional[Injection]:
        """轮次间注入声明(每轮 step 前;layer 强制 BEFORE_INPUT;None = 不注入)。"""
        return None

    # ------------------------------------------------------------------
    # LLM 前后钩子(可选)
    # ------------------------------------------------------------------
    async def before(self, snapshot: Snapshot) -> List[Action]:
        """LLM 调用前(注入声明收集后、组装收口前):返回 Action[](全部 6 种)。

        SetStop 短路后续扩展同名钩子并跳过收口与 LLM。
        """
        return []

    async def pre_tool_call(
        self, snapshot: Snapshot, name: str, input: dict
    ) -> ToolDecision:
        """工具执行前策略决策(注册序;reject 短路 / modify 叠加,§8)。

        默认 allow(不短路)。
        """
        return ToolDecision(decision="allow")

    async def on_tool_call(
        self, snapshot: Snapshot, name: str, input: dict
    ) -> Any:
        """执行工具(仅工具类枝干实现)。未实现的工具返回 NotImplemented。"""
        return NotImplemented

    async def post_tool_call(
        self,
        snapshot: Snapshot,
        name: str,
        input: dict,
        result: Any,
        duration: float,
    ) -> List[Action]:
        """工具执行后:返回 Action[](全部 6 种,增量收口校验)。"""
        return []

    async def after_step(
        self, snapshot: Snapshot, summary: StepSummary
    ) -> List[Action]:
        """每轮 step 后:返回 Action[](全部 6 种,增量收口校验)。"""
        return []

    async def after(self, snapshot: Snapshot, response: Any) -> List[Action]:
        """对话完成(after 钩子,逆序):返回 Action[](一律忽略 + 记录)。

        数据写入走 storage 消息通道(§5),不返回 action。
        """
        return []

    async def on_error(self, snapshot: Snapshot, error: Any) -> List[Action]:
        """失败/断连/拦截通知(on_error,逆序):返回 Action[](一律忽略 + 记录)。"""
        return []


class HookChain:
    """钩子链引擎:注册枝干并按协议调用(§4.2/§4.3/§4.4)。

    - 注册序 = before / build_* / pre_tool_call 调用顺序
    - after / on_error / teardown 逆序(洋葱模型)
    - 每个钩子独立超时(默认 5s),超时跳过该枝干并记日志
    - 运行时异常跳过该枝干,不影响对话
    - 立即应用 + 原子批次:每个扩展的 action 批次在调用下一个扩展前
      立即原子推进快照(§4.4)
    """

    def __init__(self, hook_timeout: float = DEFAULT_HOOK_TIMEOUT_SECONDS) -> None:
        self._branches: List[Branch] = []
        self.hook_timeout: float = hook_timeout

    def register(self, branch: Branch) -> None:
        """接入一个枝干(注册顺序 = before 调用顺序)。"""
        self._branches.append(branch)

    def unregister(self, name: str) -> bool:
        """按 name 移除枝干(运行时热插拔)。"""
        for i, b in enumerate(self._branches):
            if b.name == name:
                del self._branches[i]
                return True
        return False

    def replace(self, name: str, new_branch: Branch) -> Optional[Branch]:
        """按 name 替换枝干(保持注册位置,进程僵死重建/热插拔用)。

        返回被替换的旧分支(调用方负责异步 teardown);未命中返回 None。
        新枝干保持原注册序 —— before/pre_tool_call/on_tool_call 调用顺序
        不被重建打乱。
        """
        for i, b in enumerate(self._branches):
            if b.name == name:
                self._branches[i] = new_branch
                return b
        return None

    @property
    def branches(self) -> List[Branch]:
        return list(self._branches)

    @property
    def is_empty(self) -> bool:
        return not self._branches

    @staticmethod
    def _is_observe(branch: Branch) -> bool:
        """是否声明 observe 能力(§12;L3 订阅声明 + 只读约束)。"""
        return CAP_OBSERVE in (branch.capabilities or [])

    @staticmethod
    def _log_observe_action(branch: Branch, hook_name: str, actions: Any) -> None:
        """observe 扩展返回 action → 忽略 + error 级日志(§3.2/§15-A4③)。"""
        logger.error(
            "observe 扩展 %s 的 %s 钩子返回 %d 个 action,已忽略(只读约束)",
            branch.name, hook_name, len(actions),
        )

    # ------------------------------------------------------------------
    # 调用辅助
    # ------------------------------------------------------------------
    async def _call(self, branch: Branch, hook_name: str, *args, **kwargs) -> Any:
        """带超时与异常隔离的单钩子调用。

        超时 / 异常 → 跳过该枝干 + logger.error,不向上抛。
        """
        try:
            return await asyncio.wait_for(
                getattr(branch, hook_name)(*args, **kwargs),
                timeout=self.hook_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                "枝干 %s 的 %s 钩子超时(>%.1fs),跳过该枝干",
                branch.name, hook_name, self.hook_timeout,
            )
        except Exception as e:
            logger.error("枝干 %s 的 %s 钩子异常,跳过: %s", branch.name, hook_name, e)
        return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def setup_all(self, config: dict, host: Any) -> None:
        """启动时初始化全部枝干(setup 失败逆序回滚 + 向上抛 = 启动失败)。"""
        succeeded: List[Branch] = []
        try:
            for branch in self._branches:
                await branch.setup(config, host)
                succeeded.append(branch)
        except Exception:
            for branch in reversed(succeeded):
                try:
                    await branch.teardown()
                except Exception as e:
                    logger.warning("回滚 teardown 异常(枝干 %s): %s", branch.name, e)
            raise

    async def teardown_all(self) -> None:
        """关闭时逆序释放(幂等)。"""
        for branch in reversed(self._branches):
            try:
                await branch.teardown()
            except Exception as e:
                logger.warning("枝干 %s teardown 异常: %s", branch.name, e)

    # ------------------------------------------------------------------
    # 注入收集
    # ------------------------------------------------------------------
    async def build_injections_all(self, snapshot: Snapshot) -> List[Injection]:
        """收集全部枝干的注入声明(注册序)。"""
        result: List[Injection] = []
        for branch in self._branches:
            items = await self._call(branch, "build_injections", snapshot)
            if items:
                result.extend(items)
        return result

    async def inject_round(self, snapshot: Snapshot) -> List[Injection]:
        """轮次间注入全收集合并(§4.2 v1.7 定案,解全收集语义缺口)。

        ① 按注册序拼接;② 同 key 去重(后注册覆盖先注册);③④ 预算裁剪与
        build_injections 叠加顺序由 Assembler 统一处理。
        语义变更(§17):现状"首个非空返回" → 全收集合并。
        """
        result: List[Injection] = []
        for branch in self._branches:
            item = await self._call(branch, "inject_round", snapshot)
            if item is not None:
                result.append(item)
        return _dedupe_by_key(result)

    # ------------------------------------------------------------------
    # LLM 前后钩子
    # ------------------------------------------------------------------
    async def before_all(
        self, snapshot: Snapshot
    ) -> Tuple[Snapshot, Optional[str]]:
        """before 链(注册序,action 立即应用):返回 (最新快照, stop_reason or None)。

        SetStop 短路:任一扩展返回 SetStop → 短路后续 before 调用 → 返回
        (快照, reason),调用方据此跳过组装收口与 LLM 直接 done(intercepted)。
        observe 扩展的 action 一律忽略 + 记录,不推进快照、不短路。
        """
        cur = snapshot
        for branch in self._branches:
            actions = await self._call(branch, "before", cur)
            if not actions:
                continue
            if self._is_observe(branch):
                self._log_observe_action(branch, "before", actions)
                continue
            cur, _ = apply_action_batch(cur, actions)
            if cur.stop:
                logger.info("枝干 %s SetStop,对话被拦截(%s)", branch.name, cur.stop_reason)
                return cur, cur.stop_reason or "intercepted"
        return cur, None

    async def pre_tool_call_all(
        self, snapshot: Snapshot, name: str, input: dict
    ) -> Tuple[ToolDecision, Dict[str, Any]]:
        """pre_tool_call 链(注册序;reject 短路 / modify 叠加,§8 工具路径四连 ①)。

        返回 (最终决策, effective_input)。allow 不短路;modify 的 input 作为
        实际执行输入传给后续 pre_tool_call 与 on_tool_call(应用序后覆盖先)。
        """
        cur_input: Dict[str, Any] = dict(input) if isinstance(input, dict) else input
        for branch in self._branches:
            decision = await self._call(branch, "pre_tool_call", snapshot, name, cur_input)
            if decision is None:
                continue
            if not isinstance(decision, ToolDecision):
                logger.error(
                    "枝干 %s 的 pre_tool_call 返回非法类型 %s,按 allow 处理",
                    branch.name, type(decision).__name__,
                )
                continue
            if decision.is_reject:
                return decision, cur_input
            if decision.is_modify and decision.input is not None:
                cur_input = decision.input
        return ToolDecision(decision="allow", input=cur_input), cur_input

    async def dispatch_tool_call(
        self, snapshot: Snapshot, tool_name: str, tool_input: dict
    ) -> Any:
        """派发工具调用(§transport T-6 多扩展派发聚合):首个非 NotImplemented 的结果返回。

        - 声明 tool_executor 且具备 invoke_tool 通道的扩展(RemoteBranchAdapter)
          → 走 ``invoke_tool`` 轻量消息(仅 name+input,免快照序列化,§transport T-5)
        - 其余扩展 → 走 ``on_tool_call`` 钩子(invoke_hook 路径)
        两种通道的"不执行"信号(NotImplemented)统一计入 no_tool_executor 判定。

        全部未实现 → _NO_EXECUTOR 哨兵(loop 据此友好终止)。
        枝干异常 → 捕获并返回异常实例(loop 转 tool_result is_error 回喂 LLM,
        单枝干故障不杀死对话;§8 责任矩阵 TOOL_EXEC_FAILED)。
        """
        for branch in self._branches:
            try:
                if (
                    CAP_TOOL_EXECUTOR in (branch.capabilities or [])
                    and hasattr(branch, "invoke_tool")
                ):
                    result = await branch.invoke_tool(tool_name, tool_input)
                else:
                    result = await branch.on_tool_call(snapshot, tool_name, tool_input)
            except Exception as e:
                logger.error(
                    "枝干 %s 执行工具 %s 异常: %s", branch.name, tool_name, e
                )
                return e
            if result is not NotImplemented:
                return result
        return _NO_EXECUTOR

    async def post_tool_call_all(
        self,
        snapshot: Snapshot,
        name: str,
        input: dict,
        result: Any,
        duration: float,
    ) -> Snapshot:
        """post_tool_call 链(注册序,action 立即应用):返回最新快照。"""
        cur = snapshot
        for branch in self._branches:
            actions = await self._call(
                branch, "post_tool_call", cur, name, input, result, duration
            )
            if not actions:
                continue
            if self._is_observe(branch):
                self._log_observe_action(branch, "post_tool_call", actions)
                continue
            cur, _ = apply_action_batch(cur, actions)
        return cur

    async def after_step_all(
        self, snapshot: Snapshot, summary: StepSummary
    ) -> Snapshot:
        """after_step 链(注册序,action 立即应用):返回最新快照。"""
        cur = snapshot
        for branch in self._branches:
            actions = await self._call(branch, "after_step", cur, summary)
            if not actions:
                continue
            if self._is_observe(branch):
                self._log_observe_action(branch, "after_step", actions)
                continue
            cur, _ = apply_action_batch(cur, actions)
        return cur

    async def after_all(self, snapshot: Snapshot, response: Any) -> None:
        """after 钩子(逆序,终态):返回的 Action[] 一律忽略 + 记录(§5)。

        HOOK_TERMINAL_ACTION_IGNORED —— 对话已结束、done 已定型、落盘已完成,
        不存在 action 的接收方。数据写入经 storage 消息通道。
        """
        for branch in reversed(self._branches):
            actions = await self._call(branch, "after", snapshot, response)
            if actions:
                logger.error(
                    "HOOK_TERMINAL_ACTION_IGNORED: 枝干 %s 的 after 返回 "
                    "%d 个 action,终态钩子 action 一律忽略",
                    branch.name, len(actions),
                )

    async def on_error_all(self, snapshot: Snapshot, error: Any) -> None:
        """on_error 钩子(逆序,终态):返回的 Action[] 一律忽略 + 记录(§5)。"""
        for branch in reversed(self._branches):
            actions = await self._call(branch, "on_error", snapshot, error)
            if actions:
                logger.error(
                    "HOOK_TERMINAL_ACTION_IGNORED: 枝干 %s 的 on_error 返回 "
                    "%d 个 action,终态钩子 action 一律忽略",
                    branch.name, len(actions),
                )
