"""branch registry 装配层(B1+B2,计划 §3 定案)。

职责:
- 解析 ``core.branches`` 配置(顺序 = 注册顺序;未声明或 enabled:false 不注册;
  未知名报错 = 启动失败,防 typo 静默)
- 调工厂实例化枝干并注册 HookChain —— **不 import 任何枝干实现**
  (工厂由装配层注册,依赖方向:core 不感知枝干)
- ``setup_all``:逐枝干传入**自己的配置段**(枝干自校验依据);失败向上抛 = 启动失败
- ``build`` 幂等可重入(L2 热重载铺路):每次返回全新链,可反复调用

M2+ 热重载:``registry.rebuild(config)`` —— build 新链 → 成功则旧链 teardown+替换
(枝干无依赖 → 无级联重建)。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from .hooks import Branch, HookChain
from .types import is_valid_extension_name

logger = logging.getLogger(__name__)

# 工厂:接收该枝干的配置段(dict),返回 Branch 实例
BranchFactory = Callable[[dict], Branch]
# 扩展启动器(阶段 3):声明 transport 的条目经此创建 Branch(同步;spawn 由调用方完成)
ExtensionLauncher = Callable[[str, dict], Branch]
# host 声明构建器:接收 extension_name,返回宿主能力声明(纯数据 dict,§5 L-8)
HostBuilder = Callable[[str], dict]
# host_port 工厂:接收 extension_name,返回进程内能力端口(消息语义零成本,§18.2)
HostPortFactory = Callable[[str], Any]


class BranchRegistry:
    """配置驱动的枝干装配器。"""

    def __init__(self) -> None:
        self._factories: Dict[str, BranchFactory] = {}
        self._extension_launcher: Optional[ExtensionLauncher] = None
        self._host_port_factory: Optional[HostPortFactory] = None
        self._chain = HookChain()
        self._entries: List[Tuple[Branch, dict]] = []
        self._shutdown_done = False

    # ------------------------------------------------------------------
    # 工厂注册(装配层调用;registry 不 import 枝干实现)
    # ------------------------------------------------------------------
    def register_factory(self, name: str, factory: BranchFactory) -> None:
        """注册枝干工厂(同名覆盖)。"""
        self._factories[name] = factory

    def set_extension_launcher(self, launcher: ExtensionLauncher) -> None:
        """注册扩展启动器(阶段 3):声明 ``transport`` 的条目经此创建 Branch。

        启动器须同步返回 Branch(扩展进程 spawn 由调用方 —— Supervisor —— 完成)。
        """
        self._extension_launcher = launcher

    def set_host_port_factory(self, factory: HostPortFactory) -> None:
        """注册进程内能力端口工厂(阶段 3,§18.2):装配时注入 branch.host_port。

        同语言扩展经 host_port 走消息语义访问宿主能力(storage_*/invoke_llm/task_*),
        非对象引用注入(§transport.1)。
        """
        self._host_port_factory = factory

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    def build(self, config: dict) -> HookChain:
        """解析 core.branches 配置 → 按序实例化 → 注册 HookChain。

        幂等可重入:每次调用重建全新链与条目表,不残留上次状态。
        配置契约:
            core:
              branches:
                guardrails: { enabled: true }
                audit: { transport: stdio, command: [...], ... }
        未声明或 enabled:false → 不注册;未知名(工厂未注册)→ ValueError;
        声明 transport 但无扩展启动器 → ValueError(启动失败,防静默)。
        """
        self._chain = HookChain()
        self._entries = []
        self._shutdown_done = False  # 重建后可再次 shutdown(L2 重载)

        branches_cfg = (config or {}).get("core", {}).get("branches") or {}
        if not isinstance(branches_cfg, dict):
            raise ValueError(
                f"core.branches 必须是映射(枝干名 → 配置),实际 {type(branches_cfg).__name__}"
            )

        for name, raw_cfg in branches_cfg.items():
            if not is_valid_extension_name(str(name)):
                raise ValueError(
                    f"非法枝干名/extension_name: {name!r}"
                    "(必须匹配 ^[a-z0-9_]+$, 禁点)"
                )
            enabled, branch_cfg = _parse_branch_config(name, raw_cfg)
            if not enabled:
                logger.info("枝干 %s 未启用(enabled:false),不注册", name)
                continue
            branch = self._instantiate(name, branch_cfg)
            self._entries.append((branch, branch_cfg))
            self._chain.register(branch)
            logger.info("枝干 %s 已注册(配置顺序 #%d)", name, len(self._entries))
        return self._chain

    def _instantiate(self, name: str, branch_cfg: dict) -> Branch:
        """按条目实例化 Branch:transport 条目走扩展启动器,普通条目走工厂。"""
        if "transport" in branch_cfg:
            if self._extension_launcher is None:
                raise ValueError(
                    f"扩展 {name!r} 声明 transport: {branch_cfg.get('transport')!r} "
                    "但未配置扩展启动器(请先 set_extension_launcher)。"
                )
            branch = self._extension_launcher(name, branch_cfg)
        else:
            factory = self._factories.get(name)
            if factory is None:
                raise ValueError(
                    f"未知枝干: {name!r}(未注册工厂)。"
                    "请在装配层调用 registry.register_factory(name, factory) 注册。"
                )
            branch = factory(branch_cfg)
        if not isinstance(branch, Branch):
            raise ValueError(
                f"枝干 {name!r} 工厂返回类型错误: {type(branch).__name__}(应为 Branch)"
            )
        # 进程内能力端口注入(同语言扩展经此走消息语义,§18.2)
        if self._host_port_factory is not None:
            try:
                branch.host_port = self._host_port_factory(name)
            except Exception as e:
                logger.warning("注入 %s 的 host_port 失败: %s", name, e)
        return branch

    async def rebuild(self, config: dict, host_builder: Optional[HostBuilder] = None) -> HookChain:
        """热重载(§5 L-3 原子替换 + 回滚保旧链)。

        build 新链 → setup 全部成功 → teardown 旧链 → 原子替换;
        任一步失败 → 逆序 teardown 新链 + 恢复旧链(回滚保旧链,不取消旧链后台任务)。

        异语言扩展进程重建由 Supervisor 编排(reload):本方法只管链级替换。
        """
        old_chain = self._chain
        old_entries = self._entries

        self._chain = HookChain()
        self._entries = []
        self._shutdown_done = False

        branches_cfg = (config or {}).get("core", {}).get("branches") or {}
        if not isinstance(branches_cfg, dict):
            raise ValueError(
                f"core.branches 必须是映射(枝干名 → 配置),实际 {type(branches_cfg).__name__}"
            )

        try:
            for name, raw_cfg in branches_cfg.items():
                if not is_valid_extension_name(str(name)):
                    raise ValueError(
                        f"非法枝干名/extension_name: {name!r}"
                        "(必须匹配 ^[a-z0-9_]+$, 禁点)"
                    )
                enabled, branch_cfg = _parse_branch_config(name, raw_cfg)
                if not enabled:
                    continue
                branch = self._instantiate(name, branch_cfg)
                self._entries.append((branch, branch_cfg))
                self._chain.register(branch)
            # setup 新链全部成功(E1 原子性:任一失败逆序回滚 + 抛错)
            await self.setup_all(config, host_builder=host_builder)
        except Exception:
            # 回滚:teardown 新链,恢复旧链引用
            try:
                await self.teardown_all()
            except Exception as e:
                logger.warning("热重载回滚 teardown 新链异常: %s", e)
            self._chain = old_chain
            self._entries = old_entries
            raise
        # 替换:teardown 旧链(不取消旧链后台任务 —— 由 Supervisor 在回滚/成功路径负责)
        try:
            await self._teardown_entries(old_entries)
        except Exception as e:
            logger.warning("热重载 teardown 旧链异常(已替换新链): %s", e)
        logger.info("热重载完成: %d 个枝干(原 %d)", len(self._entries), len(old_entries))
        return self._chain

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def setup_all(
        self,
        config: dict,
        host: Any = None,
        host_builder: Optional[HostBuilder] = None,
    ) -> None:
        """逐枝干调用 setup,传入**该枝干自己的配置段**与宿主能力声明。

        E1 启动原子性:任一枝干 setup 失败 → **逆序 teardown 已成功枝干** →
        再抛错(不静默)。失败向上抛 = 启动失败。
        host(§5 v1.11 术语):宿主能力声明(纯数据,含 storage 通道与 kind 前缀)。
        优先 ``host_builder(name)`` 按扩展生成声明(kind 前缀各扩展不同);
        未提供 host_builder 时回退到统一 host 参数(向后兼容)。
        """
        succeeded: List[Branch] = []
        try:
            for branch, branch_cfg in self._entries:
                host_decl = host_builder(branch.name) if host_builder is not None else host
                await branch.setup(branch_cfg, host_decl)
                succeeded.append(branch)
        except Exception:
            for branch in reversed(succeeded):
                try:
                    await branch.teardown()
                except Exception as e:
                    logger.warning("回滚 teardown 异常(枝干 %s): %s", branch.name, e)
            raise

    async def _teardown_entries(self, entries: List[Tuple[Branch, dict]]) -> None:
        """逆序 teardown 给定条目表;单个异常仅告警,逆序继续。"""
        for branch, _ in reversed(entries):
            try:
                await branch.teardown()
            except Exception as e:
                logger.warning("枝干 %s teardown 异常: %s", branch.name, e)

    async def teardown_all(self) -> None:
        """逆序 teardown 当前全部枝干;单个异常仅告警,逆序继续。"""
        await self._teardown_entries(self._entries)

    async def shutdown(
        self,
        task_registry: Any = None,
        message_store: Any = None,
        storage_provider: Any = None,
    ) -> None:
        """L1 shutdown 冲刷编排(幂等,可多次调用;对应计划 §6.4):

        ① TaskRegistry.cancel_all → ② teardown_all(枝干逆序各自冲刷) →
        ③ message_store.close → ④ storage_provider.close
        core 编排调用链,枝干负责自己的冲刷(teardown 内)。
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        if task_registry is not None:
            task_registry.cancel_all()
        await self.teardown_all()
        if message_store is not None:
            message_store.close()
        if storage_provider is not None:
            storage_provider.close()

    @property
    def chain(self) -> HookChain:
        """当前装配的钩子链。"""
        return self._chain

    @property
    def entries(self) -> List[Tuple[Branch, dict]]:
        """(枝干, 配置段) 列表(注册序)。"""
        return list(self._entries)


def _parse_branch_config(name: str, raw_cfg: Any) -> Tuple[bool, dict]:
    """解析单个枝干配置项 → (enabled, branch_config)。

    支持三种形态:
        {enabled: true, ...} → 启用,其余键为枝干配置
        true / null          → 启用,配置空
        false                → 不注册
    """
    if raw_cfg is None or raw_cfg is True:
        return True, {}
    if raw_cfg is False:
        return False, {}
    if isinstance(raw_cfg, dict):
        enabled = raw_cfg.get("enabled", True)
        return bool(enabled), raw_cfg
    raise ValueError(
        f"枝干 {name!r} 配置必须是 dict / bool / null,实际 {type(raw_cfg).__name__}"
    )
