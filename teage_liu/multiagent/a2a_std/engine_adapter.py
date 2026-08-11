"""标准 A2A 面 ↔ 内部引擎（blackboard/director/worker）适配层。

- A2AEngineAdapter：把标准 Message 映射为内部 collab 消息写入 blackboard；
  task 生命周期由 A2ATaskManager 驱动。
- TaskDriver：后台轮询（只读），把 collab 文件中的进展映射为标准 Task
  状态/artifacts/终态并发布 SSE 事件。写操作仍归 worker/director 引擎。

映射表（标准 ↔ 内部）：
  task.id            t_<hex12>（服务端生成）
  task.contextId     collab_id / session_id（缺省生成 a2a_<hex12>）
  submitted          create_task（暂不写文件）
  working            首个内部 request 写入 collabs/{cid}.md
  input-required     内部 status:"input_required"
  completed          内部 consensus/end，或 status/result:"completed"
  failed             内部 error 或 result:"failed"
  canceled           tasks/cancel → 内部 cancelled 标记 + archive_collab
  artifacts[]        每个内部 response/result → Artifact("response_<seq>")
  history[]          collab 消息转换为标准 Message
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Optional

from teage_liu.multiagent.a2a_std.models import (
    Artifact,
    DataPart,
    Message,
    Task,
    TaskState,
    TextPart,
)
from teage_liu.multiagent.a2a_std.task_manager import A2ATaskManager
from teage_liu.multiagent.blackboard import (
    append_collab_message,
    archive_collab,
    read_collab_messages,
)

logger = logging.getLogger(__name__)

# blackboard from 字段约束：^[a-zA-Z0-9_]{3,32}$|^(user|director)$
_FROM_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$|^(user|director)$")


def _safe_from(value: str) -> str:
    """把任意 from 值规整为 blackboard 合法格式。"""
    candidate = re.sub(r"[^a-zA-Z0-9_]", "_", value)[:32]
    if candidate.isdigit() or not candidate:
        candidate = f"a2a_{candidate}" if candidate else "a2a_remote"
    if _FROM_RE.match(candidate) and len(candidate) >= 3:
        return candidate
    return "a2a_remote"


class A2AEngineAdapter:
    """标准 A2A ↔ 内部引擎适配层。"""

    def __init__(
        self,
        bb_root: Path,
        config: dict,
        task_manager: A2ATaskManager,
    ) -> None:
        self._bb_root = bb_root
        self._config = config
        self._task_manager = task_manager
        self._driver: Optional[TaskDriver] = None
        a2a_cfg = config.get("a2a", {}) or {}
        self._std_cfg = a2a_cfg.get("standard", {}) or {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 TaskDriver 后台轮询。"""
        if self._driver is None:
            self._driver = TaskDriver(self._bb_root, self._task_manager, self._config)
            await self._driver.start()
            logger.info("A2A TaskDriver 已启动")

    async def tick_once(self) -> None:
        """手动触发一轮 driver 扫描（测试用；未 start 时也生效）。"""
        if self._driver is not None:
            await self._driver.tick_once()
        else:
            driver = TaskDriver(self._bb_root, self._task_manager, self._config)
            await driver.tick_once()

    async def stop(self) -> None:
        if self._driver is not None:
            await self._driver.stop()
            self._driver = None
            logger.info("A2A TaskDriver 已停止")

    # ------------------------------------------------------------------
    # 标准方法适配
    # ------------------------------------------------------------------

    async def handle_message_send(
        self, message: Message, context_id: Optional[str] = None
    ) -> dict:
        """message/send：建 task（submitted）→ 写 collab → working。"""
        if not context_id:
            context_id = f"a2a_{uuid.uuid4().hex[:12]}"

        task = await self._task_manager.create_task(message, context_id)
        internal = self._to_internal(message, task.id, context_id)

        try:
            await append_collab_message(self._bb_root, internal, collab_id=context_id)
        except Exception as e:
            logger.error("collab 写入失败 (task=%s): %s", task.id, e)
            await self._task_manager.update_state(
                task.id, TaskState.FAILED, metadata={"_error": str(e)}
            )
            raise

        await self._task_manager.update_state(task.id, TaskState.WORKING)
        task = await self._task_manager.get_task(task.id)
        return task.model_dump(by_alias=True)

    async def handle_task_cancel(
        self, task_id: str, context_id: Optional[str] = None
    ) -> dict:
        """tasks/cancel：置终态 + 归档关联 collab。"""
        task = await self._task_manager.cancel_task(task_id, context_id)
        if task.context_id:
            try:
                await archive_collab(self._bb_root, task.context_id)
            except Exception as e:
                logger.warning("collab 归档失败 (task=%s): %s", task_id, e)
        return task.model_dump(by_alias=True)

    # ------------------------------------------------------------------
    # 内部映射
    # ------------------------------------------------------------------

    def _to_internal(self, message: Message, task_id: str, context_id: str) -> dict:
        """标准 Message → 内部 collab 消息。"""
        texts = [p.text for p in message.parts if isinstance(p, TextPart)]
        data_parts = [p.data for p in message.parts if isinstance(p, DataPart)]
        meta = message.metadata or {}

        internal: dict = {
            "from": _safe_from(str(meta.get("from") or "a2a_remote")),
            "to": str(meta.get("to") or "*"),
            "type": str(meta.get("type") or "request"),
            "content": "\n".join(texts) or str(meta.get("content") or ""),
            "message_id": message.message_id,
            "task_op_id": task_id,
            "collab_id": context_id,
            "channel": str(meta.get("channel") or "a2a_std"),
            "via": "a2a",
        }
        if data_parts:
            internal["structured"] = data_parts[0]
        return internal


class TaskDriver:
    """后台轮询器：把 collab 文件进展映射为 Task 状态/artifacts 并发布事件。

    只读 blackboard；写仅经由 A2ATaskManager.update_state（task 记录）。
    """

    def __init__(self, bb_root: Path, task_manager: A2ATaskManager, config: dict) -> None:
        self._bb_root = bb_root
        self._task_manager = task_manager
        a2a_cfg = config.get("a2a", {}) or {}
        std_cfg = a2a_cfg.get("standard", {}) or {}
        self._interval = float(std_cfg.get("driver_interval_seconds", 1.0))
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def tick_once(self) -> None:
        """执行一轮扫描（测试/手动触发用，确定性）。"""
        await self._tick_all()

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick_all()
            except Exception as e:
                logger.warning("TaskDriver tick 异常: %s", e)
            await asyncio.sleep(self._interval)

    async def _tick_all(self) -> None:
        for task in await self._task_manager.list_tasks():
            if task.status.state.is_terminal or not task.context_id:
                continue
            try:
                await self._tick(task)
            except Exception as e:
                logger.warning("TaskDriver 处理 task=%s 异常: %s", task.id, e)

    async def _tick(self, task: Task) -> None:
        msgs = await read_collab_messages(self._bb_root, collab_id=task.context_id)
        applied = int((task.metadata or {}).get("_applied_seq", 0))
        new = [m for m in msgs if isinstance(m.get("seq"), int) and m["seq"] > applied]
        if not new:
            return
        new.sort(key=lambda m: m["seq"])
        max_seq = max(m["seq"] for m in new)

        terminal: Optional[TaskState] = None
        input_required = False
        new_artifacts = list(task.artifacts)

        for m in new:
            msg_type = m.get("type")
            status = m.get("status")
            if msg_type in ("consensus", "end"):
                terminal = TaskState.COMPLETED
            elif msg_type == "error" or status == "failed":
                terminal = TaskState.FAILED
            elif status == "completed":
                terminal = TaskState.COMPLETED
            elif status == "cancelled":
                terminal = TaskState.CANCELED
            elif status == "rejected":
                terminal = TaskState.REJECTED
            elif status == "input_required":
                input_required = True
            if msg_type in ("response", "result") and m.get("content"):
                new_artifacts.append(
                    Artifact(
                        name=f"response_{m['seq']}",
                        mime_type="text/plain",
                        parts=[TextPart(text=str(m["content"]))],
                        append=True,
                    )
                )

        metadata = {**(task.metadata or {}), "_applied_seq": max_seq}
        if terminal is not None:
            await self._task_manager.update_state(
                task.id, terminal, artifacts=new_artifacts, metadata=metadata
            )
        elif input_required:
            await self._task_manager.update_state(
                task.id, TaskState.INPUT_REQUIRED,
                artifacts=new_artifacts, metadata=metadata,
            )
        else:
            # 有新的 response/result 则附带 artifacts；无则仅推进水位
            new_state = TaskState.WORKING
            kwargs: dict = {"metadata": metadata}
            if len(new_artifacts) > len(task.artifacts):
                kwargs["artifacts"] = new_artifacts
            await self._task_manager.update_state(task.id, new_state, **kwargs)
