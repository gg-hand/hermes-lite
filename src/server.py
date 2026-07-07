"""EC2 长驻 HTTP 服务。

基于 FastAPI 实现，对外提供对话、会话管理与健康检查等 REST 接口。
启动时通过 lifespan 初始化 Orchestrator 与 SessionLogger，全局复用，
避免每个请求重复初始化。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 兼容相对导入与直接运行两种方式（与 orchestrator.py 保持一致）
try:
    from .config import clear_config_cache, get_llm_timeouts, load_config, validate_required_env_vars
    from .orchestrator import Orchestrator
    from .storage.sqlite_log import SessionLogger
    from .storage.chroma_store import _get_onnx_embedder
    from .monitoring.metrics import MetricsCollector
    from .monitoring.metrics_store import MetricsStore, compute_delta
    from .monitoring.health import HealthChecker
    from .agent.audit import AuditLogger
except ImportError:  # pragma: no cover - 直接运行模块时回退
    from pathlib import Path

    _SRC_DIR = str(Path(__file__).resolve().parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from config import clear_config_cache, get_llm_timeouts, load_config, validate_required_env_vars  # type: ignore
    from orchestrator import Orchestrator  # type: ignore
    from storage.sqlite_log import SessionLogger  # type: ignore
    from storage.chroma_store import _get_onnx_embedder  # type: ignore
    from monitoring.metrics import MetricsCollector  # type: ignore
    from monitoring.metrics_store import MetricsStore, compute_delta  # type: ignore
    from monitoring.health import HealthChecker  # type: ignore
    from agent.audit import AuditLogger  # type: ignore

# Phase 4: Skill/MCP 扩展模块（可选，加载失败时降级为纯内置工具模式）
# 双层 try/except：先尝试相对导入（包内运行），失败后回退到绝对导入
# （直接运行模块时 _SRC_DIR 已在 sys.path 中），两者均失败则禁用扩展能力。
try:
    from .skill.loader import SkillLoader, load_skill_to_registry, register_skill_stub
    from .mcp.client import MCPServerDef
    from .mcp.manager import MCPManager, register_mcp_tools_to_registry
    SKILL_MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - 直接运行模块时回退
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
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from agent.approval import ApprovalManager  # type: ignore
    except ImportError:
        ApprovalManager = None  # type: ignore

# Phase 6: 任务编排模块
try:
    from .tasks.task_manager import TaskManager
    from .tasks.scheduler import CronScheduler
    from .tasks.cron_expr import CronExpr
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from tasks.task_manager import TaskManager  # type: ignore
        from tasks.scheduler import CronScheduler  # type: ignore
        from tasks.cron_expr import CronExpr  # type: ignore
    except ImportError:  # pragma: no cover
        TaskManager = None  # type: ignore
        CronScheduler = None  # type: ignore
        CronExpr = None  # type: ignore

# Phase 8 Task 3: cron 工具集 + 提议-确认协议
try:
    from .agent.cron_proposals import ProposalStore
    from .agent.cron_tools import register_cron_tools
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from agent.cron_proposals import ProposalStore  # type: ignore
        from agent.cron_tools import register_cron_tools  # type: ignore
    except ImportError:  # pragma: no cover
        ProposalStore = None  # type: ignore
        register_cron_tools = None  # type: ignore

# Phase 8 Task 5: cron_tool 动态工具系统（Layer 2 能力扩展）
# - CronToolRegistry: 独立 registry，不进全局 ToolRegistry
# - register_write_cron_tool: 注册 write_cron_tool 工具到全局 registry
#   （用户会话可用，让 LLM 生成 cron_tool 写入 .pending/）
# - cron_tool_loader: 子进程执行 + TOOL.md 解析
# 中断管理器 + 断点检测器
try:
    from .stream_manager import StreamManager, StreamCancelled
    from .breakpoint_detector import BreakpointDetector
except ImportError:
    from stream_manager import StreamManager, StreamCancelled  # type: ignore
    from breakpoint_detector import BreakpointDetector  # type: ignore

# 异步 LLM Backend: ActivityTimeout 异常（spec: async-llm-backend）
# per-token 活跃超时触发，SSE handler 捕获后 yield error + interrupt 终止流
try:
    from .llm.client import ActivityTimeout
except ImportError:
    from llm.client import ActivityTimeout  # type: ignore

try:
    from .agent.cron_tool_registry import CronToolRegistry
    from .agent.cron_tool_writer import register_write_cron_tool
    from .agent.builtin_tools import register_bash_tool
    from .tasks.cron_tool_loader import (
        CronToolError,
        DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR,
        list_pending_tools as _list_pending_cron_tools,
        list_tools as _list_active_cron_tools,
        load_tool as _load_cron_tool,
    )
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from agent.cron_tool_registry import CronToolRegistry  # type: ignore
        from agent.cron_tool_writer import register_write_cron_tool  # type: ignore
        from tasks.cron_tool_loader import (  # type: ignore
            CronToolError,
            DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR,
            list_pending_tools as _list_pending_cron_tools,
            list_tools as _list_active_cron_tools,
            load_tool as _load_cron_tool,
        )
    except ImportError:  # pragma: no cover
        CronToolRegistry = None  # type: ignore
        register_write_cron_tool = None  # type: ignore
        register_bash_tool = None  # type: ignore
        CronToolError = Exception  # type: ignore
        _CRON_TOOL_BASE_DIR = "cron_tool"  # type: ignore
        _list_pending_cron_tools = None  # type: ignore
        _list_active_cron_tools = None  # type: ignore
        _load_cron_tool = None  # type: ignore


# Phase 8 Task 6: Skill 工具管理（可选，加载失败时降级）
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


# 文件上传与 ETL 模块（可选，加载失败时降级）
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


# ---------- 常量 ----------

VERSION = "0.1.0"
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")
SERVER_LOG_PATH = os.environ.get("HERMES_SERVER_LOG", "data/server.log")
LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"


# ---------- 日志配置 ----------


class JSONLogFormatter(logging.Formatter):
    """JSON 结构化日志格式化器。

    每行输出 JSON，可直接被 Logstash / Grafana Loki / Datadog 消费。
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 异常堆栈
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "traceback": self.formatException(record.exc_info),
            }
        # 请求上下文（由中间件注入 extra）
        for attr in ("method", "path", "status_code", "duration_ms", "session_id"):
            if hasattr(record, attr):
                log_entry[attr] = getattr(record, attr)
        return json.dumps(log_entry, ensure_ascii=False)


def _setup_logging(log_file: str) -> logging.Logger:
    """配置根日志：同时输出到 stdout 与文件。

    若 root logger 已有 handler 则跳过配置，既避免 reload 时重复输出，
    也避免清掉早期模块（如 config.py）已注册的 handler。

    测试环境（unittest / pytest）下跳过文件 handler，避免测试日志污染生产
    server.log。测试用例使用 assertLogs / caplog 进行日志断言，不受此影响。

    使用 RotatingFileHandler（最大 10MB，保留 3 个备份）防止日志无限增长。
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 已有 handler 时跳过（防止重复配置，也防止清掉早期 handler）
    if root_logger.handlers:
        return logging.getLogger("hermes.server")

    # 确保日志所在目录存在
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    formatter = JSONLogFormatter()

    # stdout 输出
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # 测试环境下跳过文件 handler
    # unittest / pytest 下均跳过：测试日志通过 assertLogs / caplog 捕获，
    # 不污染生产 server.log。5 个测试文件（test_config_update.py 等）会从
    # src.server import app 触发 _setup_logging，必须在此拦截。
    _in_test_env = (
        "pytest" in sys.modules
        or "unittest" in sys.modules
        or "PYTEST_CURRENT_TEST" in os.environ
    )
    if _in_test_env:
        return logging.getLogger("hermes.server")

    # 文件输出：RotatingFileHandler，10MB 轮转，保留 3 份备份
    from logging.handlers import RotatingFileHandler

    actual_log = log_file
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except PermissionError:
        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        dirname = os.path.dirname(log_file)
        basename = os.path.basename(log_file)
        name, ext = os.path.splitext(basename)
        actual_log = os.path.join(dirname, f"{name}_{ts}{ext}") if dirname else f"{name}_{ts}{ext}"
        file_handler = RotatingFileHandler(
            actual_log, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        root_logger.warning(
            "日志文件 %s 被锁定，已切到 %s", log_file, actual_log
        )
    file_handler.setFormatter(formatter)

    # 加过滤器：只允许 hermes、src、mcp 命名空间的日志写入文件
    # （避免测试或其他第三方库（如 httpx）的日志冲刷 server.log）
    class HermesLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            name = record.name
            return name.startswith("hermes.") or name.startswith("src.") or name.startswith("mcp.")

    file_handler.addFilter(HermesLogFilter())
    root_logger.addHandler(file_handler)

    return logging.getLogger("hermes.server")


logger = _setup_logging(SERVER_LOG_PATH)


# ---------- 全局组件 ----------

# 在 lifespan 中初始化，全局复用，避免每请求重复创建
orchestrator: Optional[Orchestrator] = None
session_logger: Optional[SessionLogger] = None
metrics_collector: Optional[MetricsCollector] = None
metrics_store: Optional["MetricsStore"] = None
metrics_persist_task: Optional[asyncio.Task] = None
# /metrics/reset 触发后置 True，metrics_persist_loop 检查后重置 baseline 并清零
_metrics_baseline_reset: bool = False
audit_logger: Optional[AuditLogger] = None
approval_manager: Optional[ApprovalManager] = None
# Phase 4: Skill/MCP 扩展组件（lifespan 中按需初始化）
skill_loader = None
skill_tools_registered = False
mcp_manager = None
# Phase 6: 任务编排组件（lifespan 中按需初始化）
task_manager: Optional[TaskManager] = None
cron_scheduler: Optional[CronScheduler] = None
# Phase 8 Task 3: 提议-确认协议内存存储（lifespan 中初始化，全局复用）
proposal_store: Optional[ProposalStore] = None
# Phase 8 Task 5: cron_tool 独立 registry（lifespan 中初始化，不进全局 ToolRegistry）
cron_tool_registry: Optional["CronToolRegistry"] = None
# 健康检查器（lifespan 中初始化）
health_checker: Optional[HealthChecker] = None
# 流中断管理器
stream_manager: Optional[StreamManager] = None
# 文件上传与 ETL 管道组件（lifespan 中初始化）
upload_manager: Optional[Any] = None
etl_engine: Optional[Any] = None
file_context_injector: Optional[Any] = None


# ---------- Pydantic 请求/响应模型 ----------

class ChatRequest(BaseModel):
    """对话请求体。"""

    session_id: Optional[str] = Field(
        default=None, description="会话 ID，不传则新建会话"
    )
    message: str = Field(..., description="用户输入文本")


class CancelRequest(BaseModel):
    """中断请求体。"""

    session_id: str = Field(..., description="要中断的会话 ID")
    mode: str = Field(
        default="immediate",
        description="中断模式：immediate（立即中断）或 graceful（等待断点）",
    )
    new_message: Optional[str] = Field(
        default=None, description="graceful 模式下用户的新消息"
    )


class ChatResponse(BaseModel):
    """对话响应体。"""

    session_id: str
    response: str
    timestamp: str


class HealthResponse(BaseModel):
    """健康检查响应体。

    含 overall status、summary 计数与各子系统检测结果 detail。
    """

    status: str
    timestamp: str
    version: str
    summary: Dict[str, int]
    checks: Dict[str, Dict[str, Any]]


class SessionItem(BaseModel):
    """会话条目。

    ``title`` 为可空字段，未生成标题的会话返回 None，前端回退到 id 前 24 字符。
    cron 会话的 title 取 schedule.name；用户会话的 title 由 LLM 首轮异步生成。
    """

    id: str
    created_at: str
    updated_at: str
    title: Optional[str] = None


class SessionListResponse(BaseModel):
    """会话列表响应体。"""

    sessions: List[SessionItem]


class SessionTitleUpdate(BaseModel):
    """更新会话标题请求体（Task 0 PATCH 端点）。

    ``title`` 长度 1-100 字符（包含两端），由 FastAPI 自动校验，越界返回 422。
    """

    title: str = Field(..., min_length=1, max_length=100)


class MessageItem(BaseModel):
    """消息条目。

    ``tool_name`` / ``tool_call_id`` 为可选字段，老消息（未持久化工具
    调用元数据）或纯文本对话时为 None；工具调用卡片渲染依赖这两个字段
    与 ``role`` 联合判断（详见前端 loadMessages 渲染逻辑）。
    ``is_error`` 仅对 tool_result 有意义，标识工具执行是否出错；
    老消息（无此列）或非工具消息返回 None。
    ``attachments`` 为附件 JSON 字符串（如文件上传消息），老消息为 None。
    ``message_type`` 为消息类型标记（如 'file_upload'），普通消息为 None。
    ``reasoning`` 为 LLM 思考内容（reasoning/thinking），仅 assistant 消息有值。
    """

    role: str
    content: str
    created_at: str
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    is_error: Optional[bool] = None
    attachments: Optional[str] = None
    message_type: Optional[str] = None
    reasoning: Optional[str] = None


class MessageListResponse(BaseModel):
    """消息列表响应体。"""

    messages: List[MessageItem]


class DeleteSessionResponse(BaseModel):
    """删除会话响应体。"""

    status: str
    session_id: str


class ConfigResponse(BaseModel):
    """配置读取响应体。"""

    config: Dict[str, Any]


class ConfigUpdateRequest(BaseModel):
    """配置更新请求体。"""

    config: Dict[str, Any]


class ConfigUpdateResponse(BaseModel):
    """配置更新响应体。"""

    status: str
    message: str
    needs_restart: bool


class ApprovalResolveRequest(BaseModel):
    """审批决定请求体。"""

    decision: str = Field(..., description="approve 或 deny")
    reason: Optional[str] = Field(None, description="决定原因（用户拒绝时的备注），可选")


class ApprovalResolveResponse(BaseModel):
    """审批决定响应体。"""

    status: str
    approval_id: str
    decision: str


class ApprovalListItem(BaseModel):
    """审批队列中的 pending 条目。"""

    approval_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    reason: str
    created_at: str


class ApprovalListResponse(BaseModel):
    """审批列表响应体。"""

    pending: List[ApprovalListItem]


# Phase 6: 调度模型
class ScheduleCreateRequest(BaseModel):
    """调度项创建请求体。

    支持可选 ``workflow`` 字段（声明式 workflow 配置），结构遵循
    ``WorkflowSpec.from_dict``：
    - 简易模式: ``{"template": "research", "template_config": {...}}``
    - 多步模式: ``{"name": "...", "steps": [{...}, ...]}``
    """

    name: str
    cron: str
    task: str
    enabled: bool = True
    id: Optional[str] = None
    workflow: Optional[Dict[str, Any]] = None


class ScheduleUpdateRequest(BaseModel):
    """调度项更新请求体。

    ``workflow`` 字段变更需重启调度器才能生效（与 cron/task/name 一致）。
    """

    name: Optional[str] = None
    cron: Optional[str] = None
    task: Optional[str] = None
    enabled: Optional[bool] = None
    workflow: Optional[Dict[str, Any]] = None


class ScheduleListResponse(BaseModel):
    """调度项列表响应体。"""

    schedules: List[dict] = Field(default_factory=list)


class ScheduleResponse(BaseModel):
    """调度项操作响应体。"""

    schedule_id: str
    message: str


# 文件上传响应模型
class FileUploadResponse(BaseModel):
    """文件上传响应体。"""

    file_id: str
    is_dup: bool = False
    message: str = ""


class FileItem(BaseModel):
    """文件条目。"""

    file_id: str
    original_name: str
    size: int
    type: str
    etl_status: str
    summary: str = ""
    chunk_count: int = 0
    uploaded_at: str
    last_accessed: str
    version_seq: Optional[int] = None
    is_latest: Optional[bool] = None


class FileListResponse(BaseModel):
    """文件列表响应体。"""

    files: List[FileItem]


class FileDeleteResponse(BaseModel):
    """文件删除响应体。"""

    status: str
    file_id: str
    details: dict = {}


# ---------- 生命周期管理 ----------


async def cleanup_loop():
    """定时清理古早会话的后台任务。

    从配置文件读取 ``storage.session_ttl_days`` 与
    ``storage.cleanup_interval_hours``，周期性调用
    :meth:`SessionLogger.delete_old_sessions` 删除超过 TTL 的会话，
    随后同步清理 HistoryBuffer 持久化目录下已删除 session 对应的 JSONL 文件。
    配置缺失或 TTL 未设置时直接退出，不启动循环。
    """
    try:
        config = load_config(CONFIG_PATH)
    except Exception as e:
        logger.warning("cleanup_loop 读取配置失败，跳过清理: %s", e)
        return
    storage_cfg = config.get("storage", {})
    ttl_days = storage_cfg.get("session_ttl_days")
    interval_hours = storage_cfg.get("cleanup_interval_hours", 24)
    if not ttl_days:
        return

    while True:
        try:
            if session_logger is not None:
                deleted = session_logger.delete_old_sessions(ttl_days)
                if deleted:
                    logger.info(
                        "定时清理完成: 删除了 %d 个旧会话（超过 %d 天）",
                        deleted, ttl_days,
                    )
            # 清理过期监控历史记录（复用 session_ttl_days）
            if metrics_store is not None:
                deleted_metrics = metrics_store.delete_old_metrics(ttl_days)
                if deleted_metrics:
                    logger.info("清理了 %d 条过期监控历史记录", deleted_metrics)
            # 清理已删除 session 的 JSONL 历史文件：delete_old_sessions 已从 SQLite
            # 删除过期 session，此处同步删除 persistence_dir 下对应 session 的 JSONL。
            # 文件名格式 {session_id.replace(":","_")}.jsonl（cron:abc → cron_abc.jsonl），
            # 还原有歧义（下划线可能是原 session_id 的一部分），保守策略：
            # 文件名 stem 与「下划线还原为冒号」两种候选均不在现有 sessions 中才删除。
            if (
                orchestrator is not None
                and getattr(orchestrator, "history_buffer", None) is not None
                and orchestrator.history_buffer.persistence_dir
            ):
                persist_dir = Path(orchestrator.history_buffer.persistence_dir)
                if persist_dir.exists():
                    existing_sessions = {
                        s["id"] for s in session_logger.list_sessions()
                    }
                    for f in persist_dir.glob("*.jsonl"):
                        stem = f.stem  # 去掉 .jsonl 后缀
                        candidates = {stem, stem.replace("_", ":")}
                        if not (candidates & existing_sessions):
                            try:
                                f.unlink()
                                logger.info(
                                    "清理过期 session 历史文件: %s", f.name
                                )
                            except OSError as e:
                                logger.warning(
                                    "清理历史文件失败 %s: %s", f.name, e
                                )
                    # 同步清理 todo/ 子目录下过期 session 的 plan JSON 文件
                    # （与 JSONL 同源，命名规则一致）
                    todo_dir = persist_dir / "todo"
                    if todo_dir.exists():
                        for f in todo_dir.glob("*.json"):
                            stem = f.stem
                            candidates = {stem, stem.replace("_", ":")}
                            if not (candidates & existing_sessions):
                                try:
                                    f.unlink()
                                    logger.info(
                                        "清理过期 session todo 文件: %s",
                                        f.name,
                                    )
                                except OSError as e:
                                    logger.warning(
                                        "清理 todo 文件失败 %s: %s",
                                        f.name, e,
                                    )
        except Exception as e:
            logger.error("定时清理会话失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)


async def file_cleanup_loop() -> None:
    """定时清理过期文件磁盘的后台任务。

    从配置文件读取 ``storage.session_ttl_days`` 与
    ``files.cleanup_interval_hours``，周期性删除超过 TTL 的文件磁盘内容。
    仅删除磁盘文件（原始 + 解析缓存），不动 ChromaDB / FTS5 / SQLite 元数据。
    """
    try:
        config = load_config(CONFIG_PATH)
    except Exception as e:
        logger.warning("file_cleanup_loop 读取配置失败，跳过清理: %s", e)
        return

    storage_cfg = config.get("storage", {})
    files_cfg = config.get("files", {})
    ttl_days = storage_cfg.get("session_ttl_days")
    interval_hours = files_cfg.get("cleanup_interval_hours", 24)
    if not ttl_days:
        return

    while True:
        try:
            if upload_manager is not None:
                expired_ids = upload_manager.get_expired(ttl_days)
                for file_id in expired_ids:
                    try:
                        meta = upload_manager.get_metadata(file_id)
                        if meta is None:
                            continue
                        # 跳过 processing 状态（防御性二次检查）
                        if meta.get("etl_status") == "processing":
                            continue
                        # 删除磁盘原始文件
                        saved_path = meta.get("saved_path")
                        if saved_path:
                            try:
                                os.remove(saved_path)
                                logger.info("清理过期磁盘文件: %s", saved_path)
                            except OSError as e:
                                logger.warning("清理磁盘文件失败: %s -> %s", saved_path, e)
                        # 删除解析缓存
                        cache_dir = files_cfg.get("upload_dir", "data/uploads")
                        cache_path = os.path.join(cache_dir, f"{file_id}.parsed")
                        try:
                            os.remove(cache_path)
                        except OSError:
                            pass  # 缓存可能不存在
                        # 标记为 disk_expired
                        upload_manager.mark_disk_expired(file_id)
                    except Exception as e:
                        logger.warning("清理文件 %s 失败: %s", file_id, e)
                if expired_ids:
                    logger.info("文件清理完成: 清理了 %d 个过期文件", len(expired_ids))
        except Exception as e:
            logger.error("定时清理文件失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)


async def metrics_persist_loop() -> None:
    """定时将监控指标增量持久化到 SQLite 的后台任务。

    策略：
    - 维护内存 baseline（上次刷新时的快照）
    - 每隔 flush_interval_minutes 计算一次 delta（当前 - baseline），upsert 到当天记录
    - 午夜额外触发：将跨天前的 delta 归入旧日期
    - 重启后 baseline = 全零，首次 delta = 重启后全部活动，自动合并到当天已有记录
    - 启动后首次 flush 仅延迟 INITIAL_FLUSH_DELAY 秒，避免短时运行的服务无数据
    - 每轮循环重读 flush_interval_minutes，支持配置热更新
    - /metrics/reset 触发后重置 baseline，避免重置后 delta 丢失
    - 所有 SQLite 调用通过 asyncio.to_thread 包装，避免阻塞事件循环
    """
    global metrics_store, _metrics_baseline_reset
    if metrics_collector is None or metrics_store is None:
        return

    # 启动后首次 flush 的延迟（秒）：足够让 lifespan 完成初始化，又不会让当天记录迟迟不创建
    INITIAL_FLUSH_DELAY = 10

    baseline = metrics_collector.snapshot()
    current_date = datetime.now().date()
    is_first_flush = True

    while True:
        try:
            # 每轮循环重读配置，支持 flush_interval_minutes 热更新
            try:
                config = load_config(CONFIG_PATH)
                monitoring_cfg = config.get("monitoring", {})
                flush_interval = int(monitoring_cfg.get("flush_interval_minutes", 60))
            except Exception as e:
                logger.warning("metrics_persist_loop 读取配置失败，使用默认 60min: %s", e)
                flush_interval = 60
            if flush_interval <= 0:
                flush_interval = 60

            now = datetime.now()
            if is_first_flush:
                # 首次 flush：短延迟后立即执行，确保当天记录尽早创建
                sleep_seconds = INITIAL_FLUSH_DELAY
            else:
                next_flush = now + timedelta(minutes=flush_interval)
                next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
                sleep_until = min(next_flush, next_midnight)
                sleep_seconds = (sleep_until - now).total_seconds()
            logger.info("metrics_persist_loop: 准备 sleep %.1f 秒 (is_first=%s)", sleep_seconds, is_first_flush)
            await asyncio.sleep(sleep_seconds)
            logger.info("metrics_persist_loop: sleep 返回，开始执行 flush")

            # 检查 baseline 重置请求（/metrics/reset 触发）
            if _metrics_baseline_reset:
                baseline = metrics_collector.snapshot()
                _metrics_baseline_reset = False
                logger.info("metrics baseline 已重置（reset 接口触发）")

            current_snap = metrics_collector.snapshot()
            delta = compute_delta(current_snap, baseline)
            today = datetime.now().date()

            if today != current_date:
                # 跨天：delta 归入旧日期
                target_date = current_date.isoformat()
                current_date = today
            else:
                target_date = current_date.isoformat()

            await asyncio.to_thread(metrics_store.upsert_daily, target_date, delta)
            baseline = current_snap
            logger.info("metrics flush 完成: date=%s, delta_llm_calls=%d, is_first=%s",
                        target_date, delta.get("llm_calls_total", 0), is_first_flush)
            is_first_flush = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("监控指标持久化失败: %s", e)
            # 失败时不重置 baseline，下次重试
            is_first_flush = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时初始化资源，关闭时释放。

    使用 lifespan（而非已废弃的 on_event），由 FastAPI 在应用启停时调用。
    """
    global orchestrator, session_logger, metrics_collector, audit_logger
    global metrics_store
    global skill_loader, mcp_manager
    global approval_manager
    global task_manager, cron_scheduler
    global proposal_store
    global cron_tool_registry
    global skill_tools_registered
    global upload_manager, etl_engine, file_context_injector
    global metrics_persist_task, file_cleanup_task, cleanup_task

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
    global stream_manager
    global health_checker
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
                        from .agent.builtin_tools import register_file_tools
                    except ImportError:
                        from agent.builtin_tools import register_file_tools  # type: ignore
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

    try:
        yield
    finally:
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


# ---------- FastAPI 应用 ----------

app = FastAPI(
    title="Hermes Lite",
    description="个人 AI Agent 长驻 HTTP 服务",
    version=VERSION,
    lifespan=lifespan,
)


# ---------- CORS 配置 ----------
# 首次加载时从配置读取 CORS 允许来源，热更新需重启。

_cors_origins = ["http://localhost:3000"]
try:
    _cfg = load_config(CONFIG_PATH)
    _cors_origins = _cfg.get("server", {}).get("cors_origins", ["http://localhost:3000"])
    if not isinstance(_cors_origins, list) or not _cors_origins:
        _cors_origins = ["http://localhost:3000"]
except Exception:
    pass

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS 已配置，允许来源: %s", _cors_origins)


# ---------- API 认证中间件 ----------
# 优先于 log_requests 执行（先认证，后记录日志）。

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
        """API 认证中间件：检查 Authorization: Bearer <key> 头。

        /health 端点跳过认证（供负载均衡健康检查）。
        API Key 不存在时（配置为空）跳过认证。
        """
        # 健康检查端点跳过认证
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
    """记录每个请求的方法、路径、状态码与耗时。

    通过 ``extra`` 注入请求上下文，供 :class:`JSONLogFormatter`
    输出结构化字段（method/path/status_code/duration_ms）。
    """
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
    # 跳过健康检查成功响应的 INFO 日志，减少刷屏；
    # 健康检查异常时已在 except 中记录 ERROR，不会漏
    if not (method == "GET" and path == "/health" and response.status_code == 200):
        logger.info(
            "%s %s -> %d (%.2f ms)", method, path, response.status_code, duration_ms,
            extra={
                "method": method, "path": path,
                "status_code": response.status_code, "duration_ms": duration_ms,
            },
        )
    return response


# ---------- 工具函数 ----------

def _now_iso() -> str:
    """返回当前时间的 ISO 格式字符串。"""
    return datetime.now().isoformat()


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


# ---------- API 端点 ----------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """对话接口。

    流程:
        1. session_id 为空时调用 session_logger.create_session() 新建；
        2. 调用 orchestrator.chat(session_id, message) 获取回复；
        3. 返回会话 ID、回复与时间戳。
    异常时返回 500。
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")

    # 校验消息非空
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    try:
        session_id = req.session_id
        if not session_id:
            session_id = session_logger.create_session()
            logger.info("新建会话: %s", session_id)

        # Phase 9+ 取消支持：为非流式 /chat 创建 cancel_event
        cancel_event = None
        if stream_manager is not None:
            cancel_event = stream_manager.register(session_id)

        try:
            response_text = await orchestrator.chat(
                session_id, req.message, cancel_event=cancel_event,
            )
        finally:
            if stream_manager is not None and cancel_event is not None:
                stream_manager.unregister_event(session_id, cancel_event)

        return ChatResponse(
            session_id=session_id,
            response=response_text,
            timestamp=_now_iso(),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("处理 /chat 请求失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


def _sse_event(data: dict) -> str:
    """将 dict 序列化为 SSE 事件字符串（``data: <json>\\n\\n``）。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """流式对话接口（Server-Sent Events）。

    与 :http:post:`/chat` 等价，但通过 SSE 实时推送 LLM 文本增量与
    工具调用事件，前端可逐字渲染。

    SSE 事件格式（每行 ``data: <json>\\n\\n``）：
        - ``{"type": "session", "session_id": str, "timestamp": str}``：
          会话信息（首个事件，告知前端 session_id）。
        - ``{"type": "text", "text": str}``：
          LLM 输出文本增量。
        - ``{"type": "tool", "name": str, "input": dict, "result": str, "is_error": bool}``：
          工具调用事件。
        - ``{"type": "approval_request", "approval_id": str, "tool_name": str, "tool_input": dict, "reason": str, "risk_level": str}``：
          HIL 审批请求事件，前端应弹出审批确认框并调用
          ``POST /approvals/{approval_id}/resolve`` 提交决定。
        - ``{"type": "approval_resolved", "approval_id": str, "decision": str, "reason": str}``：
          审批决定事件，``decision`` 为 approve/deny。
        - ``{"type": "round_start", "loop_idx": int}``：
          ReactLoop 每轮循环开始事件，前端应为此轮创建独立的 streamMsg，
          避免多轮文本被 done.response 覆盖丢失。
        - ``{"type": "todo_init", "session_id": str, "todo": dict}``：
          plan_task 工具执行后事件，含完整 todo 列表（goal/steps/completed）。
        - ``{"type": "todo_update", "session_id": str, "todo": dict}``：
          update_todo 工具执行后事件，todo 字段为变更后的完整列表。
        - ``{"type": "todo_complete", "session_id": str, "todo": dict}``：
          所有 step 完成后事件，标记 plan 整体完成。
        - ``{"type": "done", "response": str, "timestamp": str}``：
          整个对话结束事件。
        - ``{"type": "error", "message": str}``：
          错误事件（流中途异常）。

    响应 Content-Type 为 ``text/event-stream``。
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")

    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    # 校验 / 创建 session_id（在生成器外完成，便于失败时直接返回 HTTP 错误）
    session_id = req.session_id
    if not session_id:
        try:
            session_id = session_logger.create_session()
            logger.info("新建会话 (stream): %s", session_id)
        except Exception as e:
            logger.exception("创建会话失败: %s", e)
            raise HTTPException(status_code=500, detail=f"创建会话失败: {e}")

    async def async_event_generator():
        """SSE 异步事件生成器（带 5min 硬超时控制）。

        客户端断连时 FastAPI 会调 generator.aclose() → 注入 GeneratorExit，
        try/finally 保证 StreamManager 资源释放。
        """
        cancel_event: Optional[threading.Event] = None
        if stream_manager is not None:
            cancel_event = stream_manager.register(session_id)
        breakpoint_detector = BreakpointDetector()
        accumulated_text = ""

        try:
            # 首个事件：会话信息
            yield _sse_event({
                "type": "session",
                "session_id": session_id,
                "timestamp": _now_iso(),
            })

            try:
                # 每次请求读取最新配置，支持 llm.activity_timeout /
                # llm.stream_total_timeout 热更新（无需重启即时生效）
                _req_cfg = load_config(CONFIG_PATH)
                _activity_timeout, _stream_total_timeout = get_llm_timeouts(_req_cfg)
                # 逐事件超时：每个事件完成后重置 _stream_total_timeout 倒计时，
                # 避免多轮工具调用的累计耗时超过单次硬限。
                # 只有 LLM 或工具真正卡住（300s 无任何事件）才会触发超时。
                _chat_stream = orchestrator.chat_stream(
                    session_id,
                    req.message,
                    cancel_event=cancel_event,
                    is_cron=False,
                    stream_manager=stream_manager,
                ).__aiter__()
                while True:
                    try:
                        event = await asyncio.wait_for(
                            _chat_stream.__anext__(),
                            timeout=_stream_total_timeout,
                        )
                    except StopAsyncIteration:
                        break

                    etype = event.get("type")

                    # graceful 模式断点检测（在 orchestrator 事件之间）
                    if etype == "text":
                        accumulated_text += event.get("text", "")
                    if (
                        stream_manager is not None
                        and stream_manager.is_graceful_pending(session_id)
                        and breakpoint_detector.should_break(accumulated_text)
                    ):
                        # 到达自然断点，触发中断
                        if cancel_event is not None:
                            cancel_event.set()
                        # 保存 InterruptNotice（含用户新消息）
                        graceful_msg = stream_manager.pop_graceful_message(
                            session_id
                        )
                        if orchestrator is not None:
                            orchestrator._save_interrupt_notice(
                                session_id, graceful_msg
                            )

                    # 中断检测
                    if cancel_event is not None and cancel_event.is_set():
                        yield _sse_event({"type": "interrupt"})
                        return

                    if etype == "done":
                        # done 事件补充 timestamp，透传 usage/reasoning_stats 等
                        # 白名单字段，不透传 messages（体积大）
                        done_evt = {
                            "type": "done",
                            "response": event.get("response", ""),
                            "timestamp": _now_iso(),
                            "is_complete": event.get("is_complete", True),
                            "termination_reason": event.get("termination_reason", "normal"),
                        }
                        # usage 字段透传（含 reasoning_tokens）
                        usage = event.get("usage")
                        if usage is not None:
                            done_evt["usage"] = usage
                        # reasoning_stats 字段透传（effort/budget/reasoning_tokens）
                        reasoning_stats = event.get("reasoning_stats")
                        if reasoning_stats is not None:
                            done_evt["reasoning_stats"] = reasoning_stats
                        # content_blocks 字段透传（含 thinking block，保留原始顺序）
                        content_blocks = event.get("content_blocks")
                        if content_blocks is not None:
                            done_evt["content_blocks"] = content_blocks
                        # stop_reason 字段透传
                        stop_reason = event.get("stop_reason")
                        if stop_reason is not None:
                            done_evt["stop_reason"] = stop_reason
                        yield _sse_event(done_evt)
                    elif etype == "reasoning":
                        # reasoning 增量事件原样透传（含 text/signature）
                        yield _sse_event(event)
                    else:
                        # text / tool / approval_request / approval_resolved /
                        # round_start / todo_init / todo_update / todo_complete
                        # 事件原样透传
                        yield _sse_event(event)
            except asyncio.TimeoutError:
                logger.warning(
                    "流式对话超时（%ss 无事件），session=%s",
                    _stream_total_timeout, session_id,
                )
                yield _sse_event({"type": "error", "message": "请求超时，请重试"})
                yield _sse_event({"type": "interrupt"})
            except StreamCancelled:
                logger.info("流被用户中断: session=%s", session_id)
                yield _sse_event({"type": "interrupt"})
            except ActivityTimeout:
                # per-token 活跃超时：LLM 在 activity_timeout 秒内未返任何 token
                logger.warning(
                    "LLM 响应超时（%ss 无输出），session=%s",
                    _activity_timeout, session_id,
                )
                yield _sse_event({
                    "type": "error",
                    "message": f"LLM 响应超时（{_activity_timeout}s 无输出）",
                })
                yield _sse_event({"type": "interrupt"})
            except Exception as e:
                logger.exception("流式对话失败: %s", e)
                yield _sse_event({"type": "error", "message": str(e)})
        finally:
            # 保证 GeneratorExit（客户端断连）/ 正常退出 / 异常都执行清理
            # Phase 9+ 所有权感知：使用 unregister_event 防止误删新流的 event
            if stream_manager is not None and cancel_event is not None:
                stream_manager.unregister_event(session_id, cancel_event)

    return StreamingResponse(
        async_event_generator(),
        media_type="text/event-stream",
        headers={
            # 禁用 nginx / 代理缓冲，确保实时推送
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/chat/cancel")
async def cancel_stream(req: CancelRequest):
    """中断正在进行的流式对话。

    模式：
    - ``immediate``：立即设置 cancel_event，LLM 输出在下一个 token 处停止
    - ``graceful``（首次）：暂存用户新消息，等待自然断点后自动中断
    - ``graceful``（二次）：已有 graceful pending 时再次调用 → force kill
      当前运行中的子进程（如 bash_exec 下载），立即中断并注入用户新消息

    返回 ``status`` 字段：
    - ``"cancelling"``：已设置取消信号
    - ``"breakpoint_pending"``：graceful 模式，已暂存消息，等待断点
    - ``"force_killed"``：二次 graceful，已强杀进程并注入消息
    - ``"already_ended"``：会话不存在或已结束（幂等）
    """
    if stream_manager is None:
        raise HTTPException(status_code=503, detail="StreamManager 尚未初始化")

    if req.mode == "graceful" and req.new_message:
        if stream_manager.is_graceful_pending(req.session_id):
            # 第二次点击 → force kill
            status, msg = stream_manager.force_cancel(req.session_id)
            if msg and orchestrator is not None:
                # 强杀当前子进程
                try:
                    from .agent.builtin_tools import kill_running_process
                    kill_running_process()
                except ImportError:
                    pass
                orchestrator._save_interrupt_notice(req.session_id, msg)
                logger.info(
                    "Force killed: session=%s, message injected", req.session_id
                )
            return {"status": "force_killed", "session_id": req.session_id}
        else:
            # 第一次点击 → graceful 等待
            stream_manager.register_graceful(req.session_id, req.new_message)
            logger.info("Graceful cancel pending: session=%s", req.session_id)
            return {"status": "breakpoint_pending", "session_id": req.session_id}

    ok = stream_manager.cancel(req.session_id)
    # immediate 模式主路径：trigger_cancel 调用注册的 cancel_callback
    # （``await stream.close()``）主动断开 LLM HTTP 连接，不等下一个 token。
    # 未注册 callback（流未启动或已结束）时返回 False，降级为仅 cancel_event
    # 兜底路径（在下一个 chunk 到达时检测 cancel_event.is_set()）。
    await stream_manager.trigger_cancel(req.session_id)
    status = "cancelling" if ok else "already_ended"
    logger.info("Cancel stream: session=%s -> %s", req.session_id, status)
    return {"status": status, "session_id": req.session_id}


class FlushResponse(BaseModel):
    status: str
    message: str
    pending_count: int = 0
    timestamp: str


@app.post("/consolidation/flush", response_model=FlushResponse)
def flush_consolidation(background_tasks: BackgroundTasks):
    """强制触发记忆沉淀（会话切换 / 手动 flush）。

    将 ConsolidationEngine 缓冲的 ``pending_messages`` 立即交给 LLM
    提取事实并写入长期记忆（chroma + memory.md），不判断阈值。

    该接口通过 ``BackgroundTasks`` 在后台异步执行 flush，立即返回 202，
    不阻塞前端。flush 涉及一次 LLM 调用，可能耗时数秒。

    返回:
        - ``status``: "accepted"（已接受，后台执行中）
        - ``pending_count``: 触发时的待沉淀消息数
        - ``timestamp``: ISO 时间戳
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")

    # 读取当前缓冲区大小（线程安全读取，flush 在后台执行）
    pending_count = 0
    if orchestrator.consolidation_engine is not None:
        pending_count = orchestrator.consolidation_engine.info_counter

    # 后台执行 flush（避免阻塞前端请求）
    background_tasks.add_task(orchestrator.flush_consolidation)

    msg = (
        f"已接受 flush 请求，后台执行中（待沉淀消息: {pending_count} 条）"
        if pending_count > 0
        else "缓冲区为空，无需 flush"
    )
    logger.info("POST /consolidation/flush: %s", msg)

    return FlushResponse(
        status="accepted",
        message=msg,
        pending_count=pending_count,
        timestamp=_now_iso(),
    )


@app.post("/reasoning/toggle")
def reasoning_toggle(req: dict = Body(...)):
    """切换 reasoning 模式开关（热更新，即时生效）。

    body: ``{"enabled": bool, "session_id": Optional[str]}``
    - ``enabled``: True 开启 / False 关闭
    - ``session_id``: 可选，当前单用户模式忽略（预留多用户扩展）

    鉴权预留：当前单用户模式不实装鉴权，未来注入 auth_hook 即可生效。
    启动期安全告警：若 security.api_key 未配置，启动时打印 WARNING。
    """
    global orchestrator
    if orchestrator is None or orchestrator.llm_client is None:
        return JSONResponse(
            content={"error": "LLMClient 尚未初始化"},
            status_code=503,
        )
    enabled = bool(req.get("enabled", False))
    # session_id 当前忽略（单用户模式），未来 per-session override 启用时使用
    # session_id = req.get("session_id")
    orchestrator.llm_client.main_reasoning_enabled = enabled
    logger.info(
        "reasoning 模式已切换: enabled=%s (main_reasoning_enabled)",
        enabled,
    )
    return {
        "enabled": enabled,
        "main": {
            "enabled": orchestrator.llm_client.main_reasoning_enabled,
            "effort": orchestrator.llm_client.main_reasoning_effort,
            "budget_tokens": orchestrator.llm_client.main_reasoning_budget_tokens,
        },
        "cron": {
            "enabled": orchestrator.llm_client.cron_reasoning_enabled,
            "effort": orchestrator.llm_client.cron_reasoning_effort,
        },
        "persist_thinking": orchestrator.llm_client.persist_thinking,
    }


@app.get("/reasoning/status")
def reasoning_status():
    """查询当前 reasoning 配置状态。

    返回 main/consolidation/cron 配置 + persist_thinking 值。
    鉴权预留：当前单用户模式不实装鉴权。
    """
    global orchestrator
    if orchestrator is None or orchestrator.llm_client is None:
        return JSONResponse(
            content={"error": "LLMClient 尚未初始化"},
            status_code=503,
        )
    return {
        "main": {
            "enabled": orchestrator.llm_client.main_reasoning_enabled,
            "effort": orchestrator.llm_client.main_reasoning_effort,
            "budget_tokens": orchestrator.llm_client.main_reasoning_budget_tokens,
        },
        "consolidation": {
            "enabled": False,  # 强制关闭，避免成本浪费
        },
        "cron": {
            "enabled": orchestrator.llm_client.cron_reasoning_enabled,
            "effort": orchestrator.llm_client.cron_reasoning_effort,
        },
        "persist_thinking": orchestrator.llm_client.persist_thinking,
    }


@app.get("/health")
def deep_health():
    """深度健康检查，逐层检测所有子系统组件。

    返回 JSON，含 overall status、summary 计数与各子系统检测结果。
    critical 级别组件不可用时返回 HTTP 503。
    """
    global health_checker
    if health_checker is None:
        return JSONResponse(
            content={
                "status": "unhealthy",
                "timestamp": _now_iso(),
                "version": VERSION,
                "summary": {"total": 0, "ok": 0, "warning": 0, "critical": 1},
                "checks": {"health_checker": {
                    "status": "critical",
                    "message": "HealthChecker 尚未初始化",
                    "detail": None,
                }},
            },
            status_code=503,
        )

    result = health_checker.run_all()
    result["version"] = VERSION
    status_code = 503 if result["status"] == "unhealthy" else 200
    return JSONResponse(content=result, status_code=status_code)


@app.get("/metrics")
def get_metrics():
    """返回当前指标快照。

    若监控未启用或 metrics_collector 未初始化，返回空字典。
    """
    if metrics_collector is None:
        return JSONResponse({})
    # 检查当前配置是否禁用监控（支持热更新 enabled=false）
    try:
        config = load_config(CONFIG_PATH)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({})
    except Exception:
        pass
    return JSONResponse(metrics_collector.snapshot())


@app.get("/metrics/history")
def get_metrics_history(days: int = Query(default=30, ge=1, le=90)):
    """返回最近 N 天的监控历史数据（按日期升序）。

    用于前端监控面板的历史趋势展示。若持久化未启用或 store 未初始化，
    返回空列表。
    """
    if metrics_store is None:
        return JSONResponse([])
    try:
        records = metrics_store.get_history(days)
        return JSONResponse(records)
    except Exception as e:
        logger.error("查询监控历史失败: %s", e)
        return JSONResponse([], status_code=500)


@app.post("/metrics/reset")
def reset_metrics():
    """重置所有指标计数器与直方图，并同步重置持久化 baseline。

    前端监控面板"重置指标"按钮调用。重置后：
    - metrics_collector 的所有计数器清零
    - metrics_persist_loop 的 baseline 同步重置，避免重置后 delta 丢失
    - 已持久化的历史数据不受影响
    """
    global _metrics_baseline_reset
    if metrics_collector is None:
        return JSONResponse({"ok": False, "error": "监控未启用"}, status_code=400)
    metrics_collector.reset()
    _metrics_baseline_reset = True
    logger.info("监控指标已重置（metrics_collector + baseline）")
    return JSONResponse({"ok": True})


@app.get("/metrics/signals")
def get_signals_metrics():
    """Phase 2 反馈监控：返回信号池仪表盘数据。

    用于监控面板渲染"攻略进度条"，按 section 分组展示信号累积状态。
    若 orchestrator 或 signal_pool 未初始化，返回空结构。
    """
    if orchestrator is None or getattr(orchestrator, "signal_pool", None) is None:
        return JSONResponse({
            "signals": [],
            "sections": {},
            "summary": {},
            "threshold": 7,
        })
    try:
        return JSONResponse(orchestrator.signal_pool.get_dashboard_data())
    except Exception as e:
        logger.warning("信号池仪表盘数据获取失败: %s", e)
        return JSONResponse({
            "signals": [],
            "sections": {},
            "summary": {},
            "threshold": 7,
            "error": str(e),
        })


@app.get("/tools")
async def list_tools_inventory():
    """返回当前工具清单（按 Tier 分类）。

    Phase 4 新增：暴露 Core/Deferred/Loaded 三层工具名称列表，
    便于运维与前端调试工具注册状态。

    返回:
        ``{"core": [...], "deferred": [...], "loaded": [...]}``
        - core: Core Tier 工具名列表（永远全量注入）
        - deferred: Deferred Tier 工具名列表（仅 stub，按需加载）
        - loaded: 已加载的 Deferred 工具名列表
    """
    if orchestrator is None or orchestrator.tool_registry is None:
        return {"core": [], "deferred": [], "loaded": []}
    registry = orchestrator.tool_registry
    return {
        "core": list(registry._core_tools.keys()),
        "deferred": list(registry._deferred_tools.keys()),
        "loaded": list(registry._loaded_tools.keys()),
    }


@app.get("/audit/logs")
def get_audit_logs(limit: int = Query(50, ge=1, le=1000)):
    """返回最近的工具调用审计日志。

    参数:
        limit: 返回条数，默认 50，范围 1-1000。
    """
    if audit_logger is None:
        return JSONResponse({"logs": []})
    # 检查当前配置是否禁用监控
    try:
        config = load_config(CONFIG_PATH)
        if not config.get("monitoring", {}).get("enabled", True):
            return JSONResponse({"logs": []})
    except Exception:
        pass
    return JSONResponse({"logs": audit_logger.get_recent(limit)})


@app.post("/approvals/{approval_id}/resolve", response_model=ApprovalResolveResponse)
def resolve_approval(approval_id: str, req: ApprovalResolveRequest):
    """提交审批决定。

    - approval_manager 未初始化 → 503
    - decision 不在 ("approve", "deny") → 400
    - approval_manager.resolve 返回 False → 404
    - 成功 → 200 ApprovalResolveResponse
    """
    if approval_manager is None:
        raise HTTPException(status_code=503, detail="审批管理器未初始化")
    if req.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="decision 必须是 approve 或 deny")
    ok = approval_manager.resolve(approval_id, req.decision, req.reason)
    if not ok:
        raise HTTPException(status_code=404, detail="审批请求不存在或已处理")
    # Phase 2 反馈监控：用户主动 approve/deny 上报（timeout 在 approval.py 内独立上报）
    if metrics_collector is not None:
        try:
            metrics_collector.observe_approval_decision(req.decision)
        except Exception:
            pass
    logger.info("审批 %s 已 %s", approval_id, req.decision)
    return ApprovalResolveResponse(
        status="resolved",
        approval_id=approval_id,
        decision=req.decision,
    )


@app.get("/approvals", response_model=ApprovalListResponse)
def list_approvals():
    """列出所有 pending 状态的审批请求。

    approval_manager 未初始化时返回空列表（不报错）。
    """
    if approval_manager is None:
        return ApprovalListResponse(pending=[])
    items = approval_manager.list_pending()
    return ApprovalListResponse(
        pending=[ApprovalListItem(**item) for item in items]
    )


# ---------- Phase 6: 调度管理端点 ----------

@app.get("/schedules", response_model=ScheduleListResponse)
def list_schedules():
    """列出所有调度项。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    return ScheduleListResponse(schedules=cron_scheduler.list_schedules())


@app.get("/schedules/runs")
def list_recent_runs(limit: int = Query(20, ge=1, le=100, description="返回条数上限")):
    """跨调度项读取最近 N 条执行记录（带时间戳）。

    遍历所有调度项的 ``runs.jsonl``，合并按 ``started_at`` 倒序，取前 ``limit``
    条。每个 run 含 ``schedule_id`` / ``schedule_name`` / ``started_at`` /
    ``finished_at`` / ``success`` / ``llm_summary`` / ``tool_calls`` 等字段。

    用于前端「调度会话」区块默认展示带时间戳的执行历史。

    参数:
        limit: 返回条数上限，默认 20，范围 1-100。

    返回:
        ``{"runs": [RunSummary.to_dict() + schedule_name], "total": N}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    runs_store = getattr(cron_scheduler, "runs_store", None)
    if runs_store is None:
        # runs_store 懒初始化，未触发过执行时可能为 None
        return {"runs": [], "total": 0}
    try:
        schedules = cron_scheduler.list_schedules()
        runs = runs_store.read_recent_all(schedules, n=limit)
    except Exception as e:
        logger.exception("查询跨调度项执行历史失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询执行历史失败: {e}")
    return {"runs": runs, "total": len(runs)}


@app.post("/schedules", response_model=ScheduleResponse)
def create_schedule(req: ScheduleCreateRequest):
    """新增调度项。cron 非法返回 400。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    # 先校验 cron 合法性
    if CronExpr is not None:
        try:
            CronExpr(req.cron)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"cron 表达式非法: {e}")
    # 校验 workflow 配置（若提供）
    if req.workflow:
        try:
            from .tasks.workflow import WorkflowSpec
            WorkflowSpec.from_dict(req.workflow)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"workflow 配置非法: {e}")
        except ImportError:
            # workflow 模块不可用时仅记录，不阻断创建（向后兼容）
            logger.warning("workflow 模块不可用，跳过 workflow 配置校验")
    try:
        sched_id = cron_scheduler.add_schedule({
            "id": req.id,
            "name": req.name,
            "cron": req.cron,
            "task": req.task,
            "enabled": req.enabled,
            "workflow": req.workflow,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ScheduleResponse(schedule_id=sched_id, message="已创建调度项")


@app.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: str, req: ScheduleUpdateRequest):
    """更新调度项。仅 enabled 即时生效；cron/task/name 变更需重启。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    fields = req.model_dump(exclude_none=True)
    # 校验 cron 合法性（如提供）
    if "cron" in fields and CronExpr is not None:
        try:
            CronExpr(fields["cron"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"cron 表达式非法: {e}")
    try:
        ok = cron_scheduler.update_schedule(schedule_id, fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="调度项不存在")
    # 判断是否需要重启：仅 enabled 变更不需要
    needs_restart = any(k in fields for k in ("cron", "task", "name", "workflow"))
    return {"status": "ok", "needs_restart": needs_restart}


@app.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    """删除调度项。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    ok = cron_scheduler.delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="调度项不存在")
    return {"status": "ok"}


@app.post("/schedules/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: str):
    """立即触发调度项一次。"""
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    # 异步触发，不阻塞响应
    asyncio.create_task(cron_scheduler.trigger_now(orchestrator, schedule_id))
    return {"status": "ok", "message": "已触发"}


# ---------- Phase 8 Task 1.8: 调度执行历史端点 ----------


@app.get("/schedules/{schedule_id}/history")
def get_schedule_history(
    schedule_id: str,
    limit: int = Query(10, ge=1, le=100, description="返回条数上限"),
):
    """获取指定调度项的执行历史（最近 N 条 assistant 回复）。

    Phase 8 Task 1.8。按 ``session_id="cron:{schedule_id}"`` 查 session_logger，
    取最近 ``limit`` 条 assistant 角色消息（倒序，最新在前）。每条返回
    id / content / created_at / tool_name。

    调度项不存在时返回 404；session_logger 未初始化时返回 503。

    返回:
        ``{"history": [{id, content, created_at, tool_name}], "total": N}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    # 校验调度项存在
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if session_logger is None:
        raise HTTPException(status_code=503, detail="session_logger 尚未初始化")
    cron_session_id = f"cron:{schedule_id}"
    try:
        rows = session_logger.get_recent_role_messages(
            cron_session_id, "assistant", limit=limit
        )
    except Exception as e:
        logger.exception("查询调度执行历史失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询历史失败: {e}")
    history = [
        {
            "id": r.get("id"),
            "content": r.get("content", ""),
            "created_at": r.get("created_at", ""),
            "tool_name": r.get("tool_name"),
        }
        for r in rows
    ]
    return {"history": history, "total": len(history)}


# ---------- Phase 8 Task 4.5: 调度审计端点 ----------


@app.get("/schedules/{schedule_id}/audit")
def get_schedule_audit(
    schedule_id: str,
    limit: int = Query(20, ge=1, le=500, description="返回条数上限"),
    run_id: Optional[str] = Query(None, description="可选执行批次 ID 过滤"),
):
    """获取指定调度项的工具调用审计日志。

    Phase 8 Task 4.5。调 ``audit_logger.get_by_schedule(schedule_id, limit,
    run_id)`` 返回该调度项最近 ``limit`` 条工具调用审计记录（倒序，最新在前）。
    可选 ``run_id`` 查询参数进一步按执行批次筛选。

    每条记录含 timestamp / session_id / tool_name / tool_input / result /
    is_error / duration_ms / decision_source / schedule_id / run_id 字段。
    旧记录缺少 Task 4.4 新字段时由 ``_normalize_entry`` 填充默认值。

    调度项不存在时返回 404；audit_logger 未初始化时返回空列表（向后兼容）。
    监控禁用时返回空列表（与 ``/audit/logs`` 行为一致）。

    返回:
        ``{"logs": [...], "total": N}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if audit_logger is None:
        return {"logs": [], "total": 0}
    # 监控禁用时返回空列表（与 /audit/logs 行为一致）
    try:
        config = load_config(CONFIG_PATH)
        if not config.get("monitoring", {}).get("enabled", True):
            return {"logs": [], "total": 0}
    except Exception:
        pass
    try:
        logs = audit_logger.get_by_schedule(
            schedule_id, limit=limit, run_id=run_id
        )
    except Exception as e:
        logger.exception("查询调度审计日志失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询审计日志失败: {e}")
    return {"logs": logs, "total": len(logs)}


@app.get("/schedules/{schedule_id}/audit/{run_id}")
def get_schedule_audit_by_run(schedule_id: str, run_id: str):
    """获取指定调度项某次执行批次的工具调用审计日志。

    Phase 8 Task 4.5。调 ``audit_logger.get_by_run_id(schedule_id, run_id)``
    返回该调度项指定 ``run_id`` 的所有工具调用审计记录（正序，最早在前，
    便于按执行时间线复盘）。

    调度项不存在时返回 404；audit_logger 未初始化时返回空列表；监控禁用时
    返回空列表。

    返回:
        ``{"logs": [...], "total": N, "run_id": "<run_id>"}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if audit_logger is None:
        return {"logs": [], "total": 0, "run_id": run_id}
    try:
        config = load_config(CONFIG_PATH)
        if not config.get("monitoring", {}).get("enabled", True):
            return {"logs": [], "total": 0, "run_id": run_id}
    except Exception:
        pass
    try:
        logs = audit_logger.get_by_run_id(schedule_id, run_id)
    except Exception as e:
        logger.exception("查询调度审计日志（run_id）失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询审计日志失败: {e}")
    return {"logs": logs, "total": len(logs), "run_id": run_id}


# ---------- Phase 8 Task 1.9: 调度记忆端点 ----------


@app.get("/schedules/{schedule_id}/memories")
def list_schedule_memories(
    schedule_id: str,
    limit: int = Query(20, ge=1, le=200, description="返回条数上限"),
):
    """列出指定调度项的隔离记忆（namespace=cron, cron_id=schedule_id）。

    Phase 8 Task 1.9。调 ``chroma_store.get_all_memories(namespace="cron",
    cron_id=schedule_id)`` 取该调度项独占的记忆条目，截断到 ``limit``。

    调度项不存在时返回 404；ChromaMemoryStore 未初始化时返回 503。

    返回:
        ``{"memories": [{id, content, metadata}], "total": N}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    # 用 cron_id 解析后的值（cron_id 字段为 None 时回退到 id）
    cron_id = schedule.get_cron_id()
    try:
        raw = orchestrator.chroma_store.get_all_memories(
            namespace="cron", cron_id=cron_id
        )
    except Exception as e:
        logger.exception("查询调度记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"查询记忆失败: {e}")
    memories = [
        {
            "id": item.get("id", ""),
            "content": item.get("content", ""),
            "metadata": item.get("metadata", {}),
        }
        for item in raw[:limit]
    ]
    return {"memories": memories, "total": len(memories)}


@app.delete("/schedules/{schedule_id}/memories/{memory_id}")
def delete_schedule_memory(schedule_id: str, memory_id: str):
    """删除指定调度项的某条隔离记忆。

    Phase 8 Task 1.9。**校验 ``cron_id`` 一致**防止跨调度项误删：先查
    ``get_all_memories(namespace="cron", cron_id=schedule.get_cron_id())``，
    若 ``memory_id`` 不在结果集中返回 404（既覆盖记忆不存在，也覆盖
    memory_id 属于其他调度项的情况）。

    调度项不存在时返回 404；ChromaMemoryStore 未初始化时返回 503。

    返回:
        ``{"status": "ok", "deleted_id": memory_id}``
    """
    if cron_scheduler is None:
        raise HTTPException(status_code=503, detail="CronScheduler 尚未初始化")
    schedule = cron_scheduler._find_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="调度项不存在")
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    cron_id = schedule.get_cron_id()
    try:
        raw = orchestrator.chroma_store.get_all_memories(
            namespace="cron", cron_id=cron_id
        )
    except Exception as e:
        logger.exception("校验调度记忆归属失败: %s", e)
        raise HTTPException(status_code=500, detail=f"校验记忆失败: {e}")
    # 校验 memory_id 属于该调度项（cron_id 一致）
    existing_ids = {item.get("id") for item in raw}
    if memory_id not in existing_ids:
        raise HTTPException(
            status_code=404,
            detail=f"记忆 {memory_id} 不存在或不属于调度项 {schedule_id}",
        )
    try:
        orchestrator.chroma_store.delete_memory(memory_id)
    except Exception as e:
        logger.exception("删除调度记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"删除记忆失败: {e}")
    logger.info(
        "已删除调度项 %s 的隔离记忆: %s", schedule_id, memory_id
    )
    return {"status": "ok", "deleted_id": memory_id}


# ---------- Phase 8 Task 3.8: 提议-确认协议端点 ----------


class ProposalModifyRequest(BaseModel):
    """提议修改请求体（修改并确认）。

    用户在前端确认卡片上修改 cron 表达式 / granted_tools 后点击「修改并确认」
    时提交。所有字段可选，仅提供的字段会被更新（浅合并到原 schedule_config；
    requested_tools 整体替换）。
    """

    schedule_config_updates: Optional[Dict[str, Any]] = Field(
        default=None,
        description="调度配置更新字段（浅合并到原配置），可含 name/cron/task/enabled/workflow 等",
    )
    requested_tools: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="新的请求工具列表（整体替换），每项含 tool/scope/allowed_paths",
    )


@app.get("/proposals")
def list_proposals():
    """列出所有提议（按创建顺序）。

    Phase 8 Task 3.8。返回 ``proposal_store`` 中所有提议的 dict 列表，
    含 proposal_id / schedule_config / requested_tools / llm_explanation /
    status / created_at / schedule_id 字段。

    proposal_store 未初始化时返回 503。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    proposals = proposal_store.list()
    return {"proposals": [p.to_dict() for p in proposals], "total": len(proposals)}


@app.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    """获取指定提议详情。

    Phase 8 Task 3.8。提议不存在时返回 404。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")
    return proposal.to_dict()


@app.post("/proposals/{proposal_id}/confirm")
def confirm_proposal(proposal_id: str):
    """确认提议：``pending_confirm → confirmed``，随后创建调度项。

    Phase 8 Task 3.8。流程：
    1. 调 ``proposal_store.confirm`` 将状态转为 ``confirmed``。
    2. 调用 ``create_schedule`` 工具逻辑（通过 tool_registry.execute_tool）
       创建调度项，锁定 active_tools_snapshot。
    3. 返回创建结果（含 schedule_id）。

    提议不存在或状态不允许转换时返回 404；调度项创建失败时返回 500。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")

    # 1. 确认提议（pending_confirm → confirmed）
    ok = proposal_store.confirm(proposal_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"提议 {proposal_id} 当前状态为 {proposal.status}，无法确认",
        )

    # 2. 调用 create_schedule 工具创建调度项
    result_str = orchestrator.tool_registry.execute_tool(
        "cron_create", {"proposal_id": proposal_id}
    )
    # create_schedule 返回 JSON 字符串，解析判断成功/失败
    try:
        result = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        result = {"raw": result_str}

    if "schedule_id" not in result:
        # 创建失败，返回错误详情
        raise HTTPException(
            status_code=500,
            detail=f"创建调度项失败: {result.get('raw', result_str)}",
        )
    logger.info(
        "提议 %s 已确认并创建调度项 %s", proposal_id, result["schedule_id"]
    )
    return {
        "status": "schedule_active",
        "proposal_id": proposal_id,
        "schedule_id": result["schedule_id"],
        "active_tools_snapshot": result.get("active_tools_snapshot", []),
        "message": "提议已确认，调度项已创建",
    }


@app.post("/proposals/{proposal_id}/modify")
def modify_proposal(proposal_id: str, req: ProposalModifyRequest):
    """修改并确认提议：``pending_confirm → modified``，随后创建调度项。

    Phase 8 Task 3.8。流程：
    1. 调 ``proposal_store.modify`` 应用修改并将状态转为 ``modified``。
    2. 调用 ``create_schedule`` 工具逻辑创建调度项（用修改后的配置）。
    3. 返回创建结果。

    提议不存在或状态不允许转换时返回 404。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")

    # 1. 修改并确认（pending_confirm → modified）
    ok = proposal_store.modify(
        proposal_id,
        schedule_config_updates=req.schedule_config_updates,
        requested_tools=req.requested_tools,
    )
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"提议 {proposal_id} 当前状态为 {proposal.status}，无法修改",
        )

    # 2. 调用 create_schedule 工具创建调度项（用修改后的配置）
    result_str = orchestrator.tool_registry.execute_tool(
        "cron_create", {"proposal_id": proposal_id}
    )
    try:
        result = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        result = {"raw": result_str}

    if "schedule_id" not in result:
        raise HTTPException(
            status_code=500,
            detail=f"创建调度项失败: {result.get('raw', result_str)}",
        )
    logger.info(
        "提议 %s 已修改并创建调度项 %s", proposal_id, result["schedule_id"]
    )
    return {
        "status": "schedule_active",
        "proposal_id": proposal_id,
        "schedule_id": result["schedule_id"],
        "active_tools_snapshot": result.get("active_tools_snapshot", []),
        "message": "提议已修改并确认，调度项已创建",
    }


@app.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str):
    """拒绝提议：``pending_confirm → rejected``，不创建调度项。

    Phase 8 Task 3.8。提议不存在或状态不允许转换时返回 404/409。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")

    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")

    ok = proposal_store.reject(proposal_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"提议 {proposal_id} 当前状态为 {proposal.status}，无法拒绝",
        )
    logger.info("提议 %s 已被拒绝", proposal_id)
    return {
        "status": "rejected",
        "proposal_id": proposal_id,
        "message": "提议已拒绝，不会创建调度项",
    }


# ---------- Phase 8 Task 5.5 / 5.6: cron_tool 管理端点 ----------


def _read_cron_tool_dir(tool_dir: Path) -> Dict[str, Any]:
    """读取 cron_tool 目录下的 TOOL.md 与 run.* 脚本内容。

    参数:
        tool_dir: 工具目录路径。

    返回:
        dict，含 ``tool_md`` / ``run_script`` / ``run_ext`` 字段。
        文件缺失时对应字段为空字符串。
    """
    import os

    result = {"tool_md": "", "run_script": "", "run_ext": ""}
    tool_md_path = tool_dir / "TOOL.md"
    if tool_md_path.is_file():
        try:
            result["tool_md"] = tool_md_path.read_text(encoding="utf-8")
        except OSError:
            pass
    for ext in (".py", ".sh", ".js"):
        run_path = tool_dir / f"run{ext}"
        if run_path.is_file():
            try:
                result["run_script"] = run_path.read_text(encoding="utf-8")
                result["run_ext"] = ext
            except OSError:
                pass
            break
    return result


@app.get("/cron_tools/pending")
def list_pending_cron_tools():
    """列出所有待审查的 cron_tool（.pending/ 下）。

    Phase 8 Task 5.5。返回每个待审查工具的 TOOL.md / run.* 内容，
    供前端审查卡片渲染。

    cron_tool 模块未初始化时返回 503。
    """
    if _list_pending_cron_tools is None:
        raise HTTPException(status_code=503, detail="cron_tool 模块尚未初始化")
    base_dir = _CRON_TOOL_BASE_DIR
    names = _list_pending_cron_tools(base_dir=base_dir)
    items = []
    for name in names:
        tool_dir = Path(base_dir) / ".pending" / name
        files = _read_cron_tool_dir(tool_dir)
        items.append(
            {
                "name": name,
                "tool_md": files["tool_md"],
                "run_script": files["run_script"],
                "run_ext": files["run_ext"],
            }
        )
    return {"pending": items, "total": len(items)}


@app.post("/cron_tools/{name}/activate")
def activate_cron_tool(name: str):
    """激活待审查的 cron_tool：从 .pending/ 移到 cron_tool/{name}/，注册到 registry。

    Phase 8 Task 5.5。流程：
    1. 校验 ``.pending/{name}/`` 存在。
    2. 覆盖保护：``cron_tool/{name}/`` 已存在时返回 409。
    3. 移动目录（``.pending/{name}/`` → ``cron_tool/{name}/``）。
    4. 调 ``cron_tool_registry.register`` 注册到独立 registry。
    5. 返回激活结果。

    激活失败时回滚（移回 .pending/）。
    """
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")

    base_dir = _CRON_TOOL_BASE_DIR
    pending_path = Path(base_dir) / ".pending" / name
    active_path = Path(base_dir) / name

    if not pending_path.is_dir():
        raise HTTPException(
            status_code=404, detail=f"待审查工具 {name} 不存在"
        )
    if active_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"已激活工具 {name} 已存在，请先删除再激活",
        )

    # 移动目录
    try:
        shutil.move(str(pending_path), str(active_path))
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"移动目录失败: {exc}"
        ) from exc

    # 注册到 registry
    try:
        meta = cron_tool_registry.register(name)
    except CronToolError as exc:
        # 注册失败：回滚（移回 .pending/）
        try:
            shutil.move(str(active_path), str(pending_path))
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail=f"激活失败（TOOL.md 解析错误）: {exc}，已回滚",
        ) from exc
    logger.info("cron_tool %s 已激活并注册（version=%s）", name, meta.version)
    return {
        "status": "activated",
        "tool_name": name,
        "version": meta.version,
        "message": f"cron_tool {name} 已激活并注册到 cron_tool_registry",
    }


@app.post("/cron_tools/{name}/reject")
def reject_cron_tool(name: str):
    """拒绝待审查的 cron_tool：删除 .pending/{name}/。

    Phase 8 Task 5.5。
    """
    if _list_pending_cron_tools is None:
        raise HTTPException(status_code=503, detail="cron_tool 模块尚未初始化")
    base_dir = _CRON_TOOL_BASE_DIR
    pending_path = Path(base_dir) / ".pending" / name
    if not pending_path.is_dir():
        raise HTTPException(
            status_code=404, detail=f"待审查工具 {name} 不存在"
        )
    try:
        shutil.rmtree(str(pending_path))
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"删除目录失败: {exc}"
        ) from exc
    logger.info("cron_tool %s 已被拒绝并删除", name)
    return {
        "status": "rejected",
        "tool_name": name,
        "message": f"cron_tool {name} 已被拒绝并删除",
    }


# ---------- Phase 8 Task 5.6: cron_tool 管理端点（已激活工具）----------


@app.get("/cron_tools")
def list_cron_tools():
    """列出所有已激活的 cron_tool。

    Phase 8 Task 5.6。返回每个工具的元数据（name/version/description/timeout）。
    """
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")
    items = []
    for name in cron_tool_registry.list_tool_names():
        meta = cron_tool_registry.get_tool_meta(name)
        if meta is None:
            continue
        items.append(
            {
                "name": meta.name,
                "version": meta.version,
                "description": meta.description,
                "author": meta.author,
                "timeout": meta.timeout,
            }
        )
    return {"cron_tools": items, "total": len(items)}


@app.delete("/cron_tools/{name}")
def delete_cron_tool(name: str):
    """删除已激活的 cron_tool：从 registry 注销 + 删除目录。

    Phase 8 Task 5.6。
    """
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")
    base_dir = _CRON_TOOL_BASE_DIR
    active_path = Path(base_dir) / name
    if not active_path.is_dir():
        raise HTTPException(
            status_code=404, detail=f"cron_tool {name} 不存在"
        )
    # 先从 registry 注销
    cron_tool_registry.unregister(name)
    # 再删除目录
    try:
        shutil.rmtree(str(active_path))
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"删除目录失败: {exc}"
        ) from exc
    logger.info("cron_tool %s 已删除并从 registry 注销", name)
    return {
        "status": "deleted",
        "tool_name": name,
        "message": f"cron_tool {name} 已删除并从 registry 注销",
    }


@app.put("/cron_tools/{name}")
def reload_cron_tool(name: str):
    """重新加载 cron_tool（编辑 TOOL.md / run.* 后调用）。

    Phase 8 Task 5.6。从磁盘重新解析 TOOL.md 并覆盖 registry 中的 meta。
    """
    if cron_tool_registry is None:
        raise HTTPException(status_code=503, detail="CronToolRegistry 尚未初始化")
    try:
        meta = cron_tool_registry.reload(name)
    except CronToolError as exc:
        raise HTTPException(
            status_code=400, detail=f"重新加载失败: {exc}"
        ) from exc
    logger.info("cron_tool %s 已重新加载（version=%s）", name, meta.version)
    return {
        "status": "reloaded",
        "tool_name": name,
        "version": meta.version,
        "message": f"cron_tool {name} 已重新加载",
    }


# ---------- Phase 8 Task 6: Skill 管理端点 ----------


@app.get("/skills")
def list_skills():
    """列出所有已发现的 Skill，含启用/禁用状态。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    try:
        discovered = skill_loader.discover()
    except Exception:
        discovered = []
    skill_state = _load_skill_state()
    disabled_list = skill_state.get("disabled", [])
    skills = []
    for meta in discovered:
        skills.append({
            "name": meta.name,
            "version": meta.version,
            "description": meta.description,
            "disabled": meta.name in disabled_list,
        })
    return {"skills": skills, "total": len(skills)}


@app.get("/skills/{name}")
def get_skill(name: str):
    """获取指定 Skill 的详细信息。

    基于 SkillMeta（来自 ``_metas`` 缓存或 ``discover()``）返回元数据，
    不再调 ``skill_loader.load()`` 读空的 ``tools.py``。同时返回软禁用状态
    与 stub 注册状态。
    """
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    # 优先从 _metas 缓存取 meta，未命中则 discover 一次刷新缓存
    meta = None
    if hasattr(skill_loader, "_metas"):
        meta = skill_loader._metas.get(name)
    if meta is None:
        try:
            for m in skill_loader.discover():
                if m.name == name:
                    meta = m
                    break
        except Exception as e:
            logger.warning("discover 扫描失败: %s", e)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' 不存在")

    registry = orchestrator.tool_registry
    # 公开 API get_full_schema 返回空 dict 表示工具未注册
    stub_schema = registry.get_full_schema(f"skill__{name}") if hasattr(registry, "get_full_schema") else {}
    return {
        "name": meta.name,
        "version": meta.version,
        "description": meta.description,
        "body_preview": (meta.body or "")[:200],
        "resources": meta.resources or [],
        "disabled": registry.is_skill_disabled(name),
        "stub_registered": bool(stub_schema),
    }


@app.post("/skills/{name}/reload")
def reload_skill(name: str):
    """热重载指定 Skill：双路径注册（stub 激活按钮 + 业务工具）。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    try:
        registry = orchestrator.tool_registry

        # 清除前缀匹配的旧业务工具（skill__{name}__*）
        # 注意：保留 skill__{name} 激活按钮本身，由 register_skill_stub 覆盖更新
        prefix = f"skill__{name}__"
        for store_key in ("_core_tools", "_deferred_tools", "_loaded_tools"):
            store = getattr(registry, store_key, {})
            for tname in list(store.keys()):
                if tname.startswith(prefix):
                    registry.unregister(tname)
        # 同步移除旧激活按钮（register_skill_stub 会重新注册）
        try:
            registry.unregister(f"skill__{name}")
        except Exception:
            pass

        # 重新加载 Skill（reload 会清 _skills/_metas 缓存并重新 import）
        skill = skill_loader.reload(name)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' 不存在或加载失败")

        stub_registered = False

        # 新路径：注册 skill__{name} 激活按钮到 Core Tier
        if register_skill_stub is not None and _make_skill_activate_handler is not None:
            try:
                # 获取最新 meta（reload 已刷新 _metas 缓存）
                meta = None
                if hasattr(skill_loader, "_metas"):
                    meta = skill_loader._metas.get(name)
                if meta is None and hasattr(skill_loader, "_parse_meta"):
                    skill_file = Path(skill_loader.skill_dir) / name / "SKILL.md"
                    if skill_file.exists():
                        meta = skill_loader._parse_meta(skill_file)
                if meta is not None:
                    handler = _make_skill_activate_handler(
                        skill_loader, orchestrator, name
                    )
                    register_skill_stub(registry, meta, handler)
                    stub_registered = True
            except Exception as e:
                logger.error("reload 时注册 skill stub %s 失败: %s", name, e)

        # 旧路径：注册业务工具到 Deferred Tier（向后兼容，tools.py 非空时）
        if skill.tools:
            load_skill_to_registry(registry, skill)

        logger.info(
            "Skill 已热重载: %s（stub=%s，%d 个业务工具）",
            name, stub_registered, len(skill.tools),
        )
        return {
            "status": "reloaded",
            "skill_name": name,
            "tool_count": len(skill.tools),
            "stub_registered": stub_registered,
            "message": f"Skill '{name}' 已重新加载",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Skill 热重载失败: %s", e)
        raise HTTPException(status_code=500, detail=f"热重载失败: {e}")


@app.post("/skills/{name}/toggle")
def toggle_skill(name: str):
    """切换 Skill 启用/禁用状态。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    state = _load_skill_state()
    disabled_list = state.get("disabled", [])
    name_lower = name.lower()

    # 检查锁定列表
    locked_list = state.get("locked", [])
    if any(ln.lower() == name_lower for ln in locked_list):
        raise HTTPException(status_code=403, detail=f"Skill '{name}' 已锁定，不可切换")

    if name in disabled_list:
        # 启用：从禁用清单移除，清除软禁用标记，重新加载并走双路径注册
        disabled_list.remove(name)
        state["disabled"] = disabled_list
        _save_skill_state(state)

        # 清除软禁用标记（使 schema 中 enabled: False 移除）
        orchestrator.tool_registry.enable_skill(name)

        stub_registered = False
        try:
            skill_loader.unload(name)
            skill = skill_loader.load(name)
            if skill is not None:
                # 新路径：注册 skill__{name} 激活按钮到 Core Tier
                if register_skill_stub is not None and _make_skill_activate_handler is not None:
                    try:
                        meta = None
                        if hasattr(skill_loader, "_metas"):
                            meta = skill_loader._metas.get(name)
                        if meta is None and hasattr(skill_loader, "_parse_meta"):
                            skill_file = Path(skill_loader.skill_dir) / name / "SKILL.md"
                            if skill_file.exists():
                                meta = skill_loader._parse_meta(skill_file)
                        if meta is not None:
                            handler = _make_skill_activate_handler(
                                skill_loader, orchestrator, name
                            )
                            register_skill_stub(orchestrator.tool_registry, meta, handler)
                            stub_registered = True
                    except Exception as e:
                        logger.error("启用 Skill 时注册 stub %s 失败: %s", name, e)

                # 旧路径：注册业务工具到 Deferred Tier（向后兼容）
                if skill.tools:
                    load_skill_to_registry(orchestrator.tool_registry, skill)
        except Exception as e:
            logger.warning("启用 Skill 后重新加载失败: %s", e)
        logger.info("Skill '%s' 已启用（stub=%s）", name, stub_registered)
        return {
            "status": "enabled",
            "skill_name": name,
            "stub_registered": stub_registered,
            "message": f"Skill '{name}' 已启用",
        }
    else:
        # 禁用：软禁用（schema 标 enabled: False，执行抛 ToolNotFoundError），
        # 工具仍保留在 registry 中保持 schema 稳定，记入禁用清单
        orchestrator.tool_registry.disable_skill(name)
        disabled_list.append(name)
        state["disabled"] = disabled_list
        _save_skill_state(state)
        logger.info("Skill '%s' 已禁用（软禁用）", name)
        return {
            "status": "disabled",
            "skill_name": name,
            "message": f"Skill '{name}' 已禁用",
        }


@app.delete("/skills/{name}")
def delete_skill(name: str):
    """注销并删除指定的 Skill。"""
    if skill_loader is None:
        raise HTTPException(status_code=503, detail="SkillLoader 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    # 从 registry 卸载工具
    prefix = f"skill__{name}__"
    for store_key in ("_core_tools", "_deferred_tools", "_loaded_tools"):
        store = getattr(orchestrator.tool_registry, store_key, {})
        for tname in list(store.keys()):
            if tname.startswith(prefix):
                orchestrator.tool_registry.unregister(tname)

    # 清理加载缓存
    skill_loader.unload(name)

    # 删除磁盘目录
    skill_dir = skill_loader.skill_dir / name
    if skill_dir.exists():
        try:
            shutil.rmtree(str(skill_dir))
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"删除 Skill 目录失败: {e}")

    # 清理状态
    state = _load_skill_state()
    disabled_list = state.get("disabled", [])
    if name in disabled_list:
        disabled_list.remove(name)
        state["disabled"] = disabled_list
        _save_skill_state(state)

    logger.info("Skill '%s' 已删除", name)
    return {
        "status": "deleted",
        "skill_name": name,
        "message": f"Skill '{name}' 已注销并删除",
    }


@app.get("/sessions", response_model=SessionListResponse)
def list_sessions(exclude_cron: bool = False, cron_only: bool = False):
    """列出所有会话。

    cron 会话（session_id 形如 ``cron:{schedule_id}``）若未设置 title，
    在此回退到 schedule.name，避免 cron 会话显示随机串。

    查询参数：
    - ``exclude_cron=true``：过滤掉 cron 会话（聊天页使用，避免调度会话污染会话列表）
    - ``cron_only=true``：仅返回 cron 会话（调度页切换器使用）
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        sessions = session_logger.list_sessions()
        items = []
        for s in sessions:
            sid = s.get("id", "")
            is_cron = sid.startswith("cron:")
            # 过滤参数互斥处理
            if exclude_cron and is_cron:
                continue
            if cron_only and not is_cron:
                continue
            title = s.get("title")
            # cron 会话兜底：title 缺失时查 CronScheduler 取 schedule.name
            if not title and is_cron and cron_scheduler is not None:
                sched_id = sid[len("cron:"):]
                try:
                    sched = cron_scheduler.get_schedule(sched_id)
                    if sched is not None:
                        title = sched.get("name")
                except Exception:
                    pass
            items.append(
                SessionItem(
                    id=sid,
                    created_at=s.get("created_at", ""),
                    updated_at=s.get("updated_at", ""),
                    title=title,
                )
            )
        return SessionListResponse(sessions=items)
    except Exception as e:
        logger.exception("列出会话失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.get("/sessions/{session_id}/messages", response_model=MessageListResponse)
def get_session_messages(
    session_id: str,
    limit: Optional[int] = Query(
        default=None, ge=1, description="限制返回数量"
    ),
):
    """获取指定会话的消息历史。

    可通过 limit 查询参数限制返回条数。
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        rows = session_logger.get_session_messages(session_id, limit=limit)
        messages = [
            MessageItem(
                role=r.get("role", ""),
                content=r.get("content", ""),
                created_at=r.get("created_at", ""),
                tool_name=r.get("tool_name"),
                tool_call_id=r.get("tool_call_id"),
                is_error=bool(r["is_error"]) if "is_error" in r.keys() else None,
                attachments=r.get("attachments"),
                message_type=r.get("message_type"),
                reasoning=r.get("reasoning"),
            )
            for r in rows
        ]
        return MessageListResponse(messages=messages)
    except Exception as e:
        logger.exception("获取会话消息失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.patch("/sessions/{session_id}")
def update_session_title(session_id: str, body: SessionTitleUpdate):
    """更新指定会话的标题（Task 0 PATCH 端点）。

    请求体：``{"title": "新标题"}``，长度 1-100 字符（由 Pydantic Field 校验，越界 422）。
    会话不存在时返回 404；成功返回 ``{id, title, updated_at}``。
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    if not session_logger.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    try:
        session_logger.update_session_title(session_id, body.title)
        # 再次校验写入成功（title 截断后为空时 update 静默忽略）
        stored = session_logger.get_session_title(session_id)
        if not stored:
            raise HTTPException(status_code=422, detail="title 不能为空")
        return {
            "id": session_id,
            "title": stored,
            "updated_at": datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("更新会话标题失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.delete("/sessions/{session_id}", response_model=DeleteSessionResponse)
def delete_session(session_id: str):
    """删除指定会话及其所有消息。

    会话不存在时返回 404。
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        deleted = session_logger.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
        # 同步清理 todo 持久化文件（避免残留）
        if (
            orchestrator is not None
            and getattr(orchestrator, "todo_registry", None) is not None
        ):
            try:
                orchestrator.todo_registry.delete(session_id)
            except Exception as e:
                logger.warning("清理会话 todo 文件失败 %s: %s", session_id, e)
        # 同步清理会话文件关联（uploaded_files 物理记录保留，可能被其他会话引用）
        if upload_manager is not None:
            try:
                upload_manager.cleanup_session(session_id)
            except Exception as e:
                logger.warning("清理会话文件关联失败 %s: %s", session_id, e)
        logger.info("已删除会话: %s", session_id)
        return DeleteSessionResponse(status="deleted", session_id=session_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除会话失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 文件上传与 ETL 端点 ----------


@app.post("/files/upload", response_model=FileUploadResponse)
async def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """上传文件并触发 ETL 处理。

    接收 multipart/form-data 格式的文件上传。
    全局 SHA256 去重：相同内容的文件返回已有 file_id。
    上传成功后后台异步执行 ETL 流水线。
    """
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")

    # 获取 session_id：query param → form 字段 → request.state（auth 中间件）
    session_id = request.query_params.get("session_id")

    if not session_id:
        try:
            form = await request.form()
            session_id = form.get("session_id")
        except Exception:
            session_id = None

    if not session_id:
        session_id = getattr(request.state, "session_id", None) if hasattr(request.state, "session_id") else None

    if not session_id:
        raise HTTPException(status_code=401, detail="缺少 session_id")

    # 读取文件（form 已在上面的 try 中解析，若未尝试过则这里取）
    try:
        form = await request.form()
    except Exception:
        form = None
    uploaded_file = form.get("file") if form is not None else None
    if uploaded_file is None:
        raise HTTPException(status_code=400, detail="未提供文件")

    filename = uploaded_file.filename or "unknown"
    content = await uploaded_file.read()

    # 保存
    file_id, is_dup = upload_manager.save(filename, content, session_id)
    if file_id is None:
        raise HTTPException(status_code=400, detail="文件上传失败（校验未通过）")

    # 写入文件上传消息到会话历史（仅首次上传，去重命中不写避免重复）
    if not is_dup and session_logger is not None:
        meta = upload_manager.get_metadata(file_id)
        if meta is not None:
            import json as _json
            file_type = meta.get("type", "")
            is_image = file_type in (".png", ".jpg", ".jpeg", ".gif")
            attachment = {
                "file_id": file_id,
                "name": filename,
                "type": file_type,
                "size": meta.get("size", 0),
                "category": "image" if is_image else "document",
                "etl_status": meta.get("etl_status", "pending"),
            }
            session_logger.log_message(
                session_id=session_id,
                role="user",
                content=f"已上传文件：{filename}",
                attachments=_json.dumps([attachment], ensure_ascii=False),
                message_type="file_upload",
            )

    # 后台 ETL
    if not is_dup and etl_engine is not None:
        background_tasks.add_task(_run_etl, file_id, session_id)

    message = "文件已在知识库中" if is_dup else "上传成功"
    return FileUploadResponse(file_id=file_id, is_dup=bool(is_dup), message=message)


def _run_etl(file_id: str, session_id: str) -> None:
    """后台执行 ETL 处理。"""
    try:
        if etl_engine is not None:
            result = etl_engine.process_file(file_id, session_id)
            if result.get("status") == "failed":
                logger.warning(
                    "ETL 失败: file_id=%s, error=%s", file_id, result.get("error")
                )
            else:
                logger.info("ETL 完成: file_id=%s, chunks=%d", file_id, result.get("chunk_count", 0))
    except Exception as e:
        logger.exception("ETL 后台任务异常: file_id=%s", file_id)


@app.get("/files", response_model=FileListResponse)
def list_files():
    """列出所有已上传文件。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        files = upload_manager.list_all()
        items = [
            FileItem(
                file_id=f.get("file_id", ""),
                original_name=f.get("original_name", ""),
                size=f.get("size", 0),
                type=f.get("type", ""),
                etl_status=f.get("etl_status", "pending"),
                summary=f.get("summary", ""),
                chunk_count=f.get("chunk_count", 0),
                uploaded_at=f.get("uploaded_at", ""),
                last_accessed=f.get("last_accessed", ""),
            )
            for f in files
        ]
        return FileListResponse(files=items)
    except Exception as e:
        logger.exception("列出文件失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.get("/files/{file_id}")
def get_file_metadata(file_id: str):
    """获取单个文件的元数据。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        meta = upload_manager.get_metadata(file_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")
        return dict(meta)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取文件元数据失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.get("/files/{file_id}/raw")
def get_file_raw(file_id: str):
    """返回文件原始内容（图片缩略图/文档下载用）。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        meta = upload_manager.get_metadata(file_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        if meta.get("etl_status") == "disk_expired" or not meta.get("saved_path"):
            raise HTTPException(status_code=410, detail="文件已过期")
        saved_path = meta["saved_path"]
        if not os.path.exists(saved_path):
            raise HTTPException(status_code=404, detail="磁盘文件丢失")
        content_types = {
            ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".pdf": "application/pdf", ".txt": "text/plain",
            ".md": "text/markdown",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        media_type = content_types.get(meta.get("type", ""), "application/octet-stream")
        return FileResponse(
            saved_path,
            media_type=media_type,
            filename=meta.get("original_name", ""),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取文件内容失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.get("/sessions/{session_id}/files", response_model=FileListResponse)
def get_session_files(session_id: str):
    """获取指定会话的上传文件列表。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        files = upload_manager.get_session_files(session_id)
        items = [
            FileItem(
                file_id=f.get("file_id", ""),
                original_name=f.get("original_name", ""),
                size=f.get("size", 0),
                type=f.get("type", ""),
                etl_status=f.get("etl_status", "pending"),
                summary=f.get("summary", ""),
                chunk_count=f.get("chunk_count", 0),
                uploaded_at=f.get("uploaded_at", ""),
                last_accessed=f.get("last_accessed", ""),
                version_seq=f.get("version_seq"),
                is_latest=f.get("is_latest"),
            )
            for f in files
        ]
        return FileListResponse(files=items)
    except Exception as e:
        logger.exception("获取会话文件列表失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.delete("/admin/files/{file_id}", response_model=FileDeleteResponse)
def admin_delete_file(file_id: str, request: Request):
    """管理员应急清理：全链路删除文件知识。

    删除：磁盘原始文件 + 解析缓存 + ChromaDB 向量块 + FTS5 索引 + SQLite 元数据。
    需要 Bearer Token 认证。
    """
    if etl_engine is None:
        raise HTTPException(status_code=503, detail="ETL 模块未初始化")
    try:
        result = etl_engine.delete_file_knowledge(file_id)
        if not result.get("deleted"):
            raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在或已删除")
        logger.info("管理员删除文件: file_id=%s, details=%s", file_id, result.get("details"))
        return FileDeleteResponse(
            status="deleted",
            file_id=file_id,
            details=result.get("details", {}),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("管理员删除文件失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@app.get("/recall")
def recall_messages(
    keyword: str = Query(..., description="搜索关键词"),
    session_id: Optional[str] = Query(None, description="按会话过滤"),
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
):
    """全文检索历史消息（基于 SQLite FTS5）。

    支持中英文关键词，返回按 ``created_at`` 降序的匹配结果。可用于 Recall
    Memory 场景：用户提到某关键词时，前端调用此接口取回历史相关消息。
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        results = session_logger.search_messages(keyword, session_id, limit)
        return {"results": results, "count": len(results)}
    except Exception as e:
        logger.exception("检索消息失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 服务重启 ----------


_SOFT_RESTART_IN_PROGRESS = False


@app.post("/restart")
async def restart_server():
    """软重启：重载配置 + 重建 Orchestrator，不杀进程。

    与硬重启（``systemctl restart``）不同，本端点：
    - 不重启 uvicorn 进程，ONNX 模型与 ChromaDB 索引保留在内存中
    - 先冲刷 consolidation 待处理队列，确保记忆不丢
    - 若新 Orchestrator 构建失败，保留旧的继续服务

    不支持热更新的配置项（如 ``server.host`` / ``server.port``）变更
    仍需硬重启。
    """
    global orchestrator, _SOFT_RESTART_IN_PROGRESS

    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")

    if _SOFT_RESTART_IN_PROGRESS:
        raise HTTPException(status_code=409, detail="软重启已在进行中")

    _SOFT_RESTART_IN_PROGRESS = True
    try:
        # 1. 读取最新配置
        new_config = load_config(CONFIG_PATH)

        # 2. 校验环境变量
        try:
            validate_required_env_vars(new_config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"配置校验失败: {e}")

        # 3. 如果存在进行中的流，等待最多 30s
        if stream_manager is not None:
            active = stream_manager.active_count()
            if active > 0:
                logger.info(
                    "软重启: 等待 %d 个进行中的流完成（最多 30s）...", active
                )
                for _ in range(30):
                    if stream_manager.active_count() == 0:
                        break
                    await asyncio.sleep(1)
                remaining = stream_manager.active_count()
                if remaining > 0:
                    logger.warning(
                        "软重启: %d 个流未在等待时间内完成，继续重启", remaining
                    )

        # 4. 冲刷旧 Orchestrator 的待处理记忆 + 关闭 ChromaDB 连接
        #    （关断后建新实例，避免两个 ChromaDB 客户端争用同一 sqlite 文件）
        old_orchestrator = orchestrator
        try:
            await asyncio.to_thread(old_orchestrator.shutdown)
        except Exception as e:
            logger.warning("软重启: 旧 Orchestrator 关闭异常（已忽略）: %s", e)

        # 5. 构建新 Orchestrator（旧资源已释放，ChromDB 文件可安全打开）
        try:
            new_orchestrator = await asyncio.to_thread(
                Orchestrator,
                config_path=CONFIG_PATH,
                metrics=metrics_collector,
                audit_logger=audit_logger,
                approval_manager=approval_manager,
                task_manager=task_manager,
            )
        except Exception as e:
            logger.exception("软重启: 新 Orchestrator 构建失败，服务不可用")
            # 构建失败时尝试硬重启恢复
            raise HTTPException(
                status_code=500,
                detail=f"新 Orchestrator 构建失败，请手动 systemctl restart: {e}",
            )

        # 6. 预热 ChromaDB 索引（哑查询，快速）
        if new_orchestrator.chroma_store is not None:
            try:
                new_orchestrator.chroma_store.query_memory(
                    "warmup", top_k=1, reinforce=False
                )
            except Exception as e:
                logger.warning("软重启: ChromaDB 预热失败: %s", e)

        # 7. 原子替换
        orchestrator = new_orchestrator
        logger.info("软重启: Orchestrator 已替换为新实例")

        # 8. 预热新实例的 ChromaDB（后台）
        return {"status": "ok", "message": "软重启完成"}

    finally:
        _SOFT_RESTART_IN_PROGRESS = False


# ---------- 配置管理 ----------

# 需要重启才能生效的配置项前缀列表
# llm.{provider,model,api_key,base_url} 涉及 SDK 客户端重建，无法热更新；
# llm.activity_timeout / llm.stream_total_timeout 不涉及客户端重建，由 SSE
# handler 每次请求通过 get_llm_timeouts() 读取最新值，即时生效（热更新），
# 故不在此列表。存储路径 / memory 的路径类配置 / server.* 涉及组件重建，无法热更新
_RESTART_REQUIRED_KEYS = {
    # llm 段细化：仅 provider/model/api_key/base_url 等涉及 AsyncAnthropic /
    # AsyncOpenAI 客户端实例重建的字段需重启；activity_timeout /
    # stream_total_timeout 由 SSE handler 每请求读取，即时生效
    "llm.main_provider",
    "llm.main_model",
    "llm.main_api_key",
    "llm.main_base_url",
    "llm.consolidation_provider",
    "llm.consolidation_model",
    "llm.consolidation_api_key",
    "llm.consolidation_base_url",
    # max_context_tokens / context_threshold 未纳入 _RUNTIME_HOTUPDATE_MAP，
    # 保留重启语义以避免变更被静默忽略（与原 "llm" 整段行为一致）
    "llm.max_context_tokens",
    "llm.context_threshold",
    "memory.chroma_path",
    "memory.memory_md_path",
    "storage.sqlite_path",
    "server",
    "monitoring.audit_log_path",
    # Phase 4: skills.* 涉及进程/连接生命周期，无法热更新
    "skills.mcp",
    # Phase 5: security.rules 涉及 PolicyEngine 规则重建，需重启
    # security.enabled 通过 _RUNTIME_HOTUPDATE_MAP 热更新（property setter
    # 仅翻转 _enabled 布尔，不重建 PolicyEngine 实例）
    "security.rules",
    # Phase 9: guardrails 的 enabled 字段通过 _RUNTIME_HOTUPDATE_MAP 热更新
    # （property setter 仅翻转布尔不重建 InjectionGuard/OutputFilter 内部实例）；
    # 其他结构性字段变更需重启重建 GuardrailEngine
    "guardrails.input_scan.action",
    "guardrails.sanitizer.trusted_tools",
    "guardrails.sanitizer.max_output_length",
    "guardrails.output_filter.enable_bank_card",
    # Condenser: strategy 变更涉及 Condenser 实例类型切换（masking ↔ llm_summary），需重启
    "memory.condenser.strategy",
    # Phase 6: plan 模式下 TaskManager 使用内存对象，无文件路径配置；
    # schedules 涉及 CronScheduler 调度项重建，需重启
    "schedules",
    # history.persistence_dir 涉及 HistoryBuffer 磁盘持久化路径，运行中切换
    # 无法迁移已写入的 JSONL，需重启重建 HistoryBuffer
    "history.persistence_dir",
    # OCR 分层：primary_engine/paddle.use_gpu/paddle.lang 涉及 PaddleOCR 实例重建，
    # vision_llm.* 涉及视觉客户端重建，均需重启
    "files.ocr.primary_engine",
    "files.ocr.paddle.use_gpu",
    "files.ocr.paddle.lang",
    "files.ocr.vision_llm.provider",
    "files.ocr.vision_llm.model",
    "files.ocr.vision_llm.api_key",
    "files.ocr.vision_llm.base_url",
    # reasoning.main.provider/model 涉及 reasoning backend 客户端重建，需重启
    "reasoning.main.provider",
    "reasoning.main.model",
}

# 配置取值哨兵：用于区分「配置项缺失」与「配置项值为 None / 空容器」
_MISSING = object()

# 配置写入串行锁：保证备份-写入-替换的原子序列在并发请求下不被交错执行
_config_write_lock = threading.Lock()

# 可热更新的运行时配置项：配置路径 → (orchestrator 属性链, 类型转换函数)
# 这些参数直接修改内存中组件的属性，无需重启即可生效
_RUNTIME_HOTUPDATE_MAP = {
    "memory.consolidation_threshold": ("consolidation_engine.threshold", int),
    "memory.dedup_similarity_threshold": ("consolidation_engine.dedup_threshold", float),
    "memory.retrieval_top_k": ("memory_retriever.top_k", int),
    "memory.history_max_turns": ("history_buffer.max_turns", int),
    "tools.max_react_loops": ("react_loop.max_loops", int),
    "tools.defer_loading_threshold": ("tool_registry.defer_loading_threshold", int),
    "security.approval_timeout_seconds": ("approval_manager.timeout", float),
    # Phase 7 Task 2: 惊讶门控配置（写入侧过滤），均可热更新即时生效
    "memory.surprise_gate_enabled": ("consolidation_engine.surprise_gate_enabled", bool),
    "memory.surprise_similarity_threshold": ("consolidation_engine.surprise_similarity_threshold", float),
    "memory.surprise_skip_threshold": ("consolidation_engine.surprise_skip_threshold", float),
    # Phase 7 Task 1: 三因子衰减参数热更新（直接修改 decay 实例属性，
    # MemoryRetriever 持有同一引用，下次检索排序立即生效）
    "memory.decay_rate": ("decay.decay_rate", float),
    "memory.frequency_weight": ("decay.frequency_weight", float),
    # 防护系统开关热更新：通过 property setter 翻转 enabled 布尔即时生效
    # PolicyEngine.enabled setter：仅记日志，pending 审批由 _apply_runtime_config 专项处理
    "security.enabled": ("policy_engine.enabled", bool),
    # GuardrailEngine 三个 setter：仅翻转 _xxx_enabled 布尔，不重建内部实例
    "guardrails.input_scan.enabled": ("guardrail_engine.input_scan_enabled", bool),
    "guardrails.sanitizer.enabled": ("guardrail_engine.sanitizer_enabled", bool),
    "guardrails.output_filter.enabled": ("guardrail_engine.output_filter_enabled", bool),
    # Reasoning 模式热更新：通过 LLMClient property setter 转发到
    # _main_backend.reasoning_profile 或 history_buffer.persist_thinking。
    # property setter 在 LLMClient 层实现（Task 7），此处仅注册条目。
    # 禁止直接访问私有属性（如 llm_client._main_backend.reasoning_profile）。
    "reasoning.main.enabled": ("llm_client.main_reasoning_enabled", bool),
    "reasoning.main.effort": ("llm_client.main_reasoning_effort", str),
    "reasoning.main.budget_tokens": ("llm_client.main_reasoning_budget_tokens", int),
    "reasoning.cron.enabled": ("llm_client.cron_reasoning_enabled", bool),
    "reasoning.cron.effort": ("llm_client.cron_reasoning_effort", str),
    "reasoning.persist_thinking": ("history_buffer.persist_thinking", bool),
    # ops-reliability-uplift Task 7: cron 上下文注入开关（属性链从 orchestrator 起步，
    # 与 memory.history_max_turns 等条目格式一致，不带 orchestrator. 前缀）
    "cron.inject_history": ("cron_inject_history_enabled", bool),
}


def _deep_merge_config(old: dict, new: dict) -> dict:
    """深度合并两个配置字典，返回新字典（不修改入参）。

    合并规则：
    - dict + dict → 递归合并；
    - dict + 非 dict → 用新值覆盖；
    - list + list → 用新值覆盖（不拼接）；
    - 标量 + 标量 → 用新值覆盖；
    - 旧 key 未在新配置中出现 → 保留旧值。

    参数:
        old: 旧配置字典（不会被修改）。
        new: 新配置字典（不会被修改，优先级高于 old）。

    返回:
        合并后的全新字典，与入参无引用共享。
    """
    merged: dict = {}
    # 先深拷贝旧值，保证旧 key 被保留且与入参解耦
    for k, v in old.items():
        merged[k] = copy.deepcopy(v)
    # 再用新值覆盖 / 递归合并
    for k, new_v in new.items():
        old_v = merged.get(k, _MISSING)
        if old_v is not _MISSING and isinstance(old_v, dict) and isinstance(new_v, dict):
            merged[k] = _deep_merge_config(old_v, new_v)
        else:
            # dict + 非 dict / list + list / 标量 + 标量 / 新增 key → 新值覆盖
            merged[k] = copy.deepcopy(new_v)
    return merged


def _backup_config(config_path: str) -> None:
    """备份配置文件到 ``config_path + ".bak"``。

    若源文件不存在则跳过（仅 debug 日志）；备份失败时记录 warning 但不抛异常，
    保证调用方流程不被中断。使用 ``shutil.copy2`` 以保留文件元数据。

    参数:
        config_path: 配置文件路径。
    """
    if not os.path.exists(config_path):
        logger.debug("配置文件不存在，跳过备份: %s", config_path)
        return
    try:
        shutil.copy2(config_path, config_path + ".bak")
        logger.debug("已备份配置文件: %s -> %s.bak", config_path, config_path)
    except Exception as e:
        logger.warning("备份配置失败: %s", e)


def _atomic_write_config(config_path: str, data: dict) -> None:
    """原子写入配置文件，避免写入中途崩溃导致配置损坏。

    流程：先写入 ``config_path + ".tmp"`` 临时文件并 ``fsync`` 刷盘，
    再通过 ``os.replace`` 原子替换原文件。若写入或替换过程中抛异常，
    会清理临时文件后重新抛出原异常。

    参数:
        config_path: 目标配置文件路径。
        data: 待写入的配置字典。

    抛出:
        写入或替换过程中发生的异常。
    """
    tmp_path = config_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    except Exception:
        # 异常时清理临时文件后重新抛出，避免残留 .tmp 干扰后续写入
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def _validate_config_schema(config: dict) -> None:
    """校验配置字典的结构与关键字段类型。

    校验失败时抛出 ``ValueError``，错误信息以「配置校验失败: 」为前缀。
    仅校验已出现的字段，未出现的字段不强制要求。

    参数:
        config: 待校验的配置字典。

    抛出:
        ValueError: 配置结构或字段类型不符合要求时。
    """
    if not isinstance(config, dict):
        raise ValueError("配置校验失败: 配置根节点必须是字典")
    # 顶层关键段若存在必须为 dict
    for seg in ("llm", "memory", "server", "storage", "skills", "monitoring", "tools", "security", "history"):
        if seg in config and not isinstance(config[seg], dict):
            raise ValueError(f"配置校验失败: {seg} 必须是字典")
    # llm 段：main_model 与 consolidation_model 必填且为非空字符串
    # 字段名与 src/llm/client.py LLMClient 启动时的强校验字段一致
    llm = config.get("llm")
    if isinstance(llm, dict):
        main_model = llm.get("main_model")
        if not isinstance(main_model, str) or not main_model.strip():
            raise ValueError("配置校验失败: llm.main_model 必填且不能为空")
        consolidation_model = llm.get("consolidation_model")
        if not isinstance(consolidation_model, str) or not consolidation_model.strip():
            raise ValueError("配置校验失败: llm.consolidation_model 必填且不能为空")
    # server.port 若存在必须为 int
    server = config.get("server")
    if isinstance(server, dict) and "port" in server:
        if not isinstance(server["port"], int) or isinstance(server["port"], bool):
            raise ValueError("配置校验失败: server.port 必须为整数")
    # memory 数值字段类型校验
    memory = config.get("memory")
    if isinstance(memory, dict):
        if "consolidation_threshold" in memory:
            v = memory["consolidation_threshold"]
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("配置校验失败: memory.consolidation_threshold 必须为整数")
        if "retrieval_top_k" in memory:
            v = memory["retrieval_top_k"]
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("配置校验失败: memory.retrieval_top_k 必须为整数")
        # Phase 7 Task 1: decay_rate / frequency_weight 类型校验（数值即可）
        for field in ("decay_rate", "frequency_weight"):
            if field in memory:
                v = memory[field]
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise ValueError(
                        f"配置校验失败: memory.{field} 必须为数值"
                    )
        # condenser 段结构校验
        condenser = memory.get("condenser")
        if condenser is not None:
            if not isinstance(condenser, dict):
                raise ValueError("配置校验失败: memory.condenser 必须是字典")
            if "enabled" in condenser and not isinstance(condenser["enabled"], bool):
                raise ValueError("配置校验失败: memory.condenser.enabled 必须为布尔值")
            if "strategy" in condenser:
                v = condenser["strategy"]
                if not isinstance(v, str) or v not in ("masking", "llm_summary"):
                    raise ValueError(
                        "配置校验失败: memory.condenser.strategy 必须为 'masking' 或 'llm_summary'"
                    )
            for field in ("keep_recent_n", "keep_first", "llm_summary_threshold"):
                if field in condenser:
                    v = condenser[field]
                    if not isinstance(v, int) or isinstance(v, bool):
                        raise ValueError(
                            f"配置校验失败: memory.condenser.{field} 必须为整数"
                        )
    # security 段字段类型校验
    security = config.get("security")
    if isinstance(security, dict):
        if "enabled" in security and not isinstance(security["enabled"], bool):
            raise ValueError("配置校验失败: security.enabled 必须为布尔值")
        if "approval_timeout_seconds" in security:
            v = security["approval_timeout_seconds"]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError("配置校验失败: security.approval_timeout_seconds 必须为数值")
        if "rules" in security and not isinstance(security["rules"], list):
            raise ValueError("配置校验失败: security.rules 必须是列表")

    # tasks 段校验：plan 模式使用内存对象，仅保留「必须是 dict」的结构校验
    # （file_path 字段已废弃，向后兼容由 TaskManager 默认值兜底）
    tasks = config.get("tasks")
    if tasks is not None and not isinstance(tasks, dict):
        raise ValueError("配置校验失败: tasks 必须是字典")

    # schedules 段校验
    schedules = config.get("schedules")
    if schedules is not None:
        if not isinstance(schedules, list):
            raise ValueError("配置校验失败: schedules 必须是列表")
        for i, item in enumerate(schedules):
            if not isinstance(item, dict):
                raise ValueError(f"配置校验失败: schedules[{i}] 必须是字典")
            cron = item.get("cron")
            if not isinstance(cron, str) or not cron.strip():
                raise ValueError(f"配置校验失败: schedules[{i}].cron 必填且为非空字符串")
            task = item.get("task")
            if not isinstance(task, str) or not task.strip():
                raise ValueError(f"配置校验失败: schedules[{i}].task 必填且为非空字符串")
            if "enabled" in item and not isinstance(item["enabled"], bool):
                raise ValueError(f"配置校验失败: schedules[{i}].enabled 必须为布尔值")
            if "id" in item and not isinstance(item["id"], str):
                raise ValueError(f"配置校验失败: schedules[{i}].id 必须为字符串")
            if "name" in item and not isinstance(item["name"], str):
                raise ValueError(f"配置校验失败: schedules[{i}].name 必须为字符串")
            # 校验 cron 表达式合法性（CronExpr 不可用时仅做字符串非空校验）
            if CronExpr is not None:
                try:
                    CronExpr(cron)
                except ValueError as e:
                    raise ValueError(f"配置校验失败: schedules[{i}].cron 非法: {e}")


def _check_needs_restart(old_config: dict, new_config: dict) -> bool:
    """检查配置变更是否需要重启才能生效。"""
    for key in _RESTART_REQUIRED_KEYS:
        parts = key.split(".")
        old_val = old_config
        new_val = new_config
        for p in parts:
            # 沿路径逐层取值；任一端非 dict 时停止下钻并直接比较当前值
            if not isinstance(old_val, dict):
                break
            if not isinstance(new_val, dict):
                break
            old_val = old_val.get(p, _MISSING)
            new_val = new_val.get(p, _MISSING)
        if old_val != new_val:
            return True
    return False


def _apply_runtime_config(new_config: dict) -> Dict[str, bool]:
    """将可热更新的运行时配置即时应用到内存中的 orchestrator 组件。

    遍历 :data:`_RUNTIME_HOTUPDATE_MAP`，对每项配置：
    1. 从 new_config 中按路径取值；
    2. 通过属性链（如 ``consolidation_engine.threshold``）定位到组件属性；
    3. 类型转换后直接赋值。

    若对应组件未初始化（为 None）或赋值失败，该项标记为 False，
    不影响其他项的更新。

    参数:
        new_config: 新的完整配置字典。

    返回:
        ``{配置路径: 是否成功应用}`` 字典。
    """
    applied: Dict[str, bool] = {}
    if orchestrator is None:
        return applied

    for cfg_path, (attr_chain, conv) in _RUNTIME_HOTUPDATE_MAP.items():
        # 1. 从 new_config 按点分路径取值，用 _MISSING 哨兵区分「缺失」与「空容器」
        parts = cfg_path.split(".")
        val: Any = new_config
        for p in parts:
            # 遇到 _MISSING（缺失）或非 dict 时停止下钻，避免对非 dict 调用 .get
            if val is _MISSING or not isinstance(val, dict):
                break
            val = val.get(p, _MISSING)
        if val is _MISSING or val is None:
            # 配置项缺失，跳过（注意：空 dict / 空 list 不视为缺失，交由类型转换处理）
            continue

        # 2. 沿属性链定位到目标属性
        obj: Any = orchestrator
        attr_parts = attr_chain.split(".")
        try:
            for ap in attr_parts[:-1]:
                obj = getattr(obj, ap)
            if obj is None:
                # 中间组件未初始化（如 consolidation_engine 为 None）
                applied[cfg_path] = False
                continue
            # 3. 类型转换并赋值
            setattr(obj, attr_parts[-1], conv(val))
            applied[cfg_path] = True
            logger.info("热更新 %s = %s", cfg_path, conv(val))
        except Exception as e:
            logger.warning("热更新 %s 失败: %s", cfg_path, e)
            applied[cfg_path] = False

    # Condenser 热更新：enabled / keep_recent_n / keep_first / llm_summary_threshold
    # 即时生效（strategy 变更已在 _check_needs_restart 拦截，到达此处时 strategy 不变）。
    # 通过 orchestrator.apply_condenser_config 重建 Condenser 实例并注入
    # context_manager。memory.condenser 段存在时才触发（缺失表示未配置，跳过）。
    memory_cfg = new_config.get("memory")
    if isinstance(memory_cfg, dict) and "condenser" in memory_cfg:
        condenser_cfg = memory_cfg.get("condenser") or {}
        try:
            orchestrator.apply_condenser_config(condenser_cfg)
            applied["memory.condenser"] = True
            logger.info("热更新 memory.condenser: %s", condenser_cfg)
        except Exception as e:
            logger.warning("热更新 memory.condenser 失败: %s", e)
            applied["memory.condenser"] = False

    # read_paths 专项热更新（dict 结构，不走 _RUNTIME_HOTUPDATE_MAP 标量映射）
    # 前端修改 security.read_paths 后即时生效，无需重启
    sec_cfg_rp = new_config.get("security") or {}
    rp_cfg = sec_cfg_rp.get("read_paths")
    if isinstance(rp_cfg, dict) and orchestrator is not None:
        try:
            orchestrator.policy_engine.set_read_paths(
                mode=rp_cfg.get("mode", "deny_first"),
                deny=rp_cfg.get("deny", []),
                allow=rp_cfg.get("allow", []),
                workspace_dirs=rp_cfg.get("workspace_dirs", []),
            )
            applied["security.read_paths"] = True
            logger.info("热更新 security.read_paths")
        except Exception as e:
            logger.warning("热更新 security.read_paths 失败: %s", e)
            applied["security.read_paths"] = False

    # 防护开关专项：关闭 HIL 时批量 deny pending 审批，唤醒所有
    # wait_for_decision 协程，避免它们傻等 approval_manager.timeout 秒超时。
    # 仅在 security.enabled 被热更新且新值为 False 时触发。
    sec_cfg = new_config.get("security")
    if (
        isinstance(sec_cfg, dict)
        and "security.enabled" in applied
        and applied["security.enabled"]
        and not bool(sec_cfg.get("enabled", True))
    ):
        if approval_manager is not None:
            try:
                n = approval_manager.resolve_all("deny", "HIL 已关闭，审批自动拒绝")
                if n > 0:
                    logger.warning("HIL 关闭，自动 deny %d 条 pending 审批", n)
                    if audit_logger is not None:
                        audit_logger.log_guardrail_decision(
                            layer="policy_switch",
                            action="disable",
                            reason=f"HIL 关闭，自动 deny {n} 条 pending 审批",
                            session_id="system",
                            risk_level="high",
                        )
            except Exception as e:
                logger.warning("HIL 关闭专项处理失败: %s", e)

    # OCR 配置热更新专项：parser 持有在 etl_engine.parser（模块级全局），
    # 不在 orchestrator 属性链，无法走 _RUNTIME_HOTUPDATE_MAP，需专项分支写入。
    # 仅处理可热更新字段（tesseract.lang/preprocess、paddle.min_confidence、
    # vision_llm.enabled）；结构性字段（primary_engine/paddle.use_gpu 等）已由
    # _RESTART_REQUIRED_KEYS 拦截要求重启。
    ocr_cfg = new_config.get("files", {}).get("ocr", {}) or {}
    if etl_engine is not None and hasattr(etl_engine, "parser"):
        parser_obj = etl_engine.parser
        try:
            t_cfg = ocr_cfg.get("tesseract", {}) or {}
            if "lang" in t_cfg:
                parser_obj.ocr_tesseract_lang = t_cfg["lang"]
                applied["files.ocr.tesseract.lang"] = True
            if "preprocess" in t_cfg:
                parser_obj.ocr_tesseract_preprocess = bool(t_cfg["preprocess"])
                applied["files.ocr.tesseract.preprocess"] = True
            p_cfg = ocr_cfg.get("paddle", {}) or {}
            if "min_confidence" in p_cfg:
                parser_obj.ocr_paddle_min_confidence = float(p_cfg["min_confidence"])
                applied["files.ocr.paddle.min_confidence"] = True
            if "infer_timeout" in p_cfg:
                parser_obj.ocr_paddle_infer_timeout = int(p_cfg["infer_timeout"])
                applied["files.ocr.paddle.infer_timeout"] = True
            v_cfg = ocr_cfg.get("vision_llm", {}) or {}
            if "enabled" in v_cfg:
                parser_obj.ocr_vision_llm_enabled = bool(v_cfg["enabled"])
                applied["files.ocr.vision_llm.enabled"] = True
            ocr_applied = {k: v for k, v in applied.items() if k.startswith("files.ocr")}
            if ocr_applied:
                logger.info("热更新 files.ocr: %s", ocr_applied)
        except Exception as e:
            logger.warning("热更新 files.ocr 失败: %s", e)

    return applied


@app.get("/config", response_model=ConfigResponse)
def get_config():
    """读取当前配置文件内容。"""
    try:
        config = load_config(CONFIG_PATH)
        return ConfigResponse(config=config)
    except Exception as e:
        logger.exception("读取配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")


@app.put("/config", response_model=ConfigUpdateResponse)
def update_config(req: ConfigUpdateRequest):
    """更新配置文件并写入磁盘，运行时参数即时生效。

    配置分两类处理：
    1. **可热更新**（memory 阈值/top_k/max_turns、tools 循环上限等）：
       写入磁盘后立即通过 :func:`_apply_runtime_config` 修改内存中
       orchestrator 组件的属性，无需重启即时生效。
    2. **需重启**（llm provider/model/api_key、存储路径、服务端口、
       memory 路径类配置）：仅写入磁盘，需重启服务才能生效，
       响应中 ``needs_restart=True``。

    写入流程（在 :data:`_config_write_lock` 串行锁保护下执行）：
    1. 读取旧配置用于比较（读取失败视为空字典）；
    2. :func:`_deep_merge_config` 深度合并新旧配置，未在新配置出现的段保留旧值，
       避免部分更新丢失其他段；
    3. :func:`_validate_config_schema` 校验合并后配置的结构与字段类型，
       失败时抛 ``HTTPException(400)`` 且不写盘；
    4. :func:`_backup_config` 备份旧配置到 ``config.yaml.bak``，备份失败仅记录
       warning 不中断流程；
    5. :func:`_atomic_write_config` 原子写入：先写 ``.tmp`` 临时文件并 ``fsync``
       刷盘，再通过 ``os.replace`` 原子替换，避免写入中途崩溃导致配置损坏；
    6. :func:`_apply_runtime_config` 将可热更新项即时应用到内存中 orchestrator 组件；
    7. :func:`_check_needs_restart` 比较新旧配置判断是否需要重启。

    响应消息与日志在锁外构造，以减少锁持有时间。
    """
    try:
        with _config_write_lock:
            # 1. 读取旧配置用于比较（读取失败视为空字典）
            try:
                old_config = load_config(CONFIG_PATH)
            except Exception:
                old_config = {}

            # 2. 深度合并：新配置覆盖旧配置，未在新配置出现的段保留旧值
            merged_config = _deep_merge_config(old_config, req.config)

            # 3. Schema 校验：失败时返回 400 且不写盘（校验在备份/写盘之前）
            try:
                _validate_config_schema(merged_config)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            # 4. 备份旧配置（失败仅 warning，不中断后续流程）
            _backup_config(CONFIG_PATH)

            # 5. 原子写入合并后的新配置到磁盘
            _atomic_write_config(CONFIG_PATH, merged_config)

            # 5.5 使配置缓存失效，避免快速连续 PUT 时 mtime 未变
            clear_config_cache()

            # 6. 应用热更新到运行时组件（用 merged_config 而非 req.config）
            applied = _apply_runtime_config(merged_config)

            # 7. 检查是否需要重启（比较 old_config 与 merged_config）
            needs_restart = _check_needs_restart(old_config, merged_config)

        # 锁外构造响应消息与日志，减少锁持有时长
        if needs_restart:
            message = "配置已保存。部分项（LLM/路径/端口）需重启服务生效。"
        elif applied:
            ok_count = sum(1 for v in applied.values() if v)
            message = f"配置已保存并即时生效（{ok_count} 项热更新）。"
        else:
            message = "配置已保存。"
        logger.info("配置已通过 API 更新，needs_restart=%s, applied=%s", needs_restart, applied)
        return ConfigUpdateResponse(
            status="saved", message=message, needs_restart=needs_restart
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("更新配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"更新配置失败: {e}")


# ---------- Phase 7 Task 4: 记忆 Dashboard 端点 ----------


def _query_memory_browse(chroma_store, query: str, top_k: int) -> list:
    """浏览场景检索记忆，不触发强化（reinforce=False）。

    Task 1 完成后 ``query_memory`` 支持 ``reinforce`` 参数（默认 True，
    命中后更新 last_accessed / access_count）；Dashboard 浏览场景应避免
    触发强化，故显式传 ``reinforce=False``。Task 1 尚未完成时
    ``query_memory`` 无此参数，通过 ``inspect`` 检测签名兼容两种情况。

    参数:
        chroma_store: ChromaMemoryStore 实例。
        query: 查询文本。
        top_k: 返回前 K 条结果。

    返回:
        与 ``chroma_store.query_memory`` 一致的记忆列表。
    """
    import inspect
    try:
        sig = inspect.signature(chroma_store.query_memory)
    except (ValueError, TypeError):
        sig = None
    if sig is not None and "reinforce" in sig.parameters:
        return chroma_store.query_memory(query, top_k=top_k, reinforce=False)
    return chroma_store.query_memory(query, top_k=top_k)


@app.get("/memories")
def search_memories(
    q: str = Query("", description="搜索关键词，空串返回空列表"),
    top_k: int = Query(20, ge=1, le=200, description="返回条数"),
    type: Optional[str] = Query(None, description="按 metadata.type 过滤"),
):
    """搜索长期记忆（向量检索）。

    调 ``chroma_store.query_memory`` 做向量检索，**浏览不触发强化**
    （reinforce=False，避免浏览也更新 last_accessed / access_count）。
    可选按 ``metadata.type`` 过滤返回结果。

    返回:
        ``{"memories": [{id, content, similarity, metadata}], "total": N}``
    """
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    # 空关键词直接返回空列表（避免无意义检索）
    if not q.strip():
        return {"memories": [], "total": 0}
    try:
        raw = _query_memory_browse(orchestrator.chroma_store, q, top_k)
    except Exception as e:
        logger.exception("检索记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"检索记忆失败: {e}")

    memories = []
    for item in raw:
        meta = item.get("metadata") or {}
        # 可选按 type 过滤（metadata.type 匹配）
        if type is not None and str(meta.get("type", "")) != type:
            continue
        memories.append({
            "id": item.get("id", ""),
            "content": item.get("content", ""),
            "similarity": item.get("similarity", 0.0),
            "metadata": meta,
        })
    return {"memories": memories, "total": len(memories)}


@app.get("/memories/all")
def list_all_memories(
    type: Optional[str] = Query(None, description="按 metadata.type 过滤"),
    limit: int = Query(100, ge=1, le=1000, description="最多返回条数"),
):
    """列出全部长期记忆（不做向量检索，按 metadata.type 过滤 + limit 截断）。

    调 ``chroma_store.get_all_memories()`` 取全量后过滤。适用于「按类型浏览」
    场景（如只看 fact / conversation_turn）。

    返回:
        ``{"memories": [{id, content, metadata}], "total": N}``
    """
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    try:
        raw = orchestrator.chroma_store.get_all_memories()
    except Exception as e:
        logger.exception("列出全部记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"列出记忆失败: {e}")

    memories = []
    for item in raw:
        meta = item.get("metadata") or {}
        if type is not None and str(meta.get("type", "")) != type:
            continue
        memories.append({
            "id": item.get("id", ""),
            "content": item.get("content", ""),
            "metadata": meta,
        })
    # limit 截断
    memories = memories[:limit]
    return {"memories": memories, "total": len(memories)}


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    """删除单条长期记忆（用户手动删除，立即生效）。

    与 LLM 通过 ``delete_memory`` 工具入队 ``pending_memory_ops``（延迟到下次
    consolidate 时执行）不同，Dashboard 的手动删除应立即生效，故直接调
    ``chroma_store.delete_memory``。

    memory_id 不存在时返回 404。

    返回:
        ``{"status": "ok", "deleted_id": memory_id}``
    """
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    # 先检查是否存在（get_all_memories 兼容 mock 与真实 chromadb）
    try:
        all_memories = orchestrator.chroma_store.get_all_memories()
        existing_ids = {m.get("id") for m in all_memories}
    except Exception as e:
        logger.exception("检查记忆存在性失败: %s", e)
        raise HTTPException(status_code=500, detail=f"检查记忆失败: {e}")
    if memory_id not in existing_ids:
        raise HTTPException(status_code=404, detail=f"记忆 {memory_id} 不存在")
    try:
        orchestrator.chroma_store.delete_memory(memory_id)
    except Exception as e:
        logger.exception("删除记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"删除记忆失败: {e}")
    logger.info("已通过 Dashboard 删除记忆: %s", memory_id)
    return {"status": "ok", "deleted_id": memory_id}


@app.get("/profile")
def get_profile():
    """返回用户画像 memory.md 全文。

    通过 ``orchestrator.memory_md_manager`` 读取 memory.md，文件不存在时
    返回空 content。``updated_at`` 为文件最后修改时间（ISO 格式），
    文件不存在时为空字符串。

    返回:
        ``{"content": "...", "updated_at": "ISO timestamp"}``
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    memory_md_manager = getattr(orchestrator, "memory_md_manager", None)
    if memory_md_manager is None:
        # memory_md_manager 未初始化，返回空内容
        return {"content": "", "updated_at": ""}
    try:
        content = memory_md_manager.read()
    except Exception as e:
        logger.exception("读取 memory.md 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取画像失败: {e}")

    # 取文件最后修改时间作为 updated_at
    updated_at = ""
    try:
        file_path = getattr(memory_md_manager, "file_path", None)
        if file_path is not None:
            from pathlib import Path
            p = Path(file_path)
            if p.exists():
                updated_at = datetime.fromtimestamp(
                    p.stat().st_mtime
                ).isoformat()
    except Exception as e:
        logger.debug("获取 memory.md mtime 失败: %s", e)
    return {"content": content, "updated_at": updated_at}


# ---------- 静态文件服务（Web 前端）----------

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@app.get("/")
def serve_index():
    """提供前端首页。

    设置 ``Cache-Control: no-cache, no-store, must-revalidate`` 防止浏览器
    缓存旧 HTML，确保前端改动即时生效（用户首次访问后无需手动 hard refresh）。
    """
    index_path = os.path.join(_WEB_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="前端文件未找到")
    return FileResponse(
        index_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/chat")
def serve_chat():
    """提供对话页（与展示首页分离后的交互页面）。"""
    chat_path = os.path.join(_WEB_DIR, "chat.html")
    if not os.path.exists(chat_path):
        raise HTTPException(status_code=404, detail="对话页未找到")
    return FileResponse(
        chat_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/monitor")
def serve_monitor():
    """提供监控面板页。"""
    monitor_path = os.path.join(_WEB_DIR, "monitor.html")
    if not os.path.exists(monitor_path):
        raise HTTPException(status_code=404, detail="监控页未找到")
    return FileResponse(
        monitor_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/scheduler")
def serve_scheduler():
    """提供调度管理页。"""
    scheduler_path = os.path.join(_WEB_DIR, "scheduler.html")
    if not os.path.exists(scheduler_path):
        raise HTTPException(status_code=404, detail="调度页未找到")
    return FileResponse(
        scheduler_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/workflow")
def serve_workflow():
    """提供 Workflow 编排页。"""
    workflow_path = os.path.join(_WEB_DIR, "workflow.html")
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow 页未找到")
    return FileResponse(
        workflow_path,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# 挂载静态文件目录（CSS/JS 等静态资源）
_STATIC_DIR = os.path.join(_WEB_DIR, "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
elif os.path.isdir(_WEB_DIR):
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")


if __name__ == "__main__":
    # 直接运行本模块时通过 uvicorn 启动；生产环境建议使用 start.sh
    import uvicorn

    try:
        cfg = load_config(CONFIG_PATH)
        server_cfg = cfg.get("server", {})
    except Exception:
        server_cfg = {}

    uvicorn.run(
        "src.server:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8000)),
        workers=1,
    )
