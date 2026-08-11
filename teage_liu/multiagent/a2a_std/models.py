"""标准 A2A v1.0 数据模型（pydantic，手写实现）。

依据 a2a-protocol.org v1.0 规范。不依赖官方 a2a-sdk。

线协议字段为 camelCase；Python 字段用 snake_case，
通过 alias_generator 双向转换，`model_dump(by_alias=True)` 输出精确 camelCase。
叶子模型 `extra="allow"`，容忍规范演进带来的未知字段。
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_camel(s: str) -> str:
    """snake_case -> camelCase（用于 alias_generator）。"""
    parts = s.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


class A2ABase(BaseModel):
    """A2A 模型基类：camelCase 别名 + 容忍未知字段。"""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
    )


# ---------------------------------------------------------------------------
# Part 模型（判别联合：kind 字段）
# ---------------------------------------------------------------------------

class TextPart(A2ABase):
    """文本部件。"""

    kind: Literal["text"] = "text"
    text: str
    metadata: Optional[dict] = None


class FilePart(A2ABase):
    """文件部件（uri 或内联 base64 bytes 二选一）。"""

    kind: Literal["file"] = "file"
    name: Optional[str] = None
    mime_type: Optional[str] = None
    uri: Optional[str] = None
    bytes: Optional[str] = None  # base64 编码的内联内容
    metadata: Optional[dict] = None


class DataPart(A2ABase):
    """结构化 JSON 部件。"""

    kind: Literal["data"] = "data"
    data: dict
    metadata: Optional[dict] = None


Part = Annotated[Union[TextPart, FilePart, DataPart], Field(discriminator="kind")]


def _coerce_part(value):
    """容忍 SDK 方言的 kind-less Part：
    - 无 kind 字段时按存在字段推断（text/data/url|bytes）
    - SDK 文件字段映射：raw→bytes、media_type→mimeType、filename→name
    """
    if not isinstance(value, dict) or "kind" in value:
        return value
    if "text" in value:
        return {**value, "kind": "text"}
    if "data" in value:
        return {**value, "kind": "data"}
    # 文件部件
    out = dict(value)
    if "raw" in out:
        out["bytes"] = out.pop("raw")
    if "media_type" in out:
        out["mime_type"] = out.pop("media_type")
    if "filename" in out:
        out["name"] = out.pop("filename")
    out["kind"] = "file"
    return out


# ---------------------------------------------------------------------------
# Message / Task 模型
# ---------------------------------------------------------------------------

class Message(A2ABase):
    """一轮对话消息。"""

    role: Literal["user", "agent"]
    message_id: str  # 客户端生成 UUID
    task_id: Optional[str] = None
    context_id: Optional[str] = None
    parts: list[Part] = Field(default_factory=list)
    metadata: Optional[dict] = None

    @field_validator("role", mode="before")
    @classmethod
    def _coerce_role(cls, v):
        """容忍 SDK 方言枚举（ROLE_USER/ROLE_AGENT）。"""
        if isinstance(v, str) and v.startswith("ROLE_"):
            return v[len("ROLE_"):].lower()
        return v

    @field_validator("parts", mode="before")
    @classmethod
    def _coerce_parts(cls, v):
        """容忍 SDK 方言 kind-less Part。"""
        if isinstance(v, list):
            return [_coerce_part(p) for p in v]
        return v


class TaskState(str, Enum):
    """标准 Task 生命周期状态（线协议值含连字符）。"""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        """终态：不可重启，新工作须在同一 contextId 下新建 Task。"""
        return self in _TERMINAL_STATES

    @property
    def is_interrupt(self) -> bool:
        """非终态中断：等待调用方补充输入/凭证后可恢复。"""
        return self in (TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED)

    @classmethod
    def parse(cls, value) -> "TaskState":
        """容忍三种线形式：规范小写 / SDK UPPER_SNAKE / protobuf 数字。"""
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            mapping = {
                1: cls.SUBMITTED, 2: cls.WORKING, 3: cls.COMPLETED,
                4: cls.FAILED, 5: cls.CANCELED, 6: cls.INPUT_REQUIRED,
                7: cls.REJECTED, 8: cls.AUTH_REQUIRED,
            }
            if value in mapping:
                return mapping[value]
            raise ValueError(f"Unknown TaskState number: {value}")
        s = str(value)
        for member in cls:
            if member.value == s:
                return member
        if s.startswith("TASK_STATE_"):
            for member in cls:
                if member.name == s[len("TASK_STATE_"):]:
                    return member
        raise ValueError(f"Unknown TaskState: {value}")


_TERMINAL_STATES = frozenset({
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED,
})


class InputRequired(A2ABase):
    """input-required 详情。"""

    properties: Optional[list[str]] = None
    message: Optional[Message] = None


class AuthRequired(A2ABase):
    """auth-required 详情。"""

    properties: Optional[list[str]] = None
    message: Optional[Message] = None


class TaskStatus(A2ABase):
    """任务状态快照。"""

    state: TaskState
    message: Optional[Message] = None
    timestamp: Optional[str] = None
    metadata: Optional[dict] = None
    input_required: Optional[InputRequired] = None
    auth_required: Optional[AuthRequired] = None

    @field_validator("state", mode="before")
    @classmethod
    def _coerce_state(cls, v):
        """容忍规范小写 / SDK UPPER_SNAKE / 数字三种线形式。"""
        return TaskState.parse(v)


class Artifact(A2ABase):
    """任务产出。"""

    name: str
    mime_type: Optional[str] = None
    parts: list[Part] = Field(default_factory=list)
    metadata: Optional[dict] = None
    append: Optional[bool] = None

    @field_validator("parts", mode="before")
    @classmethod
    def _coerce_parts(cls, v):
        """容忍 SDK 方言 kind-less Part。"""
        if isinstance(v, list):
            return [_coerce_part(p) for p in v]
        return v


class Task(A2ABase):
    """标准 Task：有状态的工作单元。"""

    id: str  # 服务端生成
    context_id: Optional[str] = None
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    metadata: Optional[dict] = None


# ---------------------------------------------------------------------------
# SSE 事件
# ---------------------------------------------------------------------------

class TaskStatusUpdateEvent(A2ABase):
    """任务状态变更事件（SSE）。"""

    id: str
    status: TaskStatus
    artifacts: Optional[list[Artifact]] = None
    metadata: Optional[dict] = None
    message: Optional[Message] = None


class TaskArtifactUpdateEvent(A2ABase):
    """任务产出更新事件（SSE）。"""

    id: str
    artifacts: list[Artifact] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Push 通知
# ---------------------------------------------------------------------------

class TaskPushNotificationAuthentication(A2ABase):
    """webhook 认证信息。"""

    schemes: Optional[list[str]] = None
    credentials: Optional[str] = None
    token: Optional[str] = None
    headers: Optional[dict] = None


class TaskPushNotificationConfig(A2ABase):
    """任务推送通知配置。"""

    url: str
    authentication: Optional[TaskPushNotificationAuthentication] = None


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------

class AgentSkill(A2ABase):
    """Agent 技能声明。"""

    id: str
    name: str
    description: str
    tags: Optional[list[str]] = None
    examples: Optional[list[str]] = None
    input_modes: Optional[list[str]] = None
    output_modes: Optional[list[str]] = None


class SecurityScheme(A2ABase):
    """认证方案声明。"""

    scheme: Literal["bearer", "basic", "mtls", "oauth2", "oauth2_with_jws", "external"]
    description: Optional[str] = None


class AgentCapabilities(A2ABase):
    """Agent 能力声明。"""

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: Optional[bool] = None
    internal: Optional[bool] = None


class AgentInterface(A2ABase):
    """Agent 接口声明（官方 SDK v1.0 方言：端点 URL 在此，而非 card.url）。"""

    url: str
    protocol_binding: Optional[str] = "JSONRPC"
    protocol_version: Optional[str] = None
    tenant: Optional[str] = None


class AgentCard(A2ABase):
    """标准 A2A Agent Card（发布在 /.well-known/agent-card.json）。"""

    protocol_version: Literal["1.0"] = "1.0"
    name: str
    description: str
    url: str
    version: Optional[str] = None
    preferred_transport: Literal["JSONRPC"] = "JSONRPC"
    # 官方 SDK v1.0 方言：接口端点声明（url 字段之外的第二端点来源）
    supported_interfaces: list[AgentInterface] = Field(default_factory=list)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text", "text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text", "text/plain"])
    skills: list[AgentSkill] = Field(default_factory=list)
    security_schemes: list[SecurityScheme] = Field(default_factory=list)
    # 本实现声明的扩展方法名（经 A2A-Extensions 头门控）
    extensions: list[str] = Field(default_factory=list)
    supports_authenticated_extended_card: bool = False
    metadata: Optional[dict] = None


# ---------------------------------------------------------------------------
# JSON-RPC 请求参数模型
# ---------------------------------------------------------------------------

class MessageSendRequest(A2ABase):
    """message/send 参数。"""

    id: Optional[str] = None  # 客户端生成的 messageId（与 message.messageId 一致）
    message: Message
    context_id: Optional[str] = None
    push_notification_config: Optional[TaskPushNotificationConfig] = None
    history_length: Optional[int] = None


class SendMessageStreamingRequest(MessageSendRequest):
    """message/stream 参数。"""

    local_name: Optional[str] = None


class TaskIdParams(A2ABase):
    """tasks/get / tasks/cancel / pushConfig 等按 taskId 定位的参数。"""

    id: str
    context_id: Optional[str] = None


class TaskQueryParams(A2ABase):
    """tasks/list 查询参数。"""

    history_length: Optional[int] = None
    context_id: Optional[str] = None
    metadata: Optional[dict] = None


class PushConfigSetRequest(A2ABase):
    """tasks/pushNotificationConfig/set 参数。"""

    id: str
    push_notification_config: TaskPushNotificationConfig


class PushConfigGetRequest(A2ABase):
    """tasks/pushNotificationConfig/get 参数。"""

    id: str


class PushConfigListRequest(A2ABase):
    """tasks/pushNotificationConfig/list 参数。"""

    context_id: Optional[str] = None


class PushConfigDeleteRequest(A2ABase):
    """tasks/pushNotificationConfig/delete 参数。"""

    id: str
