"""声明式 WorkflowSpec 数据模型（Task 3.1）。

定义通用 workflow 引擎的核心数据结构，支持 5 种 step 类型：
- ``deterministic``：调用旧 WorkflowTemplate（如 directory_watch / email_notify）
- ``llm``：单轮 LLM 调用（通过 ``_call_llm_single_turn``）
- ``tool``：直接调用单个工具（经 PolicyEngine.check 包装）
- ``react``：ReactLoop 多轮工具调用循环
- ``subworkflow``：嵌套 workflow（P2 stub，NotImplementedError）

设计原则：
- 所有 dataclass 支持 ``from_dict`` 反序列化（YAML 解析后入口）
- 缺失字段使用默认值，向后兼容旧配置
- ``RetryPolicy`` / ``OnFailure`` 提供声明式错误处理策略
- 简易模式：仅含 ``template`` 字段的 workflow 自动包装为单 step
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 错误处理策略
# ---------------------------------------------------------------------------


#: on_failure.action 允许值
# Q3 决策：移除 "retry"，step 级重试由 RetryHook 接管整次 workflow 重跑
ALLOWED_ON_FAILURE_ACTIONS = frozenset(
    {"fallback", "skip", "abort"}
)


#: step.type 允许值
ALLOWED_STEP_TYPES = frozenset(
    {"deterministic", "llm", "tool", "react", "subworkflow"}
)


#: retry.backoff_strategy 允许值
ALLOWED_BACKOFF_STRATEGIES = frozenset({"fixed", "linear", "exponential"})


@dataclass
class RetryPolicy:
    """重试策略。

    控制 step 失败后的重试行为，由 ``RetryBudget`` 消费计算 backoff 间隔。

    属性:
        max_attempts: 最大尝试次数（含首次执行）。默认 ``3``，即失败后最多再重试 2 次。
        backoff_strategy: backoff 间隔策略，``fixed`` / ``linear`` / ``exponential``。
        base_delay_ms: 基础延迟（毫秒）。``fixed`` 直接使用此值；
            ``linear`` 按 ``base_delay_ms * attempt`` 线性增长；
            ``exponential`` 按 ``base_delay_ms * (2 ** (attempt-1))`` 指数增长。
        max_delay_ms: 最大延迟上限（毫秒），避免指数退避无限增长。默认 30000ms。
        retry_on: 允许重试的 ErrorClass 集合。为空时默认 ``{TRANSIENT, TIMEOUT}``。
            ``PERMANENT`` / ``AUTH_REQUIRED`` / ``NotImplementedError`` 永不重试。
    """

    max_attempts: int = 3
    backoff_strategy: str = "fixed"
    base_delay_ms: int = 1000
    max_delay_ms: int = 30000
    retry_on: List[str] = field(default_factory=lambda: ["transient", "timeout"])

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RetryPolicy":
        """从 dict 解析 RetryPolicy，缺失字段使用默认值。"""
        if not data:
            return cls()
        return cls(
            max_attempts=int(data.get("max_attempts", 3)),
            backoff_strategy=str(data.get("backoff_strategy", "fixed")),
            base_delay_ms=int(data.get("base_delay_ms", 1000)),
            max_delay_ms=int(data.get("max_delay_ms", 30000)),
            retry_on=list(data.get("retry_on") or ["transient", "timeout"]),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict。"""
        return {
            "max_attempts": self.max_attempts,
            "backoff_strategy": self.backoff_strategy,
            "base_delay_ms": self.base_delay_ms,
            "max_delay_ms": self.max_delay_ms,
            "retry_on": list(self.retry_on),
        }


@dataclass
class OnFailure:
    """step 失败时的处理策略。

    与 ``RetryPolicy`` 配合：
    - ``action=retry``：先按 ``retry`` 策略重试，重试耗尽后按 ``fallback`` /
      ``skip`` / ``abort`` 兜底
    - ``action=fallback``：直接执行 ``fallback_config``（不重试）
    - ``action=skip``：跳过当前 step，workflow 继续后续 step
    - ``action=abort``：终止整个 workflow

    属性:
        action: 失败动作，``retry`` / ``fallback`` / ``skip`` / ``abort``。
        retry: 重试策略，仅 ``action=retry`` 时生效。
        fallback_config: fallback step 的 config（如 ``{"fallback_mode":
            "single_turn"}``）。仅 ``action=fallback`` 时生效。
        fallback_type: fallback step 的类型，默认 ``llm``。
        message: 失败时记录到 trace 的消息模板（可选）。
    """

    action: str = "abort"
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    fallback_config: Dict[str, Any] = field(default_factory=dict)
    fallback_type: str = "llm"
    message: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "OnFailure":
        """从 dict 解析 OnFailure，缺失字段使用默认值。"""
        if not data:
            return cls()
        return cls(
            action=str(data.get("action", "abort")),
            retry=RetryPolicy.from_dict(data.get("retry")),
            fallback_config=dict(data.get("fallback_config") or {}),
            fallback_type=str(data.get("fallback_type", "llm")),
            message=str(data.get("message", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict。"""
        return {
            "action": self.action,
            "retry": self.retry.to_dict(),
            "fallback_config": dict(self.fallback_config),
            "fallback_type": self.fallback_type,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Step 与 Workflow 定义
# ---------------------------------------------------------------------------


@dataclass
class StepSpec:
    """单个 step 的声明式定义。

    属性:
        id: step 唯一标识（workflow 内唯一），用于 depends_on / condition 引用。
        name: 可读名称（用于报告展示）。为空时回退到 ``id``。
        type: step 类型，``deterministic`` / ``llm`` / ``tool`` / ``react`` /
            ``subworkflow``。
        config: 类型相关配置 dict。结构由 type 决定：
            - ``deterministic``: ``{"template": "<name>", ...template_cfg}``
            - ``llm``: ``{"prompt": str, "system": str, "tools": [...]}``
            - ``tool``: ``{"tool": str, "input": dict}``
            - ``react``: ``{"task": str, "max_loops": int, "tool_whitelist": [...]}``
            - ``subworkflow``: ``{"workflow_ref": str}`` 或 inline spec dict
        depends_on: 前置 step id 列表。所有前置完成后才执行本 step。
        condition: 执行条件表达式（P0 简化版正则，仅支持
            ``steps.<id>.outputs.<key> > 0`` / ``== "foo"`` 形式）。
            为空时无条件执行。
        on_failure: 失败处理策略。
        timeout_seconds: step 级超时（秒）。为 ``None`` 时无超时。
    """

    id: str
    name: str = ""
    type: str = "llm"
    config: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    condition: str = ""
    on_failure: OnFailure = field(default_factory=OnFailure)
    timeout_seconds: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StepSpec":
        """从 dict 解析 StepSpec。

        必填字段 ``id`` 缺失时抛 ValueError。
        ``type`` 缺失时默认 ``llm``。
        """
        if not isinstance(data, dict):
            raise ValueError(f"step 必须为 dict，实际类型: {type(data).__name__}")
        step_id = data.get("id")
        if not step_id:
            raise ValueError("step 缺少必填字段 id")
        return cls(
            id=str(step_id),
            name=str(data.get("name", "")),
            type=str(data.get("type", "llm")),
            config=dict(data.get("config") or {}),
            depends_on=list(data.get("depends_on") or []),
            condition=str(data.get("condition", "")),
            on_failure=OnFailure.from_dict(data.get("on_failure")),
            timeout_seconds=(
                float(data["timeout_seconds"])
                if data.get("timeout_seconds") is not None
                else None
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict。"""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "config": dict(self.config),
            "depends_on": list(self.depends_on),
            "condition": self.condition,
            "on_failure": self.on_failure.to_dict(),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class WorkflowSpec:
    """workflow 的声明式定义。

    支持两种模式：
    - **多步模式**：含 ``steps`` 列表，由 WorkflowEngine 按拓扑序执行
    - **简易模式**：仅含 ``template`` 字段（无 ``steps``），由 adapter 包装为
      单 step WorkflowSpec 后执行

    属性:
        name: workflow 名称（用于报告 / RunSummary）。
        version: spec 版本号，默认 ``1``。
        template: 简易模式模板名（如 ``"research"``）。与 ``steps`` 互斥。
        template_config: 简易模式模板配置 dict。
        steps: 多步模式 step 列表。与 ``template`` 互斥。
        on_failure: workflow 级失败策略（step 级 on_failure 优先）。
        timeout_seconds: workflow 级超时（秒）。
        metadata: 元信息（如作者 / 描述 / tags）。
    """

    name: str = ""
    version: int = 1
    template: Optional[str] = None
    template_config: Dict[str, Any] = field(default_factory=dict)
    steps: List[StepSpec] = field(default_factory=list)
    on_failure: OnFailure = field(default_factory=OnFailure)
    timeout_seconds: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowSpec":
        """从 dict 解析 WorkflowSpec。

        支持简易模式（仅 ``template``）与多步模式（``steps``）。
        两者同时存在时以 ``steps`` 为准（多步模式优先）。
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"workflow 必须为 dict，实际类型: {type(data).__name__}"
            )

        # steps 多步模式
        raw_steps = data.get("steps")
        steps: List[StepSpec] = []
        if raw_steps:
            if not isinstance(raw_steps, list):
                raise ValueError("workflow.steps 必须为列表")
            for i, s in enumerate(raw_steps):
                try:
                    steps.append(StepSpec.from_dict(s))
                except ValueError as e:
                    raise ValueError(f"workflow.steps[{i}] 解析失败: {e}") from e

        return cls(
            name=str(data.get("name", "")),
            version=int(data.get("version", 1)),
            template=data.get("template"),
            template_config=dict(data.get("template_config") or {}),
            steps=steps,
            on_failure=OnFailure.from_dict(data.get("on_failure")),
            timeout_seconds=(
                float(data["timeout_seconds"])
                if data.get("timeout_seconds") is not None
                else None
            ),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict。"""
        return {
            "name": self.name,
            "version": self.version,
            "template": self.template,
            "template_config": dict(self.template_config),
            "steps": [s.to_dict() for s in self.steps],
            "on_failure": self.on_failure.to_dict(),
            "timeout_seconds": self.timeout_seconds,
            "metadata": dict(self.metadata),
        }

    def is_simple_mode(self) -> bool:
        """是否为简易模式（仅 template，无 steps）。"""
        return bool(self.template) and not self.steps

    def is_multi_step_mode(self) -> bool:
        """是否为多步模式（含 steps）。"""
        return bool(self.steps)


__all__ = [
    "ALLOWED_ON_FAILURE_ACTIONS",
    "ALLOWED_STEP_TYPES",
    "ALLOWED_BACKOFF_STRATEGIES",
    "RetryPolicy",
    "OnFailure",
    "StepSpec",
    "WorkflowSpec",
]
