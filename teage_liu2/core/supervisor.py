"""扩展进程监管(§5,阶段 3/4 落地):spawn/握手/心跳/优雅关闭 + 热重载原子替换 + 进程僵死自动重建。

- 每异语言(stdio)扩展一进程;spawn → 握手(互报 protocol_version + 版本协商)→ 注册宿主身份
  (TransportBus,供 kind 前缀/capability 授权)→ L3 观测订阅(observe 能力)
- 热重载(§5 L-3 接近原子 + 回滚保旧链):spawn 新进程 → build 新链 → setup 成功 →
  teardown 旧链(含后台任务 cancel)→ 替换引用 → 关闭旧进程;任一步失败 → 回滚
  (关闭新进程,保留旧链)
- 进程僵死自动重建(阶段 4,§5 B1):StdioChannel 心跳连续失败达阈值 → on_dead →
  自动 spawn 新进程 + 链内原位替换(HookChain.replace 保持注册序)+ 新 adapter setup;
  失败重试(max_retries) + 指数退避,防重建风暴;重建失败降级标记(可观测)
- 优雅关闭:shutdown 帧 → 终止兜底

配置契约(core.branches 条目,transport: stdio)::
    audit:
      transport: stdio
      command: ["python", "d:/tmp/audit_ext.py"]
      protocol_version: v1.0.0
      hooks_implemented: [before, after]
      capabilities: [observe]
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from .hooks import CAP_OBSERVE
from .registry import BranchRegistry, _parse_branch_config
from .remote_adapter import RemoteBranchAdapter
from .stdio import StdioChannel
from .transport import DEFAULT_PROTOCOL_VERSION, MSG_EVENT, TransportBus

logger = logging.getLogger(__name__)

#: 进程僵死自动重建最大尝试次数(§5 B1,防无限重建)
DEFAULT_RESTART_MAX_RETRIES: int = 2
#: 重建指数退避基数(秒):delay = base * 2**attempt
DEFAULT_RESTART_BACKOFF_BASE: float = 1.0


class Supervisor:
    """扩展进程监管 + 热重载编排 + 进程僵死自动重建。"""

    def __init__(
        self,
        transport_bus: TransportBus,
        l3_sink: Optional[Any] = None,
        heartbeat_interval: float = 30.0,
        host_builder: Optional[Any] = None,
        restart_max_retries: int = DEFAULT_RESTART_MAX_RETRIES,
        restart_backoff_base: float = DEFAULT_RESTART_BACKOFF_BASE,
    ) -> None:
        self._bus = transport_bus
        self._l3_sink = l3_sink
        self._heartbeat_interval = heartbeat_interval
        #: host 纯数据声明构建器(进程重建时新 adapter setup 用,§5 L-8)
        self._host_builder = host_builder
        self._restart_max_retries = restart_max_retries
        self._restart_backoff_base = restart_backoff_base
        #: name -> StdioChannel(当前全部已 spawn 通道,含热重载中的新进程)
        self._channels: Dict[str, StdioChannel] = {}
        #: name -> RemoteBranchAdapter(registry.launcher 的取值来源)
        self._adapters: Dict[str, RemoteBranchAdapter] = {}
        #: name -> ext_cfg(进程重建时按原配置重新 spawn)
        self._ext_configs: Dict[str, Dict[str, Any]] = {}
        #: 累计自动重建成功次数(可观测)
        self.process_restarts: int = 0
        #: 当前降级扩展集合(重建失败,服务降级中;可观测)
        self.degraded: set = set()
        self._rebuild_in_progress: set = set()

    # ------------------------------------------------------------------
    # 配置扫描
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_stdio_entries(config: dict) -> List[Tuple[str, Dict[str, Any]]]:
        """扫描 core.branches 中 transport: stdio 的启用条目,返回 (name, ext_cfg)。"""
        branches_cfg = (config or {}).get("core", {}).get("branches") or {}
        entries: List[Tuple[str, Dict[str, Any]]] = []
        for name, raw_cfg in branches_cfg.items():
            try:
                enabled, ext_cfg = _parse_branch_config(str(name), raw_cfg)
            except ValueError as e:
                logger.warning("跳过扩展 %s: %s", name, e)
                continue
            if not enabled:
                continue
            if ext_cfg.get("transport") == "stdio":
                entries.append((str(name), ext_cfg))
        return entries

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    async def launch_all(self, config: dict) -> Dict[str, RemoteBranchAdapter]:
        """spawn 全部 stdio 扩展进程 + 握手 + 注册宿主身份 + L3 订阅。

        任一扩展进程启动失败 → 逆序关闭已启动进程并向上抛(启动原子性,E1)。
        """
        entries = self._iter_stdio_entries(config)
        launched: List[str] = []
        try:
            for name, ext_cfg in entries:
                channel = await self._spawn_channel(name, ext_cfg)
                adapter = RemoteBranchAdapter(name, ext_cfg, channel)
                self._channels[name] = channel
                self._adapters[name] = adapter
                self._ext_configs[name] = dict(ext_cfg)
                self._register_extension(name, ext_cfg)
                launched.append(name)
                logger.info("扩展进程 %s 启动完成(共 %d 个)", name, len(launched))
        except Exception:
            for name in reversed(launched):
                try:
                    await self._shutdown_channel(name)
                except Exception as e:
                    logger.warning("启动回滚关闭扩展 %s 失败: %s", name, e)
            raise
        return dict(self._adapters)

    def _register_extension(self, name: str, ext_cfg: Dict[str, Any]) -> None:
        """注册宿主身份(capabilities)+ L3 观测订阅(observe 能力,§3.2 v1.11)。"""
        self._bus.register_extension(name, ext_cfg.get("capabilities") or [])
        if CAP_OBSERVE in (ext_cfg.get("capabilities") or []):
            if self._l3_sink is not None:
                self._l3_sink.subscribe(name, self._make_l3_deliver(name))

    def _make_l3_deliver(self, name: str):
        """L3 观测投递回调(异步旁路,绝不阻塞主对话流)。"""

        async def deliver(events: List[dict]) -> None:
            channel = self._channels.get(name)
            if channel is None or channel.closed:
                return
            channel.notify(MSG_EVENT, {"events": events})

        return deliver

    # ------------------------------------------------------------------
    # 扩展启动器(registry 装配用)
    # ------------------------------------------------------------------
    def launcher(self, name: str, ext_cfg: Dict[str, Any]) -> RemoteBranchAdapter:
        """registry 扩展启动器:返回已 spawn 的 adapter(同步;热重载时为新进程 adapter)。"""
        adapter = self._adapters.get(name)
        if adapter is None:
            raise ValueError(f"扩展 {name} 进程未启动(launcher 不可用)")
        return adapter

    # ------------------------------------------------------------------
    # 进程
    # ------------------------------------------------------------------
    async def _spawn_channel(self, name: str, ext_cfg: Dict[str, Any]) -> StdioChannel:
        command = ext_cfg.get("command")
        if not command or not isinstance(command, list):
            raise ValueError(
                f"扩展 {name} transport: stdio 必须配置 command(命令列表),实际 {command!r}"
            )
        channel = StdioChannel(
            name=name,
            command=[str(c) for c in command],
            host_handler=self._bus.handle,
            protocol_version=ext_cfg.get("protocol_version", DEFAULT_PROTOCOL_VERSION),
            heartbeat_interval=self._heartbeat_interval,
        )
        # 进程僵死自动重建:心跳连续失败 → on_dead → 本 Supervisor 重建
        channel.set_on_dead(self._auto_rebuild)
        await channel.start()
        return channel

    async def _shutdown_channel(self, name: str) -> None:
        channel = self._channels.pop(name, None)
        self._adapters.pop(name, None)
        self._bus.unregister_extension(name)
        if self._l3_sink is not None:
            try:
                self._l3_sink.unsubscribe(name)
            except Exception as e:
                logger.warning("L3 退订扩展 %s 失败: %s", name, e)
        if channel is not None:
            await channel.close()

    # ------------------------------------------------------------------
    # 热重载(§5 L-3 接近原子 + 回滚保旧链)
    # ------------------------------------------------------------------
    async def reload(
        self,
        new_config: dict,
        registry: BranchRegistry,
        host_builder: Optional[Any] = None,
    ) -> Any:
        """热重载:spawn 新进程 → registry.rebuild(新链 setup 成功 → teardown 旧链 →
        替换)→ 关闭旧进程;任一步失败回滚保旧链。

        时序细节:旧进程保留至新链 setup 全部成功,期间旧对话仍服务旧链;
        回滚时不取消旧链后台任务(§5 L-4)。
        """
        old_adapters = dict(self._adapters)
        old_channels = dict(self._channels)
        new_adapters: Dict[str, RemoteBranchAdapter] = {}
        new_channels: Dict[str, StdioChannel] = {}
        try:
            # ① spawn 新进程(失败 → 回滚,旧链不受影响)
            for name, ext_cfg in self._iter_stdio_entries(new_config):
                channel = await self._spawn_channel(name, ext_cfg)
                new_channels[name] = channel
                new_adapters[name] = RemoteBranchAdapter(name, ext_cfg, channel)
                self._ext_configs[name] = dict(ext_cfg)  # 重建按最新配置 spawn
                self._register_extension(name, ext_cfg)
            # ② 更新映射(registry.rebuild 的 build 阶段 launcher 返回新 adapter)
            self._adapters.update(new_adapters)
            self._channels.update(new_channels)
            # ③ registry.rebuild:build 新链 → setup 全部成功 → teardown 旧链 → 替换
            await registry.rebuild(new_config, host_builder=host_builder)
        except Exception:
            # 回滚:关闭新进程,恢复旧映射(旧链后台任务不取消,§5 L-4)
            for name, channel in new_channels.items():
                try:
                    await self._shutdown_channel(name)
                except Exception as e:
                    logger.warning("热重载回滚关闭扩展 %s 失败: %s", name, e)
            self._adapters = old_adapters
            self._channels = old_channels
            logger.error("热重载失败,已回滚保旧链")
            raise
        # ④ 成功:关闭旧进程(不在新集合中的),清理已移除扩展的配置
        for name, channel in old_channels.items():
            if name not in new_channels:
                self._ext_configs.pop(name, None)
                try:
                    await self._shutdown_channel(name)
                except Exception as e:
                    logger.warning("热重载关闭旧扩展 %s 失败: %s", name, e)
        logger.info("热重载完成: 新扩展进程 %s", sorted(new_channels))
        return registry.chain

    # ------------------------------------------------------------------
    # 进程僵死自动重建(§5 B1,阶段 4)
    # ------------------------------------------------------------------
    def _registry_chain(self, name: str) -> Optional[Any]:
        """从已装配 registry 定位含扩展 name 的 HookChain(链内原位替换用)。

        registry 经 :meth:`launch_all` 的调用方(app.py)持有;supervisor 通过
        ``_registry`` 引用。无 registry 引用(独立使用场景)→ 仅重建 adapter 映射,
        链替换由调用方负责(记录 warning)。
        """
        registry = getattr(self, "_registry", None)
        if registry is None:
            return None
        return registry.chain

    def attach_registry(self, registry: Any) -> None:
        """绑定已装配的 BranchRegistry(进程重建时链内原位替换 HookChain)。"""
        self._registry = registry

    async def _auto_rebuild(self, name: str, reason: str) -> None:
        """进程僵死自动重建:spawn 新进程 → 链内原位替换 → 新 adapter setup。

        - 防并发重建:同一扩展重建进行中则跳过(心跳回调可能重复触发)
        - 失败重试(max_retries)+ 指数退避(防重建风暴,§5 B1)
        - 重建成功:替换链内分支(保持注册序)+ 更新映射 + 计数;失败:降级标记
        - 重建不取消旧链后台任务(§5 L-4;旧 adapter 由替换方 teardown)
        """
        if name in self._rebuild_in_progress:
            logger.info("扩展 %s 重建已在进行,跳过重复触发", name)
            return
        self._rebuild_in_progress.add(name)
        ext_cfg = self._ext_configs.get(name)
        if ext_cfg is None:
            logger.error("扩展 %s 无配置,无法自动重建", name)
            self._rebuild_in_progress.discard(name)
            return
        old_channel = self._channels.get(name)
        old_adapter = self._adapters.get(name)
        for attempt in range(1, self._restart_max_retries + 1):
            try:
                new_channel = await self._spawn_channel(name, ext_cfg)
                new_adapter = RemoteBranchAdapter(name, ext_cfg, new_channel)
                self._channels[name] = new_channel
                self._adapters[name] = new_adapter
                self._register_extension(name, ext_cfg)
                # 链内原位替换(保持注册序,§hooks.HookChain.replace)
                chain = self._registry_chain(name)
                if chain is not None:
                    replaced = chain.replace(name, new_adapter)
                    if replaced is not None:
                        try:
                            await replaced.teardown()
                        except Exception as e:
                            logger.warning("重建替换旧 adapter %s teardown 异常: %s", name, e)
                # 新 adapter setup(host 纯数据声明;失败 = 重建失败)
                host_decl = self._host_builder(name) if self._host_builder is not None else None
                await new_adapter.setup(ext_cfg, host_decl)
                # 成功:关闭旧通道(不同引用才关,防误关新通道;已知僵死,短超时)
                if old_channel is not None and old_channel is not new_channel:
                    try:
                        await old_channel.close(shutdown_timeout=0.3)
                    except Exception as e:
                        logger.warning("重建后关闭旧通道 %s 失败: %s", name, e)
                self.process_restarts += 1
                self.degraded.discard(name)
                logger.info(
                    "扩展 %s 进程僵死自动重建成功(%s, attempt=%d, 累计 %d 次)",
                    name, reason, attempt, self.process_restarts,
                )
                self._rebuild_in_progress.discard(name)
                return
            except Exception as e:
                # 清理本次尝试的通道/adapter,进入重试或降级
                try:
                    ch = self._channels.pop(name, None)
                    if ch is not None and ch is not old_channel:
                        await ch.close(shutdown_timeout=0.3)
                except Exception as close_e:
                    logger.warning("重建清理扩展 %s 通道失败: %s", name, close_e)
                self._adapters.pop(name, None)
                if attempt < self._restart_max_retries:
                    delay = self._restart_backoff_base * (2 ** (attempt - 1))
                    logger.warning(
                        "扩展 %s 重建失败(attempt=%d/%d),%.1fs 后重试: %s",
                        name, attempt, self._restart_max_retries, delay, e,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "扩展 %s 重建失败 %d 次,降级(旧进程/链保留): %s",
                        name, self._restart_max_retries, e,
                    )
                    # 恢复旧引用(若旧通道仍存活,对话降级不中断)
                    if old_channel is not None and old_channel.closed is False:
                        self._channels[name] = old_channel
                    if old_adapter is not None:
                        self._adapters[name] = old_adapter
                    self.degraded.add(name)
        self._rebuild_in_progress.discard(name)

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        """关闭全部扩展进程(幂等,可多次调用)。"""
        for name in list(self._channels):
            try:
                await self._shutdown_channel(name)
            except Exception as e:
                logger.warning("关闭扩展 %s 失败: %s", name, e)
        self._channels.clear()
        self._adapters.clear()
        self._ext_configs.clear()
        self.degraded.clear()
        self._rebuild_in_progress.clear()
