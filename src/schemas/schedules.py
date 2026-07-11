"""调度相关模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ScheduleCreateRequest(BaseModel):
    """调度项创建请求体。

    支持可选 ``workflow`` 字段（声明式 workflow 配置），结构遵循
    ``WorkflowSpec.from_dict``：
    - 简易模式: ``{"template": "research", "template_config": {...}}``
    - 多步模式: ``{"name": "...", "steps": [{...}, ...]}``
    """

    name: str
    cron: str
    task: str
    enabled: bool = True
    id: Optional[str] = None
    workflow: Optional[Dict[str, Any]] = None


class ScheduleUpdateRequest(BaseModel):
    """调度项更新请求体。

    ``workflow`` 字段变更需重启调度器才能生效（与 cron/task/name 一致）。
    """

    name: Optional[str] = None
    cron: Optional[str] = None
    task: Optional[str] = None
    enabled: Optional[bool] = None
    workflow: Optional[Dict[str, Any]] = None


class ScheduleListResponse(BaseModel):
    """调度项列表响应体。"""

    schedules: List[dict] = Field(default_factory=list)


class ScheduleResponse(BaseModel):
    """调度项操作响应体。"""

    schedule_id: str
    message: str
