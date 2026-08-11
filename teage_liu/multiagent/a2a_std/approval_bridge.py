"""审批请求 ↔ input-required 桥。

标准 A2A 的 input-required 是非终态中断状态：agent 需要调用方补充输入。
本项目的人机闭环是 ApprovalManager（危险工具审批）。桥接：
- 每次 ApprovalManager.create_request → 把关联的活跃主会话 Task 置为
  input-required，并写 collab 标记（worker 侧可见）。
- 审批通过后 worker 的后续 response 自然推进 Task 到 completed。

实现为包装实例方法（不改 approval.py 本体）；通知在后台线程执行，
避免阻塞审批调用链。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class A2AApprovalBridge:
    """审批 → input-required 桥。"""

    def __init__(
        self,
        approval_manager: Any,
        task_manager: Any,
        bb_root: Path,
    ) -> None:
        self._inner = approval_manager
        self._task_manager = task_manager
        self._bb_root = bb_root
        self._installed = False
        self._enabled = True

    def install(self) -> None:
        """包装 create_request（幂等）。"""
        if self._installed:
            return
        orig = self._inner.create_request

        def wrapped(
            tool_name: str,
            tool_input: dict,
            reason: str,
            risk_level: str,
            tool_kind: str = "generic",
        ) -> str:
            approval_id = orig(tool_name, tool_input, reason, risk_level, tool_kind)
            if self._enabled:
                self._notify_input_required()
            return approval_id

        self._inner.create_request = wrapped  # type: ignore[method-assign]
        self._installed = True
        logger.info("A2AApprovalBridge 已安装（审批 → input-required）")

    def uninstall(self) -> None:
        self._installed = False
        self._enabled = False

    def _notify_input_required(self) -> None:
        """后台线程异步推进（create_request 在工具线程被调用，无运行中 loop）。"""
        threading.Thread(target=self._apply_sync, daemon=True).start()

    def _apply_sync(self) -> None:
        try:
            asyncio.run(self._apply_input_required())
        except Exception as e:
            logger.warning("A2AApprovalBridge 通知失败: %s", e)

    async def _apply_input_required(self) -> None:
        """找非终态、main_* 上下文的 Task → input-required + collab 标记。"""
        from teage_liu.multiagent.a2a_std.models import TaskState
        from teage_liu.multiagent.blackboard import append_collab_message

        tasks = await self._task_manager.list_tasks()
        target = next((
            t for t in tasks
            if not t.status.state.is_terminal
            and t.context_id and str(t.context_id).startswith("main_")
        ), None)
        if target is None:
            return
        await self._task_manager.update_state(target.id, TaskState.INPUT_REQUIRED)
        try:
            await append_collab_message(
                self._bb_root,
                {
                    "from": "a2a_bridge", "to": "*", "type": "status",
                    "status": "input_required",
                    "content": "需要用户审批/补充输入",
                    "message_id": f"bridge_ir_{target.id}",
                },
                collab_id=target.context_id,
            )
        except Exception as e:
            logger.warning("input-required collab 标记写入失败: %s", e)
