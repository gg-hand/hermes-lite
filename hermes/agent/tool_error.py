"""统一工具错误异常层次。

设计目标：
- 用结构化异常替换字符串错误载体，携带 ``stage``/``category``/``reason``/``suggestion``
  四元组，让上层（ReactLoop）能按错误阶段分流处理。
- ``pre_execution`` 阶段错误：handler 未执行，详情走 system 注入，tool_result 仅返回
  占位 ``"已拦截，详见系统提示"``。
- ``execution`` 阶段错误：handler 已执行后失败，结构化收据进 tool_result。
- ``protocol`` 阶段错误：协议层（孤立 tool_result、LLM 调用失败），不进 tool_result 链路。

零外部依赖（仅标准库 enum/dataclass/typing/subprocess），避免循环导入。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ErrorStage(Enum):
    """错误阶段。"""

    PRE_EXECUTION = "pre_execution"  # 触发前拦截（handler 未执行）
    EXECUTION = "execution"          # 执行中失败（handler 已执行）
    PROTOCOL = "protocol"            # 协议层错误


# 错误分类 → 中文标签映射（监控面板与回执文本共用）
_CATEGORY_ZH = {
    # pre_execution
    "param_error": "参数错误",
    "tool_not_found": "工具未找到",
    "policy_denied": "策略拒绝",
    "user_rejected": "用户拒绝",
    "non_stream_hil": "非流式不支持审批",
    "stuck_detected": "卡死检测",
    "cancelled": "已取消",
    # execution
    "not_found": "资源不存在",
    "permission": "权限不足",
    "timeout": "执行超时",
    "transient": "临时错误",
    "permanent": "永久错误",
    "anti_crawler": "反爬虫",
    "auth_required": "需认证",
    "internal_error": "内部错误",
    # protocol
    "orphan_tool_result": "孤立工具结果",
    "llm_failure": "LLM 调用失败",
    # hook 层（9.2）
    "hook_abort": "校验终止",
    "validation_error": "校验失败",
    "workflow_execution_error": "工作流执行失败",
    # multiagent 协作（v1.0.3 新增）
    "cas_conflict": "CAS 冲突",
    "cas_version_mismatch": "CAS 版本不匹配",
    "fencing_token_mismatch": "fencing token 不匹配",
    "lock_acquisition_failed": "锁获取失败",
    "not_my_turn": "非本机轮次",
    "director_unavailable": "Director 不可用",
    "ghost_write_attempt": "幽灵写入",
    "capability_not_in_card": "能力未声明",
    "director_signature_failed": "Director 签名失败",
    "injection_suspected": "注入嫌疑",
    "message_truncated": "消息截断",
    "path_normalized": "路径已规范化",
    "schema_validation_error": "Schema 校验失败",
    "disk_full": "磁盘满",
    "read_only_fs": "只读文件系统",
    "clock_drift": "时钟漂移",
    "watchdog_self_test_failed": "watchdog 自检失败",
    "recovery_fence_timeout": "恢复期 fence 超时",
    "lock_force_release": "锁强制释放",
    "a2a_gateway": "A2A 网关错误",
    # 旧值兼容（ErrorClassifier 历史 classification）
    "unknown": "未知",
    "success": "成功",
}


@dataclass(kw_only=True)
class ToolError(Exception):
    """所有工具错误的基类。

    Attributes:
        tool_name: 触发错误的工具名。
        stage: 错误阶段（pre_execution/execution/protocol）。
        category: 错误分类字符串（与监控 ``ERROR_TYPE_META`` key 对齐）。
        reason: 一行原因（人类可读，不含 traceback/内部变量名）。
        suggestion: 一行建议（可执行动作）。
    """

    tool_name: str
    stage: ErrorStage
    category: str
    reason: str
    suggestion: str

    def __post_init__(self) -> None:
        # dataclass + Exception 兼容：调用 Exception.__init__ 以确保 ``str(exc)`` 可用
        super().__init__(f"[{self.category}] {self.reason}")

    def to_receipt(self) -> str:
        """生成 execution 阶段的 tool_result 收据文本。"""
        return (
            f"[失败] {self._category_zh()}\n"
            f"原因：{self.reason}\n"
            f"建议：{self.suggestion}"
        )

    def to_system_block(self) -> str:
        """生成 pre_execution 阶段的系统注入块文本。"""
        return (
            f"[拦截] {self.tool_name} → {self._category_zh()}\n"
            f"原因：{self.reason}\n"
            f"建议：{self.suggestion}"
        )

    def _category_zh(self) -> str:
        return _CATEGORY_ZH.get(self.category, self.category)


# =============================================================================
# pre_execution 阶段（7 个）— 工具未执行
# =============================================================================


@dataclass(kw_only=True)
class ParamError(ToolError):
    """参数校验失败（schema 不匹配/多余字段/缺必填）。"""

    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "param_error"


@dataclass(kw_only=True)
class ToolNotFoundError(ToolError):
    """工具未注册/已禁用/Deferred 未加载。"""

    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "tool_not_found"


@dataclass(kw_only=True)
class PolicyDeniedError(ToolError):
    """策略引擎拒绝执行。"""

    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "policy_denied"


@dataclass(kw_only=True)
class UserRejectedError(ToolError):
    """HIL 审批被用户拒绝。"""

    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "user_rejected"


@dataclass(kw_only=True)
class NonStreamHILError(ToolError):
    """非流式模式遇 confirm 操作（自动拒绝）。"""

    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "non_stream_hil"


@dataclass(kw_only=True)
class StuckDetectedError(ToolError):
    """重复调用卡死检测命中。"""

    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "stuck_detected"


@dataclass(kw_only=True)
class CancelledError(ToolError):
    """用户取消执行（cancel_event 触发）。"""

    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    category: str = "cancelled"


# =============================================================================
# execution 阶段（8 个）— 工具已执行但失败
# =============================================================================


@dataclass(kw_only=True)
class NotFoundError(ToolError):
    """资源不存在（文件/目录/记录/URL 404）。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "not_found"


@dataclass(kw_only=True)
class PermissionDeniedError(ToolError):
    """OS 层权限不足。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "permission"


@dataclass(kw_only=True)
class ToolTimeoutError(ToolError):
    """执行超时（subprocess/网络）。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "timeout"


@dataclass(kw_only=True)
class TransientError(ToolError):
    """临时性失败（HTTP 5xx、网络抖动），可重试。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "transient"


@dataclass(kw_only=True)
class PermanentError(ToolError):
    """永久性失败（HTTP 4xx、逻辑错误），重试无效。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "permanent"


@dataclass(kw_only=True)
class AntiCrawlerError(ToolError):
    """触发目标网站反爬虫机制（403/412）。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "anti_crawler"


@dataclass(kw_only=True)
class AuthRequiredError(ToolError):
    """需要认证（401）。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "auth_required"


@dataclass(kw_only=True)
class InternalError(ToolError):
    """工具内部错误（兜底：非 ToolError 异常）。"""

    stage: ErrorStage = ErrorStage.EXECUTION
    category: str = "internal_error"


# =============================================================================
# protocol 阶段（2 个）— 协议层错误
# =============================================================================


@dataclass(kw_only=True)
class OrphanToolResultError(ToolError):
    """孤立 tool_result 无法匹配 tool_use（消息序列完整性问题）。"""

    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "orphan_tool_result"


@dataclass(kw_only=True)
class LLMFailureError(ToolError):
    """LLM 调用失败（流式 403/超时/异常）。"""

    stage: ErrorStage = ErrorStage.PROTOCOL
    category: str = "llm_failure"


# =============================================================================
# hook 层（3 个，9.2）— 校验终止 / spec 校验失败 / workflow 执行失败
# =============================================================================


@dataclass(kw_only=True)
class HookAbortError(ToolError):
    """校验层主动终止执行（9.2）。

    ValidateHook 等校验层 hook 在检测到致命问题时抛出，携带 ``errors`` 列表
    供上层日志与通知模板读取。
    """

    errors: list = field(default_factory=list)
    tool_name: str = "hook"
    category: str = "hook_abort"
    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    suggestion: str = "检查校验错误列表"


@dataclass(kw_only=True)
class ValidationError(ToolError):
    """workflow spec 校验失败（9.2）。

    ``validate_workflow_spec`` 在 spec 结构非法时抛出，``reason`` 由
    ``errors`` 列表 join 生成（无需显式传入）。
    """

    errors: list = field(default_factory=list)
    tool_name: str = "validate_hook"
    category: str = "validation_error"
    stage: ErrorStage = ErrorStage.PRE_EXECUTION
    suggestion: str = "修正 workflow spec 配置"
    reason: str = ""  # __post_init__ 从 errors 计算

    def __post_init__(self) -> None:
        if not self.reason and self.errors:
            self.reason = "; ".join(self.errors)
        super().__post_init__()


@dataclass(kw_only=True)
class WorkflowExecutionError(ToolError):
    """workflow 执行失败（Q3 决策 D，9.2）。

    WorkflowEngine.execute 末尾 ``if not result.success`` 时抛出，
    携带完整 WorkflowResult（含 step_traces），供 RetryHook 读取失败 step 的
    error_class 判断 permanent/transient。
    """

    result: Any = None
    tool_name: str = "workflow_engine"
    category: str = "workflow_execution_error"
    stage: ErrorStage = ErrorStage.EXECUTION
    suggestion: str = "检查 step_traces 中的失败 step"
    reason: str = ""  # __post_init__ 从 result.errors 计算

    def __post_init__(self) -> None:
        if not self.reason and self.result is not None:
            errors = getattr(self.result, "errors", None) or []
            self.reason = "; ".join(errors) if errors else "workflow 执行失败"
        super().__post_init__()


# =============================================================================
# 异常归一化工厂
# =============================================================================


def from_exception(tool_name: str, exc: Exception) -> ToolError:
    """将非 ToolError 异常转换为对应 ToolError 子类。

    registry 兜底用：handler 抛出非 ToolError 异常时，由此工厂归一化为
    具体的 ToolError 子类，避免上层 ReactLoop 拿到原始异常无法分类。

    参数:
        tool_name: 触发异常的工具名。
        exc: 原始异常。

    返回:
        对应的 ToolError 子类实例。
    """
    # 显式类型映射优先
    if isinstance(exc, FileNotFoundError):
        return NotFoundError(
            tool_name=tool_name,
            reason=f"文件不存在：{exc.filename or '未知路径'}",
            suggestion="检查路径或用 file_glob 查找",
        )
    if isinstance(exc, IsADirectoryError):
        return NotFoundError(
            tool_name=tool_name,
            reason=f"目标是目录而非文件：{exc.filename or '未知路径'}",
            suggestion="改用 file_listdir 列出目录内容",
        )
    if isinstance(exc, PermissionError):
        return PermissionDeniedError(
            tool_name=tool_name,
            reason=f"权限不足：{exc.filename or '未知路径'}",
            suggestion="检查文件权限或换路径",
        )
    if isinstance(exc, subprocess.TimeoutExpired):
        timeout = getattr(exc, "timeout", "?")
        return ToolTimeoutError(
            tool_name=tool_name,
            reason=f"执行超时（{timeout}秒）",
            suggestion="增大 timeout 或拆分任务",
        )
    if isinstance(exc, TimeoutError):
        return ToolTimeoutError(
            tool_name=tool_name,
            reason="执行超时",
            suggestion="增大 timeout 或拆分任务",
        )
    if isinstance(exc, KeyError):
        return PermanentError(
            tool_name=tool_name,
            reason=f"缺少必要字段：{exc.args[0] if exc.args else '未知'}",
            suggestion="检查输入参数是否完整",
        )
    if isinstance(exc, (TypeError, ValueError)):
        # handler 接口/参数错误（多见于幻觉性传参的兜底）
        return PermanentError(
            tool_name=tool_name,
            reason=f"参数错误：{exc}",
            suggestion="检查参数类型与工具 schema",
        )
    # 兜底
    return InternalError(
        tool_name=tool_name,
        reason=f"{type(exc).__name__}: {exc}",
        suggestion="简化输入或联系管理员",
    )


def from_cron_error(tool_name: str, err_dict: dict) -> ToolError:
    """将 cron_tool 子进程返回的错误 JSON 映射到 ToolError 子类。

    cron_tool_loader._format_error 返回形如
    ``{"error": "...", "error_type": "timeout"}`` 的 JSON 字符串，
    主进程 cron_tool_registry 检测到此格式后调用本工厂归一化。

    参数:
        tool_name: 触发异常的 cron_tool 名。
        err_dict: 子进程返回的错误 dict，含 ``error`` 与可选 ``error_type``。

    返回:
        对应的 ToolError 子类实例。
    """
    err_type = (err_dict.get("error_type") or "").lower()
    err_msg = err_dict.get("error") or "cron_tool 执行失败"

    mapping = {
        "timeout": ToolTimeoutError(
            tool_name=tool_name,
            reason=f"cron_tool 超时：{err_msg}",
            suggestion="增大 timeout 或拆分任务",
        ),
        "not_found": NotFoundError(
            tool_name=tool_name,
            reason=f"cron_tool 资源不存在：{err_msg}",
            suggestion="检查路径或参数",
        ),
        "permission": PermissionDeniedError(
            tool_name=tool_name,
            reason=f"cron_tool 权限不足：{err_msg}",
            suggestion="检查文件权限",
        ),
        "permanent": PermanentError(
            tool_name=tool_name,
            reason=f"cron_tool 永久失败：{err_msg}",
            suggestion="勿以同参数重试",
        ),
        "transient": TransientError(
            tool_name=tool_name,
            reason=f"cron_tool 临时失败：{err_msg}",
            suggestion="稍后重试",
        ),
    }
    return mapping.get(
        err_type,
        InternalError(
            tool_name=tool_name,
            reason=f"cron_tool 内部错误：{err_msg}",
            suggestion="简化输入或联系管理员",
        ),
    )


# execution 阶段所有 category 集合（用于 recent_tool_calls 计数判定）
EXECUTION_ERROR_CATEGORIES = frozenset(
    {
        "not_found",
        "permission",
        "timeout",
        "transient",
        "permanent",
        "anti_crawler",
        "auth_required",
        "internal_error",
    }
)

# pre_execution 阶段所有 category 集合
PRE_EXECUTION_ERROR_CATEGORIES = frozenset(
    {
        "param_error",
        "tool_not_found",
        "policy_denied",
        "user_rejected",
        "non_stream_hil",
        "stuck_detected",
        "cancelled",
    }
)
