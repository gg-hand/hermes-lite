"""对话相关模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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
