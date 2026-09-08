"""外壳:FastAPI 应用装配。

依赖铁律:外壳只依赖 core —— 2026-09-08 统一扩展目录树后装配点例外也已消失
(生产扩展全部来自 extensions_root 目录发现,外壳不感知任何枝干名)。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, Optional

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..core.config import core_config_from, load_config
from ..core.event_stream import EventStream, L3BatchSink
from ..core.history import SQLiteHistoryStore
from ..core.hooks import CAP_OBSERVE, Branch, HookChain
from ..core.llm import LLMClient
from ..core.pipeline import ChatPipeline
from ..core.registry import BranchRegistry
from ..core.session import SessionStore
from ..core.storage import SQLiteStorageProvider
from ..core.storage_writer import StorageWriter
from ..core.supervisor import Supervisor
from ..core.tasks import TaskRegistry
from ..core.transport import TransportBus
from .routes import router
from .session_locks import SessionLocks

# 2026-09-08 统一扩展目录树:装配点例外已消失 —— 外壳不 import 任何枝干,
# 生产扩展全部来自 extensions_root 目录发现(wire_extensions);依赖铁律纯净化。
from ..core.extension_loader import (
    make_directory_loader,
    wire_extensions,
)

logger = logging.getLogger(__name__)


#: 进程内 L3 观测投递单批超时(秒):旁路"非阻塞"义务的宿主侧兜底(§3.2)。
#: 挂起的观察者不得阻塞 L3 冲刷循环;TimeoutError 向上抛给 L3ObserverQueue.deliver
#: 统一计入 dropped(§15-A6 背压计数),不在此吞掉。
L3_DELIVER_TIMEOUT_SECONDS: float = 5.0


def subscribe_inprocess_l3(
    l3_sink: Any, hooks: HookChain, prev_names: Optional[set] = None
) -> set:
    """同语言 observe 扩展的 L3 观测订阅(§3.2/§11)。

    缺口修复(2026-08-21):此前 L3 订阅仅 supervisor 对 stdio 异语言扩展接线,
    同语言 observe 扩展收不到 L3 原始事件(tool_use/tool_result/step_end)。
    本函数为链上声明 observe 的**普通 Branch 实例**(非 RemoteBranchAdapter,
    后者由 supervisor 走 stdio 投递)注册进程内投递目标:
    L3 旁路到达时异步调用 ``branch.on_l3_events(events)``。

    退订闭环(2026-09-08):重载后链上已移除的 observe 扩展须退订,否则旧实例
    仍留在 observers 表持续收投递。``prev_names`` 传入上次调用返回的订阅名集合
    (仅含本函数历史订阅的同语言 name,不会误伤 supervisor 的 stdio 订阅),
    本次不在链上的将 unsubscribe。

    装配后与热重载(registry.rebuild 换新链)后均应调用;重复订阅覆盖同 name。
    返回本次订阅的同语言 observe 扩展名集合(下次调用作 prev_names)。
    """
    from ..core.remote_adapter import RemoteBranchAdapter

    if l3_sink is None:
        return set()
    subscribed: set = set()
    for branch in hooks.branches:
        if CAP_OBSERVE not in (branch.capabilities or []):
            continue
        if isinstance(branch, RemoteBranchAdapter):
            continue  # 异语言扩展由 supervisor 走 stdio 投递

        async def deliver(events, _branch=branch) -> None:
            # 超时兜底:挂起的观察者不阻塞 L3 冲刷循环(§3.2 旁路非阻塞义务);
            # TimeoutError 交 L3ObserverQueue.deliver 计入 dropped(§15-A6)
            await asyncio.wait_for(
                _branch.on_l3_events(events), timeout=L3_DELIVER_TIMEOUT_SECONDS
            )

        l3_sink.subscribe(branch.name, deliver)
        subscribed.add(branch.name)
        logger.info("同语言 observe 扩展 %s 已订阅 L3 观测", branch.name)
    if prev_names:
        for stale in prev_names - subscribed:
            l3_sink.unsubscribe(stale)
            logger.info("同语言 observe 扩展 %s 已移出链,退订 L3 观测", stale)
    return subscribed


def register_inprocess_extension_identities(transport_bus: Any, registry: BranchRegistry) -> None:
    """同语言扩展身份注册(2026-09-08 端到端发现的生产缺口修复)。

    此前仅 supervisor 为 stdio 异语言扩展注册 TransportBus 身份;同语言扩展
    经 host_port 发起 storage_write/invoke_llm 时被"扩展未注册,拒绝入站请求"
    静默拒绝(audit._write 吞异常仅 warning,落盘全部丢失)。
    本函数为链上全部普通 Branch 实例注册身份(kind 前缀隔离/capability 授权
    的依据);stdio 扩展(RemoteBranchAdapter)仍由 supervisor._register_extension
    负责,此处跳过。create_app 装配后与 /reload rebuild 后均须调用。
    """
    from ..core.remote_adapter import RemoteBranchAdapter

    for branch, _cfg in registry.entries:
        if isinstance(branch, RemoteBranchAdapter):
            continue  # stdio 扩展身份由 supervisor 注册
        transport_bus.register_extension(branch.name, branch.capabilities or [])
        logger.info("同语言扩展 %s 身份已注册(kind 前缀=%s.*)", branch.name, branch.name)


def create_app(
    config_path: str = "config.yaml",
    config: Optional[Dict[str, Any]] = None,
) -> FastAPI:
    """装配应用。

    主干契约:配置开了但初始化失败(LLM API Key 缺失 / 存储打不开 /
    枝干 setup 失败 / 未知枝干名)→ 抛错,启动失败并给出可读错误 —— 不静默降级。
    """
    cfg = config if config is not None else load_config(config_path)
    config_path = cfg.get("_config_path", config_path)

    # 1. LLM 客户端(失败抛错 = 启动失败)
    llm_client = LLMClient(config_path=config_path, config=cfg)

    # 2. 历史存储(SQLite 默认;M1 独立库文件,避免与老库格式冲突)
    storage_cfg = cfg.get("storage", {}) or {}
    sqlite_path = storage_cfg.get("sqlite_path", "data2/sessions.db")
    history_store = SQLiteHistoryStore(sqlite_path)

    # 2.5 存储平台(D1):枝干通用落盘通道,与历史库同一文件(WAL 多连接安全)
    storage_provider = SQLiteStorageProvider(sqlite_path)

    # 2.6 StorageWriter 异步单写者(§18.1):全部 SQLite 写经单一写队列,
    #     根治同步落盘阻塞事件循环;user flush / 其余 background
    storage_writer = StorageWriter()

    # 3. registry 装配层(B1):配置驱动注册,顺序 = 配置顺序
    #    工厂由装配层注册(registry 不 import 任何枝干实现)
    # F1/F2:core 段严格校验(未知键/类型/范围,失败 = 启动失败)
    core_config = core_config_from(cfg)

    # 3.2 统一扩展目录树(2026-09-08):扫描 → 校验声明 → 合并 stdio 字段
    cfg, extension_specs, disabled_installed = wire_extensions(cfg)
    if disabled_installed:
        logger.info("已安装未启用扩展(安装 ≠ 激活): %s", ", ".join(disabled_installed))

    # 3.5 编排容器(E4/E7):TaskRegistry 后台任务 + SessionStore 会话态
    task_registry = TaskRegistry()
    session_store = SessionStore()

    # 3.6 协议桥(阶段 3,§9/§18.3):宿主侧消息枢纽 —— storage_provider 注入改走
    #     transport storage_* 消息通道(§15-A3 前缀隔离;扩展不得以对象引用访问宿主存储,
    #     §storage S-2 / §lifecycle L-8);invoke_llm / task_* 亦经此通道
    transport_bus = TransportBus(
        llm_client=llm_client,
        storage_provider=storage_provider,
        task_registry=task_registry,
        # §18.1 全部 SQLite 写经单一写队列:扩展 storage_write 亦走 StorageWriter
        # (FIFO 单写者,与主对话消息落盘同队列,§15-A6 队列满丢弃最旧+计数)
        storage_writer=storage_writer,
    )
    # setup(config, host) 的 host = 宿主能力声明(纯数据,§5 L-8):
    # 含 storage 通道与 kind 前缀,非对象引用 —— 扩展不得经对象引用访问宿主存储
    # 同语言扩展需访问宿主能力时经 registry 注入的 host_port(进程内消息通道)
    def build_host_declaration(extension_name: str) -> dict:
        return {
            "protocol_domains": [
                "types", "events", "hooks", "lifecycle", "storage",
                "config", "transport", "errors", "evolution",
            ],
            "storage": {
                "channel": "transport.storage_*",
                "kind_prefix": extension_name,
            },
        }

    # L3 观测批处理旁路(§18.5):50ms/64 条 + 每观测扩展有界队列(1024)
    l3_sink = L3BatchSink()
    event_stream = EventStream(l3_sink=l3_sink)
    # 扩展进程监管(§5):spawn/握手/心跳/热重载原子替换/僵死自动重建
    # host_builder 供重建时新 adapter setup(§5 B1);registry 绑定供链内原位替换
    supervisor = Supervisor(
        transport_bus=transport_bus,
        l3_sink=l3_sink,
        host_builder=build_host_declaration,
    )

    registry = BranchRegistry()
    # 生产扩展唯一装载通道 = extensions_root 目录发现(2026-09-08)。
    # register_factory 保留为测试/行为套件/编程式嵌入注入通道,生产路径零调用(设计 §2.1)
    registry.set_directory_loader(make_directory_loader(extension_specs))
    # 协议桥装配:transport 条目经 supervisor.launcher 创建(进程已 spawn);
    # 同语言扩展注入 host_port(进程内通道,§18.2 消息语义零成本)
    registry.set_extension_launcher(supervisor.launcher)
    registry.set_host_port_factory(transport_bus.make_in_process_port)
    supervisor.attach_registry(registry)  # 进程重建时链内原位替换(HookChain.replace)
    hooks = registry.build(cfg)  # 未知名枝干名 → ValueError 启动失败
    hooks.hook_timeout = core_config.hook_timeout  # 钩子超时接线(F)
    # 同语言扩展身份注册(TransportBus kind 前缀隔离/capability 授权依据,见上)
    register_inprocess_extension_identities(transport_bus, registry)
    # 同语言 observe 扩展的 L3 观测订阅(缺口修复 2026-08-21):stdio 异语言扩展
    # 由 supervisor 订阅;此处为普通 Branch 实例接线进程内投递(on_l3_events)。
    # 记录订阅名集合(热重载 /reload 时据此退订已移出的扩展)
    inprocess_l3_names = subscribe_inprocess_l3(l3_sink, hooks)

    # 4. 主干
    pipeline = ChatPipeline(
        llm_client=llm_client,
        history_store=history_store,
        hooks=hooks,
        mode=core_config.mode,
        max_loops=core_config.max_loops,
        base_system_prompt=core_config.system_prompt,
        injection_budget=core_config.injection_budget_chars,
        history_window_messages=core_config.history_window_messages,
        storage_writer=storage_writer,
        event_stream=event_stream,
        # §5 L-10 会话态 extra 会话内延续:构建恢复/结束写回 SessionStore
        session_store=session_store,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ① spawn 扩展进程(stdio 握手互报 protocol_version;失败抛 = 启动失败)
        await supervisor.launch_all(cfg)
        # ② setup 全部枝干/扩展(失败抛 = 启动失败;E1 逆序回滚)
        #    host = 纯数据宿主能力声明(kind 前缀按扩展名构造)
        await registry.setup_all(cfg, host_builder=build_host_declaration)
        logger.info("teage_liu2 启动完成: %d 个枝干注册", len(registry.entries))
        yield
        # L1 shutdown 幂等编排:①TaskRegistry ②teardown 逆序 ③④storage close
        # 扩展进程时序:先 registry.shutdown(经协议通知扩展 teardown,进程仍存活可
        # 处理)→ 再 supervisor 关闭进程(shutdown 帧 + 终止兜底)
        await registry.shutdown(
            task_registry=task_registry,
            message_store=history_store,
            storage_provider=storage_provider,
        )
        try:
            await supervisor.shutdown()
        except Exception as e:
            logger.warning("关闭扩展进程失败: %s", e)
        try:
            await l3_sink.close()
        except Exception as e:
            logger.warning("关闭 L3 旁路失败: %s", e)
        # StorageWriter 排空写队列后停止写线程(队列中残余写全部执行完)
        try:
            storage_writer.close()
        except Exception as e:
            logger.warning("关闭 StorageWriter 失败: %s", e)
        try:
            llm_client.close()
        except Exception as e:
            logger.warning("关闭 LLM 客户端失败: %s", e)

    # 5. FastAPI 实例
    app = FastAPI(
        title="Teage Liu 2",
        description="主干-枝干架构的个人 AI Agent(M1 纯对话内核)",
        version="0.1.0",
        lifespan=lifespan,
    )
    cors_origins = (cfg.get("server", {}) or {}).get(
        "cors_origins", ["http://localhost:3000"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if isinstance(cors_origins, list) else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 6. 挂载路由 + 状态
    app.include_router(router)
    # 极简前端(纯 HTML/CSS/JS,无框架):/ui 提供 teage_liu2/web/ 静态页面
    _web_dir = Path(__file__).resolve().parent.parent / "web"
    if _web_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=str(_web_dir), html=True), name="ui")
    # 会话并发互斥(§18.7/B3 根治):同 session 对话经 session 级 asyncio.Lock 串行化
    session_locks = SessionLocks()
    app.state.pipeline = pipeline
    app.state.llm_client = llm_client
    app.state.history_store = history_store
    app.state.storage_provider = storage_provider
    app.state.storage_writer = storage_writer
    app.state.registry = registry
    app.state.session_store = session_store
    app.state.task_registry = task_registry
    app.state.transport_bus = transport_bus
    app.state.supervisor = supervisor
    app.state.event_stream = event_stream
    app.state.l3_sink = l3_sink
    app.state.config_path = config_path
    app.state.build_host_declaration = build_host_declaration
    app.state.session_locks = session_locks
    # 同语言 L3 订阅函数(热重载 /reload 后重新接线用)+ 当前订阅名集合(退订依据)
    app.state.subscribe_inprocess_l3 = subscribe_inprocess_l3
    app.state.inprocess_l3_names = inprocess_l3_names

    return app
