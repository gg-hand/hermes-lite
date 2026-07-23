# src/lifespan.py（重构后）
"""FastAPI lifespan: 启动时初始化资源，关闭时释放。重构后通过 container.get() 触发工厂创建。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

VERSION = "0.1.0"
CONFIG_PATH = os.environ.get("TEAGE_CONFIG", "config.yaml")
SERVER_LOG_PATH = os.environ.get("TEAGE_SERVER_LOG", "data/server.log")

from teage_liu.logging_setup import logger  # noqa: E402
from teage_liu.background_loops import cleanup_loop, file_cleanup_loop, metrics_persist_loop  # noqa: E402
from teage_liu.background_task_registry import BackgroundTaskRegistry  # noqa: E402

# 核心组件导入
from teage_liu.config import (
    load_config,
    clear_config_cache,
    validate_required_env_vars,
)
from teage_liu.orchestrator import Orchestrator
from teage_liu.storage.chroma_store import _get_onnx_embedder
from teage_liu.skill.loader import (
    SkillLoader,
    load_skill_to_registry,
    register_skill_stub,
)
from teage_liu.mcp.client import MCPServerDef
from teage_liu.mcp.manager import MCPManager, register_mcp_tools_to_registry
from teage_liu.agent.cron_proposals import ProposalStore
from teage_liu.agent.cron_tools import register_cron_tools
from teage_liu.agent.cron_tool_registry import CronToolRegistry
from teage_liu.agent.cron_tool_writer import register_write_cron_tool
from teage_liu.agent.skill_tools import (
    _make_skill_activate_handler,
    register_skill_tools,
    _load_skill_state,
)
from teage_liu.tasks.scheduler import CronScheduler
from teage_liu.tasks.cron_expr import CronExpr
from teage_liu.agent.tools.file_tools import register_file_tools
from teage_liu.agent.tools.shell_tools import register_bash_tool
_CRON_TOOL_BASE_DIR = os.environ.get("TEAGE_CRON_TOOL_DIR", "cron_tool")

# 导入为无条件绝对导入，若失败模块本身无法加载，因此标志恒为 True
SKILL_MCP_AVAILABLE = True
CRON_AVAILABLE = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化资源，关闭时释放。"""
    config = load_config(CONFIG_PATH)
    config["_config_path"] = CONFIG_PATH

    # 1. 初始化容器 + 注册组件
    from teage_liu.app import init_container, register_components, get_container, close_container, register_exception_handlers
    init_container(config)
    container = get_container()
    register_components(container)

    # 1.5 multiagent 组件注册（仅 enabled=true 时）
    if config.get("multiagent", {}).get("enabled", False):
        from pathlib import Path as _Path
        from teage_liu.multiagent.schema_validator import SchemaValidator
        from teage_liu.multiagent.file_lock import LockManager
        from teage_liu.multiagent.audit_logger import MultiAgentAuditLogger
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.watchdog_watcher import WatchdogWatcher
        from teage_liu.multiagent.recovery import RecoveryCoordinator

        bb_root_str = config["multiagent"]["blackboard_dir"]
        # 解析环境变量占位符
        if bb_root_str.startswith("${") and bb_root_str.endswith("}"):
            env_var = bb_root_str[2:-1]
            bb_root_str = os.environ.get(env_var)
            if not bb_root_str:
                raise RuntimeError(
                    f"multiagent.enabled=true but {env_var} is not set. "
                    f"Please set {env_var} to the blackboard directory path."
                )
        bb_root = _Path(bb_root_str)
        bb_root.mkdir(parents=True, exist_ok=True)
        for sub in ("agents", "audit", "locks", "tasks", "schemas", "snapshots"):
            (bb_root / sub).mkdir(exist_ok=True)

        schema_val_enabled = config["multiagent"].get("schema_validation", True)
        container.register("blackboard", lambda c: bb_root, deps=[], hot_reloadable=False)
        container.register(
            "schema_validator",
            lambda c: SchemaValidator(enabled=schema_val_enabled),
            deps=[], hot_reloadable=True,
        )
        container.register("lock_manager", lambda c: LockManager(bb_root), deps=[], hot_reloadable=True)
        container.register(
            "multiagent_audit_logger",
            lambda c: MultiAgentAuditLogger(bb_root),
            deps=[], hot_reloadable=True,
        )
        container.register(
            "agent_registry",
            lambda c: AgentRegistry(bb_root, c.get("schema_validator")),
            deps=["schema_validator"], hot_reloadable=True,
        )
        container.register(
            "watchdog_watcher",
            lambda c: WatchdogWatcher(bb_root, lambda evt: None),  # callback 由后续集成注入
            deps=[], hot_reloadable=True,
        )
        container.register(
            "recovery_manager",
            lambda c: RecoveryCoordinator(bb_root, c.get("multiagent_audit_logger")),
            deps=["multiagent_audit_logger"], hot_reloadable=True,
        )

        # Plan 2 新增：multiagent_adapter（DirectorEngine 或 WorkerAdapter）
        role = config["multiagent"].get("role", "worker")
        if role == "director":
            from teage_liu.multiagent.director_engine import DirectorEngine
            director_cfg = config["multiagent"].get("director", {})
            director_agent_id = director_cfg.get("agent_id", "director_001")
            container.register(
                "multiagent_adapter",
                lambda c: DirectorEngine(
                    bb_root=bb_root,
                    config=config,
                    agent_id=director_agent_id,
                ),
                deps=[], hot_reloadable=True,
            )
        else:
            from teage_liu.multiagent.worker_adapter import WorkerAdapter
            worker_cfg = config["multiagent"].get("worker", {})
            worker_agent_id = worker_cfg.get("agent_id", "worker_001")
            container.register(
                "multiagent_adapter",
                lambda c: WorkerAdapter(
                    bb_root=bb_root,
                    config=config,
                    agent_id=worker_agent_id,
                    orchestrator=c.get("orchestrator"),
                ),
                deps=["orchestrator"], hot_reloadable=True,
            )

        # 启动时恢复检查
        recovery = container.get("recovery_manager")
        try:
            await recovery.check_and_recover()
        except Exception as e:
            logger.warning("multiagent 启动恢复检查失败: %s", e)

        # Plan 2 新增：启动 multiagent_adapter（DirectorEngine 或 WorkerAdapter）
        try:
            multiagent_adapter = container.get("multiagent_adapter")
            if multiagent_adapter is not None and hasattr(multiagent_adapter, "start"):
                await multiagent_adapter.start()
                logger.info(
                    "multiagent adapter 启动完成 (role=%s, agent_id=%s)",
                    role,
                    multiagent_adapter._agent_id,
                )
        except Exception as e:
            logger.error("multiagent adapter 启动失败: %s", e)

    # 1.6 A2A Gateway 路由注册（仅 a2a.enabled=True 时）
    a2a_cfg = config.get("a2a", {}) or {}
    if a2a_cfg.get("enabled"):
        try:
            a2a_router = container.get("a2a_router")
            if a2a_router is not None:
                app.include_router(a2a_router)
                logger.info("A2A Gateway 路由已注册")
        except Exception as e:
            logger.error("A2A Gateway 启动失败: %s", e)

    # 1.7 multiagent REST 路由注册（仅 multiagent.enabled=True 时）
    #    Plan 4 Task 5：从容器取出 multiagent_router 挂载到 FastAPI app。
    #    路由由 register_components 条件注册，此处仅负责挂载。
    multiagent_cfg_for_router = config.get("multiagent", {}) or {}
    if multiagent_cfg_for_router.get("enabled", False):
        try:
            multiagent_router = container.get("multiagent_router")
            if multiagent_router is not None:
                app.include_router(multiagent_router)
                logger.info("multiagent REST 路由已注册")
        except Exception as e:
            logger.error("multiagent 路由注册失败: %s", e)

    # 2. 触发工厂创建（无状态组件）
    session_logger = container.get("session_logger")
    metrics_collector = container.get("metrics_collector")
    metrics_store = container.get("metrics_store")
    audit_logger = container.get("audit_logger")
    approval_manager = container.get("approval_manager")
    task_manager = container.get("task_manager")
    upload_manager = container.get("upload_manager")

    # 3. 触发工厂创建（有状态组件）
    orchestrator = container.get("orchestrator")
    cron_scheduler = container.get("cron_scheduler")
    stream_manager = container.get("stream_manager")
    skill_loader = container.get("skill_loader")
    mcp_manager = container.get("mcp_manager")
    proposal_store = container.get("proposal_store")
    health_checker = container.get("health_checker")
    etl_engine = container.get("etl_engine")

    # 3.5 将核心组件同步到 app.state，供未迁移到 DI 的路由（如 cron_tools/runs/*）使用
    # 注意：新路由应优先使用 Depends(get_cron_scheduler) DI 注入，app.state 仅作向后兼容
    app.state.cron_scheduler = cron_scheduler
    app.state.orchestrator = orchestrator
    app.state.proposal_store = proposal_store

    # 4. 异步预热
    await _warmup_onnx()
    await _warmup_chromadb(orchestrator)
    await _init_mcp_servers(container, config, mcp_manager)
    _register_skill_tools(container, orchestrator, skill_loader)
    _register_cron_tools(container, orchestrator, cron_scheduler, proposal_store)
    _register_file_tools(orchestrator, etl_engine, upload_manager)

    # 5. 创建 asyncio.Event（替代 state.metrics_baseline_reset）
    app.state.metrics_reset_event = asyncio.Event()

    # 6. 启动后台 task（注册到 registry）
    task_registry = BackgroundTaskRegistry()
    task_registry.register("cleanup",
        asyncio.create_task(cleanup_loop(session_logger, metrics_store, orchestrator)))
    task_registry.register("file_cleanup",
        asyncio.create_task(file_cleanup_loop(upload_manager)))
    if metrics_store is not None and metrics_collector is not None:
        task_registry.register("metrics_persist",
            asyncio.create_task(metrics_persist_loop(
                metrics_collector, metrics_store, app.state.metrics_reset_event)))
    if cron_scheduler is not None and orchestrator is not None:
        schedules_cfg = config.get("schedules", []) or []
        if schedules_cfg:
            cron_scheduler.load_from_config(schedules_cfg)
        task_registry.register("cron",
            asyncio.create_task(cron_scheduler.run_loop(orchestrator)))
    app.state.task_registry = task_registry

    # 7. 注入 cron 依赖到 PolicyEngine
    if cron_scheduler is not None and orchestrator is not None and orchestrator.policy_engine is not None:
        try:
            orchestrator.policy_engine.set_cron_scheduler(cron_scheduler)
        except Exception as e:
            logger.warning("注入 cron_scheduler 到 PolicyEngine 失败: %s", e)

    # 8. 注册异常处理器
    register_exception_handlers(app)

    logger.info("lifespan 启动完成")

    yield

    # 9. 关闭
    logger.info("lifespan 开始关闭")
    await task_registry.cancel_all()

    # Plan 2 新增：关闭 multiagent_adapter（DirectorEngine 或 WorkerAdapter）
    multiagent_cfg = config.get("multiagent", {}) or {}
    if multiagent_cfg.get("enabled", False):
        try:
            multiagent_adapter = container.get("multiagent_adapter")
            if multiagent_adapter is not None and hasattr(multiagent_adapter, "stop"):
                await multiagent_adapter.stop()
                logger.info("multiagent adapter 已停止")
        except Exception as e:
            logger.warning("multiagent adapter 关闭失败: %s", e)

        # 清理 Director 进程
        try:
            from teage_liu.multiagent.director_manager import create_director_manager
            dm = create_director_manager(config)
            result = await dm.stop()
            if result.get("ok"):
                logger.info("Director 进程已清理: %s", result.get("message"))
        except Exception as e:
            logger.warning("Director 清理失败: %s", e)

    close_container()
    logger.info("lifespan 关闭完成")


async def _warmup_onnx():
    """ONNX 嵌入模型预加载。"""
    try:
        _get_onnx_embedder()
        logger.info("ONNX 嵌入模型预加载完成")
    except Exception as e:
        logger.warning("ONNX 预加载失败: %s", e)


async def _warmup_chromadb(orchestrator):
    """ChromaDB 向量索引预热。"""
    if orchestrator is None or orchestrator.chroma_store is None:
        return
    try:
        orchestrator.chroma_store.query_memory("warmup", top_k=1, reinforce=False)
        logger.info("ChromaDB 预热完成")
    except Exception as e:
        logger.warning("ChromaDB 预热失败: %s", e)


async def _init_mcp_servers(container, config, mcp_manager):
    """MCP server 连接 + 工具注册。"""
    if not SKILL_MCP_AVAILABLE or mcp_manager is None:
        return
    try:
        skills_cfg = config.get("skills", {}) or {}
        mcp_servers = skills_cfg.get("mcp_servers", []) or []
        for server_def in mcp_servers:
            try:
                await mcp_manager.add_server(MCPServerDef(**server_def))
            except Exception as e:
                logger.warning("MCP server 连接失败: %s", e)
        if orchestrator := container.get("orchestrator"):
            for server_name in mcp_manager.list_servers():
                try:
                    count = register_mcp_tools_to_registry(
                        orchestrator.tool_registry, mcp_manager, server_name
                    )
                    logger.info("MCP Server '%s' 注册了 %d 个工具", server_name, count)
                except Exception as e:
                    logger.warning("MCP Server '%s' 工具注册失败: %s", server_name, e)
        logger.info("MCP 服务器初始化完成")
    except Exception as e:
        logger.error("MCP 初始化失败: %s", e)


def _register_skill_tools(container, orchestrator, skill_loader):
    """Skill stub 注册 + 管理工具注册 + 状态恢复。"""
    if not SKILL_MCP_AVAILABLE or skill_loader is None or orchestrator is None:
        return
    try:
        discovered = skill_loader.discover()
        for meta in discovered:
            handler = _make_skill_activate_handler(skill_loader, orchestrator, meta.name)
            register_skill_stub(orchestrator.tool_registry, meta, handler)
        register_skill_tools(orchestrator.tool_registry, skill_loader, orchestrator)
        skill_state = _load_skill_state()
        for skill_name in skill_state.get("disabled", []):
            try:
                orchestrator.tool_registry.disable_skill(skill_name)
            except Exception:
                pass
        logger.info("Skill 工具注册完成: %d 个", len(discovered))
    except Exception as e:
        logger.warning("Skill 注册失败: %s", e)


def _register_cron_tools(container, orchestrator, cron_scheduler, proposal_store):
    """cron 工具 + cron_tool_registry 注册。"""
    if not CRON_AVAILABLE or orchestrator is None:
        return
    try:
        if (register_cron_tools and cron_scheduler and proposal_store
                and orchestrator.tool_registry):
            register_cron_tools(orchestrator.tool_registry, cron_scheduler, proposal_store)

        cron_tool_registry = None
        if CronToolRegistry:
            cron_tool_registry = CronToolRegistry(base_dir=_CRON_TOOL_BASE_DIR)
            cron_tool_registry.load_all()

        if register_write_cron_tool and orchestrator.tool_registry:
            register_write_cron_tool(orchestrator.tool_registry, base_dir=_CRON_TOOL_BASE_DIR)

        if register_bash_tool and orchestrator.tool_registry:
            bash_timeout = int(load_config(CONFIG_PATH).get("tools", {}).get("bash_timeout", 120))
            register_bash_tool(orchestrator.tool_registry, timeout=bash_timeout)

        if orchestrator.cron_isolator:
            orchestrator.cron_isolator.set_dependencies(
                cron_scheduler=cron_scheduler,
                cron_tool_registry=cron_tool_registry,
            )
        logger.info("cron 工具注册完成")
    except Exception as e:
        logger.error("cron 工具注册失败: %s", e)


def _register_file_tools(orchestrator, etl_engine, upload_manager):
    """file_tools 注册。"""
    if not CRON_AVAILABLE or orchestrator is None:
        return
    try:
        if register_file_tools and orchestrator.tool_registry and etl_engine:
            register_file_tools(
                orchestrator.tool_registry,
                etl_engine,
                upload_manager,
                get_session_id=lambda: getattr(orchestrator, "_current_session_id", None),
            )
            logger.info("文件工具已注册")
    except Exception as e:
        logger.error("文件工具注册失败: %s", e)
