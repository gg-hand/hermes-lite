"""工具执行错误分类模块。

对工具返回的字符串进行语义分类，区分：
- SUCCESS: 正常返回
- TRANSIENT: 临时性错误（可重试）
- ANTI_CRAWLER: 反爬虫机制
- PERMANENT: 永久性错误（不应重试）
- AUTH_REQUIRED: 需要认证
- UNKNOWN: 无法判断
- PARAM_ERROR: 参数校验失败（Phase D 新增）
- NOT_FOUND: 资源不存在（Phase D 新增）
- PERMISSION: 权限不足（Phase D 新增）
- TIMEOUT: 执行超时（Phase D 新增）
- INTERNAL_ERROR: 兜底内部错误（Phase D 新增）

零依赖（仅 logging），纯函数式模块。被 ReactLoop 集成，
用于增强 _detect_tool_stuck 的语义感知与策略提示。

.. deprecated:: Phase E
    本模块在 ToolError 系统上线后退化为兜底识别器，仅用于识别未迁移 handler
    返回的错误字符串。Phase C handler 全部迁移完成后可下线。新代码应直接
    抛 ToolError 子类，不依赖本模块分类。

用法::

    from .error_classifier import ErrorClassifier, ErrorClass
    ec, reason = ErrorClassifier.classify("web_fetch", {...}, result_str)
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Tuple

logger = logging.getLogger(__name__)

# Phase D 标记位：本模块在 ToolError 系统上线后退化为兜底识别器。
# monitoring 可读取此值展示警告；Phase E handler 全部迁移完成后可下线。
DEPRECATED = True


class ErrorClass(Enum):
    """工具执行错误的语义分类。"""

    # 旧值（保留，向后兼容）
    SUCCESS = "success"
    TRANSIENT = "transient"
    ANTI_CRAWLER = "anti_crawler"
    PERMANENT = "permanent"
    AUTH_REQUIRED = "auth_required"
    UNKNOWN = "unknown"
    # 新增（Phase D 兜底，仅在 ErrorClassifier 路径产生）
    PARAM_ERROR = "param_error"
    NOT_FOUND = "not_found"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


# ---------------------------------------------------------------------------
# HTTP 状态码 → ErrorClass 映射（高置信度）
# ---------------------------------------------------------------------------

# 反爬虫/风控状态码
_ANTI_CRAWLER_STATUS = frozenset({403, 412})

# 永久性客户端错误
_PERMANENT_STATUS = frozenset({404, 410})

# 需要认证
_AUTH_STATUS = frozenset({401})

# 临时性错误（服务端）
_TRANSIENT_STATUS_MIN = 500

# ---------------------------------------------------------------------------
# 反爬虫关键词（大小写不敏感）
# ---------------------------------------------------------------------------

_ANTI_CRAWLER_KEYWORDS = [
    "captcha",
    "验证码",
    "人机验证",
    "安全验证",
    "access denied",
    "请求被拒绝",
    "too many requests",
    "频率限制",
    "访问频率",
    "triggered our security",
    "security check",
    "anti-bot",
    "反爬",
    "请完成安全验证",
    "verify you are human",
    "are you a robot",
]

# 编译正则：匹配任一关键词（大小写不敏感）
_ANTI_CRAWLER_RE = re.compile(
    "|".join(re.escape(kw) for kw in _ANTI_CRAWLER_KEYWORDS),
    re.IGNORECASE,
)

# HTTP 状态行：行首 [HTTP nnn]
_HTTP_STATUS_RE = re.compile(r"^\[HTTP\s+(\d{3})\]")

# 超时关键词
_TIMEOUT_RE = re.compile(r"超时|timeout|timed.?out", re.IGNORECASE)

# 不存在/未找到关键词（行首匹配专用，扩充覆盖真实工具错误格式）
# 真实工具错误：``文件不存在: <path>`` / ``路径不存在: <path>`` / ``not found: xxx``
_NOT_FOUND_RE = re.compile(
    r"不存在|not found|no such|doesn.?t exist|找不到|未找到",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 通用路径增强：三重误判防护
# ---------------------------------------------------------------------------
# 防护 1：扫描窗口限制 —— 仅检查结果前 N 字符
# 工具自身的错误信息一定在结果开头，源码字面量散落在文件各处，
# 限制扫描窗口可避免命中中后部字面量
_KEYWORD_SCAN_LIMIT = 1000

# 防护 2：引号字符 —— 用于排除源码字符串字面量
# 源码字符串字面量如 ``"文件元数据不存在"`` 不应被识别为工具错误
_QUOTE_CHARS = ('"', "'", "`")

# 防护 3：行首主语前缀白名单
# 关键词必须位于行首（仅缩进）或紧邻简短主语前缀（如 ``文件: ``、``路径: ``）
# 主语 + 可选分隔符（``:`` 或 ``：`` 或空白）
_LINE_START_SUBJECT_RE = re.compile(
    r"^[ \t]*"
    r"(?:"
    r"(?:文件|路径|目录|资源|对象|条目|记录|模块|属性|字段|配置)"
    r"|"
    r"(?:item|file|path|resource|entry|key|record|module|attribute|field|config)"
    r")"
    r"\s*[:：]?\s*$",
    re.IGNORECASE,
)


def _is_inside_quote(line: str, pos: int) -> bool:
    """检查 ``pos`` 是否在引号内字面量中。

    通过统计 ``pos`` 之前的引号字符数量判断：奇数表示在引号内。
    用于排除源码字符串字面量（如 ``"文件元数据不存在"``）的误判。

    参数:
        line: 当前行字符串。
        pos: 关键词在行中的起始位置。

    返回:
        True 表示 ``pos`` 在引号内字面量中。
    """
    prefix = line[:pos]
    return any(prefix.count(q) % 2 == 1 for q in _QUOTE_CHARS)


def _is_line_start_prefix(prefix: str) -> bool:
    """检查 not-found 关键词前的前缀是否符合行首约束。

    合法前缀：
    - 仅空白/缩进（关键词直接在行首）
    - 简短主语 + 可选分隔符（如 ``文件: ``、``路径: ``、``Error: ``）

    参数:
        prefix: 关键词前的所有字符。

    返回:
        True 表示前缀符合行首约束。
    """
    if not prefix.strip():
        return True
    return bool(_LINE_START_SUBJECT_RE.match(prefix))


def _match_with_triple_guard(text: str, pattern: re.Pattern) -> bool:
    """对 text 应用误判防护匹配 pattern（用于 timeout / command not found 检测）。

    与 _NOT_FOUND_RE 的行首约束不同，本函数用更精准的源码特征排除，
    避免 ``操作超时`` / ``bash: xxx: command not found`` 等真实错误被误伤。

    防护规则：
    1. 扫描窗口限制（调用方已截取 ``scan_text``）
    2. 引号内字面量排除：关键词在引号内视为源码字面量跳过
    3. 标识符字符排除：关键词前若是字母/下划线/数字（变量名一部分），跳过
       （如 ``DEFAULT_CMD_TIMEOUT`` 中的 ``TIMEOUT``）
    4. 源码特征字符排除：关键词前紧邻 ``= ( ,`` 等字符（函数参数），跳过
       （如 ``ssh_run_handler(..., timeout=60)`` 中的 ``timeout``）
    5. 注释行排除：前缀含 ``#`` 或 ``//`` 视为源码注释跳过
       （如 ``# command not found handler``）

    参数:
        text: 已截取扫描窗口的文本。
        pattern: 编译好的正则模式。

    返回:
        True 表示命中（通过所有防护）。
    """
    for line in text.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        # 防护 2：引号内字面量
        if _is_inside_quote(line, m.start()):
            continue
        # 防护 3：标识符字符排除（变量名一部分）
        # 仅排除 ASCII 字母/下划线/数字，避免误伤中文前缀（如 "操作超时" 中的 "作"）
        if m.start() > 0:
            prev_char = line[m.start() - 1]
            if prev_char.isascii() and (prev_char.isalnum() or prev_char == '_'):
                continue
        # 防护 4：源码特征字符排除（函数参数 = ( , 等）
        prefix = line[:m.start()].rstrip()
        if prefix and prefix[-1] in '=(),':
            continue
        # 防护 5：注释行排除（# 或 // 开头的源码注释）
        stripped_prefix = prefix.lstrip()
        if stripped_prefix.startswith('#') or stripped_prefix.startswith('//'):
            continue
        return True
    return False


class ErrorClassifier:
    """工具执行错误分类器。

    提供给 ReactLoop 在工具执行后调用，对字符串结果做语义级错误分类，
    辅助 _detect_tool_stuck 与策略提示决策。
    """

    @staticmethod
    def classify(
        tool_name: str,
        tool_input: dict,
        result: str,
    ) -> Tuple[ErrorClass, str]:
        """对工具返回结果做错误分类。

        参数:
            tool_name: 工具名称（影响分类策略，web_fetch 走 HTTP 路径）。
            tool_input: 工具输入参数（当前仅用于日志，保留给未来扩展）。
            result: 工具返回字符串。

        返回:
            ``(ErrorClass, reason_str)`` 二元组。``reason_str`` 包含
            可读原因（供日志 / 调试使用）。
        """
        if not result:
            return ErrorClass.UNKNOWN, "result is empty"

        # Phase D 新增：检测新 ToolError 系统 receipt 文本，直接返回 UNKNOWN
        # 避免与 ToolError 路径重复计数（见边界 9.15）。
        # 新 receipt 格式：[失败] xxx\n原因：xxx\n建议：xxx
        if result.startswith("[失败]"):
            return ErrorClass.UNKNOWN, "ToolError receipt (skip classification)"

        # web_fetch 专用路径：解析 [HTTP nnn] 前缀
        if tool_name == "web_fetch":
            ec, reason = ErrorClassifier._classify_http(result)
            # 无 [HTTP nnn] 前缀时降级到通用分类（httpx 异常等）
            if ec is ErrorClass.UNKNOWN and reason == "no [HTTP nnn] prefix found":
                return ErrorClassifier._classify_generic(result)
            return ec, reason

        # 通用路径：关键词匹配
        return ErrorClassifier._classify_generic(result)

    # ------------------------------------------------------------------
    # HTTP 专用分类
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_http(result: str) -> Tuple[ErrorClass, str]:
        """解析 [HTTP nnn] 前缀 + body 关键词做 HTTP 专用分类。

        先解析首行状态码：
        - 403/412 → ANTI_CRAWLER（高置信度）
        - 401 → AUTH_REQUIRED
        - 404/410 → PERMANENT
        - 429 → TRANSIENT（限流，可重试）
        - 5xx → TRANSIENT
        - 200 → 检查 body 反爬关键词
        - 无 [HTTP nnn] 前缀 → UNKNOWN

        参数:
            result: web_fetch 返回的完整字符串。

        返回:
            ``(ErrorClass, reason_str)`` 二元组。
        """
        first_line = result.split("\n", 1)[0]
        m = _HTTP_STATUS_RE.match(first_line)
        if not m:
            return ErrorClass.UNKNOWN, "no [HTTP nnn] prefix found"

        try:
            status_code = int(m.group(1))
        except (ValueError, IndexError):
            return ErrorClass.UNKNOWN, f"cannot parse status code from '{first_line}'"

        if status_code in _ANTI_CRAWLER_STATUS:
            return ErrorClass.ANTI_CRAWLER, (
                f"HTTP {status_code} (anti-crawler threshold)"
            )
        if status_code in _AUTH_STATUS:
            return ErrorClass.AUTH_REQUIRED, f"HTTP {status_code} (auth required)"
        if status_code in _PERMANENT_STATUS:
            return ErrorClass.PERMANENT, f"HTTP {status_code} (permanent error)"
        if status_code == 429:
            return ErrorClass.TRANSIENT, f"HTTP {status_code} (rate limited)"
        if status_code >= _TRANSIENT_STATUS_MIN:
            return ErrorClass.TRANSIENT, f"HTTP {status_code} (server error)"

        # 2xx / 3xx：检查 body 反爬关键词
        if 200 <= status_code < 400:
            body = result[result.index("\n") + 1:]  # 去掉首行
            if _ANTI_CRAWLER_RE.search(body):
                return ErrorClass.ANTI_CRAWLER, (
                    f"HTTP {status_code} but anti-crawler keywords found in body"
                )
            return ErrorClass.SUCCESS, f"HTTP {status_code} OK"

        # 其他状态码（如 418, 451 等少见码）
        return ErrorClass.UNKNOWN, f"HTTP {status_code} (unrecognized)"

    # ------------------------------------------------------------------
    # 通用分类（非 web_fetch 工具）
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_generic(result: str) -> Tuple[ErrorClass, str]:
        """通过关键词匹配做通用分类（bash_exec 等工具）。

        采用三重误判防护，避免对工具返回的大段源码/文档误判：

        1. **扫描窗口限制**：仅检查结果前 ``_KEYWORD_SCAN_LIMIT`` 字符，
           避免命中源码中后部字面量。
        2. **行首约束**：关键词必须位于行首（仅缩进）或紧邻简短主语
           前缀（如 ``文件: ``、``路径: ``）。工具自身错误一定在行首，
           源码字面量通常嵌入在更长行内。
        3. **引号排除**：关键词前若是奇数个引号字符，视为字符串字面量，
           跳过。排除 ``return {"error": "文件元数据不存在"}`` 这类源码。

        参数:
            result: 工具返回字符串。

        返回:
            ``(ErrorClass, reason_str)`` 二元组。
        """
        if not result:
            return ErrorClass.UNKNOWN, "result is empty"

        # 防护 1：限制扫描窗口
        scan_text = result[:_KEYWORD_SCAN_LIMIT]

        if _match_with_triple_guard(scan_text, _TIMEOUT_RE):
            return ErrorClass.TRANSIENT, "timeout keyword at line start (non-quoted)"
        # "command not found" 优先于通用 "not found"：缺少命令是临时性问题（可安装）
        _cmd_not_found_re = re.compile(
            r"command\s+not\s+found|未找到命令|command.*not\s+found",
            re.IGNORECASE,
        )
        if _match_with_triple_guard(scan_text, _cmd_not_found_re):
            return ErrorClass.TRANSIENT, "command not found (transient)"

        # PERMANENT：not-found 关键词 + 行首约束 + 引号排除
        for line in scan_text.splitlines():
            m = _NOT_FOUND_RE.search(line)
            if not m:
                continue
            # 防护 2：行首约束
            if not _is_line_start_prefix(line[:m.start()]):
                continue
            # 防护 3：引号内字面量排除
            if _is_inside_quote(line, m.start()):
                continue
            return ErrorClass.PERMANENT, "not-found at line start (non-quoted)"

        # Phase D 新增：PARAM_ERROR —— Python 参数校验失败
        if re.search(
            r"got an unexpected keyword argument|参数校验失败|schema validation",
            scan_text, re.IGNORECASE,
        ):
            return ErrorClass.PARAM_ERROR, "parameter validation failed"

        # Phase D 新增：PERMISSION —— 权限不足
        # 注意：HTTP 403 已在 _classify_http 路径识别为 ANTI_CRAWLER，
        # 此处仅匹配非 HTTP 上下文的 OS 权限拒绝
        if re.search(
            r"权限不足|permission denied|forbidden",
            scan_text, re.IGNORECASE,
        ):
            return ErrorClass.PERMISSION, "permission denied"

        return ErrorClass.UNKNOWN, "no recognizable error pattern"
