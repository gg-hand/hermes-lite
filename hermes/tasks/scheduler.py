"""轻量 Cron 调度器（Phase 6 Task 3）。

本模块提供 ``CronScheduler`` 类，基于 ``CronExpr``（Task 1）实现周期性
任务调度。调度循环以 60s 为粒度检查所有启用的调度项，命中时通过
``orchestrator.chat`` 触发对应任务，并持久化调度状态到 YAML 文件。

设计要点：
- 轻量调度器，60s 检查粒度（与 cron 分钟级语义一致）。
- 单次触发用 ``asyncio.to_thread`` 在线程池执行同步的
  ``orchestrator.chat``，避免阻塞事件循环。
- 失败隔离不传染：单个调度项触发异常不影响其他调度项，仅记录日志。
- cron 表达式变更需重启调度器（避免运行时竞态），仅 ``enabled`` 字段
  可热更新。
- 专用会话隔离：触发时用 ``cron:{schedule_id}`` 作为 session_id，
  使不同调度项的对话上下文互不干扰。
- 持久化到 ``data/schedules.yaml``，采用原子写入（tmp + fsync + replace）
  避免中途崩溃导致文件损坏。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# 兼容相对导入与直接运行两种方式
from hermes.tasks.cron_expr import CronExpr
from hermes.tasks.run_summary import (
    RunSummary,
    RunsJsonlStore,
    build_default_llm_summary,
)
from hermes.tasks.workflow import (
    BUILTIN_TEMPLATES,
    StepTrace,
    WorkflowContext,
    WorkflowEngine,
    WorkflowResult,
    WorkflowSpec,
    render_time_variables,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 8 Task 4.1: granted_tools 结构校验
# ---------------------------------------------------------------------------

# granted_tools 项的合法 scope 取值
_VALID_GRANT_SCOPES = ("all", "path_prefix")

# 预授权时必须路径约束的工具（写操作类工具，scope 必须为 path_prefix 且
# allowed_paths 非空）。这些工具若以 scope="all" 预授权会带来安全风险
# （可写任意路径），故强制要求路径前缀约束。
_PATH_CONSTRAINED_TOOLS = {"file_write", "file_delete"}


def validate_granted_tools(
    granted_tools: Any, strict: bool = True
) -> List[Dict[str, Any]]:
    """校验 granted_tools 列表结构合法性（Phase 8 Task 4.1）。

    granted_tools 项结构：``{tool: str, scope: "all"|"path_prefix",
    allowed_paths: List[str]}``。

    校验规则：
    1. ``granted_tools`` 为 ``None`` 或空列表时返回空列表（合法，表示无预授权）。
    2. 必须为 list，否则抛 ``ValueError``（strict=True）或返回空列表
       （strict=False）。
    3. 每项必须为 dict，含非空 ``tool``（str）字段。
    4. ``scope`` 必须为 ``"all"`` 或 ``"path_prefix"``。
    5. ``allowed_paths`` 缺失时默认为空列表；非 list 时抛 ``ValueError``。
    6. **路径约束硬约束**：``write_file`` / ``delete_file`` 等写操作工具
       预授权时 ``scope`` 必须为 ``"path_prefix"`` 且 ``allowed_paths``
       非空（SubTask 4.3 安全边界硬约束）。

    参数:
        granted_tools: 待校验的 granted_tools 列表（或 None）。
        strict: ``True`` 时非法项抛 ``ValueError``；``False`` 时跳过非法项
            并记录 warning，返回合法项子集。``add_schedule`` /
            ``update_schedule`` 用 ``strict=True``；``load_from_config`` /
            ``_load_persisted`` 用 ``strict=False`` 避免启动中断。

    返回:
        校验通过后的 granted_tools 列表（深拷贝，allowed_paths 缺失时填充默认值）。

    Raises:
        ValueError: ``strict=True`` 且发现非法结构时抛出，错误信息含具体项。
    """
    if granted_tools is None:
        return []
    if not isinstance(granted_tools, list):
        if strict:
            raise ValueError(
                f"granted_tools 必须为列表，实际类型: {type(granted_tools).__name__}"
            )
        logger.warning(
            "granted_tools 必须为列表，实际类型: %s，已忽略",
            type(granted_tools).__name__,
        )
        return []

    validated: List[Dict[str, Any]] = []
    for idx, entry in enumerate(granted_tools):
        if not isinstance(entry, dict):
            msg = (
                f"granted_tools[{idx}] 必须为 dict，实际类型: "
                f"{type(entry).__name__}"
            )
            if strict:
                raise ValueError(msg)
            logger.warning("%s，已跳过", msg)
            continue

        tool_name = entry.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            msg = f"granted_tools[{idx}] 缺少非空 tool 字段"
            if strict:
                raise ValueError(msg)
            logger.warning("%s，已跳过", msg)
            continue

        scope = entry.get("scope")
        if scope not in _VALID_GRANT_SCOPES:
            msg = (
                f"granted_tools[{idx}].scope 必须为 "
                f"{_VALID_GRANT_SCOPES} 之一，实际: {scope!r}"
            )
            if strict:
                raise ValueError(msg)
            logger.warning("%s，已跳过", msg)
            continue

        # allowed_paths 缺失时默认空列表
        allowed_paths = entry.get("allowed_paths", [])
        if allowed_paths is None:
            allowed_paths = []
        if not isinstance(allowed_paths, list):
            msg = (
                f"granted_tools[{idx}].allowed_paths 必须为 list，"
                f"实际类型: {type(allowed_paths).__name__}"
            )
            if strict:
                raise ValueError(msg)
            logger.warning("%s，已跳过", msg)
            continue

        # 路径约束硬约束：write_file / delete_file 必须 path_prefix + 非空 allowed_paths
        if tool_name in _PATH_CONSTRAINED_TOOLS:
            if scope != "path_prefix":
                msg = (
                    f"granted_tools[{idx}].tool={tool_name} 为写操作工具，"
                    f"scope 必须为 'path_prefix'（当前: {scope!r}），"
                    f"不允许 scope='all' 预授权"
                )
                if strict:
                    raise ValueError(msg)
                logger.warning("%s，已跳过", msg)
                continue
            if not allowed_paths:
                msg = (
                    f"granted_tools[{idx}].tool={tool_name} 为写操作工具，"
                    f"allowed_paths 不能为空（需路径前缀约束）"
                )
                if strict:
                    raise ValueError(msg)
                logger.warning("%s，已跳过", msg)
                continue

        validated.append(
            {
                "tool": tool_name,
                "scope": scope,
                "allowed_paths": list(allowed_paths),
            }
        )

    return validated


@dataclass
class Schedule:
    """调度项数据模型。

    Attributes:
        id: 调度项唯一标识，缺省由 ``uuid4().hex[:8]`` 生成。
        name: 调度项名称（人类可读）。
        cron: 原始 cron 表达式字符串（5 字段）。
        task: 触发时传给 ``orchestrator.chat`` 的 ``user_input``。
        enabled: 是否启用，``False`` 时不会被调度循环触发。
        last_run: 最近一次触发时间（ISO 格式字符串），未触发时为 ``None``。
        next_run: 预计下一次触发时间（ISO 格式字符串），未计算时为 ``None``。
        cron_id: Phase 8 Task 1.6。记忆隔离命名空间标识，默认 ``None``
            时取 ``id``。用于 ChromaMemoryStore 的 ``namespace="cron"`` +
            ``cron_id`` 过滤，使不同调度项的记忆互不可见。
        granted_tools: Phase 8 Task 4。预授权工具列表，每项形如
            ``{"tool": str, "scope": "all"|"path_prefix", "allowed_paths": List[str]}``。
            PolicyEngine cron 路径据此三层检查放行工具调用。``None`` 表示
            无预授权，回退默认规则。
        active_tools_snapshot: Phase 8 Task 5。调度项创建时锁定的工具名
            快照（List[str]）。Orchestrator cron 路径据此做请求级过滤
            （``[t for t in registry.get_tools_schema() if t["name"] in snapshot]``），
            不修改全局 ToolRegistry，保证用户会话 tools schema 字节级稳定
            （缓存约束 1+2）。``None`` 表示不限制，使用完整工具集。
        workflow: Phase 8 Task 2。工作流模板配置，形如
            ``{"template": "directory_watch", "watch_path": "/data", ...}``。
            含 ``workflow`` 字段时 CronScheduler 走 WorkflowTemplate.execute
            路径；否则走 legacy ``orchestrator.chat`` 路径。
        generate_llm_summary: Phase 8 Task 2。是否在执行后额外调轻模型
            生成精炼 RunSummary。默认 ``False``（仅从 audit_log +
            assistant_response 提取，无额外 LLM 成本）。
    """

    id: str
    name: str
    cron: str
    task: str
    enabled: bool = True
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    # Phase 8 Task 1.6: 隔离层 + 工作流 + 预授权字段
    cron_id: Optional[str] = None
    granted_tools: Optional[List[Dict[str, Any]]] = None
    active_tools_snapshot: Optional[List[str]] = None
    workflow: Optional[Dict[str, Any]] = None
    generate_llm_summary: bool = False

    def get_cron_id(self) -> str:
        """返回用于记忆隔离的 cron_id。

        ``cron_id`` 字段为 ``None`` 时回退到 ``id``（向后兼容）。
        """
        return self.cron_id if self.cron_id is not None else self.id


class CronScheduler:
    """轻量 Cron 调度器。

    管理 ``Schedule`` 列表，按 60s 粒度检查命中表达式的调度项并通过
    ``orchestrator.chat`` 触发。调度状态持久化到 YAML 文件，支持 CRUD
    与运行时触发（``trigger_now``）。

    调度循环 ``run_loop`` 为协程，应在事件循环中 ``await`` 运行；调用
    ``stop`` 可优雅停止循环。cron 表达式在加载时即解析并缓存为
    ``CronExpr``，运行时仅 ``enabled`` 字段可热更新，cron 变更需重启
    调度器以避免竞态。
    """

    def __init__(
        self,
        schedules_file: str = "data/schedules.yaml",
        runs_store: Optional[Any] = None,
        workflow_context_factory: Optional[Any] = None,
    ) -> None:
        """初始化调度器。

        参数:
            schedules_file: 调度状态持久化文件路径。父目录不存在时会自动
                创建。启动时尝试从该文件加载已持久化的调度项。
            runs_store: Phase 8 Task 2.8。``RunsJsonlStore`` 实例，用于
                持久化每次执行的 RunSummary。为 ``None`` 时内部懒创建
                默认实例（``RunsJsonlStore(base_dir="data/schedules")``）。
            workflow_context_factory: Phase 8 Task 2.12。可调用对象，签名
                ``(schedule: Schedule, last_run_time: Optional[datetime]) -> WorkflowContext``。
                为 ``None`` 时使用默认工厂，从 ``orchestrator`` 属性读取
                ``llm_client`` / ``chroma_store`` 等依赖。
        """
        self.schedules_file = Path(schedules_file)
        # 确保父目录存在
        self.schedules_file.parent.mkdir(parents=True, exist_ok=True)

        self._schedules: List[Schedule] = []
        # 去重表：同一 schedule_id 在同一分钟仅触发一次
        # key=schedule.id, value=current_minute_key
        self._last_triggered_minute: Dict[str, str] = {}
        # 停止信号，由 stop() 设置以中断 run_loop
        self._stop_event = asyncio.Event()
        # cron 表达式缓存：key=schedule.id, value=CronExpr 实例
        self._cron_exprs: Dict[str, CronExpr] = {}

        # Phase 8 Task 2.8: RunSummary 持久化存储器
        # 为 None 时使用默认 RunsJsonlStore（base_dir 与 schedules_file 父目录一致）
        if runs_store is not None:
            self.runs_store = runs_store
        elif RunsJsonlStore is not None:
            # 默认 base_dir 取 schedules_file 父目录（如 data/schedules）
            base_dir = str(self.schedules_file.parent)
            self.runs_store = RunsJsonlStore(base_dir=base_dir)
        else:
            self.runs_store = None  # type: ignore

        # Phase 8 Task 2.12: WorkflowContext 工厂回调
        # 由 server.py 在装配时注入，从 orchestrator 读取 llm_client / chroma_store 等。
        # 为 None 时 _trigger 内部使用默认工厂（依赖 orchestrator 属性）。
        self.workflow_context_factory = workflow_context_factory

        # ops-reliability-uplift Task 4.4: 预留 cron 调度项锁字典
        # Phase 3 Task 15 并发改造时使用，每个 schedule 一个 asyncio.Lock 防 overlap
        self._cron_locks: Dict[str, "asyncio.Lock"] = {}

        # 启动时加载已持久化的调度项
        self._load_persisted()

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------
    def load_from_config(self, schedules_cfg: list) -> None:
        """从 config.yaml 的 schedules 段加载调度项。

        解析 list of dict 配置，为每项构造 ``Schedule`` 与 ``CronExpr``。
        非法 cron 表达式的项会被标记为 ``enabled=False`` 并记录 error，
        不抛异常以保证整体加载不被单项中断。加载完成后持久化，使规范化
        后的 id（缺省生成的）写回 YAML。

        Phase 8 Task 1.6: 支持新字段 ``cron_id`` / ``granted_tools`` /
        ``active_tools_snapshot`` / ``workflow`` / ``generate_llm_summary``，
        缺失字段使用默认值（向后兼容）。

        参数:
            schedules_cfg: config 中 ``schedules`` 段，list of dict。
                每项可含字段：``id`` / ``name`` / ``cron`` / ``task`` /
                ``enabled``（默认 ``True``）/ ``cron_id`` / ``granted_tools``
                / ``active_tools_snapshot`` / ``workflow`` /
                ``generate_llm_summary``。
        """
        loaded: List[Schedule] = []
        for item in schedules_cfg or []:
            schedule_id = item.get("id") or uuid.uuid4().hex[:8]
            name = item.get("name", "")
            cron = item.get("cron", "")
            task = item.get("task", "")
            enabled = item.get("enabled", True)

            # 尝试构造 CronExpr，非法则标记禁用并记录
            try:
                cron_expr = CronExpr(cron)
                self._cron_exprs[schedule_id] = cron_expr
            except ValueError:
                logger.error(
                    "调度项 %s 的 cron 表达式非法: %s", schedule_id, cron
                )
                enabled = False

            loaded.append(
                Schedule(
                    id=schedule_id,
                    name=name,
                    cron=cron,
                    task=task,
                    enabled=enabled,
                    # Phase 8 Task 1.6: 新字段（缺失使用默认值，向后兼容）
                    cron_id=item.get("cron_id"),
                    # Phase 8 Task 4.1: granted_tools 结构校验（非 strict，
                    # 非法项记录 warning 并跳过，避免启动中断）
                    granted_tools=(
                        validate_granted_tools(
                            item.get("granted_tools"), strict=False
                        )
                        or None
                    ),
                    active_tools_snapshot=item.get("active_tools_snapshot"),
                    workflow=item.get("workflow"),
                    generate_llm_summary=bool(item.get("generate_llm_summary", False)),
                )
            )

        self._schedules = loaded
        self._persist()

    # ------------------------------------------------------------------
    # 调度循环
    # ------------------------------------------------------------------
    async def run_loop(self, orchestrator: Any) -> None:
        """主调度循环，按 60s 粒度检查并触发命中的调度项。

        循环逻辑：
        1. 取当前时间，计算 ``current_minute_key``（``%Y-%m-%d %H:%M``）。
        2. 遍历所有 ``enabled=True`` 的调度项，若 ``CronExpr.matches(now)``
           且该调度项在本分钟尚未触发过，则触发并记录去重标记。
        3. 用 ``asyncio.wait_for(stop_event.wait(), timeout=60)`` 实现可
           中断的 60s 等待：超时（``TimeoutError``）正常继续；被 ``stop``
           设置则退出循环；``CancelledError`` 也退出循环。

        参数:
            orchestrator: 触发时调用的编排器，需提供 ``chat(session_id,
                user_input)`` 同步方法。
        """
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute_key = now.strftime("%Y-%m-%d %H:%M")

            for schedule in self._schedules:
                if not schedule.enabled:
                    continue
                cron_expr = self._cron_exprs.get(schedule.id)
                if cron_expr is None:
                    # 构造失败的 cron，跳过
                    continue
                if cron_expr.matches(now) and (
                    self._last_triggered_minute.get(schedule.id)
                    != current_minute_key
                ):
                    # 标记本分钟已触发，避免重复
                    self._last_triggered_minute[schedule.id] = (
                        current_minute_key
                    )
                    await self._trigger(orchestrator, schedule)

            # 可中断的 60s 等待
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=60
                )
                # stop_event 被 set，退出循环
                break
            except asyncio.TimeoutError:
                # 超时正常继续下一轮
                continue
            except asyncio.CancelledError:
                # 被取消，退出循环
                break

    def _clear_cron_history(self, orchestrator: Any, session_id: str) -> None:
        """清空指定 cron 会话的 history_buffer（ops-reliability-uplift Task 4.2）。

        触发前调用，清空内存中的该 session 历史并删除磁盘 JSONL 文件，
        确保 LLM 上下文不含上一次执行的 user/assistant 消息。

        参数:
            orchestrator: 编排器实例，从中读取 ``history_buffer`` 属性。
            session_id: cron 会话 ID（形如 ``cron:{schedule_id}``）。
        """
        history_buffer = getattr(orchestrator, "history_buffer", None)
        if history_buffer is None:
            logger.warning(
                "orchestrator.history_buffer 为 None，跳过 cron 上下文清空 session_id=%s",
                session_id,
            )
            return
        try:
            history_buffer.clear_session(session_id)
            logger.info(
                "已清空 cron 会话历史 session_id=%s（隔离上下文）",
                session_id,
            )
        except Exception:
            logger.warning(
                "清空 cron 会话历史失败 session_id=%s",
                session_id,
                exc_info=True,
            )

    def _build_cron_archive_callback(self, orchestrator: Any, schedule_id: str):
        """构造 cron 专用 archive_callback 闭包（ops-reliability-uplift Task 6.2）。

        cron session 不走 orchestrator 默认的 ``_archive_evicted_message``
        （后者把完整 conversation_turn 写入向量库），而是改为从
        ``runs_store.read_last(schedule_id)`` 读取上次 run 的 ``llm_summary``
        字段（精炼摘要），以 ``type=summary`` 写入 cron namespace，避免
        history_buffer FIFO 淘汰时机不可控导致摘要丢失或原文污染。

        闭包签名与 orchestrator 默认 archive_callback 一致：
        ``(sid: str, msg: Dict[str, Any]) -> None``，仅处理 ``sid`` 以
        ``cron:`` 开头的会话（user session 由 orchestrator 闭包处理）。

        参数:
            orchestrator: 编排器实例，用于读取 ``chroma_store`` 属性。
            schedule_id: 调度项 ID（不含 ``cron:`` 前缀）。

        返回:
            archive_callback 闭包函数。``runs_store`` / ``chroma_store``
            不可用时返回 None（调用方应跳过覆盖）。
        """
        if self.runs_store is None:
            logger.debug(
                "runs_store 不可用，跳过 cron archive_callback 覆盖 schedule_id=%s",
                schedule_id,
            )
            return None

        chroma_store = getattr(orchestrator, "chroma_store", None)
        if chroma_store is None:
            logger.debug(
                "orchestrator.chroma_store 不可用，跳过 cron archive_callback 覆盖 schedule_id=%s",
                schedule_id,
            )
            return None

        runs_store = self.runs_store

        def _archive_cron_evicted(sid: str, msg: Dict[str, Any]) -> None:
            """cron session 归档回调：写入上次 run 的 llm_summary（非完整对话）。"""
            # 仅处理 cron session（user session 由 orchestrator 闭包处理）
            if not sid.startswith("cron:"):
                return
            try:
                last_run = runs_store.read_last(schedule_id)
            except Exception:
                logger.warning(
                    "读取 runs.jsonl 上次记录失败，跳过 cron 归档 schedule_id=%s",
                    schedule_id,
                    exc_info=True,
                )
                return
            if last_run is None:
                return
            summary_text = getattr(last_run, "llm_summary", "") or ""
            if not summary_text.strip():
                return
            metadata = {
                "type": "summary",
                "session_id": sid,
                "run_id": getattr(last_run, "run_id", "") or "",
                "started_at": getattr(last_run, "started_at", "") or "",
                "timestamp": getattr(last_run, "finished_at", "") or "",
            }
            try:
                chroma_store.add_memory(
                    summary_text,
                    metadata=metadata,
                    namespace="cron",
                    cron_id=schedule_id,
                )
                logger.debug(
                    "cron session 归档 summary 到向量库 schedule_id=%s run_id=%s",
                    schedule_id,
                    metadata["run_id"],
                )
            except Exception:
                logger.warning(
                    "cron archive_callback 写入 chroma_store 失败 schedule_id=%s",
                    schedule_id,
                    exc_info=True,
                )

        return _archive_cron_evicted

    async def _trigger(
        self, orchestrator: Any, schedule: Schedule
    ) -> None:
        """触发单个调度项。

        Phase 8 Task 2.12: 根据调度项是否含 ``workflow`` 字段分两条路径：
        - **workflow 路径**：构造 :class:`WorkflowContext`，调用
          :class:`WorkflowTemplate.execute`，产出 :class:`WorkflowResult`。
        - **legacy 路径**：调用 ``orchestrator.chat(session_id, task)``。

        两条路径执行后均生成 :class:`RunSummary` 并 append 到
        ``runs.jsonl``（SubTask 2.9）。``last_run`` / ``next_run`` 同步更新。

        触发中的异常被捕获并记录，不影响其他调度项。``started_at`` /
        ``finished_at`` / ``duration_seconds`` 自动记录。

        上下文隔离（ops-reliability-uplift Task 4）：触发前清空该 schedule 的
        history_buffer，确保 LLM 上下文不含上一次执行的 user/assistant 消息，
        从根源杜绝偷懒复用上次结果。保留 ``cron:{schedule.id}`` 稳定 session_id
        用于审计视图与画像隔离前缀检测。

        归档隔离（ops-reliability-uplift Task 6）：触发期间临时覆盖
        ``history_buffer.archive_callback`` 为 cron 专用闭包，把 FIFO 淘汰
        归档改为写入上次 run 的 ``llm_summary``（``type=summary``）而非
        完整 ``conversation_turn``，避免原文污染 + 摘要时机不可控。
        try/finally 保证 chat 异常时也能恢复原 archive_callback。

        参数:
            orchestrator: 编排器实例（用于 legacy 路径与默认 workflow 上下文）。
            schedule: 待触发的调度项。
        """
        session_id = f"cron:{schedule.id}"
        # ops-reliability-uplift Task 4.1: 触发前清空 history_buffer 隔离上下文
        self._clear_cron_history(orchestrator, session_id)

        # 确保 cron 会话在 session_logger 中存在（即使 workflow 首步即失败，
        # 用户也能在调度会话列表中看到该会话并查看错误信息）
        _sl = getattr(orchestrator, "session_logger", None)
        if _sl is not None:
            try:
                _sl.ensure_session(session_id)
                if schedule.name:
                    _sl.update_session_title(session_id, schedule.name)
            except Exception:
                logger.warning("ensure_session 失败 session_id=%s", session_id, exc_info=True)

        # ops-reliability-uplift Task 6.4: 临时覆盖 archive_callback
        # cron session 不走 orchestrator 默认归档（type=conversation_turn 全文），
        # 改为归档上次 run 的 llm_summary（type=summary 精炼摘要）。
        # 用 try/finally 保证 chat 异常时也能恢复原值，避免污染后续 user session。
        _history_buf = getattr(orchestrator, "history_buffer", None)
        _orig_archive_cb = getattr(_history_buf, "archive_callback", None) if _history_buf else None
        _new_archive_cb = self._build_cron_archive_callback(orchestrator, schedule.id)
        if _history_buf is not None and _new_archive_cb is not None:
            try:
                _history_buf.archive_callback = _new_archive_cb
            except Exception:
                logger.warning(
                    "覆盖 archive_callback 失败，降级到 orchestrator 默认归档 schedule_id=%s",
                    schedule.id,
                    exc_info=True,
                )
                _new_archive_cb = None  # 标记未生效

        try:
            started_at_dt = datetime.now()
            started_at = started_at_dt.isoformat()

            # Task 10：提前生成 run_id，供 WorkflowContext 注入与 RunSummary 持久化
            run_id = uuid.uuid4().hex[:12]

            # 解析上次执行时间（用于 WorkflowContext.last_run_time 与时间变量替换）
            last_run_dt = self._parse_last_run_time(schedule)

            # 任务文本渲染（SubTask 2.7 第三层：任务文本层时间变量替换）
            task_text = schedule.task or ""
            if render_time_variables is not None:
                task_text = render_time_variables(
                    task_text,
                    current_time=started_at_dt,
                    last_run_time=last_run_dt,
                )

            success = True
            assistant_response = ""
            tool_calls: List[Dict[str, Any]] = []
            outputs: List[Dict[str, Any]] = []
            errors: List[str] = []
            # Task 10.4：step_traces / workflow_name 从 WorkflowResult 提取并持久化
            step_traces_for_summary: List[Dict[str, Any]] = []
            workflow_name: Optional[str] = None
            # 标记是否走 workflow 路径（该路径不经过 orchestrator.chat，
            # 需手动写 session_logger 消息以使调度会话历史可见）
            is_workflow_path = False

            try:
                if schedule.workflow:
                    # workflow 路径（Task 10.2 双轨：多步经 WorkflowEngine，简易走模板）
                    is_workflow_path = True
                    wf_result = await asyncio.to_thread(
                        self._execute_workflow,
                        orchestrator,
                        schedule,
                        task_text,
                        started_at_dt,
                        last_run_dt,
                        run_id,
                    )
                    if wf_result is not None:
                        success = wf_result.success
                        assistant_response = wf_result.assistant_response
                        tool_calls = list(wf_result.tool_calls)
                        outputs = list(wf_result.outputs)
                        errors = list(wf_result.errors)
                        # Task 10.4：把 StepTrace 列表转为 List[Dict] 持久化到 runs.jsonl
                        step_traces_for_summary = [
                            t.to_dict() if hasattr(t, "to_dict") else dict(t)
                            for t in (wf_result.step_traces or [])
                        ]
                        # workflow_name 优先取 wf_result.workflow_name，回退 schedule.name
                        workflow_name = (
                            wf_result.workflow_name or schedule.name
                        )
                else:
                    # legacy 路径：直接 orchestrator.chat（Task 5 已改 async）
                    # is_cron=True 使 LLMClient 选择 reasoning.cron 配置
                    response = await orchestrator.chat(
                        session_id, task_text, is_cron=True
                    )
                    assistant_response = response or ""
                    workflow_name = schedule.name
            except Exception as e:
                logger.exception("调度项 %s 触发失败", schedule.id)
                success = False
                errors.append(f"触发异常: {e}")
                workflow_name = schedule.name

            # 设置 cron 会话标题为 schedule.name（避免 cron 会话显示随机串）
            # 首次触发后写入，后续触发复用（update_session_title 内部用 WHERE id=?，
            # 若会话尚未创建则 UPDATE 不影响任何行，等下一轮日志记录时由
            # ensure_session 创建后再写入）。orchestrator.chat 内部会调 ensure_session。
            _sl = getattr(orchestrator, "session_logger", None)
            if _sl is not None and schedule.name:
                try:
                    _sl.update_session_title(session_id, schedule.name)
                except Exception as e:
                    logger.warning("设置 cron 会话标题失败: %s", e)

            # workflow 路径不经过 orchestrator.chat()，需手动写 session_logger 消息，
            # 使调度会话历史可见（用户点击 cron 会话可查看触发任务与结果）。
            # legacy 路径由 orchestrator.chat 内部写入，无需重复。
            if is_workflow_path and _sl is not None:
                try:
                    # 1. user 消息（渲染后的任务文本）
                    _sl.log_message(
                        session_id=session_id,
                        role="user",
                        content=task_text or "",
                    )
                    # 2. tool 消息（按执行顺序记录每个工具调用）
                    for tc in tool_calls:
                        tool_name = tc.get("name", "")
                        result_str = tc.get("result", "")
                        _sl.log_message(
                            session_id=session_id,
                            role="tool",
                            content=str(result_str),
                            tool_name=tool_name,
                            is_error=bool(tc.get("is_error", False)),
                        )
                    # 3. assistant 消息（成功取 assistant_response，失败汇总 errors）
                    if success:
                        final_content = assistant_response or "(workflow 执行完成但无文本输出)"
                    else:
                        err_lines = "; ".join(errors) if errors else "未知错误"
                        final_content = f"[调度执行失败] {err_lines}"
                    _sl.log_message(
                        session_id=session_id,
                        role="assistant",
                        content=final_content,
                    )
                except Exception as e:
                    logger.warning(
                        "workflow 路径写 session_logger 失败 session_id=%s: %s",
                        session_id, e,
                    )

            finished_at_dt = datetime.now()
            finished_at = finished_at_dt.isoformat()
            duration_seconds = (finished_at_dt - started_at_dt).total_seconds()

            # 更新 last_run / next_run 并持久化（无论成功失败）
            schedule.last_run = finished_at
            cron_expr = self._cron_exprs.get(schedule.id)
            if cron_expr is not None:
                try:
                    schedule.next_run = cron_expr.next_run(
                        finished_at_dt
                    ).isoformat()
                except Exception:
                    pass
            self._persist()

            # SubTask 2.9: 生成 RunSummary 并 append 到 runs.jsonl
            # Task 10.4：透传 step_traces / workflow_name / run_id
            await asyncio.to_thread(
                self._append_run_summary,
                schedule,
                started_at,
                finished_at,
                duration_seconds,
                success,
                task_text,
                assistant_response,
                tool_calls,
                outputs,
                errors,
                step_traces_for_summary,
                workflow_name,
                run_id,
            )
        finally:
            # ops-reliability-uplift Task 6.4: 恢复原 archive_callback
            # 即使 chat 异常也要恢复，避免污染后续 user session 归档行为
            if _history_buf is not None and _orig_archive_cb is not None and _new_archive_cb is not None:
                try:
                    _history_buf.archive_callback = _orig_archive_cb
                except Exception:
                    logger.warning(
                        "恢复 archive_callback 失败 schedule_id=%s",
                        schedule.id,
                        exc_info=True,
                    )

    def _execute_workflow(
        self,
        orchestrator: Any,
        schedule: Schedule,
        task_text: str,
        started_at_dt: datetime,
        last_run_dt: Optional[datetime],
        run_id: str = "",
    ):
        """执行 workflow 路径（在线程池中同步调用）。

        Task 10.2 双轨支持：
        - **多步模式**（``workflow.steps`` 存在）：构造 :class:`WorkflowSpec`
          并调 :class:`WorkflowEngine.execute`。
        - **简易模式**（仅 ``template`` 字段）：走旧 :class:`WorkflowTemplate.execute`
          路径，由 Task 10.3 补充 step_traces 包装保持 RunSummary 字段一致。

        D2 修复：永不返回 None，所有失败路径返回
        ``WorkflowResult(success=False, errors=["...具体原因..."])``。

        参数:
            orchestrator: 编排器实例，用于默认 WorkflowContext 工厂。
            schedule: 待触发的调度项。
            task_text: 渲染后的任务文本（已替换时间变量）。
            started_at_dt: 触发开始时间。
            last_run_dt: 上次执行时间。
            run_id: 本次执行批次 ID（Task 10），注入 WorkflowContext 供
                模板/StepExecutor 关联工具调用与审计。

        返回:
            :class:`WorkflowResult` 实例（失败时 ``success=False`` + 错误描述）。
        """
        # WorkflowResult 延迟导入避免循环依赖（与模块顶层 try/except 一致）
        if WorkflowResult is None:
            logger.error("WorkflowResult 不可用，无法构造失败结果")
            return None  # 极端兜底，仅在导入失败时发生

        workflow_cfg = schedule.workflow or {}
        has_steps = bool(workflow_cfg.get("steps"))

        # 多步模式前置校验：WorkflowEngine / WorkflowSpec 必须可用
        if has_steps and (WorkflowEngine is None or WorkflowSpec is None):
            logger.warning("WorkflowEngine 不可用，无法执行多步 workflow")
            return WorkflowResult(
                success=False,
                errors=["workflow 多步模式不可用（WorkflowEngine 未加载）"],
            )

        # 简易模式前置校验：BUILTIN_TEMPLATES 必须非空
        if not has_steps and not BUILTIN_TEMPLATES:
            logger.warning("workflow 模块不可用，跳过简易模式 workflow 路径")
            return WorkflowResult(
                success=False,
                errors=["workflow 模块不可用（BUILTIN_TEMPLATES 为空）"],
            )

        # 简易模式：解析 template_name / template_cls
        template_name = ""
        template_cls = None
        if not has_steps:
            template_name = workflow_cfg.get("template")
            # D2 路径 2：缺 template 字段
            if not template_name:
                logger.warning(
                    "调度项 %s 的 workflow 配置缺少 template 字段",
                    schedule.id,
                )
                return WorkflowResult(
                    success=False,
                    errors=[
                        f"调度项 {schedule.id} 的 workflow 配置缺少 template 字段"
                    ],
                )
            template_cls = BUILTIN_TEMPLATES.get(template_name)
            # D2 路径 3：模板未找到
            if template_cls is None:
                logger.warning(
                    "调度项 %s 引用了未知的工作流模板: %s",
                    schedule.id,
                    template_name,
                )
                return WorkflowResult(
                    success=False,
                    errors=[
                        f"调度项 {schedule.id} 引用了未知的工作流模板: {template_name}"
                    ],
                )

        # 构造 WorkflowContext（Task 10.1：注入 run_id 等额外字段）
        context = self._build_workflow_context(
            orchestrator,
            schedule,
            started_at_dt,
            last_run_dt,
            run_id=run_id,
            session_id=f"cron:{schedule.id}",
        )
        # D2 路径 4：WorkflowContext 构造失败（极端兜底）
        if context is None:
            logger.error("调度项 %s 构造 WorkflowContext 失败", schedule.id)
            return WorkflowResult(
                success=False,
                errors=[f"调度项 {schedule.id} 构造 WorkflowContext 失败"],
            )

        # Task 10.2：双轨分发
        if has_steps:
            return self._execute_workflow_engine(
                workflow_cfg, context, schedule, run_id
            )
        return self._execute_workflow_template(
            workflow_cfg,
            context,
            schedule,
            run_id,
            template_name,
            template_cls,
            started_at_dt,
        )

    def _execute_workflow_engine(
        self,
        workflow_cfg: Dict[str, Any],
        context: Any,
        schedule: Schedule,
        run_id: str,
    ):
        """Task 10.2：多步 workflow 路径，经 WorkflowEngine.execute 执行。

        D5 修复：WorkflowEngine 已在末尾合并 ``context.error_channel`` 到
        ``WorkflowResult.errors``，此处不再重复合并。
        """
        try:
            spec = WorkflowSpec.from_dict(workflow_cfg)
            engine = WorkflowEngine()
            result = engine.execute(spec, context)
            if result is None:
                logger.warning(
                    "调度项 %s 的 WorkflowEngine 返回 None",
                    schedule.id,
                )
                return WorkflowResult(
                    success=False,
                    errors=["WorkflowEngine 返回 None"],
                )
            # 确保 run_id / workflow_name 填充（Task 10.4 持久化需要）
            if not result.run_id:
                result.run_id = run_id
            if not result.workflow_name:
                result.workflow_name = spec.name or schedule.name
            return result
        except NotImplementedError as e:
            logger.warning(
                "调度项 %s 的 workflow 未实装: %s", schedule.id, e
            )
            return WorkflowResult(
                success=False,
                errors=[f"workflow 未实装: {e}"],
            )
        except Exception as e:
            logger.exception(
                "调度项 %s 的 workflow 执行失败", schedule.id
            )
            return WorkflowResult(
                success=False,
                errors=[f"workflow 执行异常: {type(e).__name__}: {e}"],
            )

    def _execute_workflow_template(
        self,
        workflow_cfg: Dict[str, Any],
        context: Any,
        schedule: Schedule,
        run_id: str,
        template_name: str,
        template_cls: Any,
        started_at_dt: datetime,
    ):
        """Task 10.2/10.3：旧模板路径，含 step_traces 包装与 error_channel 合并。

        Task 10.3：旧路径执行后补充单条 StepTrace（status=success/failed），
        保持 RunSummary.step_traces 字段与多步路径一致。
        Task 10.4：合并 ``context.error_channel`` 到 ``WorkflowResult.errors``
        （D5 修复仅覆盖 WorkflowEngine 路径，旧模板路径在此处补齐）。
        """
        # 模板配置：剥离 ``template`` 字段，剩余字段作为 config
        template_cfg = {
            k: v for k, v in workflow_cfg.items() if k != "template"
        }

        template = template_cls()
        step_started = datetime.now()
        try:
            result = template.execute(template_cfg, context)
            # 模板返回 None 时兜底为失败结果（防御性）
            if result is None:
                logger.warning(
                    "调度项 %s 的模板 %s 返回 None，转为失败结果",
                    schedule.id,
                    template_name,
                )
                result = WorkflowResult(
                    success=False,
                    errors=[f"模板 {template_name} 返回 None"],
                )
        except NotImplementedError as e:
            # D2 路径 5：NotImplementedError
            logger.warning(
                "调度项 %s 引用的模板 %s 未实装: %s",
                schedule.id,
                template_name,
                e,
            )
            result = WorkflowResult(
                success=False,
                errors=[f"模板 {template_name} 未实装: {e}"],
            )
        except Exception as e:
            # D2 路径 6：其他异常
            logger.exception(
                "调度项 %s 的工作流模板 %s 执行失败",
                schedule.id,
                template_name,
            )
            result = WorkflowResult(
                success=False,
                errors=[f"模板 {template_name} 执行异常: {type(e).__name__}: {e}"],
            )

        # Task 10.4：合并 context.error_channel 到 result.errors
        # （旧模板路径未走 WorkflowEngine，D5 合并未覆盖此处，需手动补齐）
        error_channel = list(getattr(context, "error_channel", []) or [])
        if error_channel:
            for err in error_channel:
                if err not in result.errors:
                    result.errors.append(err)
            if result.success:
                # error_channel 含 LLM 调用失败等异常，标记为失败
                result.success = False

        # Task 10.3：补充单条 StepTrace 包装（保持与多步路径字段一致）
        self._wrap_template_result_with_step_trace(
            result,
            template_name=template_name,
            run_id=run_id,
            schedule=schedule,
            step_started=step_started,
        )

        # 确保 run_id / workflow_name 填充
        if not result.run_id:
            result.run_id = run_id
        if not result.workflow_name:
            result.workflow_name = schedule.name
        return result

    def _wrap_template_result_with_step_trace(
        self,
        result: Any,
        template_name: str,
        run_id: str,
        schedule: Schedule,
        step_started: datetime,
    ) -> None:
        """Task 10.3：旧模板路径补充单条 StepTrace。

        仅当 result.step_traces 为空时填充（避免与模板内部已生成的 trace 冲突）。
        状态根据 result.success 映射 success/failed，工具调用与文件产出透传。
        """
        if StepTrace is None:
            return
        if result.step_traces:
            return  # 已有 trace，不覆盖
        step_finished = datetime.now()
        duration_ms = int(
            (step_finished - step_started).total_seconds() * 1000
        )
        trace = StepTrace(
            step_id="template",
            step_name=template_name or schedule.name,
            step_type="deterministic",
            started_at=step_started.isoformat(),
            finished_at=step_finished.isoformat(),
            duration_ms=duration_ms,
            attempts=1,
            status="success" if result.success else "failed",
            tool_calls=list(result.tool_calls or []),
            files=list(result.outputs or []),
        )
        result.step_traces.append(trace)

    def _build_workflow_context(
        self,
        orchestrator: Any,
        schedule: Schedule,
        started_at_dt: datetime,
        last_run_dt: Optional[datetime],
        run_id: str = "",
        session_id: str = "",
    ):
        """构造 WorkflowContext。

        优先调用 ``workflow_context_factory``（若注入）；否则使用默认工厂
        从 ``orchestrator`` 读取 ``llm_client`` / ``chroma_store`` /
        ``session_logger`` 等依赖。

        Task 10.1：在现有 setattr 之后新增 ``run_id`` / ``skill_loader`` /
        ``tool_registry`` / ``orchestrator`` / ``policy_engine`` /
        ``audit_logger`` / ``session_id`` 注入，供 StepExecutor 的
        PolicyEngine.check 与 audit log_tool_call 透传。

        参数:
            orchestrator: 编排器实例。
            schedule: 调度项。
            started_at_dt: 触发开始时间。
            last_run_dt: 上次执行时间。
            run_id: 本次执行批次 ID（Task 10）。
            session_id: 会话 ID（默认 ``cron:{schedule.id}``）。

        返回:
            :class:`WorkflowContext` 实例。
        """
        if self.workflow_context_factory is not None:
            try:
                ctx = self.workflow_context_factory(schedule, last_run_dt)
                # Task 10.1：factory 路径也注入 run_id 等字段（若 factory 未自填）
                if ctx is not None and run_id:
                    setattr(ctx, "run_id", run_id)
                if ctx is not None:
                    setattr(
                        ctx,
                        "skill_loader",
                        getattr(orchestrator, "skill_loader", None),
                    )
                    setattr(
                        ctx,
                        "tool_registry",
                        getattr(orchestrator, "tool_registry", None),
                    )
                    setattr(ctx, "orchestrator", orchestrator)
                    setattr(
                        ctx,
                        "policy_engine",
                        getattr(orchestrator, "policy_engine", None),
                    )
                    setattr(
                        ctx,
                        "audit_logger",
                        getattr(orchestrator, "audit_logger", None),
                    )
                    if session_id:
                        setattr(ctx, "session_id", session_id)
                return ctx
            except Exception:
                logger.warning(
                    "workflow_context_factory 调用失败，回退默认工厂",
                    exc_info=True,
                )

        if WorkflowContext is None:
            return None  # type: ignore

        # 默认工厂：从 orchestrator 读取依赖
        llm_client = getattr(orchestrator, "llm_client", None)
        chroma_store = getattr(orchestrator, "chroma_store", None)
        session_logger = getattr(orchestrator, "session_logger", None)
        react_loop = getattr(orchestrator, "react_loop", None)

        ctx = WorkflowContext(
            session_id=session_id or f"cron:{schedule.id}",
            schedule_id=schedule.id,
            llm_client=llm_client,
            chroma_store=chroma_store,
            report_dir="data/reports",
            current_time=started_at_dt,
            last_run_time=last_run_dt,
        )
        # summary / research 模板需要 session_logger / react_loop
        # 通过 setattr 注入（dataclass 不强约束这些字段）
        setattr(ctx, "session_logger", session_logger)
        setattr(ctx, "react_loop", react_loop)
        # Task 10.1：注入 run_id / skill_loader / tool_registry / orchestrator /
        # policy_engine / audit_logger / session_id，供 StepExecutor 透传
        setattr(ctx, "run_id", run_id)
        setattr(ctx, "skill_loader", getattr(orchestrator, "skill_loader", None))
        setattr(ctx, "tool_registry", getattr(orchestrator, "tool_registry", None))
        setattr(ctx, "orchestrator", orchestrator)
        setattr(ctx, "policy_engine", getattr(orchestrator, "policy_engine", None))
        setattr(ctx, "audit_logger", getattr(orchestrator, "audit_logger", None))
        if session_id:
            setattr(ctx, "session_id", session_id)
        return ctx

    def _parse_last_run_time(
        self, schedule: Schedule
    ) -> Optional[datetime]:
        """从 schedule.last_run 解析上次执行时间。

        优先从 ``runs.jsonl`` 最近一条记录读取（更准确，含本次 run_id）；
        失败时回退到 ``schedule.last_run`` 字段。

        参数:
            schedule: 调度项。

        返回:
            上次执行时间的 ``datetime`` 实例，首次执行时返回 ``None``。
        """
        if self.runs_store is not None:
            try:
                last_summary = self.runs_store.read_last(schedule.id)
                if last_summary is not None and last_summary.finished_at:
                    return datetime.fromisoformat(last_summary.finished_at)
            except Exception:
                pass

        if schedule.last_run:
            try:
                return datetime.fromisoformat(schedule.last_run)
            except (ValueError, TypeError):
                return None
        return None

    def _append_run_summary(
        self,
        schedule: Schedule,
        started_at: str,
        finished_at: str,
        duration_seconds: float,
        success: bool,
        user_input: str,
        assistant_response: str,
        tool_calls: List[Dict[str, Any]],
        outputs: List[Dict[str, Any]],
        errors: List[str],
        step_traces: Optional[List[Dict[str, Any]]] = None,
        workflow_name: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        """生成 RunSummary 并 append 到 runs.jsonl（SubTask 2.9）。

        默认 ``llm_summary`` 由 :func:`build_default_llm_summary` 从
        ``assistant_response`` + ``tool_calls`` 提取，无额外 LLM 成本。
        ``schedule.generate_llm_summary=true`` 时调轻模型生成精炼摘要
        （可选，本方法不实际调用 LLM，仅留接口供 Task 4/5 扩展）。

        Task 10.4：新增 ``step_traces`` / ``workflow_name`` / ``run_id`` 参数，
        从 :class:`WorkflowResult` 透传到 :class:`RunSummary`，确保
        ``runs.jsonl`` 每条 run 含完整 step 执行轨迹。

        参数:
            schedule: 调度项。
            started_at: 开始时间 ISO 字符串。
            finished_at: 结束时间 ISO 字符串。
            duration_seconds: 耗时秒。
            success: 是否成功。
            user_input: 触发时的任务文本（已渲染时间变量）。
            assistant_response: LLM 回复文本（截断前）。
            tool_calls: 工具调用列表。
            outputs: 文件输出列表。
            errors: 错误信息列表。
            step_traces: step 执行轨迹列表（每项为 StepTrace.to_dict()），
                旧路径无 trace 时为空列表（Task 10.4）。
            workflow_name: workflow 名称（Task 10.4）。``None`` 时回退到
                ``schedule.name``。
            run_id: 本次执行批次 ID（Task 10）。``None`` 时由 RunSummary
                默认工厂自动生成（保持向后兼容）。
        """
        if RunSummary is None or self.runs_store is None:
            return

        # 构造 RunSummary，run_id 显式传入时覆盖默认工厂（避免双重生成）
        summary_kwargs: Dict[str, Any] = dict(
            schedule_id=schedule.id,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=round(duration_seconds, 3),
            success=success,
            user_input=user_input,
            assistant_response=assistant_response,
            tool_calls=tool_calls,
            outputs=outputs,
            errors=errors,
            step_traces=list(step_traces or []),
            workflow_name=workflow_name or schedule.name,
        )
        if run_id:
            summary_kwargs["run_id"] = run_id
        summary = RunSummary(**summary_kwargs)

        # 默认 llm_summary（无额外 LLM 成本）
        if build_default_llm_summary is not None:
            try:
                summary.llm_summary = build_default_llm_summary(summary)
            except Exception:
                summary.llm_summary = summary.assistant_response[:500]
        else:
            summary.llm_summary = summary.assistant_response[:500]

        # generate_llm_summary=true 时调轻模型生成精炼摘要
        # （本阶段不实际调用，留接口供 Task 4/5 扩展；
        #  当前默认摘要已足够注入 messages[0]）
        if schedule.generate_llm_summary:
            # TODO (Task 4/5): 调用 llm_client.chat_consolidation 生成精炼摘要
            # 当前保持默认摘要，避免引入额外 LLM 依赖
            pass

        try:
            self.runs_store.append(schedule.id, summary)
        except Exception:
            logger.warning(
                "append RunSummary 到 runs.jsonl 失败", exc_info=True
            )

    async def trigger_now(
        self, orchestrator: Any, schedule_id: str
    ) -> bool:
        """手动触发指定调度项（无视 cron 与 enabled）。

        供 server.py 端点 ``await`` 调用。不更新去重表，因此不会影响
        ``run_loop`` 在同一分钟的正常触发判断。

        参数:
            orchestrator: 编排器实例。
            schedule_id: 待触发的调度项 id。

        返回:
            找到并触发返回 ``True``，调度项不存在返回 ``False``。
        """
        schedule = self._find_schedule(schedule_id)
        if schedule is None:
            return False
        await self._trigger(orchestrator, schedule)
        return True

    def stop(self) -> None:
        """请求停止调度循环。

        设置 ``_stop_event``，使 ``run_loop`` 中正在等待的
        ``wait_for`` 立即返回从而退出循环。
        """
        self._stop_event.set()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def add_schedule(self, sched_dict: dict) -> str:
        """新增调度项。

        从 dict 提取 ``name`` / ``cron`` / ``task`` / ``enabled``，生成
        id（缺省 ``uuid4().hex[:8]``），构造 ``CronExpr`` 与 ``Schedule``。
        非法 cron 抛 ``ValueError``，由调用方决定如何处理（如 API 返回
        400）。新增后持久化。

        Phase 8 Task 1.6: 支持新字段 ``cron_id`` / ``granted_tools`` /
        ``active_tools_snapshot`` / ``workflow`` / ``generate_llm_summary``，
        缺失字段使用默认值（向后兼容）。

        参数:
            sched_dict: 调度项字段 dict，可含 ``id`` / ``name`` / ``cron``
                / ``task`` / ``enabled`` / ``cron_id`` / ``granted_tools``
                / ``active_tools_snapshot`` / ``workflow`` /
                ``generate_llm_summary``。

        返回:
            新调度项的 id。

        Raises:
            ValueError: cron 表达式非法时抛出。
        """
        schedule_id = sched_dict.get("id") or uuid.uuid4().hex[:8]
        name = sched_dict.get("name", "")
        cron = sched_dict.get("cron", "")
        task = sched_dict.get("task", "")
        enabled = sched_dict.get("enabled", True)

        # 非法 cron 抛 ValueError，让调用方处理
        cron_expr = CronExpr(cron)
        self._cron_exprs[schedule_id] = cron_expr

        # Phase 8 Task 4.1: granted_tools 结构校验（strict 模式，非法抛 ValueError）
        granted_tools = validate_granted_tools(
            sched_dict.get("granted_tools"), strict=True
        )

        schedule = Schedule(
            id=schedule_id,
            name=name,
            cron=cron,
            task=task,
            enabled=enabled,
            # Phase 8 Task 1.6: 新字段（缺失使用默认值，向后兼容）
            cron_id=sched_dict.get("cron_id"),
            # Task 4.1: 校验后的 granted_tools（None/空列表统一为 None 表示无预授权）
            granted_tools=granted_tools if granted_tools else None,
            active_tools_snapshot=sched_dict.get("active_tools_snapshot"),
            workflow=sched_dict.get("workflow"),
            generate_llm_summary=bool(sched_dict.get("generate_llm_summary", False)),
        )
        self._schedules.append(schedule)
        self._persist()
        return schedule_id

    def update_schedule(self, schedule_id: str, fields: dict) -> bool:
        """更新调度项字段。

        ``name`` / ``task`` / ``enabled`` 可直接更新；``cron`` 更新时会
        重新构造 ``CronExpr``（非法抛 ``ValueError``）并刷新缓存。更新后
        持久化。

        Phase 8 Task 1.6: 支持更新新字段 ``cron_id`` / ``granted_tools``
        / ``active_tools_snapshot`` / ``workflow`` / ``generate_llm_summary``。
        其中 ``granted_tools`` 变更时应同步重新锁定
        ``active_tools_snapshot``（Task 4/5 实装，此处仅支持字段写入）。

        参数:
            schedule_id: 待更新的调度项 id。
            fields: 待更新字段 dict，支持 ``name`` / ``cron`` / ``task``
                / ``enabled`` / ``cron_id`` / ``granted_tools``
                / ``active_tools_snapshot`` / ``workflow`` /
                ``generate_llm_summary``。

        返回:
            更新成功返回 ``True``，调度项不存在返回 ``False``。

        Raises:
            ValueError: ``fields`` 含 ``cron`` 且表达式非法时抛出。
        """
        schedule = self._find_schedule(schedule_id)
        if schedule is None:
            return False

        if "name" in fields:
            schedule.name = fields["name"]
        if "task" in fields:
            schedule.task = fields["task"]
        if "enabled" in fields:
            schedule.enabled = fields["enabled"]
        if "cron" in fields:
            # 重新构造 CronExpr，非法抛 ValueError
            new_cron = fields["cron"]
            cron_expr = CronExpr(new_cron)
            schedule.cron = new_cron
            self._cron_exprs[schedule_id] = cron_expr
        # Phase 8 Task 1.6: 新字段更新
        if "cron_id" in fields:
            schedule.cron_id = fields["cron_id"]
        if "granted_tools" in fields:
            # Phase 8 Task 4.1: granted_tools 结构校验（strict 模式）
            validated = validate_granted_tools(fields["granted_tools"], strict=True)
            schedule.granted_tools = validated if validated else None
        if "active_tools_snapshot" in fields:
            schedule.active_tools_snapshot = fields["active_tools_snapshot"]
        if "workflow" in fields:
            schedule.workflow = fields["workflow"]
        if "generate_llm_summary" in fields:
            schedule.generate_llm_summary = bool(fields["generate_llm_summary"])

        self._persist()
        return True

    def delete_schedule(self, schedule_id: str) -> bool:
        """删除调度项。

        从 ``_schedules`` 与 ``_cron_exprs`` 缓存中移除，并持久化。

        参数:
            schedule_id: 待删除的调度项 id。

        返回:
            删除成功返回 ``True``，调度项不存在返回 ``False``。
        """
        for idx, schedule in enumerate(self._schedules):
            if schedule.id == schedule_id:
                del self._schedules[idx]
                self._cron_exprs.pop(schedule_id, None)
                self._persist()
                return True
        return False

    def list_schedules(self) -> List[dict]:
        """返回所有调度项的 dict 列表。

        返回:
            所有调度项的 dict 列表，每个 dict 含 ``id`` / ``name`` /
            ``cron`` / ``task`` / ``enabled`` / ``last_run`` / ``next_run``。
        """
        return [asdict(s) for s in self._schedules]

    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        """返回指定调度项的 dict。

        参数:
            schedule_id: 调度项 id。

        返回:
            调度项 dict，不存在返回 ``None``。
        """
        schedule = self._find_schedule(schedule_id)
        if schedule is None:
            return None
        return asdict(schedule)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _find_schedule(self, schedule_id: str) -> Optional[Schedule]:
        """按 id 查找调度项。

        参数:
            schedule_id: 调度项 id。

        返回:
            命中的 ``Schedule``，未找到返回 ``None``。
        """
        for schedule in self._schedules:
            if schedule.id == schedule_id:
                return schedule
        return None

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _persist(self) -> None:
        """将调度项列表持久化到 YAML 文件（原子写入）。

        序列化为 list of dict（含 ``id`` / ``name`` / ``cron`` / ``task``
        / ``enabled`` / ``last_run`` / ``next_run``）。采用原子写入：
        先写 tmp 文件并 ``flush + os.fsync``，再 ``os.replace`` 替换原
        文件。异常时清理 tmp 并重新抛出。
        """
        data = [asdict(s) for s in self._schedules]
        tmp_path = str(self.schedules_file) + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    data,
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.schedules_file)
        except Exception:
            # 清理 tmp 文件后重新抛出
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _load_persisted(self) -> None:
        """从 YAML 文件加载已持久化的调度项。

        文件不存在时返回空列表。读取 YAML 后逐项构造 ``Schedule`` 与
        ``CronExpr``：非法 cron 的项标记 ``enabled=False`` 并记录 error，
        不抛异常；加载失败的项跳过。

        Phase 8 Task 1.6: 支持加载新字段 ``cron_id`` / ``granted_tools``
        / ``active_tools_snapshot`` / ``workflow`` / ``generate_llm_summary``，
        缺失字段使用默认值（向后兼容旧 YAML）。
        """
        if not self.schedules_file.exists():
            return

        try:
            with open(self.schedules_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            logger.exception("加载调度文件失败: %s", self.schedules_file)
            return

        if not data or not isinstance(data, list):
            return

        loaded: List[Schedule] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                schedule_id = item.get("id") or uuid.uuid4().hex[:8]
                cron = item.get("cron", "")
                name = item.get("name", "")
                task = item.get("task", "")
                enabled = item.get("enabled", True)
                last_run = item.get("last_run")
                next_run = item.get("next_run")

                # 构造 CronExpr，非法则标记禁用并记录
                try:
                    cron_expr = CronExpr(cron)
                    self._cron_exprs[schedule_id] = cron_expr
                except ValueError:
                    logger.error(
                        "调度项 %s 的 cron 表达式非法: %s",
                        schedule_id,
                        cron,
                    )
                    enabled = False

                loaded.append(
                    Schedule(
                        id=schedule_id,
                        name=name,
                        cron=cron,
                        task=task,
                        enabled=enabled,
                        last_run=last_run,
                        next_run=next_run,
                        # Phase 8 Task 1.6: 新字段（缺失使用默认值，向后兼容旧 YAML）
                        cron_id=item.get("cron_id"),
                        # Phase 8 Task 4.1: granted_tools 结构校验（非 strict，
                        # 非法项记录 warning 并跳过，向后兼容旧 YAML）
                        granted_tools=(
                            validate_granted_tools(
                                item.get("granted_tools"), strict=False
                            )
                            or None
                        ),
                        active_tools_snapshot=item.get("active_tools_snapshot"),
                        workflow=item.get("workflow"),
                        generate_llm_summary=bool(
                            item.get("generate_llm_summary", False)
                        ),
                    )
                )
            except Exception:
                logger.exception("加载调度项失败，已跳过: %s", item)
                continue

        self._schedules = loaded
