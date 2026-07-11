"""通用模型：健康检查、会话、消息、清理。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class FlushResponse(BaseModel):
    """清理响应体。"""

    status: str
    message: str
    pending_count: int = 0
    timestamp: str
