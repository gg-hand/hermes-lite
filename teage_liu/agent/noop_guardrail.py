"""GuardrailEngine 模块不可用时的 noop 占位。

从 react_loop.py 提取，提供 sanitize_tool_result / scan_input / filter_output
三个方法，全部直返原值，保证调用方不抛异常。仅在 GuardrailEngine 模块导入
失败时使用，正常路径下 __init__ 会用 GuardrailEngine.create_noop() 替代。
"""
from __future__ import annotations

from typing import Any


class NoopGuardrail:
    """GuardrailEngine 模块不可用时的 noop 占位（Phase 9 Task 6）。"""

    def sanitize_tool_result(self, result: Any, tool_name: str) -> Any:
        """直返原结果（noop）。"""
        return result

    def scan_input(self, text: str):  # type: ignore[no-untyped-def]
        """返回 allow（noop）。

        与 GuardrailEngine.ScanResult 兼容的最简占位，避免引入对 ScanResult
        的硬依赖（GuardrailEngine 模块可能不可用）。
        """
        try:
            from ..guardrails import ScanResult  # type: ignore
            return ScanResult(action="allow", matched_patterns=[], reason="noop")
        except Exception:
            from dataclasses import dataclass, field
            from typing import List as _List

            @dataclass
            class _ScanResultFallback:
                action: str = "allow"
                matched_patterns: _List[str] = field(default_factory=list)
                reason: str = "noop"

            return _ScanResultFallback()

    def filter_output(self, text: str):  # type: ignore[no-untyped-def]
        """直返 (text, 0)（noop）。"""
        return text, 0
