"""Phase 8 Task 3.1: cron 调度提议-确认协议的内存存储 + 状态机。

本模块提供 ``Proposal`` dataclass 与 ``ProposalStore`` 内存存储，实现
提议-确认状态机：

    proposal_created → pending_confirm → confirmed/modified/rejected → schedule_active

设计要点：
- **纯内存实现**：不持久化，服务重启后所有 proposals 清空（spec 明确要求
  "无持久化需求"，用户需重新让 LLM 提议）。
- **状态机约束**：每次状态流转都校验当前状态是否允许该转换，非法转换返回
  ``False``，保证协议一致性。
- **线程安全**：``ProposalStore`` 内部用 ``threading.Lock`` 保护所有读写
  操作，避免并发请求导致状态错乱。
- **proposal_id 唯一性**：使用 ``uuid4().hex[:12]`` 生成，足够区分用户会话
  中并发的少量 proposals。

状态语义：
- ``proposal_created``：瞬时初始状态（``create`` 调用后立即转为
  ``pending_confirm``，外部通常观察不到此状态）。
- ``pending_confirm``：等待用户在 UI 确认卡片上做出决定。
- ``confirmed``：用户点击「确认」，可调 ``create_schedule`` 创建调度项。
- ``modified``：用户点击「修改并确认」，``schedule_config`` /
  ``requested_tools`` 已被修改，可调 ``create_schedule`` 创建调度项。
- ``rejected``：用户点击「拒绝」，不可再创建调度项。
- ``schedule_active``：调度项已创建（``create_schedule`` 成功后标记），
  proposal 终态。

模块依赖：无外部依赖（仅标准库），不导入 scheduler / policy / tool_registry，
保持与 Task 2/4 并行开发的隔离性。
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# 合法的 proposal 状态值
VALID_STATUSES = (
    "proposal_created",
    "pending_confirm",
    "confirmed",
    "modified",
    "rejected",
    "schedule_active",
)

# 允许的状态转换映射：当前状态 → 允许转入的下一状态集合
# - proposal_created → pending_confirm（create 时立即转换）
# - pending_confirm → confirmed / modified / rejected（用户决定）
# - confirmed / modified → schedule_active（create_schedule 成功）
# - rejected / schedule_active 为终态，不再转换
_ALLOWED_TRANSITIONS: Dict[str, set] = {
    "proposal_created": {"pending_confirm"},
    "pending_confirm": {"confirmed", "modified", "rejected"},
    "confirmed": {"schedule_active"},
    "modified": {"schedule_active"},
    "rejected": set(),
    "schedule_active": set(),
}


@dataclass
class Proposal:
    """调度提议数据模型。

    Attributes:
        proposal_id: 提议唯一标识（``uuid4().hex[:12]``）。
        schedule_config: 调度配置 dict，含 ``name`` / ``cron`` / ``task`` /
            ``enabled`` / 可选 ``workflow`` / ``generate_llm_summary`` 等
            字段。用户「修改并确认」时会更新此字段。
        requested_tools: 请求预授权的工具列表，每项形如
            ``{"tool": str, "scope": "all"|"path_prefix", "allowed_paths": List[str]}``。
            用户「修改并确认」时可增删工具项。``propose_schedule`` 入口
            会检测硬禁止项（``delete_memory`` / ``execute_command`` /
            ``call_tool``），含硬禁止项的提议直接拒绝。
        llm_explanation: LLM 生成的提议说明（人类可读），展示在确认卡片
            上帮助用户决策。
        status: 提议当前状态，见 :data:`VALID_STATUSES`。
        created_at: 创建时间（ISO 格式字符串）。
        schedule_id: 提议对应的调度项 ID。仅在 ``schedule_active`` 状态下
            有值，``create_schedule`` 成功后由 ``mark_schedule_active`` 设置。
    """

    proposal_id: str
    schedule_config: Dict[str, Any]
    requested_tools: List[Dict[str, Any]]
    llm_explanation: str
    status: str = "pending_confirm"
    created_at: str = ""
    schedule_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """返回 proposal 的 dict 表示（用于 API 响应 / JSON 序列化）。"""
        return asdict(self)


class ProposalStore:
    """提议内存存储，管理 ``Proposal`` 生命周期与状态机。

    纯内存实现，不持久化。服务重启后所有 proposals 清空（spec 要求）。
    所有方法线程安全（内部 ``threading.Lock`` 保护）。

    状态机流转：
        create()           → status = "pending_confirm"
        confirm(id)        → pending_confirm → confirmed
        modify(id, mods)   → pending_confirm → modified（并应用修改）
        reject(id)         → pending_confirm → rejected
        mark_schedule_active(id, sched_id)
                           → confirmed/modified → schedule_active
    """

    def __init__(self) -> None:
        """初始化空存储。"""
        self._proposals: Dict[str, Proposal] = {}
        self._lock = threading.Lock()

    def create(
        self,
        schedule_config: Dict[str, Any],
        requested_tools: List[Dict[str, Any]],
        llm_explanation: str,
    ) -> str:
        """创建新提议，返回 ``proposal_id``。

        新提议初始状态为 ``pending_confirm``（``proposal_created`` 为瞬时
        状态，``create`` 内部即完成 ``proposal_created → pending_confirm``
        转换）。

        参数:
            schedule_config: 调度配置 dict。
            requested_tools: 请求预授权的工具列表。
            llm_explanation: LLM 提议说明。

        返回:
            新提议的 ``proposal_id``。
        """
        proposal_id = uuid.uuid4().hex[:12]
        proposal = Proposal(
            proposal_id=proposal_id,
            schedule_config=dict(schedule_config) if schedule_config else {},
            requested_tools=list(requested_tools) if requested_tools else [],
            llm_explanation=llm_explanation or "",
            status="pending_confirm",
            created_at=datetime.now().isoformat(),
        )
        with self._lock:
            self._proposals[proposal_id] = proposal
        logger.info(
            "已创建提议 %s（cron=%s, tools=%d）",
            proposal_id,
            schedule_config.get("cron", "?") if schedule_config else "?",
            len(requested_tools) if requested_tools else 0,
        )
        return proposal_id

    def get(self, proposal_id: str) -> Optional[Proposal]:
        """按 id 获取提议。不存在返回 ``None``。"""
        with self._lock:
            return self._proposals.get(proposal_id)

    def list(self) -> List[Proposal]:
        """返回所有提议列表（按创建顺序）。"""
        with self._lock:
            return list(self._proposals.values())

    def confirm(self, proposal_id: str) -> bool:
        """确认提议：``pending_confirm → confirmed``。

        参数:
            proposal_id: 提议 ID。

        返回:
            转换成功返回 ``True``；提议不存在或当前状态不允许转换返回
            ``False``。
        """
        return self._transition(proposal_id, "confirmed")

    def modify(
        self,
        proposal_id: str,
        schedule_config_updates: Optional[Dict[str, Any]] = None,
        requested_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """修改并确认提议：``pending_confirm → modified``。

        将 ``schedule_config_updates`` 合并到 proposal 的 ``schedule_config``
        （浅合并），若提供 ``requested_tools`` 则替换工具列表。状态转为
        ``modified``（语义上等同 confirmed，可继续创建调度项）。

        参数:
            proposal_id: 提议 ID。
            schedule_config_updates: 调度配置更新字段（浅合并到原配置）。
                为 ``None`` 时不更新配置。
            requested_tools: 新的请求工具列表（整体替换）。为 ``None`` 时不
                更新工具列表。

        返回:
            修改成功返回 ``True``；提议不存在或当前状态不允许转换返回
            ``False``。
        """
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                return False
            if "modified" not in _ALLOWED_TRANSITIONS.get(proposal.status, set()):
                logger.warning(
                    "提议 %s 当前状态 %s 不允许 modify", proposal_id, proposal.status
                )
                return False
            # 应用修改
            if schedule_config_updates:
                merged = dict(proposal.schedule_config)
                merged.update(schedule_config_updates)
                proposal.schedule_config = merged
            if requested_tools is not None:
                proposal.requested_tools = list(requested_tools)
            proposal.status = "modified"
            logger.info("提议 %s 已修改并确认", proposal_id)
            return True

    def reject(self, proposal_id: str) -> bool:
        """拒绝提议：``pending_confirm → rejected``。

        参数:
            proposal_id: 提议 ID。

        返回:
            拒绝成功返回 ``True``；提议不存在或当前状态不允许转换返回
            ``False``。
        """
        return self._transition(proposal_id, "rejected")

    def mark_schedule_active(
        self, proposal_id: str, schedule_id: str
    ) -> bool:
        """标记提议已创建调度项：``confirmed/modified → schedule_active``。

        由 ``create_schedule`` 工具在成功创建调度项后调用，记录 ``schedule_id``
        并将提议标记为终态 ``schedule_active``。

        参数:
            proposal_id: 提议 ID。
            schedule_id: 已创建的调度项 ID。

        返回:
            标记成功返回 ``True``；提议不存在或当前状态不允许转换返回
            ``False``。
        """
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                return False
            if (
                "schedule_active"
                not in _ALLOWED_TRANSITIONS.get(proposal.status, set())
            ):
                logger.warning(
                    "提议 %s 当前状态 %s 不允许 mark_schedule_active",
                    proposal_id,
                    proposal.status,
                )
                return False
            proposal.status = "schedule_active"
            proposal.schedule_id = schedule_id
            logger.info(
                "提议 %s 已创建调度项 %s，标记为 schedule_active",
                proposal_id,
                schedule_id,
            )
            return True

    def clear(self) -> None:
        """清空所有提议（主要用于测试）。"""
        with self._lock:
            self._proposals.clear()

    def _transition(self, proposal_id: str, new_status: str) -> bool:
        """通用状态转换（内部，调用方持锁或用此方法的封装）。

        参数:
            proposal_id: 提议 ID。
            new_status: 目标状态。

        返回:
            转换成功返回 ``True``，否则 ``False``。
        """
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                return False
            allowed = _ALLOWED_TRANSITIONS.get(proposal.status, set())
            if new_status not in allowed:
                logger.warning(
                    "提议 %s 当前状态 %s 不允许转换到 %s",
                    proposal_id,
                    proposal.status,
                    new_status,
                )
                return False
            proposal.status = new_status
            logger.info("提议 %s 状态转换: %s", proposal_id, new_status)
            return True
