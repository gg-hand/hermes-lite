"""配置相关 Pydantic 模型。从 server.py 提取。"""
from typing import Any, Dict

from pydantic import BaseModel


class ConfigResponse(BaseModel):
    """配置读取响应体。"""

    config: Dict[str, Any]


class ConfigUpdateRequest(BaseModel):
    """配置更新请求体。"""

    config: Dict[str, Any]


class ConfigUpdateResponse(BaseModel):
    """配置更新响应体。"""

    status: str
    message: str
    needs_restart: bool
