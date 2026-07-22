"""提议相关模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ProposalModifyRequest(BaseModel):
    """提议修改请求体（修改并确认）。

    用户在前端确认卡片上修改 cron 表达式 / granted_tools 后点击「修改并确认」
    时提交。所有字段可选，仅提供的字段会被更新（浅合并到原 schedule_config；
    requested_tools 整体替换）。
    """

    schedule_config_updates: Optional[Dict[str, Any]] = Field(
        default=None,
        description="调度配置更新字段（浅合并到原配置），可含 name/cron/task/enabled/workflow 等",
    )
    requested_tools: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="新的请求工具列表（整体替换），每项含 tool/scope/allowed_paths",
    )
