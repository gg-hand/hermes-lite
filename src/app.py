"""FastAPI 应用入口。

Task 7: app 实例在此创建，lifespan 通过 lazy wrapper 延迟导入避免循环依赖。
后续 Task 8-19 将逐步迁移路由到 routes/ 包。
Task 5: DI 容器初始化 + get_container() 供热重载使用。
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

VERSION = "0.1.0"
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")
logger = logging.getLogger("hermes.server")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan wrapper：延迟导入 lifespan 模块避免循环依赖。

    app.py 创建 FastAPI 实例时需要 lifespan，但 lifespan 模块需要
    通过 `import server` 设置全局变量，server.py 需要 app 实例。
    通过 wrapper 在运行时（而非模块加载时）导入 lifespan，打破循环。
    """
    from lifespan import lifespan as _lifespan
    async with _lifespan(app):
        yield


app = FastAPI(
    title="Hermes Lite",
    description="个人 AI Agent 长驻 HTTP 服务",
    version=VERSION,
    lifespan=lifespan,
)

# CORS 配置（首次加载时从配置读取，热更新需重启）
_cors_origins = ["http://localhost:3000"]
try:
    from config import load_config
    _cfg = load_config(CONFIG_PATH)
    _cors_origins = _cfg.get("server", {}).get("cors_origins", ["http://localhost:3000"])
    if not isinstance(_cors_origins, list) or not _cors_origins:
        _cors_origins = ["http://localhost:3000"]
except Exception:
    pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS 已配置，允许来源: %s", _cors_origins)

# ---------- API 认证中间件 ----------
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import time  # noqa: E402

_security_api_key: Optional[str] = None
try:
    _sec_cfg = load_config(CONFIG_PATH).get("security", {})
    _security_api_key = _sec_cfg.get("api_key", "") or None
except Exception:
    pass

if _security_api_key:
    logger.info("API 认证已启用（security.api_key 已配置）")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = ""

        if not token or token != _security_api_key:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "detail": "请提供有效的 API Key（Authorization: Bearer <key>）"},
            )

        return await call_next(request)
else:
    logger.warning(
        "API 认证未启用（security.api_key 未配置）。"
        "生产环境建议设置 HERMES_API_KEY 环境变量。"
    )


# ---------- 中间件：请求日志 ----------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    method = request.method
    path = request.url.path

    try:
        response = await call_next(request)
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(
            "%s %s -> 500 (%.2f ms) 异常: %s", method, path, duration_ms, e,
            extra={"method": method, "path": path, "status_code": 500, "duration_ms": duration_ms},
        )
        raise

    duration_ms = (time.time() - start_time) * 1000
    if not (method == "GET" and path == "/health" and response.status_code == 200):
        logger.info(
            "%s %s -> %d (%.2f ms)", method, path, response.status_code, duration_ms,
            extra={
                "method": method, "path": path,
                "status_code": response.status_code, "duration_ms": duration_ms,
            },
        )
    return response

# ---------------------------------------------------------------------------
# 路由注册（Task 8+: 按域从 server.py 迁移路由）
# ---------------------------------------------------------------------------

from routes.misc import router as misc_router  # noqa: E402
from routes.health import router as health_router  # noqa: E402
from routes.sessions import router as sessions_router  # noqa: E402
from routes.config import router as config_router  # noqa: E402
from routes.approvals import router as approvals_router  # noqa: E402
from routes.proposals import router as proposals_router  # noqa: E402
from routes.skills import router as skills_router  # noqa: E402
from routes.cron_tools import router as cron_tools_router  # noqa: E402
from routes.files import router as files_router  # noqa: E402
from routes.memory import router as memory_router  # noqa: E402
from routes.chat import router as chat_router  # noqa: E402
from routes.schedules import router as schedules_router  # noqa: E402

app.include_router(misc_router)
app.include_router(health_router)
app.include_router(sessions_router)
app.include_router(config_router)
app.include_router(approvals_router)
app.include_router(proposals_router)
app.include_router(skills_router)
app.include_router(cron_tools_router)
app.include_router(files_router)
app.include_router(memory_router)
app.include_router(chat_router)
app.include_router(schedules_router)


# ---------------------------------------------------------------------------
# DI 容器（Task 5）
# ---------------------------------------------------------------------------

_container = None


def init_container(config: dict):
    """初始化全局 DI 容器。

    在 server.py 的 lifespan 中调用。容器注册的组件目前仅用于热重载
    （PUT /config 时检测变更段并重建受影响组件）。Orchestrator 等核心
    组件仍由 server.py 直接管理，容器作为过渡层逐步接入。

    参数:
        config: 已解析的配置字典。
    """
    global _container
    from container import Container
    _container = Container(config)


def get_container():
    """返回全局 DI 容器，未初始化时返回 None。"""
    return _container


def close_container() -> None:
    """关闭容器并释放资源（在 lifespan 的 yield 之后调用）。"""
    global _container
    if _container is not None:
        try:
            _container.close()
        except Exception:
            pass
        _container = None


# ---------------------------------------------------------------------------
# 组件注册（Task 3: 注册15个外部组件到容器）
# ---------------------------------------------------------------------------

def _create_session_logger(config: dict):
    """从 storage 配置创建 SessionLogger。"""
    from storage.sqlite_log import SessionLogger
    storage_cfg = config.get("storage", {})
    sqlite_path = storage_cfg.get("sqlite_path", "data/sessions.db")
    return SessionLogger(db_path=sqlite_path)


def _create_metrics_collector(config: dict):
    """从 monitoring 配置创建 MetricsCollector。"""
    from monitoring.metrics import MetricsCollector
    monitoring_cfg = config.get("monitoring", {})
    if not monitoring_cfg.get("enabled", True):
        return None
    return MetricsCollector()


def _create_metrics_store(config: dict, session_logger=None):
    """从 monitoring 配置创建 MetricsStore。"""
    monitoring_cfg = config.get("monitoring", {})
    if not monitoring_cfg.get("enabled", True):
        return None
    if not monitoring_cfg.get("daily_persistence", True):
        return None
    try:
        from monitoring.metrics_store import MetricsStore
        storage_cfg = config.get("storage", {})
        sqlite_path = storage_cfg.get("sqlite_path", "data/sessions.db")
        return MetricsStore(db_path=sqlite_path)
    except Exception:
        return None


def _create_audit_logger(config: dict):
    """从 monitoring 配置创建 AuditLogger。"""
    from agent.audit import AuditLogger
    monitoring_cfg = config.get("monitoring", {})
    if not monitoring_cfg.get("enabled", True):
        return None
    audit_log_path = monitoring_cfg.get("audit_log_path", "data/audit.jsonl")
    audit_buffer_size = int(monitoring_cfg.get("audit_buffer_size", 1000))
    return AuditLogger(log_path=audit_log_path, buffer_size=audit_buffer_size)


def _create_approval_manager(config: dict, metrics_collector=None):
    """从 security 配置创建 ApprovalManager。"""
    try:
        from agent.approval import ApprovalManager
        security_cfg = config.get("security", {})
        approval_timeout = float(security_cfg.get("approval_timeout_seconds", 300))
        return ApprovalManager(timeout=approval_timeout, metrics=metrics_collector)
    except Exception:
        return None


def _create_task_manager(config: dict):
    """从 tasks 配置创建 TaskManager。"""
    try:
        from tasks.task_manager import TaskManager
        tasks_cfg = config.get("tasks", {})
        return TaskManager(file_path=tasks_cfg.get("file_path", "data/tasks.md"))
    except Exception:
        return None


def _create_stream_manager():
    """创建 StreamManager（无配置依赖）。"""
    from stream_manager import StreamManager
    return StreamManager()


def _create_skill_loader():
    """创建 SkillLoader（无配置依赖）。"""
    try:
        from skill.loader import SkillLoader
        return SkillLoader()
    except Exception:
        return None


def _create_proposal_store():
    """创建 ProposalStore（无配置依赖）。"""
    try:
        from agent.cron_proposals import ProposalStore
        return ProposalStore()
    except Exception:
        return None


def _create_mcp_manager(config: dict, skill_loader=None):
    """从 skills 配置创建 MCPManager。"""
    try:
        from mcp.manager import MCPManager
        return MCPManager()
    except Exception:
        return None


def _create_upload_manager(config: dict):
    """从 files 配置创建 UploadManager。"""
    try:
        from files.upload_manager import UploadManager
        files_cfg = config.get("files", {}) or {}
        storage_cfg = config.get("storage", {})
        sqlite_path = storage_cfg.get("sqlite_path", "data/sessions.db")
        return UploadManager(
            db_path=sqlite_path,
            upload_dir=files_cfg.get("upload_dir", "data/uploads"),
            max_upload_size_mb=float(files_cfg.get("max_upload_size_mb", 50)),
            max_files_per_session=int(files_cfg.get("max_files_per_session", 50)),
            allowed_extensions=files_cfg.get("allowed_extensions", None),
            ocr_enabled=bool(files_cfg.get("ocr_enabled", False)),
        )
    except Exception:
        return None


def _create_etl_engine(config: dict, upload_manager=None, orchestrator=None):
    """从 files 配置创建 ETLEngine。"""
    try:
        from files.etl_engine import ETLEngine
        from files.parser import WaterfallParser
        from files.chunker import DocumentChunker
        files_cfg = config.get("files", {}) or {}
        llm_fallback = bool(files_cfg.get("llm_fallback_enabled", False))
        water_parser = WaterfallParser(
            llm_fallback_enabled=llm_fallback,
            llm_client=orchestrator.llm_client if llm_fallback and orchestrator else None,
            parse_timeouts=files_cfg.get("parse_timeout_seconds", {}),
            ocr_config=files_cfg.get("ocr", {}) or {},
            metrics_collector=orchestrator.metrics if orchestrator else None,
        )
        doc_chunker = DocumentChunker(
            chunk_size=int(files_cfg.get("chunk_size", 512)),
            chunk_overlap=int(files_cfg.get("chunk_overlap", 64)),
        )
        return ETLEngine(
            upload_manager=upload_manager,
            chroma_store=orchestrator.chroma_store if orchestrator else None,
            session_logger=None,  # 由 lifespan 补注入
            parser=water_parser,
            chunker=doc_chunker,
            llm_client=orchestrator.llm_client if orchestrator else None,
            config=files_cfg,
        )
    except Exception:
        return None


def _create_cron_scheduler(config: dict, orchestrator=None):
    """从 tasks 配置创建 CronScheduler。"""
    try:
        from tasks.scheduler import CronScheduler
        return CronScheduler()
    except Exception:
        return None


def _create_health_checker(orchestrator=None, session_logger=None, mcp_manager=None,
                           skill_loader=None, metrics_collector=None, proposal_store=None):
    """创建 HealthChecker（无配置依赖，依赖运行时组件）。

    需补全 6 个参数，否则 HealthChecker.__init__ 的 orchestrator 必填参数
    会抛 TypeError，被 except 静默吞掉导致工厂永远返回 None。
    """
    try:
        from monitoring.health import HealthChecker
        return HealthChecker(
            orchestrator=orchestrator,
            session_logger_global=session_logger,
            mcp_manager=mcp_manager,
            skill_loader=skill_loader,
            metrics_collector=metrics_collector,
            proposal_store=proposal_store,
        )
    except Exception:
        return None


def register_components(container) -> None:
    """向容器注册所有 lifespan 组件。

    组件按依赖顺序注册。工厂函数中使用延迟导入避免循环依赖。
    Orchestrator 作为整体注册（方案B），内部组件对容器透明。

    参数:
        container: DI 容器实例。
    """
    # 1. 无依赖组件
    container.register("session_logger",
        lambda c: _create_session_logger(c.config),
        deps=[], hot_reloadable=True)

    container.register("metrics_collector",
        lambda c: _create_metrics_collector(c.config),
        deps=[], hot_reloadable=True)

    container.register("audit_logger",
        lambda c: _create_audit_logger(c.config),
        deps=[], hot_reloadable=True)

    container.register("task_manager",
        lambda c: _create_task_manager(c.config),
        deps=[], hot_reloadable=True)

    container.register("stream_manager",
        lambda c: _create_stream_manager(),
        deps=[], hot_reloadable=False)

    container.register("skill_loader",
        lambda c: _create_skill_loader(),
        deps=[], hot_reloadable=False)

    container.register("proposal_store",
        lambda c: _create_proposal_store(),
        deps=[], hot_reloadable=False)

    # 2. 依赖其他组件
    container.register("metrics_store",
        lambda c: _create_metrics_store(c.config, c.get("session_logger")),
        deps=["session_logger"], hot_reloadable=True)

    container.register("approval_manager",
        lambda c: _create_approval_manager(c.config, c.get("metrics_collector")),
        deps=["metrics_collector"], hot_reloadable=True)

    container.register("mcp_manager",
        lambda c: _create_mcp_manager(c.config, c.get("skill_loader")),
        deps=["skill_loader"], hot_reloadable=True)

    container.register("upload_manager",
        lambda c: _create_upload_manager(c.config),
        deps=[], hot_reloadable=True)

    # 3. Orchestrator（依赖外部组件，整体注册）
    from orchestrator import Orchestrator
    container.register("orchestrator",
        lambda c: Orchestrator(
            config_path=c.config.get("_config_path", "config.yaml"),
            metrics=c.get("metrics_collector"),
            audit_logger=c.get("audit_logger"),
            approval_manager=c.get("approval_manager"),
            task_manager=c.get("task_manager"),
        ),
        deps=["metrics_collector", "audit_logger", "approval_manager", "task_manager"],
        hot_reloadable=False)

    # 4. 依赖 Orchestrator
    container.register("etl_engine",
        lambda c: _create_etl_engine(c.config, c.get("upload_manager"), c.get("orchestrator")),
        deps=["upload_manager", "orchestrator"], hot_reloadable=True)

    container.register("cron_scheduler",
        lambda c: _create_cron_scheduler(c.config, c.get("orchestrator")),
        deps=["orchestrator"], hot_reloadable=False)

    container.register("health_checker",
        lambda c: _create_health_checker(
            orchestrator=c.get("orchestrator"),
            session_logger=c.get("session_logger"),
            mcp_manager=c.get("mcp_manager"),
            skill_loader=c.get("skill_loader"),
            metrics_collector=c.get("metrics_collector"),
            proposal_store=c.get("proposal_store"),
        ),
        deps=["orchestrator", "session_logger", "mcp_manager", "skill_loader",
              "metrics_collector", "proposal_store"],
        hot_reloadable=False)


def inject_lifespan_instances(
    container,
    *,
    orchestrator=None,
    session_logger=None,
    metrics_collector=None,
    metrics_store=None,
    audit_logger=None,
    approval_manager=None,
    task_manager=None,
    stream_manager=None,
    skill_loader=None,
    mcp_manager=None,
    upload_manager=None,
    etl_engine=None,
    cron_scheduler=None,
    proposal_store=None,
    health_checker=None,
) -> None:
    """将 lifespan 已创建的实例注入容器，绕过工厂创建。

    lifespan 完成复杂初始化（ONNX 预加载、ChromaDB 预热、异步 MCP 连接等）
    后调用此函数，将实例注入容器。None 值跳过（后续 get 时由工厂按需创建）。
    热重载时工厂仍会被调用重建实例。
    """
    instances = {
        "orchestrator": orchestrator,
        "session_logger": session_logger,
        "metrics_collector": metrics_collector,
        "metrics_store": metrics_store,
        "audit_logger": audit_logger,
        "approval_manager": approval_manager,
        "task_manager": task_manager,
        "stream_manager": stream_manager,
        "skill_loader": skill_loader,
        "mcp_manager": mcp_manager,
        "upload_manager": upload_manager,
        "etl_engine": etl_engine,
        "cron_scheduler": cron_scheduler,
        "proposal_store": proposal_store,
        "health_checker": health_checker,
    }
    for name, instance in instances.items():
        if instance is not None:
            container.set_instance(name, instance)


# ---------------------------------------------------------------------------
# 全局异常处理器（Task 6）
# ---------------------------------------------------------------------------

def register_exception_handlers(app) -> None:
    """注册统一异常处理器，将 ToolError/ConfigError 子类映射为 HTTP 响应。

    在 server.py 的 lifespan 中调用（或测试中手动调用）。

    映射表:
    - ToolNotFoundError → 404 {"error": "tool_not_found"}
    - ToolPermissionDenied → 403 {"error": "permission_denied"}
    - ToolTimeoutError → 504 {"error": "tool_timeout"}
    - ToolExecutionError → 500 {"error": "tool_execution_error"}
    - ConfigValidationError → 400 {"error": "config_validation_error"}
    - ConfigReloadError → 500 {"error": "config_reload_error"}
    - ContainerConfigError → 500 {"error": "container_config_error"}
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from errors import (
        ToolNotFoundError, ToolExecutionError, ToolPermissionDenied,
        ToolTimeoutError, ConfigValidationError, ConfigReloadError,
        ContainerConfigError,
    )

    @app.exception_handler(ToolNotFoundError)
    async def _handle_tool_not_found(request: Request, exc: ToolNotFoundError):
        return JSONResponse(status_code=404, content={
            "error": "tool_not_found", "message": str(exc),
        })

    @app.exception_handler(ToolPermissionDenied)
    async def _handle_permission_denied(request: Request, exc: ToolPermissionDenied):
        return JSONResponse(status_code=403, content={
            "error": "permission_denied", "message": str(exc),
        })

    @app.exception_handler(ToolTimeoutError)
    async def _handle_timeout(request: Request, exc: ToolTimeoutError):
        return JSONResponse(status_code=504, content={
            "error": "tool_timeout", "message": str(exc),
        })

    @app.exception_handler(ToolExecutionError)
    async def _handle_tool_execution(request: Request, exc: ToolExecutionError):
        return JSONResponse(status_code=500, content={
            "error": "tool_execution_error", "message": str(exc),
        })

    @app.exception_handler(ConfigValidationError)
    async def _handle_config_validation(request: Request, exc: ConfigValidationError):
        return JSONResponse(status_code=400, content={
            "error": "config_validation_error", "message": str(exc),
        })

    @app.exception_handler(ConfigReloadError)
    async def _handle_config_reload(request: Request, exc: ConfigReloadError):
        return JSONResponse(status_code=500, content={
            "error": "config_reload_error", "message": str(exc),
        })

    @app.exception_handler(ContainerConfigError)
    async def _handle_container_config(request: Request, exc: ContainerConfigError):
        return JSONResponse(status_code=500, content={
            "error": "container_config_error", "message": str(exc),
        })


# 当作为主模块运行时
if __name__ == "__main__":
    import uvicorn

    config_path = __import__("os").environ.get("HERMES_CONFIG", "config.yaml")
    try:
        from config import load_config
        cfg = load_config(config_path)
        server_cfg = cfg.get("server", {})
        host = server_cfg.get("host", "0.0.0.0")
        port = server_cfg.get("port", 8000)
    except Exception:
        host, port = "0.0.0.0", 8000

    uvicorn.run(app, host=host, port=port)
