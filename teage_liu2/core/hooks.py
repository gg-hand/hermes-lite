"""枝干契约(SPI):BranchContext / Branch / HookChain。

对齐 docs/SUBSYSTEM-SPI.md 的接口定义:
- 主干负责初始化 session_id / user_input / history / system_text
- 枝干只能写入 messages / tools / extra / stop;枝干间共享数据一律走 extra
- 注册顺序 = before 调用顺序;after / teardown 逆序(洋葱模型)
- 钩子超时:主干以 asyncio.wait_for 包裹每个钩子(默认 5s),超时跳过该枝干
- 异常语义:
  * setup 失败 = 启动失败(配置开了却坏了必须暴露)—— 由装配方负责,本模块不捕获
  * 运行时钩子异常 = 跳过该枝干 + logger.error(单枝干故障不影响对话)
  * on_tool_call 不加超时(工具执行由工具自身负责超时,M2 带回)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_SECONDS = 5.0


class BranchContext:
    """主干与枝干间交换的数据包:一次对话的完整状态。"""

    def __init__(self, session_id: str, user_input: str) -> None:
        self.session_id: str = session_id
        self.user_input: str = user_input
        #: 主干就绪的会话历史(枝干只读建议)
        self.history: List[Dict[str, Any]] = []
        #: 主干基础 system(build_system 可追加)
        self.system_text: str = ""
        #: 送往 LLM 的消息(枝干可写:如注入检索记忆)
        self.messages: List[Dict[str, Any]] = []
        #: 工具 schema 列表(工具枝干填充)
        self.tools: List[Dict[str, Any]] = []
        #: 枝干间共享数据
        self.extra: Dict[str, Any] = {}
        #: 枝干置 True → 主干跳过 LLM 直接返回
        self.stop: bool = False

    # 便捷访问:LLM 最终使用的 system(主干在 LLM 前计算)
    @property
    def effective_system(self) -> str:
        return self.system_text


class Branch(ABC):
    """枝干基类:实现需要的钩子,其余继承默认空实现。"""

    #: 唯一标识,用于配置 / 日志 / 审计
    name: str = "branch"

    # ------------------------------------------------------------------
    # 生命周期(setup 失败 = 启动失败,禁止 try/except 吞错)
    # ------------------------------------------------------------------
    async def setup(self, config: dict, core: Any) -> None:
        """启动时初始化:加载依赖、预热资源。配置关闭 = 不注册,不调用本方法。"""
        ...

    async def teardown(self) -> None:
        """关闭时释放资源。"""
        ...

    # ------------------------------------------------------------------
    # LLM 前注入(可选)
    # ------------------------------------------------------------------
    async def build_system(self, ctx: BranchContext) -> None:
        """向 ctx.system_text 追加稳定前缀(参与 LLM 前缀缓存,只放稳定内容)。"""
        ...

    async def build_injection(self, ctx: BranchContext) -> str:
        """返回注入 messages[0] 的文本(检索记忆 / 环境 / 任务进度)。空串 = 不注入。"""
        return ""

    # ------------------------------------------------------------------
    # LLM 前后钩子(可选)
    # ------------------------------------------------------------------
    async def before(self, ctx: BranchContext) -> None:
        """LLM 调用前:输入扫描 / 意图路由 / 工具 schema 填充。置 ctx.stop=True 可拦截。"""
        ...

    async def on_tool_call(self, ctx: BranchContext, tool_name: str,
                           tool_input: dict) -> Any:
        """执行工具(仅工具类枝干实现)。未实现的工具返回 NotImplemented。"""
        return NotImplemented

    async def after(self, ctx: BranchContext, response: Any) -> None:
        """LLM 返回后:输出过滤 / 审计 / 记忆巩固 / 指标。"""
        ...


class HookChain:
    """钩子链:注册枝干并按契约调用。

    - 注册顺序 = before / build_* 调用顺序
    - after / teardown 逆序
    - 每个钩子独立超时(默认 5s),超时跳过该枝干并记日志
    - 运行时异常跳过该枝干,不影响对话
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

    @property
    def branches(self) -> List[Branch]:
        return list(self._branches)

    @property
    def is_empty(self) -> bool:
        return not self._branches

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
    # 契约调用点
    # ------------------------------------------------------------------
    async def setup_all(self, config: dict, core: Any) -> None:
        """启动时初始化全部枝干(setup 失败向上抛 = 启动失败)。"""
        for branch in self._branches:
            await branch.setup(config, core)

    async def teardown_all(self) -> None:
        """关闭时逆序释放。"""
        for branch in reversed(self._branches):
            try:
                await branch.teardown()
            except Exception as e:
                logger.warning("枝干 %s teardown 异常: %s", branch.name, e)

    async def build_system_all(self, ctx: BranchContext) -> None:
        for branch in self._branches:
            await self._call(branch, "build_system", ctx)

    async def build_injection_all(self, ctx: BranchContext) -> str:
        """收集全部枝干的注入文本,按注册顺序拼接(非空段以空行分隔)。"""
        parts: List[str] = []
        for branch in self._branches:
            text = await self._call(branch, "build_injection", ctx)
            if text:
                parts.append(str(text))
        return "\n\n".join(parts)

    async def before_all(self, ctx: BranchContext) -> None:
        for branch in self._branches:
            await self._call(branch, "before", ctx)
            if ctx.stop:
                logger.info("枝干 %s 置 stop,对话被拦截", branch.name)
                return

    async def dispatch_tool_call(
        self, ctx: BranchContext, tool_name: str, tool_input: dict
    ) -> Any:
        """派发工具调用:遍历枝干,首个非 NotImplemented 的结果返回。

        全部未实现 → 返回 ``_NO_EXECUTOR`` 哨兵(loop 据此友好终止)。
        """
        for branch in self._branches:
            result = await branch.on_tool_call(ctx, tool_name, tool_input)
            if result is not NotImplemented:
                return result
        return _NO_EXECUTOR

    async def after_all(self, ctx: BranchContext, response: Any) -> None:
        for branch in reversed(self._branches):
            await self._call(branch, "after", ctx, response)


#: 哨兵:无枝干可执行该工具
_NO_EXECUTOR = object()


def no_executor(result: Any) -> bool:
    """判断工具派发结果是否为"无执行者"。"""
    return result is _NO_EXECUTOR
