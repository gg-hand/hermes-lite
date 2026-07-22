"""审批相关模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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
