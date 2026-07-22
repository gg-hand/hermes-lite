"""guardrails 包：AI 护栏工程（Phase 9）。

提供 prompt 注入防御软护栏（fail-open），与 ``teage_liu.agent.policy.PolicyEngine``
（fail-closed 工具调用门）并存，构成 defense in depth：

- **硬约束**（``PolicyEngine``）：工具调用门，异常时拒绝（fail-closed）。
- **软告警**（``InjectionGuard``）：输入扫描 + 工具返回值脱敏，
  异常时放行（fail-open）。

两者职责不重叠：``InjectionGuard`` 不阻塞主流程，仅做模式检测与脱敏；
``PolicyEngine`` 负责工具调用 confirm/deny 决策。

子模块：
- ``injection_guard``：``InjectionGuard`` / ``ScanResult`` / ``InjectionPattern``
  及默认模式清单 ``DEFAULT_PATTERNS``。
- ``output_filter``：``OutputFilter`` 输出侧 PII 过滤器。
- ``guardrail_engine``：``GuardrailEngine`` 统一编排器，聚合上述两个
  组件并提供 ``from_config`` 工厂与 ``create_noop`` 空操作实例。

典型用法：

    from teage_liu.guardrails import GuardrailEngine

    # 从 config 构造（推荐路径，由 orchestrator/server.py 调用）
    engine = GuardrailEngine.from_config(config_dict)

    # 输入扫描
    result = engine.scan_input(user_text)
    if result.action == "suspicious":
        # 推送告警事件，但不阻塞
        ...

    # 工具返回值脱敏
    sanitized = engine.sanitize_tool_result(tool_output, tool_name="web_fetch")

    # 输出过滤
    filtered_text, n = engine.filter_output(llm_response)

    # 装配失败时使用 noop 实例（避免 react_loop 空指针）
    try:
        engine = GuardrailEngine.from_config(config_dict)
    except Exception:
        engine = GuardrailEngine.create_noop()
"""

from __future__ import annotations

from .injection_guard import (
    DEFAULT_EXTERNAL_TOOLS,
    DEFAULT_PATTERNS,
    DEFAULT_TRUSTED_TOOLS,
    MAX_INPUT_LENGTH,
    MAX_TOOL_RESULT_LENGTH,
    InjectionGuard,
    InjectionPattern,
    ScanResult,
)
from .output_filter import OutputFilter
from .guardrail_engine import GuardrailEngine

__all__ = [
    # 主类
    "InjectionGuard",
    "ScanResult",
    "InjectionPattern",
    "OutputFilter",
    "GuardrailEngine",
    # 默认数据源
    "DEFAULT_PATTERNS",
    "DEFAULT_EXTERNAL_TOOLS",
    "DEFAULT_TRUSTED_TOOLS",
    # 常量
    "MAX_INPUT_LENGTH",
    "MAX_TOOL_RESULT_LENGTH",
]
