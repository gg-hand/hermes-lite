"""Phase 8 Task 2 工作流模板系统 + Task 3-8 通用 workflow 引擎。

提供 ``WorkflowTemplate`` 抽象基类与若干内置模板，封装「确定性步骤 + LLM 步骤」
的两阶段执行模式，供 CronScheduler 在触发含 ``workflow`` 字段的调度项时调用。

Task 3-8 新增通用 workflow 引擎：
- :class:`WorkflowEngine` 按 ``WorkflowSpec.steps`` 拓扑序执行多步 workflow
- :class:`WorkflowValidator` 静态校验 spec（id 唯一 / depends_on 无环 / type 合法 等）
- :func:`wrap_template_as_workflow` 将旧 6 个模板包装为单 step WorkflowSpec

模板列表（按 SubTask 2.2 ~ 2.6 增量交付）：
- :class:`DirectoryWatchTemplate`：监控目录变更并 LLM 分析趋势
- :class:`SummaryTemplate`：总结指定会话历史
- :class:`EmailNotifyTemplate`：纯确定性邮件通知（不调 LLM）
- :class:`CleanupSuggestTemplate`：查询低价值记忆并 LLM 生成清理建议
- :class:`ResearchTemplate`：LLM 自主研究（白名单工具内）
- :class:`CustomTemplate`：引用 cron_tool（Task 5 实装前为 stub）

设计约束（5 条缓存硬约束）：
- 模板的 system prompt **禁含动态变量**（缓存约束 5）
- 时间变量只放 messages[0]（缓存约束 3）
- LLM 步骤单轮调用（``max_loops=1``，``research`` 可配置）
"""

from __future__ import annotations

from .base import (
    WorkflowContext,
    WorkflowResult,
    WorkflowTemplate,
    render_time_variables,
)
from .directory_watch import DirectoryWatchTemplate
from .summary import SummaryTemplate
from .email_notify import EmailNotifyTemplate
from .cleanup_suggest import CleanupSuggestTemplate
from .research import ResearchTemplate
from .custom import CustomTemplate

# Task 3-8: 通用 workflow 引擎相关导出
from .spec import (
    OnFailure,
    RetryPolicy,
    StepSpec,
    WorkflowSpec,
)
from .step_trace import StepTrace
from .validator import (
    ValidationError,
    ValidationResult,
    WorkflowValidator,
)
from .engine import WorkflowEngine
from .adapter import wrap_template_as_workflow

#: 内置模板注册表（name → class），供 CronScheduler 按名查找
BUILTIN_TEMPLATES = {
    DirectoryWatchTemplate.name: DirectoryWatchTemplate,
    SummaryTemplate.name: SummaryTemplate,
    EmailNotifyTemplate.name: EmailNotifyTemplate,
    CleanupSuggestTemplate.name: CleanupSuggestTemplate,
    ResearchTemplate.name: ResearchTemplate,
    CustomTemplate.name: CustomTemplate,
}


def get_template(name: str):
    """按名查找内置模板类。

    参数:
        name: 模板名称（如 ``"directory_watch"``）。

    返回:
        模板类（``WorkflowTemplate`` 子类）。未找到返回 ``None``。
    """
    return BUILTIN_TEMPLATES.get(name)


__all__ = [
    # 基础数据结构
    "WorkflowContext",
    "WorkflowResult",
    "WorkflowTemplate",
    "render_time_variables",
    # 内置模板
    "DirectoryWatchTemplate",
    "SummaryTemplate",
    "EmailNotifyTemplate",
    "CleanupSuggestTemplate",
    "ResearchTemplate",
    "CustomTemplate",
    "BUILTIN_TEMPLATES",
    "get_template",
    # Task 3-8: 通用 workflow 引擎
    "WorkflowEngine",
    "WorkflowSpec",
    "StepSpec",
    "OnFailure",
    "RetryPolicy",
    "StepTrace",
    "WorkflowValidator",
    "ValidationResult",
    "ValidationError",
    "wrap_template_as_workflow",
]
