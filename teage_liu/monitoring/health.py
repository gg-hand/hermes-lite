"""全子系统健康检查模块。

提供 :class:`HealthChecker` 类，对所有 Hermes Lite 子系统进行轻量级健康检查。
所有检查均为属性读取或本地 I/O（<5ms），适合 ~30s 轮询频率。

用法::

    checker = HealthChecker(orchestrator=orchestrator, ...)
    result = checker.run_all()   # 返回 dict，可直接作为 JSON 响应
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

CheckSeverity = Literal["ok", "warning", "critical"]


@dataclass
class CheckResult:
    """单个子系统健康检查的结果。"""

    status: CheckSeverity
    message: str
    detail: Optional[Dict[str, Any]] = None


@dataclass
class HealthSummary:
    """健康检查汇总计数。"""

    total: int = 0
    ok: int = 0
    warning: int = 0
    critical: int = 0


# ---------------------------------------------------------------------------
# 检查注册表条目类型
# ---------------------------------------------------------------------------

_CheckFn = Any  # Callable[[], CheckResult] 但 Python 3.9 无 Self 泛型


class HealthChecker:
    """全子系统健康检查器。

    在 server.py lifespan 中初始化，传入 orchestrator 及各全局组件引用。
    """

    # 磁盘告警阈值（GB）
    _DISK_THRESHOLD_GB = 1.0

    def __init__(
        self,
        orchestrator: Any,
        session_logger_global: Any = None,
        mcp_manager: Any = None,
        skill_loader: Any = None,
        metrics_collector: Any = None,
        proposal_store: Any = None,
    ) -> None:
        """初始化健康检查器。

        参数:
            orchestrator: Orchestrator 实例（或其 duck-typed 等价物）。
            session_logger_global: server.py 全局 ``session_logger`` 变量。
            mcp_manager: server.py 全局 ``mcp_manager`` 变量。
            skill_loader: server.py 全局 ``skill_loader`` 变量。
            metrics_collector: server.py 全局 ``metrics_collector`` 变量。
            proposal_store: server.py 全局 ``proposal_store`` 变量。
        """
        self._o = orchestrator
        self._session_logger = session_logger_global
        self._mcp = mcp_manager
        self._skill = skill_loader
        self._metrics = metrics_collector
        self._proposal = proposal_store

        # 检查注册表：每项为 (check_name, default_severity, callable)
        self._checks: List[tuple] = self._build_checks()

    def _build_checks(self) -> List[tuple]:
        """组装全部检查项。可在子类中覆盖以增删检查项。"""
        return [
            # ---- Critical（任一失败 → HTTP 503） ----
            ("orchestrator",         "critical", self._check_orchestrator),
            ("llm_client",           "critical", self._check_llm_client),
            ("react_loop",           "critical", self._check_react_loop),
            ("session_logger",       "critical", self._check_session_logger),

            # ---- Warning（失败 → degraded 状态，仍返回 200） ----
            ("tool_registry",        "warning",  self._check_tool_registry),
            ("chroma_store",         "warning",  self._check_chroma_store),
            ("history_buffer",       "warning",  self._check_history_buffer),
            ("consolidation_engine", "warning",  self._check_consolidation_engine),
            ("memory_retriever",     "warning",  self._check_memory_retriever),
            ("memory_md_manager",    "warning",  self._check_memory_md_manager),
            ("context_manager",      "warning",  self._check_context_manager),
            ("condenser",            "warning",  self._check_condenser),
            ("decay",                "warning",  self._check_decay),
            ("policy_engine",        "warning",  self._check_policy_engine),
            ("approval_manager",     "warning",  self._check_approval_manager),
            ("audit_logger",         "warning",  self._check_audit_logger),
            ("file_registry",        "warning",  self._check_file_registry),
            ("todo_registry",        "warning",  self._check_todo_registry),
            ("task_manager",         "warning",  self._check_task_manager),
            ("cron_scheduler",       "warning",  self._check_cron_scheduler),
            ("cron_tool_registry",   "warning",  self._check_cron_tool_registry),
            ("proposal_store",       "warning",  self._check_proposal_store),
            ("skill_loader",         "warning",  self._check_skill_loader),
            ("mcp_manager",          "warning",  self._check_mcp_manager),
            ("metrics_collector",    "warning",  self._check_metrics_collector),
            ("disk",                 "warning",  self._check_disk),
        ]

    # ------------------------------------------------------------------
    # 各子系统检查实现
    # ------------------------------------------------------------------

    # --- Critical 级别 ---

    def _check_orchestrator(self) -> CheckResult:
        if self._o is None:
            return CheckResult("critical", "Orchestrator 未初始化，服务不可用")
        return CheckResult("ok", "Orchestrator 已初始化")

    def _check_llm_client(self) -> CheckResult:
        if self._o is None:
            return CheckResult("critical", "Orchestrator 未初始化，无法检查 LLM")
        client = getattr(self._o, "llm_client", None)
        if client is None:
            return CheckResult("critical", "LLM 客户端未初始化，对话功能不可用")
        main_ok = getattr(client, "_main_backend", None) is not None
        consol_ok = getattr(client, "_consolidation_backend", None) is not None
        detail = {"main_backend": main_ok, "consolidation_backend": consol_ok}
        if not main_ok:
            return CheckResult("critical", "LLM 主对话后端客户端未初始化", detail)
        if not consol_ok:
            return CheckResult("warning", "LLM 沉淀专用后端客户端未初始化，将使用主客户端替代", detail)
        return CheckResult("ok", "LLM 客户端已就绪（主对话 + 沉淀双后端）", detail)

    def _check_react_loop(self) -> CheckResult:
        if self._o is None:
            return CheckResult("critical", "Orchestrator 未初始化，无法检查 ReactLoop")
        rl = getattr(self._o, "react_loop", None)
        if rl is None:
            return CheckResult("critical", "ReactLoop 引擎未初始化，对话循环不可用")
        return CheckResult("ok", "ReactLoop 引擎已初始化")

    def _check_session_logger(self) -> CheckResult:
        sl = self._session_logger
        if sl is None:
            return CheckResult("critical", "SessionLogger 未初始化，会话管理不可用")
        try:
            sl.list_sessions()
            detail = {"db_path": getattr(sl, "db_path", "?")}
            return CheckResult("ok", "SQLite 会话日志已就绪", detail)
        except Exception as e:
            return CheckResult("critical", f"SQLite 会话日志查询失败: {e}")

    # --- Warning 级别 ---

    def _check_tool_registry(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 ToolRegistry")
        reg = getattr(self._o, "tool_registry", None)
        if reg is None:
            return CheckResult("warning", "ToolRegistry 未初始化，降级为纯对话模式")
        core = len(getattr(reg, "_core_tools", {}))
        deferred = len(getattr(reg, "_deferred_tools", {}))
        loaded = len(getattr(reg, "_loaded_tools", {}))
        return CheckResult(
            "ok", f"ToolRegistry 已就绪（core={core}, deferred={deferred}, loaded={loaded}）",
            {"core_count": core, "deferred_count": deferred, "loaded_count": loaded},
        )

    def _check_chroma_store(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 ChromaDB")
        cs = getattr(self._o, "chroma_store", None)
        if cs is None:
            return CheckResult("warning", "ChromaMemoryStore 未初始化，长期记忆不可用")
        try:
            count = cs.collection.count()
            return CheckResult("ok", f"ChromaDB 已就绪（{count} 条记忆）", {"memory_count": count})
        except Exception as e:
            return CheckResult("warning", f"ChromaDB 连接或查询失败: {e}")

    def _check_history_buffer(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 HistoryBuffer")
        hb = getattr(self._o, "history_buffer", None)
        if hb is None:
            return CheckResult("warning", "HistoryBuffer 未初始化，历史缓冲降级到 SQLite")
        mt = getattr(hb, "max_turns", None)
        return CheckResult("ok", "HistoryBuffer 已初始化", {"max_turns": mt, "persistence_dir": getattr(hb, "persistence_dir", None)})

    def _check_consolidation_engine(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 ConsolidationEngine")
        ce = getattr(self._o, "consolidation_engine", None)
        if ce is None:
            return CheckResult("warning", "ConsolidationEngine 未初始化，记忆沉淀已禁用")
        return CheckResult(
            "ok", "ConsolidationEngine 已就绪",
            {"threshold": getattr(ce, "threshold", "?"), "pending": getattr(ce, "info_counter", 0)},
        )

    def _check_memory_retriever(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 MemoryRetriever")
        mr = getattr(self._o, "memory_retriever", None)
        if mr is None:
            return CheckResult("warning", "MemoryRetriever 未初始化，长期记忆检索不可用")
        return CheckResult("ok", "MemoryRetriever 已就绪", {"top_k": getattr(mr, "top_k", "?")})

    def _check_memory_md_manager(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 MemoryMdManager")
        mm = getattr(self._o, "memory_md_manager", None)
        if mm is None:
            return CheckResult("warning", "MemoryMdManager 未初始化，用户画像管理不可用")
        fn = getattr(mm, "file_path", None)
        return CheckResult("ok", "MemoryMdManager 已就绪", {"file_path": str(fn) if fn else None})

    def _check_context_manager(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 ContextManager")
        cm = getattr(self._o, "context_manager", None)
        if cm is None:
            return CheckResult("warning", "ContextManager 未初始化，Prompt 上下文优化已禁用")
        return CheckResult("ok", "ContextManager 已就绪")

    def _check_condenser(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 Condenser")
        cd = getattr(self._o, "condenser", None)
        if cd is None:
            return CheckResult("warning", "Condenser 未初始化，历史上下文压缩已禁用")
        strategy = getattr(cd, "strategy", None)
        return CheckResult("ok", "Condenser 已就绪", {"strategy": strategy})

    def _check_decay(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 MemoryDecay")
        dc = getattr(self._o, "decay", None)
        if dc is None:
            return CheckResult("warning", "MemoryDecay 未初始化，记忆排序使用静态重要性")
        return CheckResult("ok", "MemoryDecay 已就绪", {"decay_rate": getattr(dc, "decay_rate", "?")})

    def _check_policy_engine(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 PolicyEngine")
        pe = getattr(self._o, "policy_engine", None)
        if pe is None:
            return CheckResult("warning", "PolicyEngine 未初始化，安全策略拦截已禁用")
        enabled = getattr(pe, "enabled", None)
        detail = {"enabled": enabled}
        if hasattr(pe, "cron_scheduler") and getattr(pe, "cron_scheduler", None) is not None:
            detail["cron_preauth"] = True
        return CheckResult("ok", "PolicyEngine 已就绪", detail)

    def _check_approval_manager(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 ApprovalManager")
        am = getattr(self._o, "approval_manager", None)
        if am is None:
            return CheckResult("warning", "ApprovalManager 未初始化，HIL 审批降级为拒绝")
        timeout = getattr(am, "timeout", None)
        return CheckResult("ok", "ApprovalManager 已就绪", {"timeout_seconds": timeout})

    def _check_audit_logger(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 AuditLogger")
        al = getattr(self._o, "audit_logger", None)
        if al is None:
            return CheckResult("warning", "AuditLogger 未初始化，工具调用审计未记录")
        log_path = getattr(al, "log_path", None)
        return CheckResult("ok", "AuditLogger 已就绪", {"log_path": str(log_path) if log_path else None})

    def _check_file_registry(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 FileRegistry")
        fr = getattr(self._o, "file_registry", None)
        if fr is None:
            return CheckResult("warning", "FileOperationRegistry 未初始化，文件操作追踪已禁用")
        return CheckResult("ok", "FileOperationRegistry 已就绪")

    def _check_todo_registry(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 TodoRegistry")
        tr = getattr(self._o, "todo_registry", None)
        if tr is None:
            return CheckResult("warning", "TodoListRegistry 未初始化，plan 模式不可用")
        return CheckResult("ok", "TodoListRegistry 已就绪")

    def _check_task_manager(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 TaskManager")
        tm = getattr(self._o, "task_manager", None)
        if tm is None:
            return CheckResult("warning", "TaskManager 未初始化，任务归档查看不可用")
        return CheckResult("ok", "TaskManager 已就绪")

    def _check_cron_scheduler(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 CronScheduler")
        cs = getattr(self._o, "cron_scheduler", None)
        if cs is None:
            return CheckResult("warning", "CronScheduler 未初始化，定时调度已禁用")
        schedules = getattr(cs, "list_schedules", lambda: [])()
        return CheckResult("ok", f"CronScheduler 已就绪（{len(schedules)} 个调度项）", {"schedule_count": len(schedules)})

    def _check_cron_tool_registry(self) -> CheckResult:
        if self._o is None:
            return CheckResult("warning", "Orchestrator 未初始化，无法检查 CronToolRegistry")
        ctr = getattr(self._o, "cron_tool_registry", None)
        if ctr is None:
            return CheckResult("warning", "CronToolRegistry 未初始化，cron_tool 执行不可用")
        try:
            tools = ctr.list_tool_names()
            return CheckResult("ok", f"CronToolRegistry 已就绪（{len(tools)} 个工具）", {"tool_count": len(tools), "tools": tools})
        except Exception as e:
            return CheckResult("warning", f"CronToolRegistry 查询失败: {e}")

    def _check_proposal_store(self) -> CheckResult:
        ps = self._proposal
        if ps is None:
            return CheckResult("warning", "ProposalStore 未初始化，提议-确认协议不可用")
        try:
            proposals = ps.list()
            pending = sum(1 for p in proposals if getattr(p, "status", "") == "pending_confirm")
            return CheckResult("ok", f"ProposalStore 已就绪（{len(proposals)} 提议, {pending} 待确认）", {"total": len(proposals), "pending": pending})
        except Exception as e:
            return CheckResult("warning", f"ProposalStore 查询失败: {e}")

    def _check_skill_loader(self) -> CheckResult:
        sl = self._skill
        if sl is None:
            return CheckResult("warning", "SkillLoader 未初始化，Skill 扩展已禁用")
        try:
            discovered = sl.discover()
            names = [m.name for m in discovered] if discovered else []
            return CheckResult("ok", f"SkillLoader 已就绪（发现 {len(names)} 个 Skill）", {"skill_count": len(names), "skills": names})
        except Exception as e:
            return CheckResult("warning", f"SkillLoader 发现失败: {e}")

    def _check_mcp_manager(self) -> CheckResult:
        mgr = self._mcp
        if mgr is None:
            return CheckResult("warning", "MCPManager 未初始化，MCP 扩展已禁用")
        try:
            connected = list(getattr(mgr, "_clients", {}).keys())
            if connected:
                return CheckResult("ok", f"MCPManager 已就绪（{len(connected)} 个已连接 Server）", {"connected_servers": connected, "server_count": len(connected)})
            else:
                return CheckResult("warning", "MCPManager 已初始化但无已连接 Server", {"connected_servers": [], "server_count": 0})
        except Exception as e:
            return CheckResult("warning", f"MCPManager 查询失败: {e}")

    def _check_metrics_collector(self) -> CheckResult:
        mc = self._metrics
        if mc is None:
            return CheckResult("warning", "MetricsCollector 未初始化，监控指标不可用")
        return CheckResult("ok", "MetricsCollector 已就绪")

    def _check_disk(self) -> CheckResult:
        try:
            usage = shutil.disk_usage(".")
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            detail = {"free_gb": round(free_gb, 2), "total_gb": round(total_gb, 2), "threshold_gb": self._DISK_THRESHOLD_GB}
            if free_gb < self._DISK_THRESHOLD_GB:
                return CheckResult("warning", f"磁盘剩余空间不足: {free_gb:.1f}GB（阈值: {self._DISK_THRESHOLD_GB}GB）", detail)
            return CheckResult("ok", f"磁盘空间充足: {free_gb:.1f}GB 可用", detail)
        except Exception as e:
            return CheckResult("warning", f"磁盘空间检测失败: {e}")

    # ------------------------------------------------------------------
    # 聚合与编排
    # ------------------------------------------------------------------

    def run_all(self) -> Dict[str, Any]:
        """执行全部注册检查，返回健康检查 API 响应体。"""
        checks: Dict[str, CheckResult] = {}
        summary = HealthSummary()

        for name, severity, check_fn in self._checks:
            try:
                result = check_fn()
            except Exception as exc:
                result = CheckResult(severity, f"检查执行异常: {exc}")

            # 日志策略：ok 不输出，warning→WARNING，critical→ERROR
            if result.status == "warning":
                logger.warning(
                    "健康检查 [%s] 警告: %s", name, result.message,
                    extra={"system": "health", "check_name": name, "check_status": "warning"},
                )
            elif result.status == "critical":
                logger.error(
                    "健康检查 [%s] 严重: %s", name, result.message,
                    extra={"system": "health", "check_name": name, "check_status": "critical"},
                )
            # ok: 不输出日志，避免 30s 轮询刷屏

            checks[name] = result
            summary.total += 1
            if result.status == "ok":
                summary.ok += 1
            elif result.status == "warning":
                summary.warning += 1
            else:
                summary.critical += 1

        # 判定整体状态
        if summary.critical > 0:
            overall = "unhealthy"
        elif summary.warning > 0:
            overall = "degraded"
        else:
            overall = "healthy"

        # 仅在非 healthy 时记录整体日志
        if overall != "healthy":
            log_fn = logger.error if overall == "unhealthy" else logger.warning
            log_fn(
                "健康检查结果: %s (critical=%d warning=%d ok=%d)",
                overall, summary.critical, summary.warning, summary.ok,
                extra={"system": "health", "overall": overall},
            )

        # 构建响应（CheckResult → 纯 dict）
        return {
            "status": overall,
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "total": summary.total,
                "ok": summary.ok,
                "warning": summary.warning,
                "critical": summary.critical,
            },
            "checks": {
                name: {"status": r.status, "message": r.message, "detail": r.detail}
                for name, r in checks.items()
            },
        }
