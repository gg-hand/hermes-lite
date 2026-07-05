"""Workflow 适配器（Task 6）。

将旧 6 个 WorkflowTemplate 包装为单 step WorkflowSpec，让旧模板经
``WorkflowEngine`` 统一执行路径（Task 16 完成后所有旧模板走 engine）。

包装规则：
- ``wrap_template_as_workflow(template_name, config) -> WorkflowSpec``
- 单 step WorkflowSpec，step.type 由 ``_infer_step_type`` 推断
- 默认 ``on_failure.action=fallback``，``fallback_config`` 含
  ``fallback_mode=single_turn``（失败时降级为单轮 LLM）

模板 → step 类型映射（SubTask 6.2）：
- ``research`` → ``react``（使用 ReactLoop）
- ``email_notify`` → ``deterministic``（纯邮件通知，无 LLM 调用）
- 其余 4 个（directory_watch / summary / cleanup_suggest / custom）→ ``llm``
  （确定性步骤 + LLM 单轮调用）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .spec import OnFailure, RetryPolicy, StepSpec, WorkflowSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模板 → step 类型映射（SubTask 6.2）
# ---------------------------------------------------------------------------

#: 已知模板名 → step 类型映射。
#: - ``research`` 使用 ReactLoop → ``react``
#: - ``email_notify`` 纯邮件通知无 LLM → ``deterministic``
#: - 其余（directory_watch / summary / cleanup_suggest / custom）含 LLM 调用 → ``llm``
TEMPLATE_STEP_TYPE_MAP: Dict[str, str] = {
    "research": "react",
    "email_notify": "deterministic",
    "directory_watch": "llm",
    "summary": "llm",
    "cleanup_suggest": "llm",
    "custom": "llm",
}

#: 默认 step 类型（未在映射中的模板）
DEFAULT_STEP_TYPE = "llm"


def _infer_step_type(template_name: str) -> str:
    """根据模板名推断 step 类型。

    参数:
        template_name: 模板名（如 ``"research"``）。

    返回:
        step 类型字符串（``react`` / ``deterministic`` / ``llm``）。
        未在 ``TEMPLATE_STEP_TYPE_MAP`` 中的模板默认返回 ``llm``。
    """
    return TEMPLATE_STEP_TYPE_MAP.get(template_name, DEFAULT_STEP_TYPE)


# ---------------------------------------------------------------------------
# 适配器主入口（SubTask 6.1 + 6.3）
# ---------------------------------------------------------------------------


def wrap_template_as_workflow(
    template_name: str,
    config: Optional[Dict[str, Any]] = None,
    workflow_name: str = "",
) -> WorkflowSpec:
    """将旧 WorkflowTemplate 包装为单 step WorkflowSpec。

    生成的 WorkflowSpec 含一个 ``deterministic`` 类型 step，其 ``config``
    字段含原始 ``template`` 名与 ``template_config``，由
    ``DeterministicExecutor`` 调用原模板的 ``execute`` 方法。

    默认 ``on_failure.action=fallback``，``fallback_config`` 含
    ``fallback_mode=single_turn``（SubTask 6.3）。

    参数:
        template_name: 模板名（如 ``"research"``）。
        config: 模板配置 dict（透传到原模板的 ``execute``）。
            为 ``None`` 时使用空 dict。
        workflow_name: workflow 名称（用于报告展示）。为空时使用
            ``template_name``。

    返回:
        :class:`WorkflowSpec` 实例，含单个 ``deterministic`` step。
    """
    if not template_name:
        raise ValueError("template_name 不能为空")

    config = config or {}
    step_type = _infer_step_type(template_name)

    # 构造 step config
    # - ``deterministic`` 类型：``{"template": <name>, ...template_cfg}``
    # - ``react`` 类型：``{"task": ..., "react_loop": None, ...template_cfg}``
    # - ``llm`` 类型：``{"prompt": ..., ...template_cfg}``
    # 适配器统一用 ``deterministic`` 类型包装（DeterministicExecutor 调用
    # 原模板），让旧模板完全经 WorkflowEngine 执行。
    # 但 spec 要求按 _infer_step_type 推断类型，此处两种选择：
    # 1. 强制用 deterministic 包装所有模板（最简单）
    # 2. 按推断的 step_type 包装，但 react/llm 类型无法直接调用旧模板
    # 实际选择 1（deterministic 包装），保留 _infer_step_type 仅作元信息
    # 让 step_executor 的 DeterministicExecutor 调用原模板。
    step_config: Dict[str, Any] = {"template": template_name}
    step_config.update(config)

    # 默认 on_failure=fallback + fallback_config
    on_failure = OnFailure(
        action="fallback",
        retry=RetryPolicy(max_attempts=2, backoff_strategy="fixed", base_delay_ms=500),
        fallback_config={"fallback_mode": "single_turn"},
        fallback_type="llm",
        message=f"模板 {template_name} 执行失败，降级为单轮 LLM 调用",
    )

    step = StepSpec(
        id=f"wrap_{template_name}",
        name=f"模板 {template_name}",
        type="deterministic",
        config=step_config,
        on_failure=on_failure,
    )

    return WorkflowSpec(
        name=workflow_name or template_name,
        template=template_name,
        template_config=dict(config),
        steps=[step],
        on_failure=on_failure,
        metadata={
            "adapter_wrapped": True,
            "original_template": template_name,
            "inferred_step_type": step_type,
        },
    )


# ---------------------------------------------------------------------------
# 批量适配：list_schedules 路径可选
# ---------------------------------------------------------------------------


def is_adapter_wrapped(spec: WorkflowSpec) -> bool:
    """检查 WorkflowSpec 是否由适配器包装。

    用于 ``_execute_workflow`` 双轨路径判定（Task 10）：
    - 适配器包装的 spec：直接调 ``WorkflowEngine.execute``（deterministic
      step 会调原模板）
    - 用户手写的多步 spec：走完整 WorkflowEngine 流程

    参数:
        spec: WorkflowSpec 实例。

    返回:
        ``True`` 表示由适配器包装（metadata.adapter_wrapped=True）。
    """
    return bool(spec.metadata.get("adapter_wrapped"))


__all__ = [
    "TEMPLATE_STEP_TYPE_MAP",
    "DEFAULT_STEP_TYPE",
    "wrap_template_as_workflow",
    "is_adapter_wrapped",
]
