"""文件上传相关模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class FileUploadResponse(BaseModel):
    """文件上传响应体。"""

    file_id: str
    is_dup: bool = False
    message: str = ""


class FileItem(BaseModel):
    """文件条目。"""

    file_id: str
    original_name: str
    size: int
    type: str
    etl_status: str
    summary: str = ""
    chunk_count: int = 0
    uploaded_at: str
    last_accessed: str
    version_seq: Optional[int] = None
    is_latest: Optional[bool] = None


class FileListResponse(BaseModel):
    """文件列表响应体。"""

    files: List[FileItem]


class FileDeleteResponse(BaseModel):
    """文件删除响应体。"""

    status: str
    file_id: str
    details: dict = {}
