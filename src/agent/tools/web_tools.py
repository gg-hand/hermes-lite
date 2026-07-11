"""Web 工具：web_fetch (http_request), web_search。

Re-export 自 builtin_tools.py，后续将迁移函数体到此文件。
"""
from __future__ import annotations

try:
    from ..builtin_tools import (
        http_request,
        web_search,
        _html_to_plain_text,
        _extract_domain,
        _load_domain_state,
        _save_domain_state,
        _classify_quality,
        _build_enhanced_headers,
        _persist_success_headers,
        _search_baidu,
    )
except ImportError:  # pragma: no cover
    from agent.builtin_tools import (  # type: ignore
        http_request,
        web_search,
        _html_to_plain_text,
        _extract_domain,
        _load_domain_state,
        _save_domain_state,
        _classify_quality,
        _build_enhanced_headers,
        _persist_success_headers,
        _search_baidu,
    )

__all__ = [
    "http_request",
    "web_search",
]
