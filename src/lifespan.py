"""FastAPI lifespan: 启动时初始化资源，关闭时释放。

从 server.py 提取。全局组件变量保留在 server.py（因 40 个测试 patch
src.server.orchestrator），lifespan 函数内部通过 ``import server; server.xxx = value``
设置全局变量。state.py 代理到 server 模块，routes 通过 state.xxx 访问。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI

# ---------- 常量 ----------
VERSION = "0.1.0"
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")
SERVER_LOG_PATH = os.environ.get("HERMES_SERVER_LOG", "data/server.log")

# ---------- 日志 ----------
from logging_setup import logger  # noqa: E402

# ---------- 后台循环 ----------
from background_loops import cleanup_loop, file_cleanup_loop, metrics_persist_loop  # noqa: E402

# ---------- 核心组件导入（双层 try/except 兼容包内运行与直接运行） ----------
try:
    from .config import clear_config_cache, get_llm_timeouts, load_config, validate_required_env_vars
    from .orchestrator import Orchestrator
    from .storage.sqlite_log import SessionLogger
    from .storage.chroma_store import _get_onnx_embedder
    from .monitoring.metrics import MetricsCollector
    from .monitoring.metrics_store import MetricsStore, compute_delta
    from .monitoring.health import HealthChecker
    from .agent.audit import AuditLogger
except ImportError:  # pragma: no cover
    from config import clear_config_cache, get_llm_timeouts, load_config, validate_required_env_vars  # type: ignore
    from orchestrator import Orchestrator  # type: ignore
    from storage.sqlite_log import SessionLogger  # type: ignore
    from storage.chroma_store import _get_onnx_embedder  # type: ignore
    from monitoring.metrics import MetricsCollector  # type: ignore
    from monitoring.metrics_store import MetricsStore, compute_delta  # type: ignore
    from monitoring.health import HealthChecker  # type: ignore
    from agent.audit import AuditLogger  # type: ignore

# Phase 4: Skill/MCP 扩展模块
try:
    from .skill.loader import SkillLoader, load_skill_to_registry, register_skill_stub
    from .mcp.client import MCPServerDef
    from .mcp.manager import MCPManager, register_mcp_tools_to_registry
    SKILL_MCP_AVAILABLE = True
except ImportError:
    try:
        from skill.loader import SkillLoader, load_skill_to_registry, register_skill_stub  # type: ignore
        from mcp.client import MCPServerDef  # type: ignore
        from mcp.manager import MCPManager, register_mcp_tools_to_registry  # type: ignore
        SKILL_MCP_AVAILABLE = True
    except ImportError as e:
        logger.warning(f"Skill/MCP 模块加载失败，扩展能力禁用: {e}")
        SkillLoader = None  # type: ignore
        load_skill_to_registry = None  # type: ignore
        register_skill_stub = None  # type: ignore
        MCPServerDef = None  # type: ignore
        MCPManager = None  # type: ignore
        register_mcp_tools_to_registry = None  # type: ignore
        SKILL_MCP_AVAILABLE = False

# Phase 5: 安全权限 + HIL 审批
try:
    from .agent.approval import ApprovalManager
except ImportError:
    try:
        from agent.approval import ApprovalManager  # type: ignore
    except ImportError:
        ApprovalManager = None  # type: ignore

# Phase 6: 任务编排模块
try:
    from .tasks.task_manager import TaskManager
    from .tasks.scheduler import CronScheduler
    from .tasks.cron_expr import CronExpr
except ImportError:
    try:
        from tasks.task_manager import TaskManager  # type: ignore
        from tasks.scheduler import CronScheduler  # type: ignore
        from tasks.cron_expr import CronExpr  # type: ignore
    except ImportError:
        TaskManager = None  # type: ignore
        CronScheduler = None  # type: ignore
        CronExpr = None  # type: ignore

# Phase 8 Task 3: cron 工具集 + 提议-确认协议
try:
    from .agent.cron_proposals import ProposalStore
    from .agent.cron_tools import register_cron_tools
except ImportError:
    try:
        from agent.cron_proposals import ProposalStore  # type: ignore
        from agent.cron_tools import register_cron_tools  # type: ignore
    except ImportError:
        ProposalStore = None  # type: ignore
        register_cron_tools = None  # type: ignore

# 中断管理器 + 断点检测器
try:
    from .stream_manager import StreamManager, StreamCancelled
    from .breakpoint_detector import BreakpointDetector
except ImportError:
    from stream_manager import StreamManager, StreamCancelled  # type: ignore
    from breakpoint_detector import BreakpointDetector  # type: ignore

# 异步 LLM Backend: ActivityTimeout 异常
try:
    from .llm.client import ActivityTimeout
except ImportError:
    from llm.client import ActivityTimeout  # type: ignore

try:
    from .agent.cron_tool_registry import CronToolRegistry
    from .agent.cron_tool_writer import register_write_cron_tool
    from .agent.tools.shell_tools import register_bash_tool
    from .tasks.cron_tool_loader import (
        CronToolError,
        DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR,
        list_pending_tools as _list_pending_cron_tools,
        list_tools as _list_active_cron_tools,
        load_tool as _load_cron_tool,
    )
except ImportError:
    try:
        from agent.cron_tool_registry import CronToolRegistry  # type: ignore
        from agent.cron_tool_writer import register_write_cron_tool  # type: ignore
        from agent.tools.shell_tools import register_bash_tool  # type: ignore
        from tasks.cron_tool_loader import (
            CronToolError,
            DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR,
            list_pending_tools as _list_pending_cron_tools,
            list_tools as _list_active_cron_tools,
            load_tool as _load_cron_tool,
        )
    except ImportError:
        CronToolRegistry = None  # type: ignore
        register_write_cron_tool = None  # type: ignore
        register_bash_tool = None  # type: ignore
        CronToolError = Exception  # type: ignore
        _CRON_TOOL_BASE_DIR = "cron_tool"  # type: ignore
        _list_pending_cron_tools = None  # type: ignore
        _list_active_cron_tools = None  # type: ignore
        _load_cron_tool = None  # type: ignore

# Phase 8 Task 6: Skill 工具管理
try:
    from .agent.skill_tools import register_skill_tools, _make_skill_activate_handler
    SKILL_TOOLS_AVAILABLE = True
except ImportError:
    try:
        from agent.skill_tools import register_skill_tools, _make_skill_activate_handler
        SKILL_TOOLS_AVAILABLE = True
    except ImportError:
        SKILL_TOOLS_AVAILABLE = False
        _make_skill_activate_handler = None  # type: ignore

# 文件上传与 ETL 模块
try:
    from .files.upload_manager import UploadManager
    from .files.parser import WaterfallParser
    from .files.chunker import DocumentChunker
    from .files.etl_engine import ETLEngine
    from .files.context_injector import FileContextInjector
    FILE_MODULE_AVAILABLE = True
except ImportError:
    try:
        from files.upload_manager import UploadManager  # type: ignore
        from files.parser import WaterfallParser  # type: ignore
        from files.chunker import DocumentChunker  # type: ignore
        from files.etl_engine import ETLEngine  # type: ignore
        from files.context_injector import FileContextInjector  # type: ignore
        FILE_MODULE_AVAILABLE = True
    except ImportError:
        UploadManager = None  # type: ignore
        WaterfallParser = None  # type: ignore
        DocumentChunker = None  # type: ignore
        ETLEngine = None  # type: ignore
        FileContextInjector = None  # type: ignore
        FILE_MODULE_AVAILABLE = False


# ---------- Skill 管理状态 ----------

SKILL_STATE_PATH = "data/skills_state.json"
_skill_state_lock = threading.Lock()


def _load_skill_state() -> dict:
    """从 JSON 文件加载 Skill 状态（禁用/锁定清单）。"""
    try:
        if os.path.exists(SKILL_STATE_PATH):
            with open(SKILL_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("加载 Skill 状态失败: %s", e)
    return {"disabled": [], "locked": []}


def _save_skill_state(state: dict) -> None:
    """持久化 Skill 状态到 JSON 文件。"""
    try:
        os.makedirs(os.path.dirname(SKILL_STATE_PATH), exist_ok=True)
        with open(SKILL_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存 Skill 状态失败: %s", e)


# ---------- 生命周期管理 ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时初始化资源，关闭时释放。

    使用 lifespan（而非已废弃的 on_event），由 FastAPI 在应用启停时调用。
    """
    # 局部变量初始化（原 server.py 全局变量，lifespan 结束前同步到 server 模块）
    orchestrator: Optional[Any] = None
    session_logger: Optional[Any] = None
    metrics_collector: Optional[Any] = None
    metrics_store: Optional[Any] = None
    audit_logger: Optional[Any] = None
    approval_manager: Optional[Any] = None
    task_manager: Optional[Any] = None
    cron_scheduler: Optional[Any] = None
    proposal_store: Optional[Any] = None
    cron_tool_registry: Optional[Any] = None
    skill_loader = None
    mcp_manager = None
    skill_tools_registered = False
    upload_manager: Optional[Any] = None
    etl_engine: Optional[Any] = None
    file_context_injector: Optional[Any] = None
    stream_manager: Optional[Any] = None
    health_checker: Optional[Any] = None


    # Phase 6: cron_task 在 startup 段赋值，shutdown 段引用；
    # 预初始化为 None 避免 CronScheduler 启动失败时 shutdown 抛 NameError
    cron_task = None
    metrics_persist_task = None
    file_cleanup_task = None
    cleanup_task = None

    logger.info("正在加载配置: %s", CONFIG_PATH)
    config = load_config(CONFIG_PATH)

    # Phase X: 校验关键环境变量（API Key 等）是否已正确配置
    try:
        validate_required_env_vars(config)
    except ValueError as e:
        logger.critical("配置校验失败: %s", e)
        raise RuntimeError(str(e)) from e

    # 预加载 ONNX 嵌入模型（all-MiniLM-L6-v2），避免首次对话时
    # 临时下载模型权重导致 20+ 秒等待。模型约 80MB，首次启动时
    # 自动下载到 ~/.cache/chroma/onnx_models/，后续启动直接加载。
    # 注意：_get_onnx_embedder() 只初始化包装类，不会触发模型下载；
    # 必须实际调用一次嵌入才能触发 _download_model_if_not_exists。
    try:
        logger.info("正在预加载 ONNX 嵌入模型...")
        model = _get_onnx_embedder()
        model(["warmup"])  # 触发模型下载/解压
        logger.info("ONNX 嵌入模型已就绪")
    except Exception as e:
        logger.warning("ONNX 嵌入模型预加载失败（首次使用时将按需加载）: %s", e)

    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8000)

    # 初始化会话日志（独立实例，供会话管理接口直接使用）
    storage_cfg = config.get("storage", {})
    sqlite_path = storage_cfg.get("sqlite_path", "data/sessions.db")
    session_logger = SessionLogger(db_path=sqlite_path)
    logger.info("SessionLogger 已初始化: %s", sqlite_path)

    # 初始化监控组件（若 monitoring.enabled 为 True）
    monitoring_cfg = config.get("monitoring", {})
    monitoring_enabled = monitoring_cfg.get("enabled", True)
    if monitoring_enabled:
        metrics_collector = MetricsCollector()
        if monitoring_cfg.get("daily_persistence", True):
            try:
                metrics_store = MetricsStore(db_path=sqlite_path)
                logger.info("MetricsStore 已初始化: %s", sqlite_path)
            except Exception as e:
                logger.warning("MetricsStore 初始化失败，按天持久化降级: %s", e)
                metrics_store = None
        audit_log_path = monitoring_cfg.get("audit_log_path", "data/audit.jsonl")
        audit_buffer_size = int(monitoring_cfg.get("audit_buffer_size", 1000))
        audit_logger = AuditLogger(
            log_path=audit_log_path,
            buffer_size=audit_buffer_size,
        )
        logger.info("监控组件已初始化: metrics=on, audit=%s", audit_log_path)

    # Phase 5: 初始化共享 ApprovalManager（供 ReactLoop 与 /approvals 端点共用）
    if ApprovalManager is not None:
        try:
            security_cfg = config.get("security", {})
            approval_timeout = float(security_cfg.get("approval_timeout_seconds", 300))
            approval_manager = ApprovalManager(
                timeout=approval_timeout,
                metrics=metrics_collector,
            )
            logger.info("ApprovalManager 已初始化: timeout=%ss", approval_timeout)
        except Exception as e:
            logger.warning("ApprovalManager 初始化失败: %s", e)

    # Phase 6: 初始化共享 TaskManager（供 Orchestrator 与 /tasks 端点共用）
    if TaskManager is not None:
        try:
            tasks_cfg = config.get("tasks", {})
            task_manager = TaskManager(
                file_path=tasks_cfg.get("file_path", "data/tasks.md")
            )
            logger.info("TaskManager 已初始化: %s", tasks_cfg.get("file_path", "data/tasks.md"))
        except Exception as e:
            logger.warning("TaskManager 初始化失败: %s", e)

    # 初始化编排器（传入 metrics 与 audit_logger）
    orchestrator = Orchestrator(
        config_path=CONFIG_PATH,
        metrics=metrics_collector,
        audit_logger=audit_logger,
        approval_manager=approval_manager,
        task_manager=task_manager,
    )
    logger.info("Orchestrator 已初始化")

    # 预热 ChromaDB 向量索引：做一次哑查询加载 HNSW 索引到内存，
    # 避免重启后首次对话因索引加载耗时 20s+。
    if orchestrator is not None and orchestrator.chroma_store is not None:
        try:
            logger.info("正在预热 ChromaDB 向量索引...")
            orchestrator.chroma_store.query_memory(
                "warmup", top_k=1, reinforce=False
            )
            logger.info("ChromaDB 向量索引已就绪")
        except Exception as e:
            logger.warning("ChromaDB 预热失败（首次对话时将按需加载）: %s", e)

    # 初始化流中断管理器
    stream_manager = StreamManager()
    logger.info("StreamManager 已初始化")
    logger.info(
        "Hermes Lite HTTP 服务启动中: http://%s:%s (version=%s)", host, port, VERSION
    )

    # Phase 4: 加载 Skill 与 MCP 扩展（失败降级为纯内置工具模式）
    if SKILL_MCP_AVAILABLE and orchestrator is not None and orchestrator.tool_registry is not None:
        try:
            # 1. 初始化 SkillLoader 并发现本地 Skill
            skill_loader = SkillLoader()
            discovered = skill_loader.discover()
            # 供 orchestrator._build_active_skills_section 使用（L2 body 注入）
            orchestrator.skill_loader = skill_loader
            logger.info(
                "已发现 %d 个本地 Skill: %s",
                len(discovered),
                [m.name for m in discovered],
            )

            # 2. 为每个 discovered skill 注册激活按钮到 Core Tier
            #    （对齐 agentskills.io: 不再 importlib 加载 tools.py，改为 L1 stub + L2 body 按需注入）
            skills_cfg = config.get("skills", {}) or {}
            if _make_skill_activate_handler is not None and register_skill_stub is not None:
                for meta in discovered:
                    try:
                        handler = _make_skill_activate_handler(
                            skill_loader, orchestrator, meta.name
                        )
                        register_skill_stub(orchestrator.tool_registry, meta, handler)
                        logger.info("已注册 Skill 激活按钮: %s", meta.name)
                    except Exception as e:
                        logger.error("Skill '%s' 注册失败: %s", meta.name, e)
            else:
                logger.warning("register_skill_stub / _make_skill_activate_handler 不可用，跳过 Skill stub 注册")

            # 3. 连接 MCP Server 并注册工具到 registry 的 Core Tier
            mcp_manager = MCPManager()
            for mcp_def in skills_cfg.get("mcp", []) or []:
                try:
                    server_def = MCPServerDef(**mcp_def)
                    connected = await mcp_manager.add_server(server_def)
                    if connected:
                        count = register_mcp_tools_to_registry(
                            registry=orchestrator.tool_registry,
                            mcp_manager=mcp_manager,
                            server_name=server_def.name,
                            hil=mcp_def.get("hil", True),
                            advertise_threshold=skills_cfg.get("mcp_advertise_threshold", 30),
                        )
                        logger.info(
                            "已连接 MCP Server: %s（%d 个工具，hil=%s）",
                            server_def.name,
                            count,
                            mcp_def.get("hil", True),
                        )
                    # add_server 失败时已由 MCPManager 记录 error 日志
                except Exception as e:
                    logger.error(
                        "MCP Server 配置无效 '%s': %s",
                        mcp_def.get("name", "?"),
                        e,
                    )

            # 4. 构建 MCP HIL 配置并注入 PolicyEngine（支持热更新）
            mcp_hil_config = {
                m.get("name", ""): m.get("hil", True)
                for m in (skills_cfg.get("mcp", []) or [])
            }
            if (
                hasattr(orchestrator, "policy_engine")
                and orchestrator.policy_engine is not None
                and mcp_hil_config
            ):
                orchestrator.policy_engine.set_mcp_hil_config(mcp_hil_config)
                logger.info("已注入 MCP HIL 配置: %s", mcp_hil_config)
        except Exception as e:
            logger.error("Skill/MCP 扩展加载失败（整体降级）: %s", e)
            # 不抛异常，服务继续启动（纯内置工具模式）

    # Phase 8 Task 6: 注册 Skill 管理工具（register_skill_tools）
    if SKILL_TOOLS_AVAILABLE and orchestrator is not None and orchestrator.tool_registry is not None:
        try:
            register_skill_tools(orchestrator.tool_registry, skill_loader, orchestrator)
            skill_tools_registered = True
            logger.info("Skill 管理工具已注册（Core Tier）")
        except Exception as e:
            logger.error("注册 Skill 管理工具失败: %s", e)

    # 从持久化状态恢复 Skill 的 disabled 标记
    # 启动期 discover 循环已为所有 skill 注册 stub（保持 schema 稳定），
    # 此处对 disabled 列表中的 skill 调 disable_skill 实现软禁用：
    # schema 标 enabled: False，工具仍在 registry 但执行抛 ToolNotFoundError。
    if skill_loader is not None and orchestrator is not None and orchestrator.tool_registry is not None:
        try:
            skill_state = _load_skill_state()
            for skill_name in skill_state.get("disabled", []):
                try:
                    orchestrator.tool_registry.disable_skill(skill_name)
                    logger.info(
                        "Skill 已注册 stub 并标记为 disabled: %s", skill_name
                    )
                except Exception as e:
                    logger.warning(
                        "Skill %s 标记 disabled 失败: %s", skill_name, e
                    )
        except Exception as e:
            logger.warning("恢复 Skill 状态失败: %s", e)

    # 文件上传与 ETL 管道初始化
    if FILE_MODULE_AVAILABLE and orchestrator is not None:
        try:
            files_cfg = config.get("files", {}) or {}
            upload_dir = files_cfg.get("upload_dir", "data/uploads")
            max_upload_size_mb = float(files_cfg.get("max_upload_size_mb", 50))
            max_files_per_session = int(files_cfg.get("max_files_per_session", 50))
            allowed_extensions = files_cfg.get("allowed_extensions", None)
            ocr_enabled = bool(files_cfg.get("ocr_enabled", False))
            chunk_size = int(files_cfg.get("chunk_size", 512))
            chunk_overlap = int(files_cfg.get("chunk_overlap", 64))

            # UploadManager
            sqlite_path = storage_cfg.get("sqlite_path", "data/sessions.db")
            upload_manager = UploadManager(
                db_path=sqlite_path,
                upload_dir=upload_dir,
                max_upload_size_mb=max_upload_size_mb,
                max_files_per_session=max_files_per_session,
                allowed_extensions=allowed_extensions,
                ocr_enabled=ocr_enabled,
            )
            logger.info("UploadManager 已初始化: %s (max=%dMB)", upload_dir, max_upload_size_mb)

            # WaterfallParser
            llm_fallback = bool(files_cfg.get("llm_fallback_enabled", False))
            parse_timeouts = files_cfg.get("parse_timeout_seconds", {})
            ocr_cfg = files_cfg.get("ocr", {}) or {}
            water_parser = WaterfallParser(
                llm_fallback_enabled=llm_fallback,
                llm_client=orchestrator.llm_client if llm_fallback else None,
                parse_timeouts=parse_timeouts,
                ocr_config=ocr_cfg,
                metrics_collector=metrics_collector,
            )
            logger.info("WaterfallParser 已初始化 (llm_fallback=%s)", llm_fallback)

            # DocumentChunker
            doc_chunker = DocumentChunker(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            logger.info("DocumentChunker 已初始化 (chunk_size=%d, overlap=%d)", chunk_size, chunk_overlap)

            # ETLEngine
            if (
                orchestrator.chroma_store is not None
                and session_logger is not None
            ):
                etl_engine = ETLEngine(
                    upload_manager=upload_manager,
                    chroma_store=orchestrator.chroma_store,
                    session_logger=session_logger,
                    parser=water_parser,
                    chunker=doc_chunker,
                    llm_client=orchestrator.llm_client,
                    config=files_cfg,
                )
                logger.info("ETLEngine 已初始化")

                # FileContextInjector
                max_injected = int(files_cfg.get("max_injected_files", 5))
                max_tokens = int(files_cfg.get("max_file_injection_tokens", 1000))
                file_context_injector = FileContextInjector(
                    upload_manager=upload_manager,
                    max_files=max_injected,
                    max_tokens=max_tokens,
                )
                # 注入到 ContextManager
                if orchestrator.context_manager is not None:
                    orchestrator.context_manager.file_context_injector = file_context_injector
                logger.info("FileContextInjector 已初始化 (max_files=%d, max_tokens=%d)", max_injected, max_tokens)

                # 注册文件工具到 ToolRegistry
                if orchestrator.tool_registry is not None:
                    try:
                        from .agent.tools.file_tools import register_file_tools
                    except ImportError:
                        from agent.tools.file_tools import register_file_tools  # type: ignore
                    register_file_tools(
                        orchestrator.tool_registry,
                        etl_engine,
                        upload_manager,
                        get_session_id=lambda: getattr(orchestrator, "_current_session_id", None),
                    )
                    logger.info("文件工具已注册: file_query / file_list_uploads / file_read_uploaded")

        except Exception as e:
            logger.error("文件模块初始化失败: %s", e)
            upload_manager = None
            etl_engine = None
            file_context_injector = None

    # 启动文件清理后台任务
    file_cleanup_task = asyncio.create_task(file_cleanup_loop())

    # 启动定时清理古早会话的后台任务
    cleanup_task = asyncio.create_task(cleanup_loop())

    # 启动监控指标按天持久化后台任务
    if metrics_store is not None and metrics_collector is not None:
        def _log_persist_task_exception(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error("metrics_persist_task 异常退出: %r", exc, exc_info=exc)
        metrics_persist_task = asyncio.create_task(metrics_persist_loop())
        metrics_persist_task.add_done_callback(_log_persist_task_exception)
        logger.info("监控指标持久化任务已启动")

    # Phase 6: 启动 CronScheduler 后台调度循环
    if CronScheduler is not None and orchestrator is not None:
        try:
            schedules_cfg = config.get("schedules", []) or []
            cron_scheduler = CronScheduler()
            if schedules_cfg:
                cron_scheduler.load_from_config(schedules_cfg)
                logger.info("CronScheduler 已加载 %d 个调度项", len(schedules_cfg))
            cron_task = asyncio.create_task(cron_scheduler.run_loop(orchestrator))
            logger.info("CronScheduler 调度循环已启动")
        except Exception as e:
            logger.error("CronScheduler 启动失败: %s", e)

    # Phase 8 Task 4.2: 注入 cron_scheduler 到 PolicyEngine，启用 cron 路径预授权检查
    # PolicyEngine 在 Orchestrator 构造时创建（早于 CronScheduler），此处补注入。
    if (
        cron_scheduler is not None
        and orchestrator is not None
        and orchestrator.policy_engine is not None
    ):
        try:
            orchestrator.policy_engine.set_cron_scheduler(cron_scheduler)
            logger.info("已注入 cron_scheduler 到 PolicyEngine（cron 预授权检查启用）")
        except Exception as e:
            logger.warning("注入 cron_scheduler 到 PolicyEngine 失败: %s", e)

    # Phase 8 Task 3: 初始化 ProposalStore + 注册 4 个 cron 工具到全局 ToolRegistry
    # proposal_store 为纯内存存储，重启后清空（spec 要求无持久化）。
    # 4 个 cron 工具（list_schedules / propose_schedule / create_schedule /
    # update_schedule）注册到全局 ToolRegistry 供用户会话使用，不涉及 cron
    # 执行会话的 tools schema，不污染 cron 缓存（缓存约束 1）。
    if ProposalStore is not None:
        proposal_store = ProposalStore()
        logger.info("ProposalStore 已初始化（纯内存，重启后清空）")
    if (
        register_cron_tools is not None
        and orchestrator is not None
        and orchestrator.tool_registry is not None
        and cron_scheduler is not None
        and proposal_store is not None
    ):
        try:
            register_cron_tools(
                orchestrator.tool_registry,
                cron_scheduler,
                proposal_store,
            )
            logger.info("已注册 4 个 cron 工具到全局 ToolRegistry（用户会话可用）")
        except Exception as e:
            logger.error("注册 cron 工具失败: %s", e)

    # Phase 8 Task 5: 初始化 CronToolRegistry（独立 registry，不进全局 ToolRegistry）
    # + 注册 write_cron_tool 工具到全局 registry（用户会话可用）
    # 缓存约束 1：cron_tool 仅在 cron 调度会话内可见，用户会话 tools schema 字节级不变
    if CronToolRegistry is not None:
        try:
            cron_tool_registry = CronToolRegistry(
                base_dir=_CRON_TOOL_BASE_DIR
            )
            # 启动时批量加载已激活的 cron_tool
            loaded = cron_tool_registry.load_all()
            if loaded:
                logger.info(
                    "CronToolRegistry 已初始化，加载 %d 个已激活 cron_tool: %s",
                    len(loaded),
                    list(loaded.keys()),
                )
            else:
                logger.info("CronToolRegistry 已初始化（无已激活 cron_tool）")
        except Exception as e:
            logger.error("CronToolRegistry 初始化失败: %s", e)
            cron_tool_registry = None

    if (
        register_write_cron_tool is not None
        and orchestrator is not None
        and orchestrator.tool_registry is not None
    ):
        try:
            register_write_cron_tool(
                orchestrator.tool_registry,
                base_dir=_CRON_TOOL_BASE_DIR,
            )
            logger.info(
                "已注册 cron_tool_create 工具到全局 ToolRegistry（用户会话可用）"
            )
        except Exception as e:
            logger.error("注册 cron_tool_create 工具失败: %s", e)

    # bash_exec 最后注册，排在工具列表末尾
    if orchestrator is not None and orchestrator.tool_registry is not None:
        try:
            bash_timeout = int(config.get("tools", {}).get("bash_timeout", 120))
            register_bash_tool(orchestrator.tool_registry, timeout=bash_timeout)
        except Exception as e:
            logger.error("注册 bash_exec 工具失败: %s", e)

    # Phase 8 Task 5.7: 注入 cron_scheduler + cron_tool_registry 到 Orchestrator，
    # 启用 cron 会话路径的请求级工具过滤（active_tools_snapshot 锁定）+
    # cron_tool 子进程执行派发。用户会话路径不受影响（仅 cron: 前缀会话读取）。
    if orchestrator is not None:
        try:
            orchestrator.set_cron_dependencies(
                cron_scheduler=cron_scheduler,
                cron_tool_registry=cron_tool_registry,
            )
            if cron_scheduler is not None or cron_tool_registry is not None:
                logger.info(
                    "已注入 cron 依赖到 Orchestrator（cron_scheduler=%s, cron_tool_registry=%s）",
                    cron_scheduler is not None,
                    cron_tool_registry is not None,
                )
        except Exception as e:
            logger.warning("注入 cron 依赖到 Orchestrator 失败: %s", e)

    # 初始化健康检查器（所有子系统装配完成后，在 yield 前最后一步初始化）
    try:
        _hc = HealthChecker(
            orchestrator=orchestrator,
            session_logger_global=session_logger,
            mcp_manager=mcp_manager,
            skill_loader=skill_loader,
            metrics_collector=metrics_collector,
            proposal_store=proposal_store,
        )
        health_checker = _hc
        logger.info("HealthChecker 已初始化（%d 个子系统检查项）", len(_hc._checks))
    except Exception as e:
        logger.warning("HealthChecker 初始化失败，健康检查降级: %s", e)
        health_checker = None

    # 启动期安全告警：/reasoning/toggle 路由无鉴权风险检测（SubTask 14.5）
    # security.api_key 未配置时，服务监听 0.0.0.0 存在远程调用风险
    _security_cfg = config.get("security", {}) or {}
    _api_key = _security_cfg.get("api_key", "")
    if not _api_key:
        logger.warning(
            "[security] /reasoning/toggle 路由无鉴权，服务监听 %s 时存在远程调用风险，"
            "建议配置 security.api_key",
            host,
        )

    # Task 4: 初始化 DI 容器 + 注册组件 + 注入 lifespan 实例
    # 容器接管热重载：PUT /config 时检测变更段并重建受影响组件。
    # lifespan 已完成复杂初始化（ONNX/ChromaDB/MCP），通过 set_instance
    # 注入实例避免工厂重复创建。热重载时工厂仍会被调用重建。
    try:
        from app import init_container, register_components, inject_lifespan_instances, get_container
        config["_config_path"] = CONFIG_PATH
        init_container(config)
        container = get_container()
        register_components(container)
        inject_lifespan_instances(
            container,
            orchestrator=orchestrator,
            session_logger=session_logger,
            metrics_collector=metrics_collector,
            metrics_store=metrics_store,
            audit_logger=audit_logger,
            approval_manager=approval_manager,
            task_manager=task_manager,
            stream_manager=stream_manager,
            skill_loader=skill_loader,
            mcp_manager=mcp_manager,
            upload_manager=upload_manager,
            etl_engine=etl_engine,
            cron_scheduler=cron_scheduler,
            proposal_store=proposal_store,
            health_checker=health_checker,
        )
        logger.info("DI 容器已初始化并注入 %d 个 lifespan 实例（热重载就绪）",
                    sum(1 for v in container._instances.values() if v is not None))
    except Exception as e:
        logger.warning("DI 容器初始化失败（热重载降级）: %s", e)

    # Task 6: 注册全局异常处理器
    try:
        from app import register_exception_handlers
        register_exception_handlers(app)
        logger.info("全局异常处理器已注册")
    except Exception as e:
        logger.warning("全局异常处理器注册失败: %s", e)

    # Task 8: 同步全局状态到 state 模块（供 routes/ 读取）
    import state as _state
    _state.orchestrator = orchestrator
    _state.session_logger = session_logger
    _state.metrics_collector = metrics_collector
    _state.metrics_store = metrics_store
    _state.audit_logger = audit_logger
    _state.approval_manager = approval_manager
    _state.task_manager = task_manager
    _state.cron_scheduler = cron_scheduler
    _state.proposal_store = proposal_store
    _state.health_checker = health_checker
    _state.stream_manager = stream_manager
    _state.skill_loader = skill_loader
    _state.mcp_manager = mcp_manager
    _state.upload_manager = upload_manager
    _state.etl_engine = etl_engine
    _state.file_context_injector = file_context_injector

    # 同步局部变量到 server 模块（供 state.py 代理 + 测试 patch）
    import server as _server_mod
    _server_mod.orchestrator = orchestrator
    _server_mod.session_logger = session_logger
    _server_mod.metrics_collector = metrics_collector
    _server_mod.metrics_store = metrics_store
    _server_mod.audit_logger = audit_logger
    _server_mod.approval_manager = approval_manager
    _server_mod.task_manager = task_manager
    _server_mod.cron_scheduler = cron_scheduler
    _server_mod.proposal_store = proposal_store
    _server_mod.cron_tool_registry = cron_tool_registry
    _server_mod.skill_loader = skill_loader
    _server_mod.mcp_manager = mcp_manager
    _server_mod.skill_tools_registered = skill_tools_registered
    _server_mod.upload_manager = upload_manager
    _server_mod.etl_engine = etl_engine
    _server_mod.file_context_injector = file_context_injector
    _server_mod.stream_manager = stream_manager
    _server_mod.health_checker = health_checker
    _server_mod.metrics_persist_task = metrics_persist_task

    try:
        yield
    finally:
        # Task 5: 关闭 DI 容器
        try:
            from app import close_container
            close_container()
        except Exception:
            pass
        # 取消清理任务
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        # 取消监控指标持久化任务
        if metrics_persist_task is not None:
            metrics_persist_task.cancel()
            try:
                await metrics_persist_task
            except asyncio.CancelledError:
                pass
        if metrics_store is not None:
            try:
                metrics_store.close()
            except Exception as e:
                logger.warning("关闭 MetricsStore 失败: %s", e)
        # Phase 6: 停止 CronScheduler
        if cron_scheduler is not None:
            try:
                cron_scheduler.stop()
                # 等待调度循环退出（最多 5 秒）
                if cron_task is not None:
                    try:
                        await asyncio.wait_for(cron_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        cron_task.cancel()
                        try:
                            await cron_task
                        except asyncio.CancelledError:
                            pass
                logger.info("CronScheduler 已停止")
            except Exception as e:
                logger.warning("CronScheduler 停止失败: %s", e)
        # 关闭时释放资源
        logger.info("正在关闭服务，释放资源...")
        # Phase 4: 释放 MCP 连接（子进程/HTTP 连接）
        if mcp_manager is not None:
            try:
                await mcp_manager.close_all()
                logger.info("MCPManager 已关闭所有连接")
            except Exception as e:
                logger.error("MCPManager 关闭失败: %s", e)
        if orchestrator is not None:
            try:
                orchestrator.close()
            except Exception as e:
                logger.warning("关闭 Orchestrator 失败: %s", e)
        if session_logger is not None:
            try:
                session_logger.close()
            except Exception as e:
                logger.warning("关闭 SessionLogger 失败: %s", e)
        if audit_logger is not None:
            try:
                audit_logger.close()
            except Exception as e:
                logger.warning("关闭 AuditLogger 失败: %s", e)
        logger.info("Hermes Lite HTTP 服务已停止")

