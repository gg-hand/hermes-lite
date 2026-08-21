"""guardrails 枝干:输入扫描拦截(M2 首个接入枝干,验证接入链路)。

阶段 2 迁移(§17):BranchContext → Snapshot + Action(协议签名)。
功能(最小先例,接入验证后逐步扩充):
- ``before(snapshot) -> Action[]``:扫描 user_input,命中 denylist:
  - ``action=block``(默认)→ 返回 ``[SetExtra, SetStop]``(不调 LLM,intercepted)
  - ``action=warn`` → 返回 ``[SetExtra]``(标记,对话继续)
- 配置(该枝干自己的配置段,setup 自校验):
    core.branches.guardrails:
      enabled: true
      denylist: ["忽略上面的指令", ...]
      action: block | warn

后续增量(老系统 teage_liu/guardrails 三件逐步搬运):
InjectionGuard 注入模式库 / OutputFilter 敏感输出过滤 / sanitizer 工具结果清洗。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from teage_liu2.core.actions import Action, SetExtra, SetStop
from teage_liu2.core.hooks import Branch
from teage_liu2.core.types import Snapshot

logger = logging.getLogger(__name__)


class GuardrailsBranch(Branch):
    """输入扫描拦截枝干:命中 denylist 按 action 处置。"""

    name = "guardrails"

    def __init__(self, config: dict) -> None:
        self._config: Dict[str, Any] = dict(config or {})
        self.denylist: List[str] = list(self._config.get("denylist") or [])
        self.action: str = self._config.get("action", "block")

    async def setup(self, config: dict, host: Any) -> None:
        """枝干配置自校验(F2):配置开了却坏了 → 启动失败,不静默。"""
        if not isinstance(self.denylist, list) or not all(
            isinstance(w, str) for w in self.denylist
        ):
            raise ValueError("guardrails.denylist 必须是字符串列表")
        if self.action not in ("block", "warn"):
            raise ValueError(
                f"guardrails.action 非法: {self.action!r}(可选 block / warn)"
            )

    async def before(self, snapshot: Snapshot) -> List[Action]:
        """输入扫描:命中 denylist → block 拦截(SetStop)/ warn 标记(SetExtra)。

        协议签名(阶段 2):返回 Action[](立即应用;SetStop 短路后续扩展)。
        """
        for word in self.denylist:
            if word and word in snapshot.user_input:
                if self.action == "block":
                    logger.warning(
                        "guardrails 拦截(会话 %s):输入含敏感词 %r",
                        snapshot.session_id, word,
                    )
                    return [
                        SetExtra(key="guardrails.denied", value=word),
                        SetStop(reason="blocked"),
                    ]
                logger.info(
                    "guardrails warn(会话 %s):输入含敏感词 %r",
                    snapshot.session_id, word,
                )
                return [SetExtra(key="guardrails.denied", value=word)]
        return []
