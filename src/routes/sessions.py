"""sessions 路由。从 server.py 提取。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from storage.sqlite_log import SessionLogger
from tasks.scheduler import CronScheduler

from schemas.common import SessionItem, SessionListResponse, SessionTitleUpdate, MessageItem, MessageListResponse, DeleteSessionResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
orchestrator = None
session_logger = None
cron_scheduler = None
upload_manager = None

@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(exclude_cron: bool = False, cron_only: bool = False):
    """列出所有会话。

    cron 会话（session_id 形如 ``cron:{schedule_id}``）若未设置 title，
    在此回退到 schedule.name，避免 cron 会话显示随机串。

    查询参数：
    - ``exclude_cron=true``：过滤掉 cron 会话（聊天页使用，避免调度会话污染会话列表）
    - ``cron_only=true``：仅返回 cron 会话（调度页切换器使用）
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        sessions = session_logger.list_sessions()
        items = []
        for s in sessions:
            sid = s.get("id", "")
            is_cron = sid.startswith("cron:")
            # 过滤参数互斥处理
            if exclude_cron and is_cron:
                continue
            if cron_only and not is_cron:
                continue
            title = s.get("title")
            # cron 会话兜底：title 缺失时查 CronScheduler 取 schedule.name
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
    limit: Optional[int] = Query(
        default=None, ge=1, description="限制返回数量"
    ),
):
    """获取指定会话的消息历史。

    可通过 limit 查询参数限制返回条数。
    """
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
def update_session_title(session_id: str, body: SessionTitleUpdate):
    """更新指定会话的标题（Task 0 PATCH 端点）。

    请求体：``{"title": "新标题"}``，长度 1-100 字符（由 Pydantic Field 校验，越界 422）。
    会话不存在时返回 404；成功返回 ``{id, title, updated_at}``。
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    if not session_logger.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    try:
        session_logger.update_session_title(session_id, body.title)
        # 再次校验写入成功（title 截断后为空时 update 静默忽略）
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
def delete_session(session_id: str):
    """删除指定会话及其所有消息。

    会话不存在时返回 404。
    """
    if session_logger is None:
        raise HTTPException(status_code=503, detail="SessionLogger 尚未初始化")
    try:
        deleted = session_logger.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
        # 同步清理 todo 持久化文件（避免残留）
        if (
            orchestrator is not None
            and getattr(orchestrator, "todo_registry", None) is not None
        ):
            try:
                orchestrator.todo_registry.delete(session_id)
            except Exception as e:
                logger.warning("清理会话 todo 文件失败 %s: %s", session_id, e)
        # 同步清理会话文件关联（uploaded_files 物理记录保留，可能被其他会话引用）
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


# ---------- 文件上传与 ETL 端点 ----------
