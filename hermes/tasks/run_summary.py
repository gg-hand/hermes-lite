"""RunSummary 数据结构 + runs.jsonl 持久化（Phase 8 Task 2.8）。

记录 cron 调度项每次执行的完整摘要，用于：
- 注入到下一轮 cron 上下文 messages[0]（时间上下文 + 历史执行摘要，
  SubTask 2.10）
- 前端「执行历史」端点展示（SubTask 1.8）
- LLM 自我回顾与决策（基于历史 RunSummary 调整本次执行策略）

持久化策略：
- 每个调度项独立一份 JSONL 文件：``data/schedules/{id}/runs.jsonl``
- 每行一条 JSON 记录，append 模式写入（与 audit.jsonl 一致）
- ``read_recent`` 从文件末尾倒序读取最近 N 条（避免全量加载）

数据结构：
- :class:`RunSummary` dataclass：单次执行摘要
- :class:`RunsJsonlStore`：JSONL 文件读写器
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# assistant_response 字段截断阈值（避免单条 JSONL 记录过大）
_MAX_ASSISTANT_RESPONSE_LENGTH = 500


@dataclass
class RunSummary:
    """cron 调度项单次执行摘要。

    由 CronScheduler 在每次执行后构造（SubTask 2.9），通过
    :class:`RunsJsonlStore` 持久化到 ``data/schedules/{id}/runs.jsonl``。

    属性:
        schedule_id: 调度项 ID。
        run_id: 本次执行的唯一 ID（``uuid4().hex[:12]``）。
        started_at: 执行开始时间（ISO 格式字符串）。
        finished_at: 执行结束时间（ISO 格式字符串）。
        duration_seconds: 执行耗时（秒，保留 3 位小数）。
        success: 是否执行成功（无致命错误）。
        user_input: 触发时的任务文本（``schedule.task`` 渲染后）。
        assistant_response: LLM 回复文本（截断到 500 字）。
        tool_calls: 工具调用列表，每项 ``{"name": str, "input": dict,
            "result": str, "is_error": bool}``。
        outputs: 文件输出列表，每项 ``{"path": str, "type": str}``。
        errors: 错误信息列表。
        llm_summary: 执行摘要文本。默认 = ``assistant_response[:500]`` +
            工具调用摘要；``generate_llm_summary=true`` 时由 LLM 生成精炼摘要。
        step_traces: step 执行轨迹列表（Task 8.3）。每项为 step_trace.to_dict()
            序列化的 dict，由 CronScheduler._trigger 从
            WorkflowResult.step_traces 转换写入。旧路径无 step_traces 时为空列表。
            ``from_dict`` 对缺失字段 ``.get(default=[])`` 容错。
        workflow_name: workflow 名称（Task 8.3）。由 CronScheduler 从
            WorkflowResult.workflow_name 或 schedule.name 写入。
            为 ``None`` 时调用方可回退到 ``schedule.name``。
    """

    schedule_id: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    success: bool = True
    user_input: str = ""
    assistant_response: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    llm_summary: str = ""
    step_traces: List[Dict[str, Any]] = field(default_factory=list)
    workflow_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """转为 dict（用于 JSON 序列化）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunSummary":
        """从 dict 构造（用于 JSON 反序列化）。

        兼容缺失字段（向后兼容旧记录）：``step_traces`` 与
        ``workflow_name`` 缺失时分别使用空列表与 ``None``。
        """
        return cls(
            schedule_id=data.get("schedule_id", ""),
            run_id=data.get("run_id", ""),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            success=bool(data.get("success", True)),
            user_input=data.get("user_input", ""),
            assistant_response=data.get("assistant_response", ""),
            tool_calls=data.get("tool_calls", []) or [],
            outputs=data.get("outputs", []) or [],
            errors=data.get("errors", []) or [],
            llm_summary=data.get("llm_summary", ""),
            # Task 8.3: 新字段容错（向后兼容旧记录）
            step_traces=data.get("step_traces", []) or [],
            workflow_name=data.get("workflow_name", None),
        )

    def truncate_assistant_response(self) -> None:
        """截断 ``assistant_response`` 到 500 字（原地修改）。

        超过阈值时保留前 500 字并追加 ``...[truncated]`` 标记。
        """
        if len(self.assistant_response) > _MAX_ASSISTANT_RESPONSE_LENGTH:
            self.assistant_response = (
                self.assistant_response[:_MAX_ASSISTANT_RESPONSE_LENGTH]
                + "...[truncated]"
            )


def build_default_llm_summary(summary: RunSummary) -> str:
    """构造默认的 llm_summary（无额外 LLM 成本）。

    规则（SubTask 2.9 默认提取）：
    - ``assistant_response`` 截断到 500 字
    - 追加工具调用摘要：``工具调用: N 次（name1, name2, ...）``
    - 追加错误摘要（如有）：``错误: M 条``
    - 追加文件输出摘要（如有）：``输出: K 个文件``

    参数:
        summary: 待生成摘要的 RunSummary 实例。

    返回:
        默认摘要文本。
    """
    parts: List[str] = []
    # 截断 assistant_response
    resp = summary.assistant_response
    if len(resp) > _MAX_ASSISTANT_RESPONSE_LENGTH:
        resp = resp[:_MAX_ASSISTANT_RESPONSE_LENGTH] + "...[truncated]"
    parts.append(resp)

    # 工具调用摘要
    if summary.tool_calls:
        tool_names = [tc.get("name", "") for tc in summary.tool_calls]
        parts.append(f"工具调用: {len(summary.tool_calls)} 次（{', '.join(tool_names)}）")

    # 错误摘要
    if summary.errors:
        parts.append(f"错误: {len(summary.errors)} 条")

    # 文件输出摘要
    if summary.outputs:
        parts.append(f"输出: {len(summary.outputs)} 个文件")

    return "\n".join(parts)


class RunsJsonlStore:
    """runs.jsonl 文件读写器。

    每个调度项独立一份 JSONL 文件：``data/schedules/{schedule_id}/runs.jsonl``。
    采用 append 模式写入，``read_recent`` 从文件末尾倒序读取最近 N 条。

    线程安全：通过文件锁（``threading.Lock``）保护写入与读取。
    """

    def __init__(self, base_dir: str = "data/schedules") -> None:
        """初始化 JSONL 存储器。

        参数:
            base_dir: 调度项数据根目录，默认 ``data/schedules``。
                每个调度项的 runs.jsonl 存放在 ``{base_dir}/{schedule_id}/runs.jsonl``。
        """
        self.base_dir = base_dir
        import threading

        self._lock = threading.Lock()

    def _get_runs_path(self, schedule_id: str) -> str:
        """返回指定调度项的 runs.jsonl 路径。"""
        return os.path.join(self.base_dir, schedule_id, "runs.jsonl")

    def append(self, schedule_id: str, summary: RunSummary) -> None:
        """追加一条 RunSummary 到 ``runs.jsonl``。

        文件不存在时自动创建（含父目录）。采用 append 模式，每行一条 JSON。

        参数:
            schedule_id: 调度项 ID。
            summary: 待持久化的 RunSummary 实例。
        """
        # 截断 assistant_response（避免单条记录过大）
        summary.truncate_assistant_response()

        runs_path = self._get_runs_path(schedule_id)
        line = json.dumps(summary.to_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            os.makedirs(os.path.dirname(runs_path), exist_ok=True)
            try:
                with open(runs_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
            except OSError as e:
                logger.warning("写入 runs.jsonl 失败: %s", e)

    def read_recent(
        self, schedule_id: str, n: int = 3
    ) -> List[RunSummary]:
        """读取最近 N 条 RunSummary（按时间倒序，最新在前）。

        实现策略：从文件末尾倒序读取 N 行（避免全量加载大文件），
        每行 JSON 解析为 RunSummary。

        参数:
            schedule_id: 调度项 ID。
            n: 返回的最大条数，默认 3。

        返回:
            RunSummary 列表，最新在前。文件不存在或为空时返回空列表。
        """
        runs_path = self._get_runs_path(schedule_id)
        with self._lock:
            if not os.path.exists(runs_path):
                return []
            try:
                with open(runs_path, "r", encoding="utf-8") as f:
                    # 全量读取后取末尾 N 行（JSONL 文件通常不会太大，
                    # 单调度项一年约 365*24=8760 条记录，可接受）
                    lines = f.readlines()
            except OSError as e:
                logger.warning("读取 runs.jsonl 失败: %s", e)
                return []

        # 取末尾 N 行并倒序（最新在前）
        recent_lines = lines[-n:] if n > 0 else lines
        recent_lines = list(reversed(recent_lines))

        summaries: List[RunSummary] = []
        for line in recent_lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                summaries.append(RunSummary.from_dict(data))
            except json.JSONDecodeError:
                logger.warning("解析 runs.jsonl 行失败，跳过: %s", line[:100])
                continue
        return summaries

    def read_last(self, schedule_id: str) -> Optional[RunSummary]:
        """读取最近一条 RunSummary（若存在）。

        用于 ``last_run_time`` 与 ``days_since_last`` 计算（SubTask 2.10）。

        参数:
            schedule_id: 调度项 ID。

        返回:
            最近的 RunSummary 实例，无记录时返回 ``None``。
        """
        recent = self.read_recent(schedule_id, n=1)
        return recent[0] if recent else None

    def read_recent_all(
        self, schedules: List[Dict[str, Any]], n: int = 20
    ) -> List[Dict[str, Any]]:
        """跨调度项读取最近 N 条 RunSummary（合并按时间倒序）。

        遍历传入的调度项列表，对每个调度项复用 :meth:`read_recent` 读取最近
        N 条记录，合并后按 ``started_at`` 倒序排序，取前 ``n`` 条。返回的
        每个 dict 在 ``RunSummary.to_dict()`` 基础上 join ``schedule_name``
        字段，便于前端展示。

        参数:
            schedules: 调度项列表，每项含 ``id`` 与 ``name`` 字段。
                由调用方传入（通常来自 ``cron_scheduler.list_schedules()``），
                避免读取已删除调度项的孤儿目录。
            n: 返回的最大条数，默认 20。

        返回:
            dict 列表，最新在前。每个 dict 含 RunSummary 全部字段 +
            ``schedule_name``。无记录时返回空列表。
        """
        # 构建 id → name 映射
        id_to_name = {
            s.get("id", ""): s.get("name", s.get("id", ""))
            for s in schedules
        }
        all_dicts: List[Dict[str, Any]] = []
        for schedule_id, schedule_name in id_to_name.items():
            if not schedule_id:
                continue
            summaries = self.read_recent(schedule_id, n=n)
            for s in summaries:
                d = s.to_dict()
                d["schedule_name"] = schedule_name
                all_dicts.append(d)
        # 按 started_at 倒序（ISO 字符串字典序等于时间序）
        all_dicts.sort(key=lambda d: d.get("started_at", ""), reverse=True)
        return all_dicts[:n]
