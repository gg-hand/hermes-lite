"""multiagent 协作异常类。

对齐 hermes/agent/tool_error.py 的 @dataclass(kw_only=True) 风格。
所有异常继承 ToolError，stage=PROTOCOL（不走 tool_result 链路）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hermes.agent.tool_error import ErrorStage, ToolError


@dataclass(kw_only=True)
class CASConflictError(ToolError):
    """CAS 写入冲突（重试耗尽）。"""

    lock_name: str = ""
    expected_version: int = 0
    actual_version: int = 0
    tool_name: str = "multiagent_cas"
    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "cas_conflict"
    reason: str = ""
    suggestion: str = "重试 CAS 写入或走字段级合并降级"

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = (
                f"CAS conflict on '{self.lock_name}': "
                f"expected version {self.expected_version}, actual {self.actual_version}"
            )
        super().__post_init__()


@dataclass(kw_only=True)
class CASVersionMismatchError(ToolError):
    """CAS 版本不匹配（单次冲突，可重试）。"""

    expected: int = 0
    actual: int = 0
    tool_name: str = "multiagent_cas"
    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "cas_version_mismatch"
    reason: str = ""
    suggestion: str = "重读 status.json 后重试 CAS"

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = (
                f"CAS version mismatch: expected {self.expected}, actual {self.actual}"
            )
        super().__post_init__()


@dataclass(kw_only=True)
class FencingTokenMismatchError(ToolError):
    """fencing_token 不匹配（旧 token 幽灵写入）。"""

    lock_name: str = ""
    expected_token: int = 0
    actual_token: int = 0
    writer_id: str = ""
    tool_name: str = "multiagent_lock"
    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "fencing_token_mismatch"
    reason: str = ""
    suggestion: str = "重新获取锁以获得新 fencing_token"

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = (
                f"Fencing token mismatch on '{self.lock_name}': "
                f"expected {self.expected_token}, actual {self.actual_token} "
                f"(writer={self.writer_id})"
            )
        super().__post_init__()


@dataclass(kw_only=True)
class LockAcquisitionError(ToolError):
    """锁获取失败。"""

    lock_name: str = ""
    current_holder: str = ""
    tool_name: str = "multiagent_lock"
    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "lock_acquisition_failed"
    reason: str = "held_by_other"
    suggestion: str = "等待当前持锁者释放或加入 wait_queue"

    def __post_init__(self) -> None:
        base_reason = self.reason or "held_by_other"
        full_reason = f"Lock '{self.lock_name}' acquisition failed: {base_reason}"
        if self.current_holder:
            full_reason += f" (current_holder={self.current_holder})"
        self.reason = full_reason
        super().__post_init__()


@dataclass(kw_only=True)
class NotMyTurnError(ToolError):
    """非本机轮次（发言被阻断）。"""

    expected_agent: str = ""
    actual_agent: str = ""
    turn_started_at: str = ""
    tool_name: str = "multiagent_turn"
    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "not_my_turn"
    reason: str = ""
    suggestion: str = "等待轮次或写 messages.pending.md"

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = (
                f"Not my turn: expected={self.expected_agent}, actual={self.actual_agent} "
                f"(turn_started_at={self.turn_started_at})"
            )
        super().__post_init__()


@dataclass(kw_only=True)
class DirectorUnavailableError(ToolError):
    """Director 心跳超时（进入自治模式）。"""

    last_tick: str = ""
    age_seconds: float = 0.0
    tool_name: str = "multiagent_director"
    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "director_unavailable"
    reason: str = ""
    suggestion: str = "进入自治模式，等待 Director 恢复"

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = (
                f"Director unavailable: last_tick={self.last_tick}, "
                f"age={self.age_seconds:.1f}s"
            )
        super().__post_init__()


@dataclass(kw_only=True)
class GhostWriteAttemptError(ToolError):
    """幽灵写入尝试（旧 fencing_token 写入）。"""

    writer_id: str = ""
    lock_name: str = ""
    fencing_token: int = 0
    current_token: int = 0
    tool_name: str = "multiagent_lock"
    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "ghost_write_attempt"
    reason: str = ""
    suggestion: str = "重新获取锁"

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = (
                f"Ghost write by {self.writer_id} on '{self.lock_name}': "
                f"token={self.fencing_token}, current={self.current_token}"
            )
        super().__post_init__()


@dataclass(kw_only=True)
class CapabilityNotInCardError(ToolError):
    """工具不在 agent_card capabilities 中。"""

    tool_name: str = ""
    agent_id: str = ""
    declared_capabilities: list = None
    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "capability_not_in_card"
    reason: str = ""
    suggestion: str = "更新 agent_card capabilities 或禁用该工具"

    def __post_init__(self) -> None:
        if not self.reason:
            caps = self.declared_capabilities or []
            self.reason = (
                f"Tool '{self.tool_name}' not in capabilities of agent '{self.agent_id}': "
                f"declared={caps}"
            )
        super().__post_init__()


@dataclass(kw_only=True)
class A2AGatewayError(ToolError):
    """A2A Gateway 通信错误。"""

    endpoint: str = ""
    reason: str = ""
    tool_name: str = "a2a_gateway"
    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "a2a_gateway"
    suggestion: str = "检查远程 agent 状态或重试"

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = f"A2A gateway error: {self.endpoint}"
        super().__post_init__()


class PathSafetyError(Exception):
    """路径安全违规（绝对路径 / .. 穿越 / symlink 逃逸）。"""


# =============================================================================
# Phase 2 新增：Director 签名 + VerifyResult
# =============================================================================


@dataclass
class VerifyResult:
    """Director 签名验证结果（三级：ok / degraded / distrust）。

    level:
        - ok: 签名验证通过，failure_count 重置为 0
        - degraded: 单次失败或签名字段缺失（软约束），可继续执行
        - distrust: 连续失败达阈值（3 次），进入自治模式
    """

    level: Literal["ok", "degraded", "distrust"]
    reason: str
    failure_count: int = 0


@dataclass(kw_only=True)
class DirectorSignatureError(ToolError):
    """Director 签名验证失败（衔接 VerifyResult.degraded / distrust）。"""

    level: Literal["degraded", "distrust"] = "degraded"
    failure_count: int = 0
    threshold: int = 3
    tool_name: str = "director_engine"
    category: str = "director_signature_failed"
    stage: ErrorStage = ErrorStage.PROTOCOL
    suggestion: str = "连续失败达阈值时进入自治模式"
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = (
                f"director signature verification failed "
                f"(level={self.level}, failure_count={self.failure_count}/{self.threshold})"
            )
        super().__post_init__()

