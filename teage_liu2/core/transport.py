"""传输协议层(§9/§18.3 定案,阶段 3 落地):TransportFrame + JSON 基线编码 + 宿主侧消息枢纽。

对齐 PROTOCOL/transport.schema.json(唯一契约源):
- 帧: ``{type, payload, encoding: full|delta, protocol_version}``,JSON 单行编码
- 12 消息类型: invoke_hook / invoke_tool / invoke_llm / storage_write / storage_read /
  storage_query / storage_delete / task_register / task_cancel / event / heartbeat / shutdown
- delta 帧格式(极简增量,与 action 语义对齐): ``{base_revision, ops}``;
  delta 仅优化传输、不承载正确性 —— 接收方 base_revision 不匹配 → 请求 full 重发
  (宿主 v1.0 实现以 full 编码为主,§18.2 分层实施;delta 定义 + fallback 防御随本模块落地)

安全边界(§15-A7 序列化边界校验):
- 帧长上限 FRAME_MAX_BYTES / JSON 深度上限 JSON_MAX_DEPTH / 非法帧拒绝

宿主侧消息枢纽 :class:`TransportBus`:
- 扩展身份模型: register_extension(name, capabilities) —— kind 前缀隔离与 capability
  授权均以其为准(§15-A3/A4)
- 请求-响应统一: handle(extension_name, frame) -> response payload({result} / {error});
  响应/请求关联经帧 payload 约定(v1.0.0 顺序约束:同一方向同一时间仅一个挂起请求)
- invoke_llm: 走 LLMClient.chat_role 直调(协议级防重入,不进 pipeline/钩子链,§15-A5)
  + 并发信号量硬边界(§15-A6)
- storage_*: kind 前缀隔离强制(§15-A3)+ payload schema 校验(§15-A1)
- task_register/task_cancel: 宿主登记(任务归属扩展进程,T-4)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .hooks import CAP_LLM
from .types import is_valid_extension_name, is_valid_kind

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 消息类型(与 transport.schema.json MessageType 逐字一致)
# ---------------------------------------------------------------------------
MSG_INVOKE_HOOK = "invoke_hook"
MSG_INVOKE_TOOL = "invoke_tool"
MSG_INVOKE_LLM = "invoke_llm"
MSG_STORAGE_WRITE = "storage_write"
MSG_STORAGE_READ = "storage_read"
MSG_STORAGE_QUERY = "storage_query"
MSG_STORAGE_DELETE = "storage_delete"
MSG_TASK_REGISTER = "task_register"
MSG_TASK_CANCEL = "task_cancel"
MSG_EVENT = "event"
MSG_HEARTBEAT = "heartbeat"
MSG_SHUTDOWN = "shutdown"

MESSAGE_TYPES: frozenset = frozenset({
    MSG_INVOKE_HOOK, MSG_INVOKE_TOOL, MSG_INVOKE_LLM,
    MSG_STORAGE_WRITE, MSG_STORAGE_READ, MSG_STORAGE_QUERY, MSG_STORAGE_DELETE,
    MSG_TASK_REGISTER, MSG_TASK_CANCEL,
    MSG_EVENT, MSG_HEARTBEAT, MSG_SHUTDOWN,
})

STORAGE_MSG_TYPES: frozenset = frozenset({
    MSG_STORAGE_WRITE, MSG_STORAGE_READ, MSG_STORAGE_QUERY, MSG_STORAGE_DELETE,
})

# 编码类型(transport.schema.json EncodingType)
ENCODING_FULL = "full"
ENCODING_DELTA = "delta"
ENCODINGS: frozenset = frozenset({ENCODING_FULL, ENCODING_DELTA})

DEFAULT_PROTOCOL_VERSION = "v1.0.0"

# ---------------------------------------------------------------------------
# §15-A7 序列化边界(协议常量,帧长/JSON 深度上限)
# ---------------------------------------------------------------------------
#: 单帧字节上限(防超大帧 DoS)
FRAME_MAX_BYTES: int = 4 * 1024 * 1024
#: JSON 嵌套深度上限(防深度嵌套导致解析器栈溢出)
JSON_MAX_DEPTH: int = 64
#: 单条消息体积上限(与 types 域 ResourceLimits 对齐)
MAX_MESSAGE_BYTES: int = 512 * 1024

# invoke_llm 并发上限(§15-A6 信号量硬边界;阈值与 RESOURCE_LIMITS 同级)
INVOKE_LLM_MAX_CONCURRENCY: int = 4

# 错误码(消息级;宿主侧处理器返回,语义与 errors 域 ErrorCode 对齐)
ERR_INVALID_FRAME = "invalid_frame"
ERR_UNKNOWN_MESSAGE = "unknown_message_type"
ERR_INVALID_PAYLOAD = "invalid_payload"
ERR_PREFIX_VIOLATION = "kind_prefix_violation"
ERR_CAPABILITY_NOT_DECLARED = "capability_not_declared"
ERR_UNAVAILABLE = "unavailable"
ERR_STORAGE_FAILED = "storage_failed"
ERR_LLM_CALL_FAILED = "llm_call_failed"
ERR_TASK_REJECTED = "task_rejected"
ERR_INTERNAL = "internal_error"


# ---------------------------------------------------------------------------
# evolution 版本协商(§evolution V-2 / §10.2,阶段 4 落地)
# ---------------------------------------------------------------------------
#: semver 正则(允许可选 v 前缀,如 "v1.2.3" / "1.2.3")
_SEMVER_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")


def parse_protocol_version(version: str) -> tuple:
    """解析 semver 版本字符串 → (major, minor, patch)。

    非法格式抛 ValueError(可读错误)。注意:protocol_version 是
    ``^v?\\d+\\.\\d+\\.\\d+(-[0-9A-Za-z.-]+)?$``(lifecycle ProtocolVersion),
    预发布段不参与协商比较(仅信息保留)。
    """
    if not isinstance(version, str) or not version:
        raise ValueError(f"protocol_version 必须是非空字符串,实际 {version!r}")
    m = _SEMVER_RE.match(version.strip())
    if m is None:
        raise ValueError(
            f"非法 protocol_version: {version!r}"
            "(必须匹配 ^v?\\d+\\.\\d+\\.\\d+(-[0-9A-Za-z.-]+)?$)"
        )
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def negotiate_protocol_version(
    core_version: str, extension_version: Optional[str]
) -> Dict[str, Any]:
    """core 与扩展互验 protocol_version(§evolution V-2 / evolution.schema.json)。

    返回 ``{core_version, extension_version, compatible, action}``:
    - major 不匹配(任一方向破坏性变更)→ ``compatible=False, action="reject"``
      (握手失败,可读错误)
    - major 匹配且扩展 minor ≤ core minor → ``compatible=True, action="proceed"``
    - major 匹配且扩展 minor > core minor → ``compatible=True, action="downgrade"``
      (扩展新于 core,降级使用 core 能力子集 —— 双版本共存过渡期语义,§10.2)
    扩展未上报 protocol_version → 保守降级(``compatible=True, action="downgrade"``,
    扩展侧可能为旧实现,core 按能力子集对待)。
    """
    if extension_version is None:
        return {
            "core_version": core_version,
            "extension_version": None,
            "compatible": True,
            "action": "downgrade",
        }
    core_major, core_minor, _ = parse_protocol_version(core_version)
    ext_major, ext_minor, _ = parse_protocol_version(extension_version)
    if core_major != ext_major:
        return {
            "core_version": core_version,
            "extension_version": extension_version,
            "compatible": False,
            "action": "reject",
        }
    action = "downgrade" if ext_minor > core_minor else "proceed"
    return {
        "core_version": core_version,
        "extension_version": extension_version,
        "compatible": True,
        "action": action,
    }


# ---------------------------------------------------------------------------
# 序列化边界校验(§15-A7)
# ---------------------------------------------------------------------------
def json_depth(obj: Any, depth: int = 0) -> int:
    """递归计算 JSON 结构嵌套深度。"""
    if isinstance(obj, dict):
        if not obj:
            return depth + 1
        return max(json_depth(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return depth + 1
        return max(json_depth(v, depth + 1) for v in obj)
    return depth


def check_frame_limits(payload: Any, max_bytes: int = FRAME_MAX_BYTES) -> Optional[str]:
    """序列化边界校验(§15-A7):JSON 深度 + 体积上限。

    合法返回 None;非法返回错误描述(调用方据此拒绝该帧)。
    """
    if json_depth(payload) > JSON_MAX_DEPTH:
        return f"payload 嵌套深度超过上限 {JSON_MAX_DEPTH}"
    try:
        size = len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        return f"payload 不可 JSON 序列化: {e}"
    if size > max_bytes:
        return f"payload 体积 {size} 字节超过上限 {max_bytes}"
    return None


def validate_payload_schema(msg_type: str, payload: Any) -> Optional[str]:
    """消息 payload schema 级校验(§15-A1 协议边界)。

    对齐 PROTOCOL/transport.schema.json + storage.schema.json 的各消息定义;
    合法返回 None;非法返回错误描述(调用方拒帧并返回 error 响应)。
    """
    if not isinstance(payload, dict):
        return f"{msg_type}.payload 必须是对象,实际 {type(payload).__name__}"
    if msg_type == MSG_INVOKE_LLM:
        role = payload.get("role")
        if not isinstance(role, str) or not role:
            return "invoke_llm.role 必须是非空字符串"
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return "invoke_llm.messages 必须是非空数组"
        for i, m in enumerate(messages):
            if not isinstance(m, dict) or not m.get("role"):
                return f"invoke_llm.messages[{i}] 非法(应为含 role 的对象)"
        system = payload.get("system")
        if system is not None and not isinstance(system, str):
            return "invoke_llm.system 必须是字符串或 null"
        max_tokens = payload.get("max_tokens")
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
        ):
            return "invoke_llm.max_tokens 必须是正整数或 null"
        return None
    if msg_type == MSG_STORAGE_WRITE:
        kind = payload.get("kind")
        if not is_valid_kind(kind):
            return "storage_write.kind 必须匹配 ^[a-z0-9_.]+$"
        docs = payload.get("docs")
        if not isinstance(docs, list) or not docs:
            return "storage_write.docs 必须是非空数组"
        if any(not isinstance(d, dict) for d in docs):
            return "storage_write.docs 元素必须是对象"
        return None
    if msg_type in (MSG_STORAGE_READ, MSG_STORAGE_DELETE):
        kind = payload.get("kind")
        if not is_valid_kind(kind):
            return f"{msg_type}.kind 必须匹配 ^[a-z0-9_.]+$"
        doc_id = payload.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            return f"{msg_type}.doc_id 必须是非空字符串"
        return None
    if msg_type == MSG_STORAGE_QUERY:
        kind = payload.get("kind")
        if not is_valid_kind(kind):
            return "storage_query.kind 必须匹配 ^[a-z0-9_.]+$"
        limit = payload.get("limit")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            return "storage_query.limit 必须是正整数或 null"
        return None
    if msg_type == MSG_TASK_REGISTER:
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return "task_register.task_id 必须是非空字符串"
        return None
    if msg_type == MSG_TASK_CANCEL:
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return "task_cancel.task_id 必须是非空字符串"
        return None
    # invoke_hook / invoke_tool / event / heartbeat / shutdown:payload 自由形态
    return None


def check_kind_prefix(extension_name: str, kind: str) -> Optional[str]:
    """kind 前缀隔离校验(§15-A3 / storage S-3/S-4)。

    谓词 = ``kind.startswith(f"{extension_name}.")`` 且 extension_name 匹配
    ``^[a-z0-9_]+$``;合法返回 None;非法返回错误描述。
    """
    if not is_valid_extension_name(extension_name):
        return f"非法 extension_name: {extension_name!r}(必须匹配 ^[a-z0-9_]+$)"
    if not isinstance(kind, str) or not kind:
        return "kind 必须是非空字符串"
    if not kind.startswith(f"{extension_name}."):
        return (
            f"kind {kind!r} 不属于扩展 {extension_name!r}:kind 必须带 "
            f"'{extension_name}.' 前缀(跨前缀访问拒绝,§15-A3)"
        )
    return None


# ---------------------------------------------------------------------------
# TransportFrame(§9/§18.3):type/payload/encoding/protocol_version
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TransportFrame:
    """传输帧:core 与扩展间一切消息的统一封装(JSON 单行编码)。"""

    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    encoding: str = ENCODING_FULL
    protocol_version: str = DEFAULT_PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """序列化为协议帧 dict(4 键,与 TransportFrame schema 逐字一致)。"""
        return {
            "type": self.type,
            "payload": self.payload,
            "encoding": self.encoding,
            "protocol_version": self.protocol_version,
        }

    def encode(self) -> str:
        """编码为 JSON 单行(帧长边界校验在 decode 侧同样强制)。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def decode(cls, line: str) -> "TransportFrame":
        """解码 + 序列化边界校验(§15-A7)。

        非法帧(非 JSON / 非对象 / 类型非法 / 编码非法 / 版本缺失 / 超限)→ ValueError。
        """
        if len(line) > FRAME_MAX_BYTES:
            raise ValueError(f"帧超过字节上限 {FRAME_MAX_BYTES}")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"非法帧:JSON 解析失败: {e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"非法帧:帧必须是对象,实际 {type(obj).__name__}")
        if "type" not in obj or "payload" not in obj or "encoding" not in obj or "protocol_version" not in obj:
            missing = sorted({"type", "payload", "encoding", "protocol_version"} - set(obj))
            raise ValueError(f"非法帧:缺少字段 {missing}")
        if obj["type"] not in MESSAGE_TYPES:
            raise ValueError(f"非法帧:未知消息类型 {obj['type']!r}")
        if obj["encoding"] not in ENCODINGS:
            raise ValueError(f"非法帧:未知编码 {obj['encoding']!r}")
        if not isinstance(obj["protocol_version"], str) or not obj["protocol_version"]:
            raise ValueError("非法帧:protocol_version 必须是非空字符串")
        problem = check_frame_limits(obj)
        if problem:
            raise ValueError(f"非法帧:{problem}")
        # delta 编码:payload 必须符合 {base_revision, ops} 形态(§transport.3)
        if obj["encoding"] == ENCODING_DELTA:
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("非法 delta 帧:payload 必须是对象")
            if "base_revision" not in payload or "ops" not in payload:
                raise ValueError("非法 delta 帧:payload 缺少 base_revision/ops")
            if isinstance(payload.get("base_revision"), bool) or not isinstance(
                payload.get("base_revision"), int
            ):
                raise ValueError("非法 delta 帧:base_revision 必须是整数")
            if not isinstance(payload.get("ops"), list):
                raise ValueError("非法 delta 帧:ops 必须是数组")
        return cls(
            type=obj["type"],
            payload=obj["payload"],
            encoding=obj["encoding"],
            protocol_version=obj["protocol_version"],
        )

    # 请求-响应约定(实现层,payload 顶层字段):
    # - 请求帧 payload = 消息结构(无 result/error 键)
    # - 响应帧 payload = {"result": ...} 或 {"error": {code, message}}
    def is_response(self) -> bool:
        """是否为响应帧(约定:payload 含 result/error 键)。"""
        if not isinstance(self.payload, dict):
            return False
        return "result" in self.payload or "error" in self.payload


# ---------------------------------------------------------------------------
# delta 帧(极简增量,与 action 语义对齐;非 RFC 6902,§transport.3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeltaFrame:
    """delta 帧载荷: ``{base_revision, ops}``。

    ops 元素(与 action 语义对齐)::
        {"op": "append_messages", "messages": [...]}
        {"op": "overwrite", "tools": [...] | "system": str | "extra_kv": {...} | "stop": bool}

    fallback(§transport.3):接收方 base_revision 不匹配(进程重启/断链重连)→
    请求 full 重发;delta 仅优化传输、不承载正确性。
    """

    base_revision: int
    ops: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"base_revision": self.base_revision, "ops": list(self.ops)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeltaFrame":
        if not isinstance(d, dict):
            raise ValueError(f"delta 帧必须是对象,实际 {type(d).__name__}")
        if "base_revision" not in d or "ops" not in d:
            raise ValueError("delta 帧缺少 base_revision/ops")
        if isinstance(d["base_revision"], bool) or not isinstance(d["base_revision"], int):
            raise ValueError("delta 帧 base_revision 必须是整数")
        if not isinstance(d["ops"], list):
            raise ValueError("delta 帧 ops 必须是数组")
        return cls(base_revision=d["base_revision"], ops=list(d["ops"]))

    def revision_matches(self, current_revision: int) -> bool:
        """base_revision 与接收方当前 revision 匹配(不匹配 → full 重发)。"""
        return self.base_revision == current_revision


# ---------------------------------------------------------------------------
# TransportBus:宿主侧消息枢纽(§9/§18.3)
# ---------------------------------------------------------------------------
class TransportBus:
    """宿主侧传输枢纽:扩展(经 stdio 通道 / in-process 通道)访问宿主能力的唯一入口。

    - 扩展身份: ``register_extension(name, capabilities)`` 注册后,kind 前缀隔离
      (§15-A3)与 capability 授权(§15-A4)均以其为准
    - 统一请求处理: ``handle(extension_name, frame) -> response payload``,
      响应 payload 统一 ``{"result": ...}`` / ``{"error": {code, message}}``
    - invoke_llm:直调 LLMClient.chat_role(防重入 §15-A5)+ 并发信号量(§15-A6)
    - 未注册扩展的入站请求一律拒绝(身份模型强制)
    """

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        storage_provider: Optional[Any] = None,
        task_registry: Optional[Any] = None,
        storage_writer: Optional[Any] = None,
    ) -> None:
        self._llm = llm_client
        self._storage = storage_provider
        self._tasks = task_registry
        #: StorageWriter 异步单写者(§18.1):扩展 storage_write 亦经写队列,
        #: 与主对话消息落盘共享 FIFO 单写者(§18.1"全部 SQLite 写经写队列")
        self._storage_writer = storage_writer
        #: 扩展身份表:name -> {"capabilities": frozenset}
        self._extensions: Dict[str, Dict[str, Any]] = {}
        #: invoke_llm 并发信号量(§15-A6 硬边界)
        self._llm_semaphore = asyncio.Semaphore(INVOKE_LLM_MAX_CONCURRENCY)

    # ------------------------------------------------------------------
    # 扩展身份
    # ------------------------------------------------------------------
    def register_extension(self, extension_name: str, capabilities: List[str]) -> None:
        """注册扩展身份(供 kind 前缀/capability 授权;重复注册覆盖声明)。"""
        if not is_valid_extension_name(extension_name):
            raise ValueError(
                f"非法 extension_name: {extension_name!r}(必须匹配 ^[a-z0-9_]+$)"
            )
        self._extensions[extension_name] = {
            "capabilities": frozenset(capabilities or []),
        }

    def unregister_extension(self, extension_name: str) -> None:
        self._extensions.pop(extension_name, None)

    def is_registered(self, extension_name: str) -> bool:
        return extension_name in self._extensions

    def capabilities_of(self, extension_name: str) -> frozenset:
        return self._extensions.get(extension_name, {}).get("capabilities", frozenset())

    def make_in_process_port(self, extension_name: str) -> Any:
        """进程内能力端口(§18.2 消息语义零成本实现):同语言扩展经此走 storage_*/invoke_llm 消息。

        非对象引用注入 —— 是协议消息通道的本地实现(调用方仍在消息语义下与宿主交互)。
        """
        return InProcessHostPort(self, extension_name)

    # ------------------------------------------------------------------
    # 入站请求统一入口
    # ------------------------------------------------------------------
    async def handle(self, extension_name: str, frame: TransportFrame) -> Dict[str, Any]:
        """处理扩展入站请求帧,返回响应 payload({result} / {error})。

        未注册扩展 → 拒绝(身份模型强制);处理器异常 → error 响应(绝不抛)。
        """
        if extension_name not in self._extensions:
            return {
                "error": {
                    "code": ERR_UNAVAILABLE,
                    "message": f"扩展 {extension_name!r} 未注册,拒绝入站请求",
                }
            }
        try:
            problem = validate_payload_schema(frame.type, frame.payload)
            if problem:
                return {
                    "error": {"code": ERR_INVALID_PAYLOAD, "message": problem}
                }
            msg_type = frame.type
            payload = frame.payload or {}
            if msg_type == MSG_INVOKE_LLM:
                return await self._handle_invoke_llm(extension_name, payload)
            if msg_type in STORAGE_MSG_TYPES:
                return await self._handle_storage(extension_name, msg_type, payload)
            if msg_type == MSG_TASK_REGISTER:
                return self._handle_task_register(extension_name, payload)
            if msg_type == MSG_TASK_CANCEL:
                return self._handle_task_cancel(extension_name, payload)
            if msg_type == MSG_HEARTBEAT:
                return {"result": {"status": "ok", "protocol_version": DEFAULT_PROTOCOL_VERSION}}
            return {
                "error": {
                    "code": ERR_UNKNOWN_MESSAGE,
                    "message": f"宿主不处理入站消息类型: {msg_type}",
                }
            }
        except Exception as e:
            logger.error(
                "TransportBus 处理 %s 的 %s 消息异常: %s",
                extension_name, frame.type, e,
            )
            return {"error": {"code": ERR_INTERNAL, "message": str(e)}}

    # ------------------------------------------------------------------
    # invoke_llm(§15-A5 防重入 + §15-A6 并发上限)
    # ------------------------------------------------------------------
    async def _handle_invoke_llm(
        self, extension_name: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        caps = self.capabilities_of(extension_name)
        if CAP_LLM not in caps:
            return {
                "error": {
                    "code": ERR_CAPABILITY_NOT_DECLARED,
                    "message": f"扩展 {extension_name} 未声明 llm capability,拒绝 invoke_llm(§15-A4)",
                }
            }
        if self._llm is None:
            return {
                "error": {
                    "code": ERR_UNAVAILABLE,
                    "message": "宿主未配置 LLM 客户端,invoke_llm 不可用",
                }
            }
        role = payload.get("role") or "main"
        messages = payload.get("messages") or []
        system = payload.get("system")
        max_tokens = payload.get("max_tokens")
        # §15-A5 协议级防重入:直调 LLMClient.chat_role(LLMAdapter 直调),
        # 不进 pipeline/钩子链,不存在重入钩子链的路径
        # §15-A6 并发上限:信号量硬边界(超出等待,不并发放大)
        async with self._llm_semaphore:
            try:
                resp = await self._llm.chat_role(
                    role=role, messages=messages,
                    system=system, max_tokens=max_tokens,
                )
            except Exception as e:
                logger.error("invoke_llm 失败(扩展 %s, role=%s): %s", extension_name, role, e)
                return {
                    "error": {"code": ERR_LLM_CALL_FAILED, "message": str(e)}
                }
        return {
            "result": {
                "content_blocks": resp.content,
                "stop_reason": resp.stop_reason,
                "usage": resp.usage,
            }
        }

    # ------------------------------------------------------------------
    # storage_*(§15-A3 kind 前缀隔离)
    # ------------------------------------------------------------------
    async def _handle_storage(
        self, extension_name: str, msg_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        kind = payload.get("kind", "")
        prefix_problem = check_kind_prefix(extension_name, kind)
        if prefix_problem:
            return {"error": {"code": ERR_PREFIX_VIOLATION, "message": prefix_problem}}
        if self._storage is None:
            return {
                "error": {"code": ERR_UNAVAILABLE, "message": "宿主未配置存储平台"}
            }
        try:
            if msg_type == MSG_STORAGE_WRITE:
                docs = payload.get("docs") or []
                # §18.1 扩展写经 StorageWriter 异步单写者:与主对话消息落盘
                # 共享单一 FIFO 写队列(全部 SQLite 写经写队列);未配置 writer
                # (None,旧调用方/测试)→ 降级 asyncio.to_thread(遵守 async 铁律)
                if self._storage_writer is not None:
                    doc_ids = await self._storage_writer.enqueue_flush(
                        lambda: self._storage.write(kind, docs)
                    )
                else:
                    doc_ids = await asyncio.to_thread(self._storage.write, kind, docs)
                return {"result": {"doc_ids": doc_ids}}
            if msg_type == MSG_STORAGE_READ:
                doc = await asyncio.to_thread(
                    self._storage.read, kind, payload.get("doc_id", "")
                )
                return {"result": {"doc": doc}}
            if msg_type == MSG_STORAGE_QUERY:
                limit = payload.get("limit")
                filters = payload.get("filters") or {}
                docs = await asyncio.to_thread(
                    self._storage.query, kind, limit, **filters
                )
                return {"result": {"docs": docs}}
            if msg_type == MSG_STORAGE_DELETE:
                await asyncio.to_thread(
                    self._storage.delete, kind, payload.get("doc_id", "")
                )
                return {"result": {}}
        except Exception as e:
            logger.error("storage_%s 失败(扩展 %s, kind=%s): %s", msg_type, extension_name, kind, e)
            return {"error": {"code": ERR_STORAGE_FAILED, "message": str(e)}}
        return {"error": {"code": ERR_UNKNOWN_MESSAGE, "message": msg_type}}

    # ------------------------------------------------------------------
    # task_*(T-4:宿主登记,任务归属扩展进程)
    # ------------------------------------------------------------------
    def _handle_task_register(
        self, extension_name: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        task_id = payload.get("task_id", "")
        if self._tasks is None:
            return {"result": {}}  # 无任务注册表:登记 no-op(宿主不承载执行)
        try:
            self._tasks.register_task(
                task_id, description=payload.get("description", ""), owner=extension_name
            )
        except Exception as e:
            logger.error("task_register 失败(扩展 %s): %s", extension_name, e)
            return {"error": {"code": ERR_TASK_REJECTED, "message": str(e)}}
        return {"result": {}}

    def _handle_task_cancel(
        self, extension_name: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        task_id = payload.get("task_id", "")
        if self._tasks is None:
            return {"result": {}}
        try:
            self._tasks.cancel_task(task_id)
        except Exception as e:
            logger.error("task_cancel 失败(扩展 %s): %s", extension_name, e)
            return {"error": {"code": ERR_TASK_REJECTED, "message": str(e)}}
        return {"result": {}}

    async def close(self) -> None:
        """关闭枢纽(幂等)。"""
        self._extensions.clear()


class InProcessHostPort:
    """进程内能力端口(§18.2):同语言扩展经此走消息语义访问宿主能力。

    零拷贝引用仅限快照/值对象;storage_*/invoke_llm/task_* 一律经
    TransportBus.handle 消息路径(prefix 隔离/防重入/并发上限全生效),
    不绕过协议直接抓宿主对象引用(§transport.1)。
    """

    def __init__(self, bus: TransportBus, extension_name: str) -> None:
        self._bus = bus
        self._name = extension_name

    async def storage_write(self, kind: str, docs: List[dict]) -> Any:
        resp = await self._bus.handle(
            self._name,
            TransportFrame(MSG_STORAGE_WRITE, {"kind": kind, "docs": docs}),
        )
        self._raise_if_error(resp)
        return resp.get("result", {}).get("doc_ids")

    async def storage_read(self, kind: str, doc_id: str) -> Any:
        resp = await self._bus.handle(
            self._name,
            TransportFrame(MSG_STORAGE_READ, {"kind": kind, "doc_id": doc_id}),
        )
        self._raise_if_error(resp)
        return resp.get("result", {}).get("doc")

    async def storage_query(self, kind: str, limit: Optional[int] = None, **filters) -> List[dict]:
        payload: Dict[str, Any] = {"kind": kind, "filters": filters}
        if limit is not None:
            payload["limit"] = limit
        resp = await self._bus.handle(
            self._name, TransportFrame(MSG_STORAGE_QUERY, payload)
        )
        self._raise_if_error(resp)
        return resp.get("result", {}).get("docs") or []

    async def storage_delete(self, kind: str, doc_id: str) -> None:
        resp = await self._bus.handle(
            self._name,
            TransportFrame(MSG_STORAGE_DELETE, {"kind": kind, "doc_id": doc_id}),
        )
        self._raise_if_error(resp)

    async def invoke_llm(
        self,
        role: str = "main",
        messages: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": role, "messages": messages or []}
        if system is not None:
            payload["system"] = system
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        resp = await self._bus.handle(
            self._name, TransportFrame(MSG_INVOKE_LLM, payload)
        )
        self._raise_if_error(resp)
        return resp.get("result", {})

    def register_task(self, task_id: str, description: str = "") -> None:
        self._bus.handle(
            self._name,
            TransportFrame(MSG_TASK_REGISTER, {"task_id": task_id, "description": description}),
        )

    def cancel_task(self, task_id: str) -> None:
        self._bus.handle(
            self._name, TransportFrame(MSG_TASK_CANCEL, {"task_id": task_id})
        )

    @staticmethod
    def _raise_if_error(resp: Dict[str, Any]) -> None:
        if "error" in resp:
            error = resp["error"] or {}
            raise RuntimeError(f"{error.get('code', 'error')}: {error.get('message', '')}")
