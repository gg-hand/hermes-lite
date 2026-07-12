"""工具执行器：从 ReactLoop 提取的工具执行逻辑。

封装以下职责：
- 参数 hash 计算（重试检测去重）
- 工具调用卡死检测
- 策略评估（PolicyEngine 拦截）
- 审计日志记录
- 工具派发执行（cron_tool_registry → tool_registry）

模块级函数 ``compute_params_hash`` / ``detect_tool_stuck`` /
``build_stuck_message`` / ``extract_schedule_id`` 为纯函数，无副作用，
可独立测试。``ToolExecutor`` 类封装有状态的工具执行逻辑（依赖
tool_registry / policy_engine / audit_logger）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

if TYPE_CHECKING:
    from .audit import AuditLogger
    from .policy import PolicyEngine

# Phase 5: 策略模块（运行时需要 Decision 实例化）
try:
    from .policy import Decision
except ImportError:  # pragma: no cover
    try:
        from agent.policy import Decision  # type: ignore
    except ImportError:
        from dataclasses import dataclass
        from typing import Literal

        @dataclass
        class Decision:  # type: ignore
            action: Literal["allow", "confirm", "deny"]
            reason: str
            risk_level: Literal["low", "medium", "high"]

# 统一工具错误异常层次
try:
    from .tool_error import ToolError, ToolNotFoundError
except ImportError:
    try:
        from agent.tool_error import ToolError, ToolNotFoundError  # type: ignore
    except ImportError:  # pragma: no cover
        ToolError = None  # type: ignore
        ToolNotFoundError = None  # type: ignore

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 纯函数（无副作用，可独立测试）
# ──────────────────────────────────────────────────────────────────


def compute_params_hash(tool_input: dict) -> str:
    """计算工具输入参数的 hash（用于重试检测去重）。

    使用 ``md5(json.dumps(tool_input, sort_keys=True))`` 保证同一 dict
    任意顺序的 key 都得到相同 hash。``tool_input`` 为 ``None`` 或非
    dict 时降级为空 dict 处理。

    参数:
        tool_input: 工具输入参数 dict。

    返回:
        32 字符的十六进制 md5 摘要字符串。
    """
    try:
        payload = json.dumps(tool_input or {}, sort_keys=True)
    except (TypeError, ValueError):
        payload = repr(tool_input or {})
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def detect_tool_stuck(
    tool_name: str,
    params_hash: str,
    recent_calls: List[Tuple[str, str, Optional[str]]],
    window_size: int = 5,
    threshold: int = 3,
) -> Tuple[bool, str]:
    """检测工具是否陷入重复调用卡死。

    在最近 ``window_size`` 次工具调用记录中，按优先级判断卡死：
    1. 历史上同工具同参数已触发 ANTI_CRAWLER → 立即卡死（"anti_crawler"）
    2. 历史上同工具同参数已触发 PERMANENT → 立即卡死（"permanent"）
    3. 同参数重复次数 ≥ ``threshold`` → 卡死（""）

    注：原第 4 条规则"仅同工具名重复 ≥ 宽松阈值"已下线（P0 止血），
    因为对"读 N 个不同文件"的正常探索行为过于敏感。探索预算由
    prompts 软提示（"≥8 次注入提醒"）管理，不需要循环检测兼任。

    参数:
        tool_name: 当前待执行的工具名。
        params_hash: 当前调用的参数 hash（``compute_params_hash`` 输出）。
        recent_calls: 最近工具调用记录列表，每项为
            ``(tool_name, params_hash, error_class)`` 三元组，
            ``error_class`` 可为 ``None``（执行前记录）、``ErrorClass.value`` 字符串。
        window_size: 滑动窗口大小，默认 5。
        threshold: 触发卡死的重复次数阈值，默认 3。

    返回:
        ``(is_stuck, reason)`` 二元组：
        ``is_stuck`` 为 ``True`` 表示卡死需终止；
        ``reason`` 为 ``"anti_crawler"`` / ``"permanent"`` / ``""``（无特殊原因）。
    """
    if not recent_calls or threshold <= 0:
        return False, ""

    window = recent_calls[-window_size:]

    # ── 1. 语义分类检测（同参数 + 历史错误分类） ──
    for name, phash, ec_str in window:
        if name == tool_name and phash == params_hash:
            if ec_str in ("anti_crawler",):
                return True, "anti_crawler"
            if ec_str in ("permanent",):
                return True, "permanent"

    # ── 2. 同参数重复检测（params_hash 相同） ──
    exact_matches = sum(
        1
        for name, phash, _ in window
        if name == tool_name and phash == params_hash
    )
    if exact_matches >= (threshold - 1):
        return True, ""

    return False, ""


def build_stuck_message(tool_name: str, reason: str = "") -> str:
    """构造卡死终止消息（run() 返回值用）。

    参数:
        tool_name: 卡死的工具名。
        reason: 卡死原因，支持 ``"anti_crawler"`` / ``"permanent"`` / ``""``。
    """
    if reason == "anti_crawler":
        return (
            f"工具 {tool_name} 触发了目标网站的反爬虫机制，已终止重试。"
            f"建议：更换获取方式、添加请求头、或询问用户。"
        )
    if reason == "permanent":
        return f"工具 {tool_name} 返回了永久性错误，无需重试，已终止。"
    return f"检测到工具 {tool_name} 重复调用卡死，已终止"


def extract_schedule_id(session_id: Optional[str]) -> Optional[str]:
    """从 session_id 提取 cron 调度项 ID。

    ``session_id`` 以 ``cron:`` 开头时返回前缀之后的部分（调度项 ID）；
    其他值或 ``None`` 返回 ``None``。用于审计日志的 ``schedule_id``
    冗余字段，加速按调度项查询。

    参数:
        session_id: 会话 ID。

    返回:
        调度项 ID（cron 会话）或 ``None``（用户会话）。
    """
    if session_id and session_id.startswith("cron:"):
        return session_id[5:]
    return None


# ──────────────────────────────────────────────────────────────────
# ToolExecutor 类（有状态，封装工具执行依赖）
# ──────────────────────────────────────────────────────────────────


class ToolExecutor:
    """工具执行器：封装工具派发、策略评估、审计日志。

    从 ReactLoop 提取，使 ReactLoop 聚焦于 LLM 循环逻辑，工具执行
    逻辑可独立测试和替换。

    属性:
        tool_registry: 全局 ToolRegistry 实例（内置工具 + MCP/Skill）。
        cron_tool_registry: 可选的 CronToolRegistry（cron 会话路径注入）。
        policy_engine: 可选策略评估器，为 None 时直接放行。
        audit_logger: 可选审计日志记录器，为 None 时跳过审计。
    """

    def __init__(
        self,
        tool_registry: Optional[Any] = None,
        cron_tool_registry: Optional[Any] = None,
        policy_engine: Optional["PolicyEngine"] = None,
        audit_logger: Optional["AuditLogger"] = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.cron_tool_registry = cron_tool_registry
        self.policy_engine = policy_engine
        self.audit_logger = audit_logger

    def evaluate_policy(
        self,
        tool_name: str,
        tool_input: dict,
        session_id: Optional[str] = None,
    ) -> "Decision":
        """评估工具调用的策略决策。

        若 policy_engine 为 None（向后兼容），返回 allow 决策跳过拦截。
        policy_engine.check 抛异常时降级为 allow，避免策略层故障阻断主流程。

        参数:
            tool_name: 待评估的工具名。
            tool_input: 工具输入参数 dict。
            session_id: v2 可选会话 ID，透传给 PolicyEngine 做参数感知决策
                （write_file / delete_file 按文件状态决策）。

        返回:
            Decision 实例，action 为 allow / confirm / deny 三选一。
        """
        if self.policy_engine is None:
            return Decision("allow", "", "low")
        try:
            return self.policy_engine.check(
                tool_name, tool_input, session_id=session_id
            )
        except Exception as e:
            logger.warning("策略评估异常，降级为 allow: %s", e)
            return Decision("allow", "", "low")

    def log_audit(
        self,
        session_id: Optional[str],
        tool_name: str,
        tool_input: dict,
        result: str,
        is_error: bool,
        duration_ms: float,
        decision: Optional["Decision"] = None,
    ) -> None:
        """记录工具调用审计日志。

        封装 ``audit_logger.log_tool_call``，自动从 ``session_id`` 提取
        ``schedule_id``、从 ``decision`` 读取 ``decision_source``，简化
        调用方代码。``audit_logger`` 为 ``None`` 时静默跳过。

        参数:
            session_id: 会话 ID。
            tool_name: 工具名称。
            tool_input: 工具输入参数 dict。
            result: 工具执行结果字符串。
            is_error: 是否出错。
            duration_ms: 调用耗时（毫秒）。
            decision: 策略评估结果（含 ``decision_source``），为 ``None``
                时 ``decision_source`` 使用默认值 ``"default_rule"``。
        """
        if self.audit_logger is None:
            return
        decision_source = (
            decision.decision_source if decision is not None else "default_rule"
        )
        schedule_id = extract_schedule_id(session_id)
        self.audit_logger.log_tool_call(
            session_id=session_id or "",
            tool_name=tool_name,
            tool_input=tool_input,
            result=result,
            is_error=is_error,
            duration_ms=duration_ms,
            decision_source=decision_source,
            schedule_id=schedule_id,
        )

    def execute_tool_with_dispatch(
        self,
        tool_name: str,
        tool_input: dict,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """执行工具调用，按 cron_tool_registry → tool_registry 顺序派发。

        Phase 8 Task 5.7。``tools_override`` 仅覆盖 LLM 可见 schema，工具
        执行仍需派发：cron_tool 名称不在全局 ToolRegistry 中，需路由到
        :class:`CronToolRegistry`（子进程执行）；其他工具走原
        ``tool_registry.execute_tool`` 路径。

        Phase 9+ 中断上下文：通过 ``current_cancel_event`` ContextVar 将
        ``cancel_event`` 传播到同步工具 handler 内部（如 http_request）。

        派发顺序：
        1. ``cron_tool_registry`` 非 None 且 ``has_tool(tool_name)`` → 走
           ``cron_tool_registry.execute_tool``（子进程执行，返回结果字符串）。
        2. 否则走 ``tool_registry.execute_tool``（全局 registry，含内置工具）。
        3. ``tool_registry`` 为 None 时抛 :class:`ToolNotFoundError`。

        失败语义（统一异常层次）：
        - 两个 registry 的 ``execute_tool`` 均抛 :class:`ToolError` 子类，
          本方法原样上抛，由调用方（``run`` / ``run_stream``）捕获并按
          ``stage`` 分流处理。
        - cron_tool 派发时若发生非 ToolError 异常（如网络/序列化错误），
          回退到 tool_registry 派发（向后兼容）。

        参数:
            tool_name: 工具名称。
            tool_input: 工具输入参数 dict。
            cancel_event: 可选的取消事件，通过 ContextVar 传播到工具 handler。

        返回:
            执行结果字符串（成功时）。

        抛出:
            ToolError: 工具执行失败的统一异常基类。
        """
        token = None
        try:
            from ._cancel_context import current_cancel_event
            token = current_cancel_event.set(cancel_event)
        except ImportError:
            pass

        try:
            # 1. cron_tool_registry 派发（仅 cron 会话路径注入了 cron_tool_registry）
            cron_reg = self.cron_tool_registry
            if cron_reg is not None:
                try:
                    if cron_reg.has_tool(tool_name):
                        return cron_reg.execute_tool(tool_name, tool_input)
                except ToolError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "cron_tool %s 派发异常，回退到 tool_registry: %s",
                        tool_name,
                        exc,
                    )
            # 2. 全局 tool_registry 派发
            if self.tool_registry is None:
                raise ToolNotFoundError(
                    tool_name=tool_name,
                    reason=f"tool_registry 未注入，无法执行 {tool_name}",
                    suggestion="检查 ReactLoop 初始化配置",
                )
            return self.tool_registry.execute_tool(tool_name, tool_input)
        finally:
            if token is not None:
                try:
                    from ._cancel_context import current_cancel_event
                    current_cancel_event.reset(token)
                except ImportError:
                    pass
