"""Action 值对象(§4.1/§4.4,阶段 2 落地):扩展的变更请求(纯数据)。

6 种 Action + ActionResult + ToolDecision + schema 级校验(§15-A1)。
扩展经钩子返回 Action 列表;core 立即应用/原子批次(§4.4)。

Action 应用规则:
- AppendMessage: 叠加(按注册序);仅允许 role=user(§4.4 交替约束)
- SetTools: 后注册覆盖先注册(整体列表覆盖)
- SetExtra: 覆盖;key 白名单 ^[a-z0-9_]+\\.[a-z0-9_.]+$
- SetStop: 覆盖;短路后续同名钩子
- SetSystem: 覆盖基础 system
- ModifyToolSchema: 按工具名定向修改单个工具 schema(与 SetTools 区分)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .types import is_valid_setextra_key

# Action op 常量
OP_APPEND_MESSAGE = "AppendMessage"
OP_SET_TOOLS = "SetTools"
OP_SET_EXTRA = "SetExtra"
OP_SET_STOP = "SetStop"
OP_SET_SYSTEM = "SetSystem"
OP_MODIFY_TOOL_SCHEMA = "ModifyToolSchema"

ACTION_OPS = (
    OP_APPEND_MESSAGE, OP_SET_TOOLS, OP_SET_EXTRA, OP_SET_STOP,
    OP_SET_SYSTEM, OP_MODIFY_TOOL_SCHEMA,
)


@dataclass(frozen=True)
class AppendMessage:
    """追加一条消息(仅 role=user;叠加)。"""

    op: str = OP_APPEND_MESSAGE
    message: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SetTools:
    """整体覆盖工具 schema 列表(后注册覆盖先注册)。"""

    op: str = OP_SET_TOOLS
    tools: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SetExtra:
    """写入扩展间共享数据(key 白名单,覆盖)。"""

    op: str = OP_SET_EXTRA
    key: str = ""
    value: Any = None


@dataclass(frozen=True)
class SetStop:
    """拦截整个对话(短路后续同名钩子)。"""

    op: str = OP_SET_STOP
    reason: str = "intercepted"


@dataclass(frozen=True)
class SetSystem:
    """覆盖基础 system(收口阶段注入叠加其上)。"""

    op: str = OP_SET_SYSTEM
    text: str = ""


@dataclass(frozen=True)
class ModifyToolSchema:
    """按工具名定向修改单个工具 schema(非整体覆盖,§4.1 v1.11 语义)。"""

    op: str = OP_MODIFY_TOOL_SCHEMA
    name: str = ""
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None


Action = Union[AppendMessage, SetTools, SetExtra, SetStop, SetSystem, ModifyToolSchema]


def make_action(op: str, **kwargs: Any) -> Action:
    """Action 工厂:按 op 构造对应值对象(未知 op 抛 ValueError)。"""
    if op == OP_APPEND_MESSAGE:
        return AppendMessage(message=kwargs.get("message") or {})
    if op == OP_SET_TOOLS:
        return SetTools(tools=kwargs.get("tools") or [])
    if op == OP_SET_EXTRA:
        return SetExtra(key=kwargs.get("key", ""), value=kwargs.get("value"))
    if op == OP_SET_STOP:
        return SetStop(reason=kwargs.get("reason", "intercepted"))
    if op == OP_SET_SYSTEM:
        return SetSystem(text=kwargs.get("text", ""))
    if op == OP_MODIFY_TOOL_SCHEMA:
        return ModifyToolSchema(
            name=kwargs.get("name", ""),
            description=kwargs.get("description"),
            input_schema=kwargs.get("input_schema"),
        )
    raise ValueError(
        f"未知 Action op: {op!r}(可选: {', '.join(ACTION_OPS)})"
    )


def validate_action(action: Any) -> Optional[str]:
    """Action schema 级校验(§15-A1 协议边界)。

    合法返回 None;非法返回错误描述。逐条校验在批次应用前执行,
    非法条 → 拒绝 + error 日志 + 跳过该条(§4.4 两层校验分离)。
    """
    if not isinstance(action, tuple(ACTION_CLASSES)):
        return f"非法 action 类型: {type(action).__name__}"
    if isinstance(action, AppendMessage):
        m = action.message
        if not isinstance(m, dict):
            return "AppendMessage.message 必须是对象"
        role = m.get("role")
        if role != "user":
            # §4.4:AppendMessage 仅允许 role=user(交替约束;assistant 需求走 RFC)
            return f"AppendMessage 仅允许 role=user,实际 {role!r}"
        if m.get("content") is None:
            return "AppendMessage.message 缺少 content"
        return None
    if isinstance(action, SetTools):
        if not isinstance(action.tools, list):
            return "SetTools.tools 必须是列表"
        return None
    if isinstance(action, SetExtra):
        if not is_valid_setextra_key(action.key):
            return f"SetExtra key 非法: {action.key!r}(必须匹配 ^[a-z0-9_]+\\.[a-z0-9_.]+$)"
        return None
    if isinstance(action, SetStop):
        if not isinstance(action.reason, str):
            return "SetStop.reason 必须是字符串"
        return None
    if isinstance(action, SetSystem):
        if not isinstance(action.text, str):
            return "SetSystem.text 必须是字符串"
        return None
    if isinstance(action, ModifyToolSchema):
        if not action.name or not isinstance(action.name, str):
            return "ModifyToolSchema.name 必须是字符串"
        if action.description is None and action.input_schema is None:
            return "ModifyToolSchema 须提供 description 或 input_schema"
        return None
    return "未知 action"


ACTION_CLASSES = (
    AppendMessage, SetTools, SetExtra, SetStop, SetSystem, ModifyToolSchema,
)


@dataclass(frozen=True)
class ActionResult:
    """钩子返回结果:Action[] 列表。"""

    actions: List[Action] = field(default_factory=list)


@dataclass(frozen=True)
class ToolDecision:
    """pre_tool_call 决策(§8 工具语义边界)。

    - allow: 放行(不短路,后续 pre_tool_call 仍执行)
    - reject: 策略拒绝(首个 reject 短路;tool_result is_error 回喂)
    - modify: 变形器(改入参;多级按应用序后覆盖先;重过 input_schema)
    """

    decision: str = "allow"
    input: Optional[Dict[str, Any]] = None

    @property
    def is_reject(self) -> bool:
        return self.decision == "reject"

    @property
    def is_modify(self) -> bool:
        return self.decision == "modify"
