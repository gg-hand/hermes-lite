"""任务清单只读归档解析器（Phase 6 Task 2 重构）。

本模块原为运行时任务管理器，现已改造为只读归档解析器：保留对历史
``data/tasks.md`` 的 Markdown 解析能力，用于查看归档任务清单与进度，
但不再作为运行时数据源。运行时任务管理改用新的 TodoList 内存对象。

设计要点：
- 只读：不再提供创建 / 更新 / 删除 / 清理等写接口，所有方法仅做解析与查询。
- 无锁：只读场景无并发写入风险，移除 ``threading.Lock``。
- Markdown 解析：保留 ``_parse_markdown`` 解析历史归档文件，人类可读且
  Git 友好，便于版本对比与手工查阅。
- 轻量化设计，仅做单人单 Agent 场景的归档任务清单查阅。

任务 ID 形如 ``T001`` / ``T002``，按创建顺序自增。状态取值为
``pending`` / ``in_progress`` / ``completed`` / ``failed``。依赖通过
``depends_on`` 字段表达，``get_next_ready`` 据此挑选下一个可执行任务。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Task:
    """任务数据模型。

    Attributes:
        id: 任务 ID，形如 ``T001`` / ``T002``，按创建顺序自增。
        description: 任务描述（人类可读）。
        status: 任务状态，取值 ``pending`` / ``in_progress`` /
            ``completed`` / ``failed``。
        depends_on: 依赖的任务 ID 列表，所有依赖完成后该任务才可被
            ``get_next_ready`` 选中。
        created_at: 创建时间（ISO 格式字符串）。
        updated_at: 最近一次更新时间（ISO 格式字符串），未更新时为 ``None``。
        result: 执行结果摘要，未执行时为 ``None``。
    """

    id: str
    description: str
    status: str
    depends_on: List[str]
    created_at: str
    updated_at: Optional[str]
    result: Optional[str]


class TaskManager:
    """只读任务清单归档解析器。

    以 Markdown 文件为只读数据源，提供查询 / 依赖编排 / 进度统计等
    能力。不再支持创建 / 更新 / 删除 / 清理等写操作，运行时任务管理
    请使用新的 TodoList 内存对象。本类仅用于查阅历史 ``data/tasks.md``
    归档，无并发写入风险，故不使用锁。

    文件格式示例::

        # 任务清单

        ## T001: 调研 RAG 方案
        - 状态: completed
        - 依赖: T002, T003
        - 创建: 2026-06-28T12:00:00.000000
        - 更新: 2026-06-28T12:05:00.000000
        - 结果: Milvus 是开源向量数据库...

        ## T002: 整理对比文档
        - 状态: pending
        - 依赖:
        - 创建: 2026-06-28T12:00:00.000000
    """

    def __init__(self, file_path: str = "data/tasks.md") -> None:
        """初始化只读任务归档解析器。

        参数:
            file_path: 任务清单 Markdown 文件路径，默认 ``"data/tasks.md"``。
                仅用于读取历史归档，不保证父目录存在，也不创建文件。
        """
        self.file_path = Path(file_path)

    # ------------------------------------------------------------------
    # 对外接口（只读）
    # ------------------------------------------------------------------
    def list_tasks(self, status: Optional[str] = None) -> List[dict]:
        """列出任务（可按状态过滤）。

        参数:
            status: 状态过滤值，``None`` 时返回全部任务。

        返回:
            任务 dict 列表（由 ``dataclasses.asdict`` 转换）。
        """
        tasks = self._load()
        if status is None:
            return [asdict(t) for t in tasks]
        return [asdict(t) for t in tasks if t.status == status]

    def get_next_ready(self) -> Optional[str]:
        """获取下一个可执行任务的 ID。

        查找首个 ``status="pending"`` 且所有 ``depends_on`` 均为
        ``completed`` 的任务。依赖的任务不存在时视为未满足。

        返回:
            任务 ID，无可用任务时返回 ``None``。
        """
        tasks = self._load()
        status_map = {t.id: t.status for t in tasks}
        for task in tasks:
            if task.status != "pending":
                continue
            # 检查所有依赖是否均为 completed；依赖不存在时 get 返回 None，视为未满足
            if all(status_map.get(dep) == "completed" for dep in task.depends_on):
                return task.id
        return None

    def get_progress_summary(self) -> str:
        """获取任务进度摘要。

        统计 total / completed / in_progress / failed / pending 数量，
        返回人类可读的进度字符串。无任务时返回空字符串 ``""``（不注入上下文）。

        返回:
            形如 ``"📋 任务进度: X/Y 完成, A 进行中, B 失败, C 待办"``，
            无任务时返回 ``""``。
        """
        tasks = self._load()
        if not tasks:
            return ""
        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == "completed")
        in_progress = sum(1 for t in tasks if t.status == "in_progress")
        failed = sum(1 for t in tasks if t.status == "failed")
        pending = sum(1 for t in tasks if t.status == "pending")
        return (
            f"📋 任务进度: {completed}/{total} 完成, "
            f"{in_progress} 进行中, {failed} 失败, {pending} 待办"
        )

    # ------------------------------------------------------------------
    # 内部方法（只读解析）
    # ------------------------------------------------------------------
    def _load(self) -> List[Task]:
        """读取 Markdown 文件并解析为 Task 列表。

        文件不存在或读取失败时返回空列表，不抛异常。

        返回:
            Task 列表。
        """
        if not self.file_path.exists():
            return []
        try:
            text = self.file_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return self._parse_markdown(text)

    def _parse_markdown(self, text: str) -> List[Task]:
        """解析 Markdown 文本为 Task 列表。

        解析规则：
        - ``## (T\\d+)[:：]\\s*(.*)`` 提取 ID 与描述。
        - ``- 状态: xxx`` 提取 status，缺失或空时默认 ``"pending"``。
        - ``- 依赖: T001, T002`` 提取 depends_on，按逗号分隔，
          空字符串时为空列表。
        - ``- 创建: xxx`` 提取 created_at，缺失时为空字符串。
        - ``- 更新: xxx`` 提取 updated_at，无此行或空时为 ``None``。
        - ``- 结果: xxx`` 提取 result，无此行或空时为 ``None``。
        - 解析失败的字段降级为默认值，不抛异常。

        参数:
            text: Markdown 文本。

        返回:
            Task 列表。
        """
        tasks: List[Task] = []
        # 当前正在解析的任务字段缓冲
        current: Optional[dict] = None

        def _flush() -> None:
            """将 current 缓冲区封装为 Task 并追加到 tasks。"""
            if current is None:
                return
            try:
                tasks.append(
                    Task(
                        id=current["id"],
                        description=current["description"],
                        status=current["status"],
                        depends_on=current["depends_on"],
                        created_at=current["created_at"],
                        updated_at=current["updated_at"],
                        result=current["result"],
                    )
                )
            except Exception:
                # 字段缺失或类型错误时跳过该任务，不抛异常
                pass

        for line in text.splitlines():
            # 匹配任务标题行：## T001: 描述
            header = re.match(r"^##\s+(T\d+)[:：]\s*(.*)$", line)
            if header:
                # 先 flush 上一个任务
                _flush()
                current = {
                    "id": header.group(1),
                    "description": header.group(2).strip(),
                    "status": "pending",
                    "depends_on": [],
                    "created_at": "",
                    "updated_at": None,
                    "result": None,
                }
                continue
            if current is None:
                # 标题行之前的内容（如 "# 任务清单"）跳过
                continue
            # 匹配属性行
            if line.startswith("- 状态:"):
                val = line[len("- 状态:"):].strip()
                current["status"] = val or "pending"
            elif line.startswith("- 依赖:"):
                dep_str = line[len("- 依赖:"):].strip()
                if dep_str:
                    current["depends_on"] = [
                        d.strip() for d in dep_str.split(",") if d.strip()
                    ]
                else:
                    current["depends_on"] = []
            elif line.startswith("- 创建:"):
                current["created_at"] = line[len("- 创建:"):].strip()
            elif line.startswith("- 更新:"):
                val = line[len("- 更新:"):].strip()
                current["updated_at"] = val if val else None
            elif line.startswith("- 结果:"):
                val = line[len("- 结果:"):].strip()
                current["result"] = val if val else None

        # flush 最后一个任务
        _flush()
        return tasks
