"""sessions 路由：会话列表、消息历史、标题更新、会话删除。

Task 10: 从 server.py 迁移 4 个端点。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from app import (
    get_session_logger,
    get_cron_scheduler,
    get_orchestrator,
    get_upload_manager,
)
from schemas.common import (
    SessionListResponse,
    SessionItem,
    SessionTitleUpdate,
    MessageItem,
    MessageListResponse,
    DeleteSessionResponse,
)

logger = logging.getLogger("hermes.server")

router = APIRouter()


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(exclude_cron: bool = False,
                  cron_only: bool = False,
                  session_logger=Depends(get_session_logger),
                  cron_scheduler=Depends(get_cron_scheduler)):
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        sessions = session_logger.list_sessions()
        items = []
        for s in sessions:
            sid = s.get("id", "")
            is_cron = sid.startswith("cron:")
            if exclude_cron and is_cron:
                continue
            if cron_only and not is_cron:
                continue
            title = s.get("title")
            if not title and is_cron and cron_scheduler is not None:
                sched_id = sid[len("cron:"):]
                try:
                    sched = cron_scheduler.get_schedule(sched_id)
                    if sched is not None:
                        title = sched.get("name")
                except Exception:
                    pass
            items.append(
                SessionItem(
                    id=sid,
                    created_at=s.get("created_at", ""),
                    updated_at=s.get("updated_at", ""),
                    title=title,
                )
            )
        return SessionListResponse(sessions=items)
    except Exception as e:
        logger.exception("列出会话失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@router.get("/sessions/{session_id}/messages", response_model=MessageListResponse)
def get_session_messages(
    session_id: str,
    limit: Optional[int] = Query(default=None, ge=1, description="限制返回数量"),
    session_logger=Depends(get_session_logger),
):
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        rows = session_logger.get_session_messages(session_id, limit=limit)
        messages = [
            MessageItem(
                role=r.get("role", ""),
                content=r.get("content", ""),
                created_at=r.get("created_at", ""),
                tool_name=r.get("tool_name"),
                tool_call_id=r.get("tool_call_id"),
                is_error=bool(r["is_error"]) if "is_error" in r.keys() else None,
                attachments=r.get("attachments"),
                message_type=r.get("message_type"),
                reasoning=r.get("reasoning"),
            )
            for r in rows
        ]
        return MessageListResponse(messages=messages)
    except Exception as e:
        logger.exception("获取会话消息失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@router.patch("/sessions/{session_id}")
def update_session_title(session_id: str,
                         body: SessionTitleUpdate,
                         session_logger=Depends(get_session_logger)):
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    if not session_logger.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    try:
        session_logger.update_session_title(session_id, body.title)
        stored = session_logger.get_session_title(session_id)
        if not stored:
            raise HTTPException(status_code=422, detail="title 不能为空")
        return {
            "id": session_id,
            "title": stored,
            "updated_at": datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("更新会话标题失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


@router.delete("/sessions/{session_id}", response_model=DeleteSessionResponse)
def delete_session(session_id: str,
                   session_logger=Depends(get_session_logger),
                   orchestrator=Depends(get_orchestrator),
                   upload_manager=Depends(get_upload_manager)):
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        deleted = session_logger.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
        if (
            orchestrator is not None
            and getattr(orchestrator, "todo_registry", None) is not None
        ):
            try:
                orchestrator.todo_registry.delete(session_id)
            except Exception as e:
                logger.warning("清理会话 todo 文件失败 %s: %s", session_id, e)
        if upload_manager is not None:
            try:
                upload_manager.cleanup_session(session_id)
            except Exception as e:
                logger.warning("清理会话文件关联失败 %s: %s", session_id, e)
        logger.info("已删除会话: %s", session_id)
        return DeleteSessionResponse(status="deleted", session_id=session_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除会话失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")
