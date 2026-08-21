"""audit 观测枝干:第一个实验性扩展(M2 轻量观测,验证接入链路)。

声明 ``observe`` 能力(只读):钩子一律返回 [] 不干预对话,数据经
``host_port.storage_write`` 消息通道落宿主存储(kind 带 ``audit.`` 前缀)。

记录三类事件:
- ``audit.steps``:每轮 LLM step 摘要(after_step,L2 StepSummary)
- ``audit.conversations``:对话完成摘要(after,终态;含终止原因)
- ``audit.errors``:对话失败记录(on_error,终态)

实验发现(2026-08-21):同语言 observe 扩展的 L3 观测投递未接线
(supervisor 仅对 stdio 异语言扩展做 l3_sink.subscribe)——本枝干刻意走
L2 通道(钩子摘要),不依赖 L3;缺口另记 CORE-缺口记录.md。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from teage_liu2.core.actions import Action
from teage_liu2.core.hooks import Branch
from teage_liu2.core.types import AfterResponse, Snapshot, StepSummary

logger = logging.getLogger(__name__)


class AuditBranch(Branch):
    """对话审计观测枝干:只读快照,经 host_port 落盘审计记录。"""

    name = "audit"
    #: observe 只读(§15-A4③):L3 订阅声明 + 钩子返回 action 一律忽略
    capabilities: List[str] = ["observe"]

    def __init__(self, config: dict) -> None:
        self._config: Dict[str, Any] = dict(config or {})
        self.record_steps: bool = bool(self._config.get("record_steps", True))

    async def setup(self, config: dict, host: Any) -> None:
        """枝干配置自校验(F2):配置开了却坏了 → 启动失败。"""
        if not isinstance(self.record_steps, bool):
            raise ValueError("audit.record_steps 必须是布尔值")

    # ------------------------------------------------------------------
    # 内部:经 host_port 消息通道落盘(observe 只读,不返回 action)
    # ------------------------------------------------------------------
    async def _write(self, kind: str, doc: Dict[str, Any]) -> None:
        """best-effort 落盘:失败仅告警,绝不向上抛(单扩展故障不影响对话)。"""
        if self.host_port is None:
            logger.warning("audit 未注入 host_port,跳过 %s 落盘", kind)
            return
        try:
            # kind 必须带 "audit." 前缀(§15-A3 跨前缀拒绝)
            await self.host_port.storage_write(kind, [doc])
        except Exception as e:
            logger.warning("audit 落盘失败(kind=%s): %s", kind, e)

    # ------------------------------------------------------------------
    # 钩子(全部返回 [],observe 只读约束)
    # ------------------------------------------------------------------
    async def after_step(self, snapshot: Snapshot, summary: StepSummary) -> List[Action]:
        """每轮 step 摘要(非终态钩子;observe 返回 action 会被忽略,故返回 [])。"""
        if not self.record_steps:
            return []
        await self._write("audit.steps", {
            "session_id": snapshot.session_id,
            "round": summary.round,
            "text": summary.text[:200],
            "usage": summary.usage,
            "duration": summary.duration,
        })
        return []

    async def after(self, snapshot: Snapshot, response: AfterResponse) -> List[Action]:
        """对话完成摘要(终态钩子:action 一律忽略,落盘走消息通道)。"""
        done = (response.done_event or {})
        await self._write("audit.conversations", {
            "session_id": snapshot.session_id,
            "user_input": snapshot.user_input[:200],
            "response_text": response.text[:200],
            "termination_reason": done.get("termination_reason", ""),
            "rounds": snapshot.round,
        })
        return []

    async def on_error(self, snapshot: Snapshot, error: Any) -> List[Action]:
        """对话失败记录(终态钩子)。"""
        await self._write("audit.errors", {
            "session_id": snapshot.session_id,
            "user_input": snapshot.user_input[:200],
            "error": str(error)[:500] if error is not None else "",
            "round": snapshot.round,
        })
        return []
