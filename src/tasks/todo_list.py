"""会话级 TodoList 内存对象（plan 模式数据模型）。

将 plan 模式重构为「对话流内 todo 卡片」（参考 Claude Code / Cursor /
Trae TodoWrite 风格）。``TodoList`` 为会话级内存对象，不持久化，跟随
会话生命周期。``TodoListRegistry`` 按 ``session_id`` 维护多个会话的
TodoList。

设计要点：
- 纯内存对象，不落盘：随会话生灭，避免持久化开销与残留。
- 自增 ID：step 的 id 从 0 开始按 ``init_plan`` 顺序自增。
- 严格状态机：``pending → in_progress → completed/failed``，终态
  （completed/failed）不可变更。
- 依赖编排：``depends_on`` 引用 step ID，依赖未满足时禁止启动。
- 自动推进：当前 in_progress 步骤完成（标记 completed）后，自动将第一
  个 ``status=pending`` 且所有 ``depends_on`` 均为 ``completed`` 的步骤
  标记为 ``in_progress``；找不到则不推进。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


# 合法的 step 状态枚举
_VALID_STATUSES = ("pending", "in_progress", "completed", "failed")


@dataclass
class Step:
    """单步任务数据模型。

    Attributes:
        id: 步骤 ID（0/1/2...，按 ``init_plan`` 顺序自增）。
        content: 步骤描述。
        status: 步骤状态，取值 ``pending`` / ``in_progress`` /
            ``completed`` / ``failed``。
        depends_on: 依赖的 step ID 列表，所有依赖完成后该步骤才可启动。
        result: 执行结果摘要，未执行时为 ``None``。
    """

    id: int
    content: str
    status: str
    depends_on: List[int]
    result: Optional[str]


class TodoList:
    """会话级 TodoList 内存对象。

    管理一个目标下的多步骤任务清单，提供初始化、状态更新与序列化能力。
    不持久化，随会话生灭。状态机严格：``pending → in_progress →
    completed/failed``，终态（completed/failed）不可变更。

    自动推进：当前 in_progress 步骤完成（标记 completed）后，自动将
    第一个 ``status=pending`` 且所有 ``depends_on`` 均为 ``completed``
    的步骤标记为 ``in_progress``；找不到则不推进。
    """

    def __init__(self, goal: str) -> None:
        """初始化 TodoList。

        参数:
            goal: 本次 plan 的目标描述。允许为空字符串，待后续设置。
        """
        self.goal: str = goal
        self.steps: List[Step] = []
        self.completed: bool = False

    def init_plan(self, steps: List[dict]) -> None:
        """初始化步骤列表，分配自增 ID。

        第一个 step 自动标记为 ``in_progress``，其余为 ``pending``。
        ``depends_on`` 缺省为空列表。重复调用会清空旧 steps 重新分配。

        参数:
            steps: 步骤定义列表，每项含 ``content`` 与可选 ``depends_on``。

        Raises:
            ValueError: ``steps`` 为空时抛出。
        """
        if not steps:
            raise ValueError("steps 不能为空")

        self.steps = []
        for idx, item in enumerate(steps):
            self.steps.append(
                Step(
                    id=idx,
                    content=item.get("content", ""),
                    status="in_progress" if idx == 0 else "pending",
                    depends_on=list(item.get("depends_on", [])),
                    result=None,
                )
            )
        self.completed = False

    def update_step(
        self, step_id: int, status: str, result: str = ""
    ) -> str:
        """更新步骤状态（严格状态机）。

        状态机规则：
        - step 当前为 ``completed`` 或 ``failed`` → 返回错误（终态不可变更）
        - step 当前为 ``pending`` 且依赖未满足 → 返回错误
        - step 当前为 ``pending`` 且 status=in_progress 且依赖已满足 → 允许
          （手动启动场景）
        - step 当前为 ``in_progress``，可标记 ``completed`` 或 ``failed``

        完成（标记 completed）后自动推进下一个依赖满足的 pending step。
        所有 step 均为 ``completed`` 时，标记 ``self.completed = True``。

        参数:
            step_id: 步骤 ID。
            status: 新状态，需为 4 个枚举值之一。
            result: 执行结果摘要，非空时写入 step.result。

        返回:
            成功时返回 ``"✅ 步骤 {id} 状态已更新为: {status}"``，
            失败时返回 ``"❌ ..."`` 描述错误原因。不抛异常。
        """
        step = self._find_step(step_id)
        if step is None:
            return f"❌ 步骤 {step_id} 不存在"

        if status not in _VALID_STATUSES:
            return f"❌ 非法状态: {status}"

        # 终态不可变更
        if step.status in ("completed", "failed"):
            return f"❌ 步骤 {step_id} 已为 {step.status}，不允许变更"

        if step.status == "pending":
            if status != "in_progress":
                return (
                    f"❌ 步骤 {step_id} 为 pending，"
                    f"仅可标记为 in_progress"
                )
            if not self._deps_satisfied(step):
                return f"❌ 步骤 {step_id} 依赖未满足"
            step.status = "in_progress"
            if result:
                step.result = result
            return f"✅ 步骤 {step_id} 状态已更新为: in_progress"

        # step.status == "in_progress"
        if status not in ("completed", "failed"):
            return (
                f"❌ 步骤 {step_id} 为 in_progress，"
                f"仅可标记为 completed 或 failed"
            )

        step.status = status
        if result:
            step.result = result

        # 完成后自动推进
        if status == "completed":
            self._auto_advance()

        # 全部完成则标记 completed
        if self.steps and all(s.status == "completed" for s in self.steps):
            self.completed = True

        return f"✅ 步骤 {step_id} 状态已更新为: {status}"

    def to_dict(self) -> dict:
        """序列化为 dict。

        返回:
            形如 ``{"goal": ..., "steps": [...], "completed": ...}`` 的
            dict，每个 step 含 ``id`` / ``content`` / ``status`` /
            ``depends_on`` / ``result``。
        """
        return {
            "goal": self.goal,
            "steps": [
                {
                    "id": s.id,
                    "content": s.content,
                    "status": s.status,
                    "depends_on": list(s.depends_on),
                    "result": s.result,
                }
                for s in self.steps
            ],
            "completed": self.completed,
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _find_step(self, step_id: int) -> Optional[Step]:
        """按 id 查找 step。

        参数:
            step_id: 步骤 ID。

        返回:
            命中的 ``Step``，未找到返回 ``None``。
        """
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def _deps_satisfied(self, step: Step) -> bool:
        """检查 step 的所有依赖是否均为 completed。

        依赖的 step 不存在时视为未满足。``depends_on`` 为空时返回 ``True``。

        参数:
            step: 待检查的步骤。

        返回:
            所有依赖均为 ``completed`` 时返回 ``True``，否则 ``False``。
        """
        status_map = {s.id: s.status for s in self.steps}
        return all(
            status_map.get(dep) == "completed" for dep in step.depends_on
        )

    def _auto_advance(self) -> None:
        """自动推进：将第一个依赖满足的 pending step 标记为 in_progress。

        找不到则不推进（所有 step 已完成或依赖未满足）。
        """
        for step in self.steps:
            if step.status == "pending" and self._deps_satisfied(step):
                step.status = "in_progress"
                return


class TodoListRegistry:
    """会话级 TodoList 注册表。

    按 ``session_id`` 维护多个会话的 ``TodoList``，提供创建 / 获取 /
    更新 / 序列化能力。同一 ``session_id`` 多次 ``init_plan`` 会覆盖
    前一次（旧 plan 失效）。
    """

    def __init__(self) -> None:
        """初始化注册表，内部 dict 存储 session_id → TodoList 映射。"""
        self._todos: Dict[str, TodoList] = {}

    def get(self, session_id: str) -> Optional[TodoList]:
        """获取指定会话的 TodoList。

        参数:
            session_id: 会话 ID。

        返回:
            ``TodoList``，不存在返回 ``None``。
        """
        return self._todos.get(session_id)

    def init_plan(
        self, session_id: str, goal: str, steps: List[dict]
    ) -> TodoList:
        """为指定会话初始化 plan（覆盖旧 plan）。

        同一 ``session_id`` 多次调用会覆盖前一次，旧 plan 失效。

        参数:
            session_id: 会话 ID。
            goal: 本次 plan 的目标描述。
            steps: 步骤定义列表，每项含 ``content`` 与可选 ``depends_on``。

        返回:
            新创建的 ``TodoList``。

        Raises:
            ValueError: ``steps`` 为空时抛出（透传自 ``TodoList.init_plan``）。
        """
        todo = TodoList(goal)
        todo.init_plan(steps)
        self._todos[session_id] = todo
        return todo

    def update_step(
        self, session_id: str, step_id: int, status: str, result: str = ""
    ) -> str:
        """更新指定会话的步骤状态（透传到 TodoList）。

        参数:
            session_id: 会话 ID。
            step_id: 步骤 ID。
            status: 新状态。
            result: 执行结果摘要，非空时写入 step.result。

        返回:
            透传 ``TodoList.update_step`` 的返回值；session 无 plan 时
            返回 ``"❌ 会话 {session_id} 无 plan"``。
        """
        todo = self._todos.get(session_id)
        if todo is None:
            return f"❌ 会话 {session_id} 无 plan"
        return todo.update_step(step_id, status, result)

    def get_todo_dict(self, session_id: str) -> Optional[dict]:
        """返回指定会话 TodoList 的 dict 序列化。

        参数:
            session_id: 会话 ID。

        返回:
            ``TodoList.to_dict()`` 的结果，不存在返回 ``None``。
        """
        todo = self._todos.get(session_id)
        if todo is None:
            return None
        return todo.to_dict()
