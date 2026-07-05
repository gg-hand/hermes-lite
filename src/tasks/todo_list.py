"""会话级 TodoList 对象（plan 模式数据模型）。

将 plan 模式重构为「对话流内 todo 卡片」（参考 Claude Code / Cursor /
Trae TodoWrite 风格）。``TodoList`` 为会话级对象，``TodoListRegistry``
按 ``session_id`` 维护多个会话的 TodoList。

设计要点：
- 内存 + 磁盘双写：``TodoListRegistry`` 接受可选 ``persistence_dir``，
 传入时启用持久化（``{persistence_dir}/todo/{session_id}.json``），
  ``init_plan`` / ``update_step`` 同步原子写，重启后懒加载恢复。
  未传入时降级为纯内存（向后兼容老测试）。
- 自增 ID：step 的 id 从 0 开始按 ``init_plan`` 顺序自增。
- 严格状态机：``pending → in_progress → completed/failed``，终态
  （completed/failed）不可变更。
- 依赖编排：``depends_on`` 引用 step ID，依赖未满足时禁止启动。
- 自动推进：当前 in_progress 步骤完成（标记 completed）后，自动将第一
  个 ``status=pending`` 且所有 ``depends_on`` 均为 ``completed`` 的步骤
  标记为 ``in_progress``；找不到则不推进。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


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
    """会话级 TodoList 对象。

    管理一个目标下的多步骤任务清单，提供初始化、状态更新与序列化能力。
    持久化由 ``TodoListRegistry`` 负责（传入 ``persistence_dir`` 时）。
    状态机严格：``pending → in_progress → completed/failed``，终态
    （completed/failed）不可变更。

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

    持久化：传入 ``persistence_dir`` 时启用磁盘持久化，``init_plan`` /
    ``update_step`` 同步原子写（tmp + fsync + replace），``get`` /
    ``get_todo_dict`` / ``update_step`` 均走懒加载（内存未命中则从磁盘
    恢复）。文件路径 ``{persistence_dir}/todo/{session_id}.json``，
    与 JSONL 历史同源（共用 TTL）。未传入时降级为纯内存（向后兼容）。
    """

    def __init__(self, persistence_dir: Optional[str] = None) -> None:
        """初始化注册表。

        参数:
            persistence_dir: 持久化目录（与 HistoryBuffer 共用）。
                传入时在子目录 ``todo/`` 下存放每个会话的 plan JSON；
                为 ``None`` 时降级为纯内存（不落盘，向后兼容老测试）。
        """
        self._todos: Dict[str, TodoList] = {}
        self._lock = threading.Lock()
        # 子目录 todo/，与 JSONL 同源（共用 persistence_dir 生命周期）
        self._todo_dir: Optional[Path] = (
            Path(persistence_dir) / "todo" if persistence_dir else None
        )
        if self._todo_dir is not None:
            self._todo_dir.mkdir(parents=True, exist_ok=True)

    def _file_for(self, session_id: str) -> Optional[Path]:
        """返回指定会话的 todo 文件路径。

        文件名做 ``:`` → ``_`` 替换（cron 会话 ``cron:abc`` →
        ``cron_abc.json``），与 JSONL 历史命名一致。``_todo_dir`` 为
        ``None`` 时返回 ``None``。
        """
        if self._todo_dir is None:
            return None
        return self._todo_dir / f"{session_id.replace(':', '_')}.json"

    def _get_or_load(self, session_id: str) -> Optional[TodoList]:
        """获取或懒加载 TodoList（统一入口，加锁）。

        内存命中 → 直接返回；未命中且启用持久化 → 从磁盘加载并缓存；
        否则返回 ``None``。所有读路径（``get`` / ``get_todo_dict`` /
        ``update_step``）都应走此方法，避免重启后 update_step 报错。

        参数:
            session_id: 会话 ID。

        返回:
            ``TodoList``，不存在返回 ``None``。
        """
        with self._lock:
            todo = self._todos.get(session_id)
            if todo is not None:
                return todo
            path = self._file_for(session_id)
            if path is None or not path.exists():
                return None
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                todo = TodoList(data.get("goal", ""))
                todo.steps = [
                    Step(
                        id=s["id"],
                        content=s["content"],
                        status=s["status"],
                        depends_on=list(s.get("depends_on", [])),
                        result=s.get("result"),
                    )
                    for s in data.get("steps", [])
                ]
                todo.completed = data.get("completed", False)
                self._todos[session_id] = todo
                return todo
            except (OSError, json.JSONDecodeError, KeyError) as e:
                logger.warning("加载 TodoList 失败 %s: %s", session_id, e)
                return None

    def _save(self, session_id: str, todo: TodoList) -> None:
        """原子写入 TodoList 到磁盘（tmp + fsync + replace）。

        ``_todo_dir`` 为 ``None`` 时直接返回（纯内存模式）。锁外执行
        IO，避免持锁 IO。失败时清理 tmp 文件并记日志，不抛异常（避免
        持久化失败影响主流程）。

        并发安全：每次写入使用唯一 tmp 文件名（含线程 ID），避免多线程
        同时写同一 tmp 文件导致 WinError 32 / 拒绝访问。
        """
        path = self._file_for(session_id)
        if path is None:
            return
        # 唯一 tmp 文件名，避免并发写冲突
        tmp = path.with_suffix(
            f".{threading.get_ident()}.{os.getpid()}.json.tmp"
        )
        try:
            data = todo.to_dict()
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(path)  # 原子替换
        except OSError as e:
            logger.warning("保存 TodoList 失败 %s: %s", session_id, e)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def get(self, session_id: str) -> Optional[TodoList]:
        """获取指定会话的 TodoList（懒加载）。

        参数:
            session_id: 会话 ID。

        返回:
            ``TodoList``，不存在返回 ``None``。
        """
        return self._get_or_load(session_id)

    def init_plan(
        self, session_id: str, goal: str, steps: List[dict]
    ) -> TodoList:
        """为指定会话初始化 plan（覆盖旧 plan）。

        同一 ``session_id`` 多次调用会覆盖前一次，旧 plan 失效。``_save``
        的 ``tmp.replace`` 天然覆盖旧文件，无需额外删除。

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
        with self._lock:
            self._todos[session_id] = todo
        self._save(session_id, todo)  # 锁外写文件
        return todo

    def update_step(
        self, session_id: str, step_id: int, status: str, result: str = ""
    ) -> str:
        """更新指定会话的步骤状态（透传到 TodoList，懒加载）。

        参数:
            session_id: 会话 ID。
            step_id: 步骤 ID。
            status: 新状态。
            result: 执行结果摘要，非空时写入 step.result。

        返回:
            透传 ``TodoList.update_step`` 的返回值；session 无 plan 时
            返回 ``"❌ 会话 {session_id} 无 plan"``。
        """
        todo = self._get_or_load(session_id)
        if todo is None:
            return f"❌ 会话 {session_id} 无 plan"
        msg = todo.update_step(step_id, status, result)
        self._save(session_id, todo)  # 锁外写文件
        return msg

    def get_todo_dict(self, session_id: str) -> Optional[dict]:
        """返回指定会话 TodoList 的 dict 序列化（懒加载）。

        参数:
            session_id: 会话 ID。

        返回:
            ``TodoList.to_dict()`` 的结果，不存在返回 ``None``。
        """
        todo = self._get_or_load(session_id)
        return todo.to_dict() if todo else None

    def delete(self, session_id: str) -> None:
        """删除指定会话的 TodoList（内存 + 磁盘）。

        供 server.py 删除会话路由调用，确保 todo 文件不残留。

        参数:
            session_id: 会话 ID。
        """
        with self._lock:
            self._todos.pop(session_id, None)
        path = self._file_for(session_id)
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError as e:
                logger.warning("删除 TodoList 文件失败 %s: %s", session_id, e)
