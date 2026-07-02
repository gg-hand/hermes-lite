"""工具执行错误分类模块。

对工具返回的字符串进行语义分类，区分：
- SUCCESS: 正常返回
- TRANSIENT: 临时性错误（可重试）
- ANTI_CRAWLER: 反爬虫机制
- PERMANENT: 永久性错误（不应重试）
- AUTH_REQUIRED: 需要认证
- UNKNOWN: 无法判断

零依赖（仅 logging），纯函数式模块。被 ReactLoop 集成，
用于增强 _detect_tool_stuck 的语义感知与策略提示。

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


class ErrorClass(Enum):
    """工具执行错误的语义分类。"""

    SUCCESS = "success"
    TRANSIENT = "transient"
    ANTI_CRAWLER = "anti_crawler"
    PERMANENT = "permanent"
    AUTH_REQUIRED = "auth_required"
    UNKNOWN = "unknown"


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

# 不存在/未找到
_NOT_FOUND_RE = re.compile(
    r"不存在|not found|no such|doesn.?t exist",
    re.IGNORECASE,
)


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

        参数:
            result: 工具返回字符串。

        返回:
            ``(ErrorClass, reason_str)`` 二元组。
        """
        if _TIMEOUT_RE.search(result):
            return ErrorClass.TRANSIENT, "timeout keyword matched"
        # "command not found" 优先于通用 "not found"：缺少命令是临时性问题（可安装）
        if re.search(r"command\s+not\s+found|未找到命令|command.*not\s+found", result, re.IGNORECASE):
            return ErrorClass.TRANSIENT, "command not found (transient)"
        if _NOT_FOUND_RE.search(result):
            return ErrorClass.PERMANENT, "not-found keyword matched"
        return ErrorClass.UNKNOWN, "no recognizable error pattern"
