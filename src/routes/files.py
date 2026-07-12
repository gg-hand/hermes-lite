"""files 路由：文件上传、列表、元数据查询、原始内容下载、会话文件、管理员删除。

Task 9: 从 server.py 迁移 6 个端点 + _run_etl 后台辅助函数。
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app import get_upload_manager, get_session_logger, get_etl_engine
from schemas.files import (
    FileDeleteResponse,
    FileItem,
    FileListResponse,
    FileUploadResponse,
)

logger = logging.getLogger("hermes.server")

router = APIRouter()


# ---------- 后台 ETL 辅助 ----------


def _run_etl(file_id: str, session_id: str, etl_engine) -> None:
    """后台执行 ETL 处理。"""
    try:
        if etl_engine is not None:
            result = etl_engine.process_file(file_id, session_id)
            if result.get("status") == "failed":
                logger.warning(
                    "ETL 失败: file_id=%s, error=%s", file_id, result.get("error")
                )
            else:
                logger.info(
                    "ETL 完成: file_id=%s, chunks=%d",
                    file_id,
                    result.get("chunk_count", 0),
                )
    except Exception as e:
        logger.exception("ETL 后台任务异常: file_id=%s", file_id)


# ---------- 文件上传 ----------


@router.post("/files/upload", response_model=FileUploadResponse)
async def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    upload_manager=Depends(get_upload_manager),
    session_logger=Depends(get_session_logger),
    etl_engine=Depends(get_etl_engine),
):
    """上传文件并触发 ETL 处理。

    接收 multipart/form-data 格式的文件上传。
    全局 SHA256 去重：相同内容的文件返回已有 file_id。
    上传成功后后台异步执行 ETL 流水线。
    """
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")

    # 获取 session_id：query param → form 字段 → request.state（auth 中间件）
    session_id = request.query_params.get("session_id")

    if not session_id:
        try:
            form = await request.form()
            session_id = form.get("session_id")
        except Exception:
            session_id = None

    if not session_id:
        session_id = (
            getattr(request.state, "session_id", None)
            if hasattr(request.state, "session_id")
            else None
        )

    if not session_id:
        raise HTTPException(status_code=401, detail="缺少 session_id")

    # 读取文件（form 已在上面的 try 中解析，若未尝试过则这里取）
    try:
        form = await request.form()
    except Exception:
        form = None
    uploaded_file = form.get("file") if form is not None else None
    if uploaded_file is None:
        raise HTTPException(status_code=400, detail="未提供文件")

    filename = uploaded_file.filename or "unknown"
    content = await uploaded_file.read()

    # 保存
    file_id, is_dup = upload_manager.save(filename, content, session_id)
    if file_id is None:
        raise HTTPException(status_code=400, detail="文件上传失败（校验未通过）")

    # 写入文件上传消息到会话历史（仅首次上传，去重命中不写避免重复）
    if not is_dup and session_logger is not None:
        meta = upload_manager.get_metadata(file_id)
        if meta is not None:
            import json as _json

            file_type = meta.get("type", "")
            is_image = file_type in (".png", ".jpg", ".jpeg", ".gif")
            attachment = {
                "file_id": file_id,
                "name": filename,
                "type": file_type,
                "size": meta.get("size", 0),
                "category": "image" if is_image else "document",
                "etl_status": meta.get("etl_status", "pending"),
            }
            session_logger.log_message(
                session_id=session_id,
                role="user",
                content=f"已上传文件：{filename}",
                attachments=_json.dumps([attachment], ensure_ascii=False),
                message_type="file_upload",
            )

    # 后台 ETL
    if not is_dup and etl_engine is not None:
        background_tasks.add_task(_run_etl, file_id, session_id, etl_engine)

    message = "文件已在知识库中" if is_dup else "上传成功"
    return FileUploadResponse(file_id=file_id, is_dup=bool(is_dup), message=message)


# ---------- 文件列表 ----------


@router.get("/files", response_model=FileListResponse)
def list_files(upload_manager=Depends(get_upload_manager)):
    """列出所有已上传文件。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        files = upload_manager.list_all()
        items = [
            FileItem(
                file_id=f.get("file_id", ""),
                original_name=f.get("original_name", ""),
                size=f.get("size", 0),
                type=f.get("type", ""),
                etl_status=f.get("etl_status", "pending"),
                summary=f.get("summary", ""),
                chunk_count=f.get("chunk_count", 0),
                uploaded_at=f.get("uploaded_at", ""),
                last_accessed=f.get("last_accessed", ""),
            )
            for f in files
        ]
        return FileListResponse(files=items)
    except Exception as e:
        logger.exception("列出文件失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 单文件元数据 ----------


@router.get("/files/{file_id}")
def get_file_metadata(file_id: str,
                      upload_manager=Depends(get_upload_manager)):
    """获取单个文件的元数据。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        meta = upload_manager.get_metadata(file_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")
        return dict(meta)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取文件元数据失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 文件原始内容 ----------


@router.get("/files/{file_id}/raw")
def get_file_raw(file_id: str,
                 upload_manager=Depends(get_upload_manager)):
    """返回文件原始内容（图片缩略图/文档下载用）。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        meta = upload_manager.get_metadata(file_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        if meta.get("etl_status") == "disk_expired" or not meta.get("saved_path"):
            raise HTTPException(status_code=410, detail="文件已过期")
        saved_path = meta["saved_path"]
        if not os.path.exists(saved_path):
            raise HTTPException(status_code=404, detail="磁盘文件丢失")
        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        media_type = content_types.get(meta.get("type", ""), "application/octet-stream")
        return FileResponse(
            saved_path,
            media_type=media_type,
            filename=meta.get("original_name", ""),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取文件内容失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 会话文件列表 ----------


@router.get("/sessions/{session_id}/files", response_model=FileListResponse)
def get_session_files(session_id: str,
                      upload_manager=Depends(get_upload_manager)):
    """获取指定会话的上传文件列表。"""
    if upload_manager is None:
        raise HTTPException(status_code=503, detail="文件模块未初始化")
    try:
        files = upload_manager.get_session_files(session_id)
        items = [
            FileItem(
                file_id=f.get("file_id", ""),
                original_name=f.get("original_name", ""),
                size=f.get("size", 0),
                type=f.get("type", ""),
                etl_status=f.get("etl_status", "pending"),
                summary=f.get("summary", ""),
                chunk_count=f.get("chunk_count", 0),
                uploaded_at=f.get("uploaded_at", ""),
                last_accessed=f.get("last_accessed", ""),
                version_seq=f.get("version_seq"),
                is_latest=f.get("is_latest"),
            )
            for f in files
        ]
        return FileListResponse(files=items)
    except Exception as e:
        logger.exception("获取会话文件列表失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")


# ---------- 管理员文件删除 ----------


@router.delete("/admin/files/{file_id}", response_model=FileDeleteResponse)
def admin_delete_file(file_id: str,
                      request: Request,
                      etl_engine=Depends(get_etl_engine)):
    """管理员应急清理：全链路删除文件知识。

    删除：磁盘原始文件 + 解析缓存 + ChromaDB 向量块 + FTS5 索引 + SQLite 元数据。
    需要 Bearer Token 认证。
    """
    if etl_engine is None:
        raise HTTPException(status_code=503, detail="ETL 模块未初始化")
    try:
        result = etl_engine.delete_file_knowledge(file_id)
        if not result.get("deleted"):
            raise HTTPException(
                status_code=404, detail=f"文件 {file_id} 不存在或已删除"
            )
        logger.info(
            "管理员删除文件: file_id=%s, details=%s",
            file_id,
            result.get("details"),
        )
        return FileDeleteResponse(
            status="deleted",
            file_id=file_id,
            details=result.get("details", {}),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("管理员删除文件失败: %s", e)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}")
