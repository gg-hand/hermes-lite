"""消息模型与事件类型(流是基建,类型最先定死)。

事件是主干产出的最小单元:
- 外壳(server/)将其编码为 SSE 帧
- 非流式调用方收集事件拼字符串
- 枝干与主干均只依赖此处定义的事件类型,不得自造事件

事件类型清单(M1):
- step_start   : 一次 LLM 调用的开始(循环形态下每轮一个 step)
- text_delta   : LLM 输出文本增量
- reasoning_delta: LLM 推理增量(可选,透传给前端思考区)
- step_end     : 一次 LLM 调用的结束(携带 content_blocks / stop_reason / usage;
                  循环形态**逐轮透传**,主干据此消息级落盘 assistant)
- tool_use     : LLM 请求调用工具(循环形态发出)
- tool_result  : 工具执行结果回传(携带 tool_use_id,配对落盘依据)
- done         : 整个对话结束(携带最终文本与终止原因)
- error        : 对话失败(不产生 done)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Literal, Optional, Union

# ---------------------------------------------------------------------------
# 事件类型常量
# ---------------------------------------------------------------------------
EV_STEP_START = "step_start"
EV_TEXT_DELTA = "text_delta"
EV_REASONING_DELTA = "reasoning_delta"
EV_STEP_END = "step_end"
EV_TOOL_USE = "tool_use"
EV_TOOL_RESULT = "tool_result"
EV_DONE = "done"
EV_ERROR = "error"

EventType = Literal[
    "step_start",
    "text_delta",
    "reasoning_delta",
    "step_end",
    "tool_use",
    "tool_result",
    "done",
    "error",
]

# 终止原因(与 done 事件配套)
TERMINATION_NORMAL = "normal"          # end_turn 自然完成
TERMINATION_MAX_LOOPS = "max_loops"    # 达到循环上限
TERMINATION_USER_CANCEL = "user_cancel"  # 用户取消
TERMINATION_NO_TOOL_EXECUTOR = "no_tool_executor"  # LLM 想调工具但无枝干执行
TERMINATION_LLM_ERROR = "llm_error"    # LLM 调用失败
TERMINATION_INTERCEPTED = "intercepted"  # 枝干 before 钩子置 ctx.stop 拦截

TerminationReason = str

# ---------------------------------------------------------------------------
# 消息模型(Anthropic 风格 content block)
# ---------------------------------------------------------------------------
# 消息: {"role": "user"|"assistant"|"system"|"tool", "content": str | list[block]}
# content block: {"type": "text"|"tool_use"|"tool_result"|"thinking", ...}
Message = Dict[str, Any]
Block = Dict[str, Any]

# 事件: {"type": EventType, "session_id": str, ...payload}
Event = Dict[str, Any]


class StepSummary:
    """每轮 step 后的轮摘要(after_step 钩子携带,§4.2)。

    结构:``{ round, text, content_blocks, tool_uses, usage, duration }``。
    """

    def __init__(
        self,
        round_: int,
        text: str,
        content_blocks: Optional[List[Block]] = None,
        tool_uses: Optional[List[Dict[str, Any]]] = None,
        usage: Optional[Dict[str, Any]] = None,
        duration: float = 0.0,
    ) -> None:
        self.round: int = round_
        self.text: str = text
        self.content_blocks: List[Block] = content_blocks or []
        self.tool_uses: List[Dict[str, Any]] = tool_uses or []
        self.usage: Optional[Dict[str, Any]] = usage
        self.duration: float = duration

    def __repr__(self) -> str:
        return (
            f"StepSummary(round={self.round}, text={self.text[:30]!r}, "
            f"blocks={len(self.content_blocks)})"
        )


class AfterResponse:
    """after 钩子收到的完成摘要(B3,计划 §3.2)。

    观测枝干(记忆巩固 / 审计 / 指标)只读此对象,不干预对话。
    """

    def __init__(
        self,
        text: str,
        content_blocks: Optional[List[Block]] = None,
        usage: Optional[Dict[str, Any]] = None,
        done_event: Optional[Event] = None,
    ) -> None:
        self.text: str = text
        self.content_blocks: List[Block] = content_blocks or []
        self.usage: Optional[Dict[str, Any]] = usage
        self.done_event: Optional[Event] = done_event

    def __repr__(self) -> str:
        return (
            f"AfterResponse(text={self.text[:30]!r}, "
            f"blocks={len(self.content_blocks)}, usage={self.usage is not None})"
        )


def text_block(text: str) -> Block:
    """构造文本 content block。"""
    return {"type": "text", "text": text}


def tool_use_block(block_id: str, name: str, tool_input: dict) -> Block:
    """构造 tool_use content block。"""
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def tool_result_block(tool_use_id: str, content: str, is_error: bool = False) -> Block:
    """构造 tool_result content block。"""
    block: Block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _json_len(obj: Any) -> int:
    """JSON 序列化长度(ensure_ascii=False);不可序列化 → 0(与历史 _estimate_bytes 一致)。"""
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class _SizeCache:
    """快照体积记账缓存(实现层内部,不进协议序列化)。

    字段:
    - ``messages_ref``:记账时 ``messages`` 的**列表对象** —— 靠同一性判定缓存是否仍
      对应当前消息列表(外部用 ``dataclasses.replace(..., messages=...)`` 绕过
      ``with_messages`` 时会自动失效,不用长度启发式);
    - ``msg_bytes``:``len(json.dumps(messages, ensure_ascii=False))``;
    - ``base_bytes``:``messages`` 置空后**整个快照**的 JSON 长度;``-1`` = 未知。

    有效性前提:快照仅经 ``with_*`` / ``apply_action_batch`` 演进(生产路径如此)。
    外部若绕过它们直接改动,必须同时传 ``_size_cache=None`` 显式失效
    (2026-09-10 执行后审计 D-4 补全):
    - ``dataclasses.replace(snapshot, messages=...)`` —— 列表同一性变化可自动覆盖;
    - **任何计入 base 分量的协议字段**(尤其 ``history``,以及 ``tools`` / ``extra`` /
      ``system_text``)被 ``dataclasses.replace`` 改写 —— 同一性检查只看 ``messages``,
      不覆盖这些字段(旧的全量重算实现对此天然免疫,新实现不再免疫);
    - **原地改写** ``messages`` 内的已有元素(如 ``snapshot.messages[0]["content"] = ...``)
      —— 列表对象同一性不变,增量记账不会察觉。
    构造入口 ``from_dict`` / ``dataclasses.replace(..., _size_cache=None)`` 产出的实例
    天然失效(``None``)。
    """
    messages_ref: List[Message]
    msg_bytes: int
    base_bytes: int


def _base_unknown(cache: Optional[_SizeCache]) -> Optional[_SizeCache]:
    """保留消息分量、把 base 分量置为未知(-1)。"""
    return None if cache is None else _SizeCache(cache.messages_ref, cache.msg_bytes, -1)


def extract_text(content: Union[str, list, None]) -> str:
    """从消息 content(str 或 block 列表)提取纯文本。

    用于 token 粗估 / 归档 / 非流式收集。tool_use 只取 name,tool_result 递归提取。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
            elif btype == "tool_use":
                name = block.get("name", "")
                if isinstance(name, str):
                    parts.append(name)
            elif btype == "tool_result":
                parts.append(extract_text(block.get("content")))
        return "".join(parts)
    return str(content) if content is not None else ""


def split_content_blocks(content_blocks: List[Block]) -> tuple[List[str], List[Block], List[Block]]:
    """按类型拆分 content_blocks,返回 (text_parts, tool_use_blocks, thinking_blocks)。"""
    text_parts: List[str] = []
    tool_use_blocks: List[Block] = []
    thinking_blocks: List[Block] = []
    for block in content_blocks:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif btype == "tool_use":
            tool_use_blocks.append(block)
        elif btype == "thinking":
            thinking_blocks.append(block)
    return text_parts, tool_use_blocks, thinking_blocks


def validate_messages(messages: List[Message]) -> Optional[str]:
    """校验消息序列结构合法性(C1:core 的保证,组装后双保险之一)。

    M1 规则:角色白名单(user/assistant)+ 交替(首条非 tool);
    M2 扩展:tool_result 配对 tool_use。
    合法返回 None;不合法返回错误描述(主干发 error 事件,不发送给 LLM)。
    """
    last_role: Optional[str] = None
    for i, m in enumerate(messages):
        role = m.get("role")
        if role not in ("user", "assistant"):
            return f"消息 {i} 角色非法 {role!r}(M1 仅允许 user/assistant 交替)"
        if role == last_role:
            return f"消息 {i} 与上一条同角色 {role!r}(user/assistant 必须交替)"
        last_role = role
    return None


def normalize_history(messages: List[Message]) -> List[Message]:
    """将历史消息规整为 {role, content} 形式(剔除附加字段,符合 LLM API 规范)。

    消息级落盘后(D1):content_blocks(JSON 字符串)优先重建 Anthropic 风格
    content(工具配对结构);旧行无 content_blocks → 回退纯文本 content。
    丢弃 role/content 均为空的异常项。
    """
    clean: List[Message] = []
    for m in messages:
        role = m.get("role")
        if role is None:
            continue
        raw_blocks = m.get("content_blocks")
        if raw_blocks:
            try:
                blocks = json.loads(raw_blocks)
            except (TypeError, json.JSONDecodeError):
                blocks = None
            if blocks:
                clean.append({"role": role, "content": blocks})
                continue
        content = m.get("content")
        if content is None:
            continue
        clean.append({"role": role, "content": content})
    return clean


# ---------------------------------------------------------------------------
# 协议值对象层(阶段 1,对齐 PROTOCOL/types 域)
# ---------------------------------------------------------------------------
# 命名约束三模式(协议强制,与 types.schema.json 逐字一致,§15-A2 安全边界)
_EXTENSION_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_KIND_RE = re.compile(r"^[a-z0-9_.]+$")
_SETEXTRA_KEY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_.]+$")


def is_valid_extension_name(name: str) -> bool:
    """extension_name 字符集校验(禁点,消除 '{name}.' 前缀解析歧义)。"""
    return bool(name) and bool(_EXTENSION_NAME_RE.fullmatch(name))


def is_valid_kind(kind: str) -> bool:
    """kind 字符集校验(允许点)。"""
    return bool(kind) and bool(_KIND_RE.fullmatch(kind))


def is_valid_setextra_key(key: str) -> bool:
    """SetExtra key 白名单校验(分支段 + 单个点 + 自由子键段)。"""
    return bool(key) and bool(_SETEXTRA_KEY_RE.fullmatch(key))


# 资源上限(§15-A6,与 types.schema.json ResourceLimits 逐字一致,防 DoS)
RESOURCE_LIMITS: Dict[str, int] = {
    "max_snapshot_bytes": 2 * 1024 * 1024,          # 2 MiB
    "max_message_bytes": 512 * 1024,                # 512 KiB
    "max_messages_per_conversation": 2000,          # 2000 条
}

# 消息角色白名单(协议 Message.role)
_MESSAGE_ROLES = ("user", "assistant", "system", "tool")


def make_message(role: str, content: Union[str, List[Block]]) -> Message:
    """构造一条协议 Message(role 白名单校验,非法抛 ValueError)。"""
    if role not in _MESSAGE_ROLES:
        raise ValueError(
            f"非法消息角色 {role!r}(可选: {', '.join(_MESSAGE_ROLES)})"
        )
    return {"role": role, "content": content}


def validate_message_shape(m: Message) -> Optional[str]:
    """单条消息结构校验(角色白名单 + content 形态,协议 schema 级)。

    合法返回 None;非法返回错误描述。这是逐条入站校验,
    与 :func:`validate_messages`(LLM 前交替校验)分层。
    """
    if not isinstance(m, dict):
        return f"消息必须是对象,实际 {type(m).__name__}"
    role = m.get("role")
    if role not in _MESSAGE_ROLES:
        return f"消息角色非法 {role!r}(可选: {', '.join(_MESSAGE_ROLES)})"
    content = m.get("content")
    if content is None:
        return "消息缺少 content"
    if isinstance(content, str):
        return None
    if isinstance(content, list):
        for i, b in enumerate(content):
            if not isinstance(b, dict):
                return f"content block {i} 必须是对象,实际 {type(b).__name__}"
            btype = b.get("type")
            if btype not in ("text", "tool_use", "tool_result", "thinking"):
                return f"content block {i} 类型非法 {btype!r}"
        return None
    return f"content 必须是字符串或 block 列表,实际 {type(content).__name__}"


@dataclass(frozen=True)
class Snapshot:
    """ContextSnapshot 值对象:不可变、只读、可序列化(协议 §4,types 域 §4)。

    字段与 PROTOCOL/types.schema.json 的 Snapshot 逐字一致。
    ``revision`` 由 host 独占递增(§18.2),扩展只读;本阶段仅定义结构,
    快照推进/递增逻辑随阶段 2 的 action 应用落地。
    """

    session_id: str
    user_input: str
    round: int = 0
    started_at: str = ""
    history: List[Message] = field(default_factory=list)
    system_text: str = ""
    messages: List[Message] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    stop: bool = False
    revision: int = 0
    #: 实现层内部字段(不进协议序列化,11 键保持与 types.schema.json 一致):
    #: SetStop action 的 reason,供 done(intercepted) 事件呈现
    stop_reason: Optional[str] = None
    #: 实现层内部字段(不进协议序列化,11 键保持与 types.schema.json 一致):
    #: 体积记账缓存(见 _SizeCache);None = 失效/未建立。
    #: §15-A6 预算检查据此在稳态下只做常数级算术(不再全量 json.dumps)。
    _size_cache: Optional[_SizeCache] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为协议快照 dict(与 types.schema.json 结构一致,不含 stop_reason)。"""
        return {
            "session_id": self.session_id,
            "user_input": self.user_input,
            "round": self.round,
            "started_at": self.started_at,
            "history": list(self.history),
            "system_text": self.system_text,
            "messages": list(self.messages),
            "tools": list(self.tools),
            "extra": dict(self.extra),
            "stop": self.stop,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Snapshot":
        """从协议快照 dict 构造(必填字段缺失抛 ValueError)。"""
        required = (
            "session_id", "user_input", "round", "started_at", "history",
            "system_text", "messages", "tools", "extra", "stop", "revision",
        )
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError(f"快照缺少必填字段: {', '.join(missing)}")
        return cls(
            session_id=d["session_id"],
            user_input=d["user_input"],
            round=int(d["round"]),
            started_at=str(d["started_at"]),
            history=list(d["history"]),
            system_text=str(d["system_text"]),
            messages=list(d["messages"]),
            tools=list(d["tools"]),
            extra=dict(d["extra"]),
            stop=bool(d["stop"]),
            revision=int(d["revision"]),
        )

    def validate(self) -> Optional[str]:
        """快照结构校验(命名约束 + 字段类型,协议 schema 级)。

        合法返回 None;非法返回错误描述。revision 单调性由 host 侧
        递增逻辑保证,此处只校验形状。
        """
        if not self.session_id or not isinstance(self.session_id, str):
            return "snapshot.session_id 必须是非空字符串"
        if not isinstance(self.revision, int) or self.revision < 0:
            return "snapshot.revision 必须是非负整数"
        for key in self.extra:
            if not is_valid_setextra_key(str(key)):
                return f"snapshot.extra 含非法 SetExtra key: {key!r}"
        for i, m in enumerate(self.messages):
            problem = validate_message_shape(m)
            if problem:
                return f"snapshot.messages[{i}]: {problem}"
        return None

    def with_messages(self, messages: List[Message]) -> "Snapshot":
        """结构共享推进:仅替换 messages(新列表 + 共享元素引用,§18.2)。

        体积记账:新列表是旧列表的"纯追加"(逐元素**同一性**)时增量维护 messages
        JSON 长度(base 分量不含 messages,保持有效);截尾 / 换元素一律置为失效,
        由下一次预算检查精确重算。
        """
        return replace(self, messages=messages, _size_cache=self._cache_for(messages))

    def _cache_for(self, messages: List[Message]) -> Optional[_SizeCache]:
        cache = self._size_cache
        if cache is None or cache.msg_bytes < 0 or cache.messages_ref is not self.messages:
            return None
        old = self.messages
        if len(messages) < len(old):
            return None  # 截尾(loop 终止清理):不做减法记账,直接重算
        if any(a is not b for a, b in zip(old, messages[: len(old)])):
            return None
        msg_bytes = cache.msg_bytes
        for index in range(len(old), len(messages)):
            msg_bytes += _json_len(messages[index]) + (0 if index == 0 else 2)
        return _SizeCache(messages, msg_bytes, cache.base_bytes)

    def with_tools(self, tools: List[Dict[str, Any]]) -> "Snapshot":
        return replace(self, tools=list(tools), _size_cache=_base_unknown(self._size_cache))

    def with_round(self, round_: int) -> "Snapshot":
        """轮次推进:``round`` 在快照 JSON 中为整数,长度可精确差分,base 不失效。"""
        cache = self._size_cache
        if cache is not None and cache.base_bytes >= 0:
            cache = _SizeCache(
                cache.messages_ref,
                cache.msg_bytes,
                cache.base_bytes + len(str(round_)) - len(str(self.round)),
            )
        return replace(self, round=round_, _size_cache=cache)

    def with_extra(self, extra: Dict[str, Any]) -> "Snapshot":
        return replace(self, extra=dict(extra), _size_cache=_base_unknown(self._size_cache))

    def with_stop(self, stop: bool = True, reason: Optional[str] = None) -> "Snapshot":
        # stop_reason 不进 to_dict(仅 stop 进),故只失效 base 分量
        return replace(self, stop=stop, stop_reason=reason,
                       _size_cache=_base_unknown(self._size_cache))
