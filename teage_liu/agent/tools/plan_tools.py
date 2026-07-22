"""Plan 模式工具：plan_task, update_todo。

本模块提供 plan 模式相关工具的注册逻辑，包含 ``register_plan_tools``
函数，用于将 ``plan_create`` 和 ``plan_update_step`` 两个工具注册到
ToolRegistry 的 Core Tier。

注册的工具：
- plan_create: 规划复杂任务的执行步骤并初始化 todo 清单
- plan_update_step: 更新某个 todo 步骤的状态

注：``_plan_task`` / ``_update_todo`` 是 ``register_plan_tools`` 内部的
closure（嵌套函数），无法在模块级导入。如需访问，请通过
``register_plan_tools`` 注入后由 registry 取出 handler。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from ...tasks.todo_list import TodoListRegistry

logger = logging.getLogger(__name__)


def register_plan_tools(
    registry,
    todo_registry: TodoListRegistry,
    get_session_id: Callable[[], Optional[str]],
) -> None:
    """注册 plan 模式工具到 ToolRegistry 的 Core Tier。

    注册 2 个工具：
    - plan_task: 规划复杂任务的执行步骤并初始化 todo 清单
    - update_todo: 更新某个 todo 步骤的状态

    所有工具通过 register_core 注册，保证字节级稳定（KV cache 100% 命中）。

    参数:
        registry: ToolRegistry 实例。
        todo_registry: TodoListRegistry 实例（来自 src/tasks/todo_list.py）。
        get_session_id: 一个 callable，调用时返回当前请求的 session_id 字符串
            或 None。因为 ReactLoop 是同步执行工具的，而 session_id 在请求
            上下文中，需要从外部传入一个获取函数。
    """
    # plan_task 工具
    def _plan_task(goal: str, steps: list) -> str:
        """规划任务步骤并初始化 todo 清单。"""
        try:
            session_id = get_session_id()
            if session_id is None:
                return "❌ 无法获取 session_id"
            todo_registry.init_plan(session_id, goal, steps)
            first_content = steps[0].get("content", "") if steps else ""
            return (
                f"已规划 {len(steps)} 个步骤，"
                f"开始执行 step 0: {first_content}"
            )
        except Exception as e:
            return f"plan_task 执行失败: {e}"

    registry.register_deferred(
        name="plan_create",
        description="规划一个复杂任务的执行步骤并初始化 todo 清单。当用户提出多步骤任务时主动启用。",
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "任务目标描述"},
                "steps": {
                    "type": "array",
                    "description": "步骤列表，按执行顺序排列",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "步骤描述"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "依赖的 step 索引列表（基于 0 起的 step ID）",
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            "required": ["goal", "steps"],
        },
        handler=_plan_task,
    )

    # update_todo 工具
    def _update_todo(step_id: int, status: str, result: str = "") -> str:
        """更新 todo 步骤状态。"""
        try:
            session_id = get_session_id()
            if session_id is None:
                return "❌ 无法获取 session_id"
            return todo_registry.update_step(session_id, step_id, status, result)
        except Exception as e:
            return f"update_todo 执行失败: {e}"

    registry.register_deferred(
        name="plan_update_step",
        description="更新某个 todo 步骤的状态。仅可标记当前 in_progress 的 step 为 completed 或 failed。",
        input_schema={
            "type": "object",
            "properties": {
                "step_id": {"type": "integer", "description": "步骤 ID"},
                "status": {
                    "type": "string",
                    "enum": ["completed", "failed"],
                    "description": "新状态",
                },
                "result": {"type": "string", "description": "执行结果摘要（可选）"},
            },
            "required": ["step_id", "status"],
        },
        handler=_update_todo,
    )
