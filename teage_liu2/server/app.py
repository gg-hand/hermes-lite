"""外壳:FastAPI 应用装配。

依赖铁律:运行时逻辑(路由/传输)只依赖 core,不感知 branches;
装配点例外:composition root 需注册枝干工厂(见下方 import 注释)。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..core.config import core_config_from, load_config
from ..core.event_stream import EventStream, L3BatchSink
from ..core.history import SQLiteHistoryStore
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

# 装配点例外(composition root):注册枝干工厂必须感知枝干类;
# 运行时逻辑(路由/传输)不感知枝干 —— 依赖铁律对 core 与运行时保持。
from ..branches.audit import AuditBranch  # noqa: E402
from ..branches.guardrails import GuardrailsBranch  # noqa: E402

logger = logging.getLogger(__name__)


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
    registry.register_factory("guardrails", lambda cfg: GuardrailsBranch(cfg))
    # 首个实验性扩展(2026-08-21):audit 观测枝干(observe 只读,经 host_port 落盘)
    registry.register_factory("audit", lambda cfg: AuditBranch(cfg))
    # 协议桥装配:transport 条目经 supervisor.launcher 创建(进程已 spawn);
    # 同语言扩展注入 host_port(进程内通道,§18.2 消息语义零成本)
    registry.set_extension_launcher(supervisor.launcher)
    registry.set_host_port_factory(transport_bus.make_in_process_port)
    supervisor.attach_registry(registry)  # 进程重建时链内原位替换(HookChain.replace)
    hooks = registry.build(cfg)  # 未知名枝干名 → ValueError 启动失败
    hooks.hook_timeout = core_config.hook_timeout  # 钩子超时接线(F)

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

    return app
