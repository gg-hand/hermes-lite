"""Phase 8 Cron 隔离层上下文定义。

定义 :class:`CronIsolation` 数据结构，用于 cron 调度会话与用户会话的隔离。
Orchestrator 检测 ``session_id`` 以 ``cron:`` 开头时构建 CronIsolation context，
传递给 ContextManager.build_cron_context 构建隔离的 prompt 上下文。

隔离策略（5 条缓存硬约束之一：system_text 禁含动态变量）：
- cron system_text = SYSTEM_PROMPT + 工作流模板固定 prompt（不含 memory.md 用户画像）
- cron messages[0] = 时间上下文 + 检索记忆（cron namespace）+ 工作流数据
- cron 路径不注入 TaskManager 进度（inject_todo=False）
- cron 路径不注入用户画像（inject_profile=False）
- 检索记忆按 namespace=cron + cron_id 过滤，与用户会话记忆互不可见
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CronIsolation:
    """cron 调度会话的隔离上下文。

    由 Orchestrator 在检测到 ``session_id`` 以 ``cron:`` 开头时构建，
    传递给 :meth:`ContextManager.build_cron_context` 构建隔离的 prompt。

    属性:
        cron_id: 调度项 ID（= session_id 去掉 ``cron:`` 前缀）。
            用于记忆检索/沉淀的 namespace 隔离（``namespace="cron"`` +
            ``cron_id=<id>``），不同调度项之间记忆互不可见。
        namespace: 固定为 ``"cron"``（用户会话为 ``"user"``）。
        inject_profile: 是否注入用户画像（memory.md）。cron 路径固定为
            ``False``——cron 调度不应感知用户画像，且 memory.md 异步更新
            会让 system_text 缓存命中区失效（5 条缓存硬约束之一）。
        inject_todo: 是否注入 TaskManager 进度。cron 路径固定为 ``False``
            ——cron 调度不持有 plan 模式 todo，且任务进度是动态变量会
            破坏缓存稳定性。
        consolidate: 是否在 cron 调度结束后触发记忆沉淀。默认 ``True``，
            可由调度项配置 ``generate_llm_summary`` 等覆盖。
    """

    cron_id: str
    namespace: str = "cron"
    inject_profile: bool = False
    inject_todo: bool = False
    consolidate: bool = True

    @staticmethod
    def from_session_id(session_id: Optional[str]) -> Optional["CronIsolation"]:
        """从 session_id 解析 CronIsolation context。

        ``session_id`` 以 ``cron:`` 开头时返回 CronIsolation 实例
        （``cron_id`` 取前缀之后的部分）；其他值或 None 返回 None
        （表示用户会话，不走隔离路径）。

        参数:
            session_id: 会话 ID。

        返回:
            CronIsolation 实例（cron 会话）或 None（用户会话）。
        """
        if (
            session_id
            and isinstance(session_id, str)
            and session_id.startswith("cron:")
        ):
            cron_id = session_id[5:]
            if not cron_id:
                # cron: 前缀但 cron_id 为空，视为无效，降级到用户会话
                return None
            return CronIsolation(cron_id=cron_id)
        return None
