"""approvals 路由：审批决定提交、pending 列表查询。

Task: 从 server.py 迁移 2 个端点。
- POST /approvals/{approval_id}/resolve
- GET  /approvals
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from teage_liu.app import get_approval_manager, get_metrics_collector
from teage_liu.schemas.approvals import (
    ApprovalListItem,
    ApprovalListResponse,
    ApprovalResolveRequest,
    ApprovalResolveResponse,
)

logger = logging.getLogger("teage_liu.server")

router = APIRouter()


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalResolveResponse)
def resolve_approval(
    approval_id: str,
    req: ApprovalResolveRequest,
    approval_manager=Depends(get_approval_manager),
    metrics_collector=Depends(get_metrics_collector),
):
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


@router.get("/approvals", response_model=ApprovalListResponse)
def list_approvals(approval_manager=Depends(get_approval_manager)):
    """列出所有 pending 状态的审批请求。

    approval_manager 未初始化时返回空列表（不报错）。
    """
    if approval_manager is None:
        return ApprovalListResponse(pending=[])
    items = approval_manager.list_pending()
    return ApprovalListResponse(
        pending=[ApprovalListItem(**item) for item in items]
    )
