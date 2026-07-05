"""内存审批队列，用于 Human-in-the-Loop (HIL) 审批流程。

存储策略：
- 仅保存于内存中，进程重启即丢失（个人 Agent 场景下可接受）。
- 使用 ``asyncio.Event`` 跨协程信令：审批请求创建时生成 Event，等待方
  ``await event.wait()`` 阻塞，``resolve`` 时调用 ``event.set()`` 唤醒。

超时策略：
- 默认超时 ``timeout`` 秒后自动拒绝（deny），避免请求无限期挂起。
- 超时在 ``wait_for_decision`` 中通过 ``asyncio.wait_for`` 实现，超时后
  调用 ``resolve`` 将状态置为 DENIED 并写入 decision_reason。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

# 审批状态常量
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"


@dataclass
class ApprovalRequest:
    """审批请求记录。

    Attributes:
        approval_id: 唯一标识（uuid4().hex）。
        tool_name: 触发审批的工具名。
        tool_input: 工具输入参数 dict。
        reason: 风险原因（展示给用户）。
        risk_level: 风险等级 low/medium/high。
        created_at: 创建时间 ISO 字符串。
        status: 当前状态（PENDING/APPROVED/DENIED/EXPIRED）。
        decision_reason: 决定原因（用户拒绝时的备注或超时提示），可为 None。
        tool_kind: 工具类别（generic/skill/mcp/file/shell/memory），
            用于前端弹窗差异化渲染。默认 "generic"。
    """

    approval_id: str
    tool_name: str
    tool_input: dict
    reason: str
    risk_level: str
    created_at: str
    status: str = PENDING
    decision_reason: Optional[str] = None
    tool_kind: str = "generic"


class ApprovalManager:
    def __init__(
        self,
        timeout: float = 300.0,
        metrics: Optional["object"] = None,
    ) -> None:
        """初始化审批管理器。

        参数:
            timeout: 默认审批超时秒数，超时自动拒绝。默认 300 秒。
            metrics: 可选 MetricsCollector 实例，用于上报 approve/deny/timeout
                决策计数。为 None 时不上报（向后兼容）。
        """
        self.timeout = timeout
        self._requests: Dict[str, ApprovalRequest] = {}
        self._events: Dict[str, asyncio.Event] = {}
        # Phase 2 反馈监控：注入 metrics 用于决策计数
        self.metrics = metrics

    def create_request(
        self,
        tool_name: str,
        tool_input: dict,
        reason: str,
        risk_level: str,
        tool_kind: str = "generic",
    ) -> str:
        """创建审批请求，返回 approval_id。

        用 uuid4().hex 生成 approval_id，创建 ApprovalRequest（status=PENDING，
        created_at=当前 ISO 时间），存入 _requests 与 _events（asyncio.Event()），
        返回 approval_id。
        """
        approval_id = uuid4().hex
        request = ApprovalRequest(
            approval_id=approval_id,
            tool_name=tool_name,
            tool_input=tool_input,
            reason=reason,
            risk_level=risk_level,
            created_at=datetime.now().isoformat(),
            status=PENDING,
            decision_reason=None,
            tool_kind=tool_kind,
        )
        self._requests[approval_id] = request
        self._events[approval_id] = asyncio.Event()
        logger.info(
            "审批请求已创建 approval_id=%s tool=%s risk=%s",
            approval_id,
            tool_name,
            risk_level,
        )
        return approval_id

    async def wait_for_decision(
        self, approval_id: str, timeout: Optional[float] = None
    ) -> tuple:
        """等待审批决定。

        参数:
            approval_id: 审批 ID。
            timeout: 超时秒数，None 时用 self.timeout。

        返回:
            (decision_str, reason) 元组：
            - decision_str: "approve" 或 "deny"
            - reason: 决定原因，可为 None

        异常处理:
            - approval_id 不存在 → 返回 ("deny", "审批请求不存在")
            - asyncio.TimeoutError（wait_for 超时）→ 调用
              self.resolve(approval_id, "deny", "审批超时自动拒绝")，
              返回 ("deny", "审批超时自动拒绝")
            - asyncio.CancelledError → 重新抛出（不吞掉）
        """
        if approval_id not in self._requests:
            return ("deny", "审批请求不存在")
        event = self._events[approval_id]
        actual_timeout = timeout if timeout is not None else self.timeout
        try:
            await asyncio.wait_for(event.wait(), timeout=actual_timeout)
        except asyncio.TimeoutError:
            self.resolve(approval_id, "deny", "审批超时自动拒绝")
            # Phase 2 反馈监控：超时独立上报（resolve 内部不重复上报）
            if self.metrics is not None:
                try:
                    self.metrics.observe_approval_decision("timeout")
                except Exception:
                    pass
            return ("deny", "审批超时自动拒绝")
        except asyncio.CancelledError:
            raise
        req = self._requests[approval_id]
        # 根据 status 映射返回 decision_str
        if req.status == APPROVED:
            return ("approve", req.decision_reason)
        else:
            return ("deny", req.decision_reason)

    def resolve(
        self, approval_id: str, decision: str, reason: Optional[str] = None
    ) -> bool:
        """提交审批决定。

        参数:
            approval_id: 审批 ID。
            decision: "approve" 或 "deny"（其他值返回 False）。
            reason: 决定原因，可为 None。

        返回:
            bool: True 表示成功处理；False 表示 approval_id 不存在、状态非
            PENDING、或 decision 非法。

        逻辑:
            - approval_id 不在 _requests 中 → False
            - 当前 status != PENDING → False（幂等，重复 resolve 被忽略）
            - decision 不在 ("approve", "deny") → False
            - 否则更新 status（approve→APPROVED / deny→DENIED）与
              decision_reason，调用 _events[approval_id].set() 唤醒等待方，
              返回 True
        """
        if approval_id not in self._requests:
            return False
        req = self._requests[approval_id]
        if req.status != PENDING:
            # 幂等：重复 resolve 被忽略
            return False
        if decision not in ("approve", "deny"):
            return False
        req.status = APPROVED if decision == "approve" else DENIED
        req.decision_reason = reason
        self._events[approval_id].set()
        logger.info(
            "审批决定已提交 approval_id=%s decision=%s reason=%s",
            approval_id,
            decision,
            reason,
        )
        return True

    def resolve_all(self, decision: str, reason: Optional[str] = None) -> int:
        """批量 resolve 所有 PENDING 状态的审批。

        用于 HIL 开关关闭场景：``_apply_runtime_config`` 检测到
        ``security.enabled`` 翻转为 False 时调用此方法，唤醒所有
        ``wait_for_decision`` 协程，避免它们傻等 ``timeout`` 秒超时。

        参数:
            decision: "approve" 或 "deny"（其他值跳过所有项，返回 0）。
            reason: 决定原因，可为 None。

        返回:
            int: 成功处理的 PENDING 审批数量。

        逻辑:
            - 遍历 ``_requests`` 的快照（``list(self._requests.keys())``），
              避免迭代过程中字典变更。
            - 对每条 PENDING 审批调 ``resolve``，复用其幂等性与 event.set()。
            - 非 PENDING 审批与非法 decision 自动跳过。
        """
        if decision not in ("approve", "deny"):
            return 0
        count = 0
        for approval_id in list(self._requests.keys()):
            if self.resolve(approval_id, decision, reason):
                count += 1
        if count > 0:
            logger.info(
                "批量 resolve %d 条审批 decision=%s reason=%s",
                count,
                decision,
                reason,
            )
        return count

    def list_pending(self) -> list:
        """返回所有 PENDING 状态的审批摘要列表。

        每条摘要为 dict：{approval_id, tool_name, tool_input, reason,
        created_at, tool_kind}（不含 status 与 decision_reason）。
        """
        return [
            {
                "approval_id": req.approval_id,
                "tool_name": req.tool_name,
                "tool_input": req.tool_input,
                "reason": req.reason,
                "created_at": req.created_at,
                "tool_kind": req.tool_kind,
            }
            for req in self._requests.values()
            if req.status == PENDING
        ]

    def get_status(self, approval_id: str) -> Optional[dict]:
        """返回单条审批的状态摘要。

        摘要 dict：{approval_id, tool_name, tool_input, reason, risk_level,
        created_at, status, decision_reason, tool_kind}。
        approval_id 不存在返回 None。
        """
        req = self._requests.get(approval_id)
        if req is None:
            return None
        return {
            "approval_id": req.approval_id,
            "tool_name": req.tool_name,
            "tool_input": req.tool_input,
            "reason": req.reason,
            "risk_level": req.risk_level,
            "created_at": req.created_at,
            "status": req.status,
            "decision_reason": req.decision_reason,
            "tool_kind": req.tool_kind,
        }
