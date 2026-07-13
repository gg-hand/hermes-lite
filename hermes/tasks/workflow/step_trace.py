"""StepTrace 全链路追溯数据结构（Task 3.2）。

每个 step 执行产生一条 StepTrace，记录执行起止时间、状态、错误分类、
输出、工具调用与文件产出，用于：

- RunSummary.step_traces 持久化到 ``runs.jsonl``
- 报告末尾「执行轨迹」表格渲染
- ``GET /schedules/{id}/runs/{run_id}/steps`` 端点查询

向后兼容：``from_dict`` 对所有字段 ``.get()`` 容错，旧记录缺失字段时
返回默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


#: StepTrace.status 允许值
ALLOWED_STEP_STATUSES = frozenset(
    {"pending", "running", "success", "failed", "skipped", "fallback", "timeout"}
)


def _parse_iso_datetime(value: Any) -> Optional[str]:
    """将 datetime / 字符串原样保留为 ISO 字符串。

    StepTrace 持久化时 ``started_at`` / ``finished_at`` 统一存 ISO 字符串，
    避免 datetime 不可序列化的问题。本方法接受 datetime / 字符串 / None。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@dataclass
class StepTrace:
    """单个 step 执行的轨迹记录。

    属性:
        step_id: 对应 StepSpec.id。
        step_name: 对应 StepSpec.name（用于报告展示）。
        step_type: 对应 StepSpec.type。
        started_at: 开始时间（ISO 字符串）。
        finished_at: 结束时间（ISO 字符串）。
        duration_ms: 耗时（毫秒）。
        attempts: 实际尝试次数（含首次，1 表示首次即成功）。
        status: 终态状态，``success`` / ``failed`` / ``skipped`` /
            ``fallback`` / ``timeout`` / ``running`` / ``pending``。
        error_class: 错误分类（对应 ErrorClass.value），仅失败时填充。
        error_message: 错误信息（一行精炼描述），仅失败时填充。
        outputs: step 产出变量字典，供后续 step 通过 condition 引用。
        tool_calls: 工具调用列表（同 WorkflowResult.tool_calls 结构）。
        files: 文件产出列表（同 WorkflowResult.outputs 结构）。
    """

    step_id: str = ""
    step_name: str = ""
    step_type: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: int = 0
    attempts: int = 1
    status: str = "pending"
    error_class: str = ""
    error_message: str = ""
    outputs: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "StepTrace":
        """从 dict 解析 StepTrace。

        所有字段 ``.get()`` 容错，旧记录缺失字段时返回默认值。
        """
        if not data:
            return cls()
        return cls(
            step_id=str(data.get("step_id", "")),
            step_name=str(data.get("step_name", "")),
            step_type=str(data.get("step_type", "")),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            duration_ms=int(data.get("duration_ms", 0)),
            attempts=int(data.get("attempts", 1)),
            status=str(data.get("status", "pending")),
            error_class=str(data.get("error_class", "")),
            error_message=str(data.get("error_message", "")),
            outputs=dict(data.get("outputs") or {}),
            tool_calls=list(data.get("tool_calls") or []),
            files=list(data.get("files") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict（用于 JSON 持久化）。"""
        return {
            "step_id": self.step_id,
            "step_name": self.step_name,
            "step_type": self.step_type,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "status": self.status,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "outputs": dict(self.outputs),
            "tool_calls": list(self.tool_calls),
            "files": list(self.files),
        }

    def mark_started(self, started_at: Optional[datetime] = None) -> None:
        """标记 step 开始执行。"""
        self.status = "running"
        self.started_at = _parse_iso_datetime(started_at or datetime.now())

    def mark_finished(
        self,
        status: str,
        finished_at: Optional[datetime] = None,
        error_class: str = "",
        error_message: str = "",
    ) -> None:
        """标记 step 执行结束。

        自动计算 ``duration_ms``（若 ``started_at`` 已设置）。
        """
        self.status = status
        end = finished_at or datetime.now()
        self.finished_at = _parse_iso_datetime(end)
        if self.started_at:
            try:
                # 兼容旧记录的 ISO 字符串解析
                start_dt = datetime.fromisoformat(self.started_at)
                self.duration_ms = int((end - start_dt).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass
        if error_class:
            self.error_class = error_class
        if error_message:
            self.error_message = error_message

    def add_tool_call(self, tool_call: Dict[str, Any]) -> None:
        """追加一条工具调用记录。"""
        self.tool_calls.append(tool_call)

    def add_file(self, file_info: Dict[str, Any]) -> None:
        """追加一条文件产出记录。"""
        self.files.append(file_info)

    def is_terminal(self) -> bool:
        """是否为终态（不再变化）。"""
        return self.status in {
            "success",
            "failed",
            "skipped",
            "fallback",
            "timeout",
        }


__all__ = ["StepTrace", "ALLOWED_STEP_STATUSES"]
