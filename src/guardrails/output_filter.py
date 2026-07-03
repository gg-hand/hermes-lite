"""输出侧 PII 过滤器（Phase 9 Task 3）。

本模块提供 ``OutputFilter``，在 LLM 响应返回用户前过滤个人身份信息（PII），
防止手机号 / 身份证 / 邮箱 / 银行卡等敏感数据泄漏。零外部依赖，仅使用
``re`` 与 ``logging``，与项目其他模块风格一致。

设计要点：
- 零外部依赖：仅 ``re`` 与 ``logging``，与项目其他模块风格一致。
- 内置 PII 正则：
  - 手机号：``1[3-9]\\d{9}``（11 位）
  - 身份证：``\\d{17}[\\dXx]``（18 位，末位可为 X）
  - 邮箱：``[\\w.+-]+@[\\w-]+\\.[\\w.]+``
  - 银行卡（可选）：``\\d{16,19}``
- ``filter`` 方法对输入文本执行 PII 替换，返回
  ``(filtered_text, replacements_count)`` 二元组，匹配项替换为占位符
  （如 ``[手机号已脱敏]``）。
- 不实现 ``filter_streaming_chunk``：流式 token 级无法识别跨块 PII
  （如手机号被拆分到两个 token），流式场景应由上层在聚合完整响应后
  调用 ``filter``。
- 二进制内容跳过：非打印字符占比 >30% 视为 base64 / 二进制，跳过 PII
  正则，避免对二进制数据误判（例如 base64 编码的图片字节流可能恰好
  含 11 位数字片段）。
- 误报接受：脱敏优于漏报。11 位数字订单号如 ``13800138000`` 会被
  匹配为手机号，这是可接受的边界，调用方按需在 UI 上提示用户即可。
- ``detect_prompt_leakage`` 可选检测 ``SYSTEM_PROMPT`` 泄漏：响应同时
  包含 ``"指令优先级"`` 与 ``"Hermes Lite"`` 时判定为泄漏。

PII 正则匹配顺序（特异性优先，避免相互覆盖）：
1. **邮箱**：含 ``@`` 与域名点，特异性最高，先匹配。
2. **身份证**：18 位且末位可为 ``X``，比手机号更长更具体。
3. **手机号**：11 位 ``1[3-9]\\d{9}``。
4. **银行卡**：``\\d{16,19}`` 最宽泛，放最后，仅对前序未替换的长
   数字串生效（身份证已在前序步骤被替换为占位符，不会再被银行卡
   正则误匹配）。

注：19 位银行卡前 18 位会被身份证正则 ``\\d{17}[\\dXx]`` 匹配（末位
为数字亦满足 ``[\\dXx]``），导致银行卡被识别为身份证。此为已知边界，
因脱敏行为一致（均替换为占位符），不影响安全语义，按"误报接受"原则
不额外处理。
"""

from __future__ import annotations

import logging
import re
from typing import Pattern, Tuple

logger = logging.getLogger(__name__)

# PII 类型 → 占位符映射
_PLACEHOLDERS = {
    "phone": "[手机号已脱敏]",
    "id_card": "[身份证已脱敏]",
    "email": "[邮箱已脱敏]",
    "bank_card": "[银行卡已脱敏]",
}

# PII 正则：按"特异性优先"排序。
# 邮箱含 @ 符号，特异性最高；身份证 18 位且末位可能为 X；
# 手机号 1[3-9]\d{9}（11 位）；银行卡 \d{16,19} 最宽泛，放最后。
# 注意：银行卡正则会与身份证（前 17/18 位）和手机号（前 11 位）冲突，
# 因此必须放在最后匹配，仅对前序未替换的长数字串生效。
_PII_PATTERNS: Tuple[Tuple[str, Pattern], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("id_card", re.compile(r"\d{17}[\dXx]")),
    ("phone", re.compile(r"1[3-9]\d{9}")),
    ("bank_card", re.compile(r"\d{16,19}")),
)

# SYSTEM_PROMPT 泄漏检测的标志性短语（同时出现判定为泄漏）
_LEAKAGE_MARKER_PROMPT_PRIORITY = "指令优先级"
_LEAKAGE_MARKER_HERMES = "Hermes Lite"


class OutputFilter:
    """输出侧 PII 过滤器。

    ``filter`` 方法对文本执行 PII 检测与替换，将匹配到的 PII 替换为
    占位符（如 ``[手机号已脱敏]``）。支持检测 base64 / 二进制内容并跳过
    PII 正则（避免对二进制数据误判）。

    使用示例::

        >>> f = OutputFilter()
        >>> filtered, n = f.filter("联系我: 13800138000")
        >>> filtered
        '联系我: [手机号已脱敏]'
        >>> n
        1
    """

    # 非打印字符占比阈值：超过此值视为 base64 / 二进制内容，跳过 PII 正则
    _BINARY_RATIO_THRESHOLD = 0.30

    def __init__(self, enable_bank_card: bool = True) -> None:
        """初始化输出过滤器。

        参数:
            enable_bank_card: 是否启用银行卡检测。银行卡正则
            ``\\d{16,19}`` 较宽泛，可能误报长数字串（如订单号、
            时间戳毫秒）。默认 ``True``（脱敏优于漏报），可由调用方
            按场景关闭（例如订单查询场景可关闭以减少误报）。
        """
        self._enable_bank_card = enable_bank_card

    def filter(self, text: str) -> Tuple[str, int]:
        """过滤文本中的 PII，返回替换后的文本与替换次数。

        过滤流程：
        1. 空字符串或 ``None`` → 原样返回，0 替换。
        2. 检测 base64 / 二进制内容（非打印字符占比 > 30%）→ 跳过 PII
           正则，原样返回，0 替换。避免对二进制数据（如 base64 编码
           的字节流）误判。
        3. 按特异性优先顺序应用 PII 正则（邮箱 > 身份证 > 手机号 >
           银行卡），匹配项替换为占位符（如 ``[手机号已脱敏]``）。
           每个正则独立计数，累加得 ``replacements_count``。

        参数:
            text: 待过滤的 LLM 响应文本。``None`` 视为空字符串处理
                （返回 ``("", 0)``）。

        返回:
            ``(filtered_text, replacements_count)`` 二元组：
            - ``filtered_text``：替换 PII 后的文本（无 PII 时与输入
              相同的对象引用）。
            - ``replacements_count``：替换的 PII 数量（0 表示未检测
              到 PII 或二进制内容跳过）。
        """
        if not text:
            return text, 0

        # 检测 base64 / 二进制内容：非打印字符占比 > 阈值时跳过 PII 正则
        if self._is_binary_like(text):
            logger.debug(
                "输出文本非打印字符占比 > %.0f%%，跳过 PII 过滤",
                self._BINARY_RATIO_THRESHOLD * 100,
            )
            return text, 0

        # 按特异性优先顺序应用 PII 正则
        # 每次替换后文本会变化，需顺序应用并在每步累计计数。
        # 顺序保证：邮箱先匹配（避免被手机号 / 银行卡截断含 @ 的子串），
        # 身份证先于手机号（避免身份证前 11 位被识别为手机号），
        # 银行卡最后（避免与身份证 / 手机号冲突）。
        filtered = text
        replacements = 0
        for pii_type, pattern in self._get_active_patterns():
            new_filtered, count = pattern.subn(
                _PLACEHOLDERS[pii_type], filtered
            )
            if count > 0:
                filtered = new_filtered
                replacements += count

        return filtered, replacements

    def detect_prompt_leakage(self, text: str) -> bool:
        """检测响应是否泄漏 ``SYSTEM_PROMPT``（可选）。

        当响应同时包含 ``"指令优先级"`` 与 ``"Hermes Lite"`` 时判定为
        泄漏 ``SYSTEM_PROMPT``。这两个短语是 ``src/llm/prompts.py`` 中
        ``SYSTEM_PROMPT`` 的标志性内容（首段标题与角色描述），同时出现
        强烈提示模型在响应中复述了系统提示词。

        参数:
            text: LLM 响应文本。

        返回:
            是否疑似泄漏 ``SYSTEM_PROMPT``。空字符串返回 ``False``。
        """
        if not text:
            return False
        # 必须同时含两个标志性短语才判定为泄漏（降低误报）
        if _LEAKAGE_MARKER_PROMPT_PRIORITY not in text:
            return False
        return _LEAKAGE_MARKER_HERMES in text

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _get_active_patterns(self) -> Tuple[Tuple[str, Pattern], ...]:
        """返回当前启用的 PII 正则列表。

        ``enable_bank_card=False`` 时排除银行卡正则（避免长数字串误报）。
        返回顺序保持特异性优先（邮箱 > 身份证 > 手机号 > 银行卡）。

        返回:
            启用的 ``(pii_type, compiled_pattern)`` 元组列表。
        """
        if not self._enable_bank_card:
            return tuple(p for p in _PII_PATTERNS if p[0] != "bank_card")
        return _PII_PATTERNS

    @classmethod
    def _is_binary_like(cls, text: str) -> bool:
        """检测文本是否为 base64 / 二进制内容。

        计算非打印字符占比，超过阈值视为二进制内容。使用 Python
        ``str.isprintable()`` 判定可打印性（Unicode 感知：中文字符、
        日文、韩文、emoji 等均视为可打印，仅控制字符
        ``\\x00``-``\\x1F``（除 ``\\t`` ``\\n`` ``\\r``）、Unicode Cc /
        Cf / Co / Cn 类别字符视为非打印）。

        注：``str.isprintable()`` 将 ``\\t`` / ``\\n`` / ``\\r`` 视为
        不可打印，但它们是合法文本换行符，此处显式视为可打印，避免
        多行正常文本被误判为二进制。

        参数:
            text: 待检测的文本。

        返回:
            非打印字符占比 > ``_BINARY_RATIO_THRESHOLD`` 时返回 ``True``。
            空字符串返回 ``False``（空文本不视为二进制，由 ``filter``
            提前返回）。
        """
        if not text:
            return False
        total = len(text)
        # str.isprintable() Unicode 感知：中文字符等返回 True，
        # 控制字符 \x00 等返回 False。显式将 \t \n \r 视为可打印。
        non_printable = sum(
            1
            for c in text
            if not (c.isprintable() or c in ("\t", "\n", "\r"))
        )
        return (non_printable / total) > cls._BINARY_RATIO_THRESHOLD
