"""Phase 9 Task 4: GuardrailEngine — AI 护栏统一编排器。

本模块提供 ``GuardrailEngine``，聚合 ``InjectionGuard``（输入扫描 + 工具
返回值脱敏）与 ``OutputFilter``（输出侧 PII 过滤）两个组件，对外暴露三个
统一入口：

1. ``scan_input(text) -> ScanResult``：委托 ``InjectionGuard.scan_input``，
   ``input_scan.enabled=False`` 或异常时返回 ``allow``（fail-open）。
2. ``sanitize_tool_result(result, tool_name) -> Any``：委托
   ``InjectionGuard.sanitize_tool_result``，``sanitizer.enabled=False`` 或
   异常时返回原结果（fail-open）。
3. ``filter_output(text) -> (filtered_text, replacements_count)``：委托
   ``OutputFilter.filter``，``output_filter.enabled=False`` 或异常时返回
   ``(text, 0)``（fail-open）。

设计要点：
- **fail-open 软护栏**：每个入口独立 try/except，异常时放行 +
  ``logger.warning``，不阻塞主流程。与 ``PolicyEngine``（fail-closed）
  并存构成 defense in depth。
- **零外部依赖**：仅 ``logging`` 与 ``typing``，与 ``injection_guard`` /
  ``output_filter`` 一致，便于离线/边缘部署。
- **子组件独立 enable**：``input_scan.enabled`` / ``sanitizer.enabled`` /
  ``output_filter.enabled`` 三者互不影响。例如关闭输入扫描不影响
  工具返回值脱敏。
- **noop 实例**：``create_noop()`` 静态方法返回所有组件禁用的实例，
  供装配失败时使用（避免 ``react_loop`` 空指针）。``react_loop`` 可直接
  调用 ``scan_input`` / ``sanitize_tool_result`` / ``filter_output``，
  无需 None 判断。
- **不实现 update_config**：复用 ``server.py`` 的重新构造机制
  （配置变更时重新构造 ``Orchestrator`` 与 ``GuardrailEngine``），
  避免运行时半更新导致的状态不一致。

config 结构（``from_config`` 入参为完整 config dict）::

    {
        "guardrails": {
            "input_scan": {"enabled": True, "action": "warn"},
            "sanitizer": {
                "enabled": True,
                "trusted_tools": ["memory_search"],
                "max_output_length": 20000
            },
            "output_filter": {"enabled": True, "enable_bank_card": True}
        }
    }

缺失 ``guardrails`` 段时降级为全 enabled（安全默认，defense in depth）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence, Tuple

from .injection_guard import (
    DEFAULT_TRUSTED_TOOLS,
    InjectionGuard,
    MAX_TOOL_RESULT_LENGTH,
    ScanResult,
)
from .output_filter import OutputFilter

logger = logging.getLogger(__name__)

# action 配置合法取值（透传给 InjectionGuard.action_on_match）
_VALID_INPUT_SCAN_ACTIONS = ("block", "warn", "off")


class GuardrailEngine:
    """AI 护栏统一编排器（fail-open）。

    聚合 ``InjectionGuard`` 与 ``OutputFilter``，对外提供三个统一入口
    （``scan_input`` / ``sanitize_tool_result`` / ``filter_output``），
    每个入口独立 try/except fail-open + ``logger.warning``。

    子组件独立 enable：``input_scan_enabled`` / ``sanitizer_enabled`` /
    ``output_filter_enabled`` 三者互不影响。例如关闭输入扫描不影响
    工具返回值脱敏。

    典型用法::

        engine = GuardrailEngine.from_config(config_dict)

        # 输入扫描
        result = engine.scan_input(user_text)

        # 工具返回值脱敏
        sanitized = engine.sanitize_tool_result(tool_output, "web_fetch")

        # 输出过滤
        filtered_text, n = engine.filter_output(llm_response)

    装配失败时使用 ``create_noop()`` 而非 ``None``，避免 ``react_loop``
    空指针::

        try:
            engine = GuardrailEngine.from_config(config_dict)
        except Exception:
            engine = GuardrailEngine.create_noop()
    """

    def __init__(
        self,
        injection_guard: Optional[InjectionGuard] = None,
        output_filter: Optional[OutputFilter] = None,
        input_scan_enabled: bool = True,
        sanitizer_enabled: bool = True,
        output_filter_enabled: bool = True,
    ) -> None:
        """初始化护栏编排器。

        参数:
            injection_guard: 注入防御护栏实例。``None`` 时使用默认
                ``InjectionGuard()``（``action_on_match="warn"``）。
            output_filter: 输出 PII 过滤器实例。``None`` 时使用默认
                ``OutputFilter()``（``enable_bank_card=True``）。
            input_scan_enabled: 是否启用输入扫描。``False`` 时
                ``scan_input`` 直接返回 ``allow``，不调用 ``InjectionGuard``。
            sanitizer_enabled: 是否启用工具返回值脱敏。``False`` 时
                ``sanitize_tool_result`` 直接返回原结果。
            output_filter_enabled: 是否启用输出过滤。``False`` 时
                ``filter_output`` 直接返回 ``(text, 0)``。
        """
        self._injection_guard = (
            injection_guard if injection_guard is not None else InjectionGuard()
        )
        self._output_filter = (
            output_filter if output_filter is not None else OutputFilter()
        )
        self._input_scan_enabled = bool(input_scan_enabled)
        self._sanitizer_enabled = bool(sanitizer_enabled)
        self._output_filter_enabled = bool(output_filter_enabled)

    # ------------------------------------------------------------------
    # from_config 工厂方法
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "GuardrailEngine":
        """从 config dict 构造 ``GuardrailEngine``。

        从 ``config["guardrails"]`` 读取三个子组件的配置：
        - ``input_scan``：``enabled``（默认 ``True``）+ ``action``
          （默认 ``"warn"``，透传给 ``InjectionGuard.action_on_match``）。
        - ``sanitizer``：``enabled``（默认 ``True``）+ ``trusted_tools``
          （默认 ``DEFAULT_TRUSTED_TOOLS``）+ ``max_output_length``
          （默认 ``MAX_TOOL_RESULT_LENGTH``）。
        - ``output_filter``：``enabled``（默认 ``True``）+
          ``enable_bank_card``（默认 ``True``）。

        缺失 ``guardrails`` 段时降级为全 enabled（安全默认，defense in
        depth）。非法 ``action`` 值透传给 ``InjectionGuard``，由其回退
        到 ``"warn"`` 并记录 warning。

        参数:
            config: 完整 config dict。可为 ``None`` 或空 dict，此时使用
                全默认配置（全 enabled）。

        返回:
            构造好的 ``GuardrailEngine`` 实例。
        """
        # 防御性处理：None 或非 dict 视为空 dict
        if not isinstance(config, dict):
            config = {}

        guardrails_cfg = config.get("guardrails") or {}

        # 防御：guardrails_cfg 不是 dict 时降级为空 dict
        if not isinstance(guardrails_cfg, dict):
            logger.warning(
                "guardrails 配置段非 dict（实际类型 %s），降级为全默认",
                type(guardrails_cfg).__name__,
            )
            guardrails_cfg = {}

        # ------------------------------------------------------------------
        # 解析 input_scan 段
        # ------------------------------------------------------------------
        input_scan_cfg = guardrails_cfg.get("input_scan") or {}
        if not isinstance(input_scan_cfg, dict):
            logger.warning(
                "guardrails.input_scan 非 dict，降级为默认配置"
            )
            input_scan_cfg = {}
        input_scan_enabled = bool(input_scan_cfg.get("enabled", True))
        action_on_match = input_scan_cfg.get("action", "warn")
        if action_on_match not in _VALID_INPUT_SCAN_ACTIONS:
            logger.warning(
                "guardrails.input_scan.action=%s 非法，将透传给 "
                "InjectionGuard（其内部回退到 warn）",
                action_on_match,
            )

        # ------------------------------------------------------------------
        # 解析 sanitizer 段
        # ------------------------------------------------------------------
        sanitizer_cfg = guardrails_cfg.get("sanitizer") or {}
        if not isinstance(sanitizer_cfg, dict):
            logger.warning(
                "guardrails.sanitizer 非 dict，降级为默认配置"
            )
            sanitizer_cfg = {}
        sanitizer_enabled = bool(sanitizer_cfg.get("enabled", True))
        trusted_tools = sanitizer_cfg.get("trusted_tools")
        if trusted_tools is None:
            trusted_tools = list(DEFAULT_TRUSTED_TOOLS)
        else:
            # 防御：确保为 list/tuple
            if not isinstance(trusted_tools, (list, tuple)):
                logger.warning(
                    "guardrails.sanitizer.trusted_tools 非 list，降级为默认"
                )
                trusted_tools = list(DEFAULT_TRUSTED_TOOLS)
            else:
                trusted_tools = list(trusted_tools)
        max_output_length = sanitizer_cfg.get(
            "max_output_length", MAX_TOOL_RESULT_LENGTH
        )
        # 防御：max_output_length 必须为正整数
        if not isinstance(max_output_length, int) or max_output_length <= 0:
            logger.warning(
                "guardrails.sanitizer.max_output_length=%s 非法，"
                "降级为默认 %d",
                max_output_length,
                MAX_TOOL_RESULT_LENGTH,
            )
            max_output_length = MAX_TOOL_RESULT_LENGTH

        # ------------------------------------------------------------------
        # 解析 output_filter 段
        # ------------------------------------------------------------------
        output_filter_cfg = guardrails_cfg.get("output_filter") or {}
        if not isinstance(output_filter_cfg, dict):
            logger.warning(
                "guardrails.output_filter 非 dict，降级为默认配置"
            )
            output_filter_cfg = {}
        output_filter_enabled = bool(output_filter_cfg.get("enabled", True))
        enable_bank_card = bool(output_filter_cfg.get("enable_bank_card", True))

        # ------------------------------------------------------------------
        # 构造子组件
        # ------------------------------------------------------------------
        # 单一 InjectionGuard 实例同时服务 scan_input 与 sanitize_tool_result
        # （两者共用编译后的模式清单，避免重复编译）。
        injection_guard = InjectionGuard(
            action_on_match=action_on_match,
            default_trusted_tools=trusted_tools,
            max_tool_result_length=max_output_length,
        )
        output_filter = OutputFilter(enable_bank_card=enable_bank_card)

        return cls(
            injection_guard=injection_guard,
            output_filter=output_filter,
            input_scan_enabled=input_scan_enabled,
            sanitizer_enabled=sanitizer_enabled,
            output_filter_enabled=output_filter_enabled,
        )

    # ------------------------------------------------------------------
    # create_noop 静态方法
    # ------------------------------------------------------------------
    @staticmethod
    def create_noop() -> "GuardrailEngine":
        """创建空操作 ``GuardrailEngine`` 实例。

        所有组件均禁用：
        - ``scan_input`` 直接返回 ``allow``（不执行扫描）。
        - ``sanitize_tool_result`` 直接返回原结果（不脱敏）。
        - ``filter_output`` 直接返回 ``(text, 0)``（不过滤）。

        装配失败时（如 ``from_config`` 抛异常）使用本方法构造 noop
        实例而非 ``None``，避免 ``react_loop`` 空指针。``react_loop``
        可直接调用三个入口方法，无需 None 判断。

        返回:
            所有组件禁用的 ``GuardrailEngine`` 实例。
        """
        return GuardrailEngine(
            injection_guard=InjectionGuard(),
            output_filter=OutputFilter(),
            input_scan_enabled=False,
            sanitizer_enabled=False,
            output_filter_enabled=False,
        )

    # ------------------------------------------------------------------
    # scan_input
    # ------------------------------------------------------------------
    def scan_input(self, text: str) -> ScanResult:
        """扫描用户输入，检测 prompt 注入模式。

        委托 ``InjectionGuard.scan_input``，``input_scan.enabled=False``
        或异常时返回 ``ScanResult(action="allow")``（fail-open）。

        参数:
            text: 待扫描的输入文本。

        返回:
            ``ScanResult``。``input_scan.enabled=False`` 或异常时
            ``action="allow"``，``matched_patterns`` 为空。
        """
        if not self._input_scan_enabled:
            return ScanResult(
                action="allow",
                matched_patterns=[],
                reason="input_scan 已禁用",
            )
        try:
            return self._injection_guard.scan_input(text)
        except Exception:
            logger.warning(
                "GuardrailEngine.scan_input 异常，fail-open 放行",
                exc_info=True,
            )
            return ScanResult(
                action="allow",
                matched_patterns=[],
                reason="扫描异常，fail-open 放行",
            )

    # ------------------------------------------------------------------
    # sanitize_tool_result
    # ------------------------------------------------------------------
    def sanitize_tool_result(self, result: Any, tool_name: str) -> Any:
        """对外部工具返回值做脱敏处理。

        委托 ``InjectionGuard.sanitize_tool_result``，
        ``sanitizer.enabled=False`` 或异常时返回原 ``result``（fail-open）。

        参数:
            result: 工具返回值（str 或可 JSON 序列化对象）。
            tool_name: 工具名。

        返回:
            可信工具或 ``sanitizer.enabled=False`` 时返回原始 ``result``
            （保留类型）；非可信工具返回脱敏后的 str（含边界标记）。
            异常时返回原 ``result``。
        """
        if not self._sanitizer_enabled:
            return result
        try:
            return self._injection_guard.sanitize_tool_result(
                result, tool_name=tool_name
            )
        except Exception:
            logger.warning(
                "GuardrailEngine.sanitize_tool_result 异常，"
                "fail-open 返回原结果",
                exc_info=True,
            )
            return result

    # ------------------------------------------------------------------
    # filter_output
    # ------------------------------------------------------------------
    def filter_output(self, text: str) -> Tuple[str, int]:
        """过滤 LLM 响应中的 PII，返回替换后的文本与替换次数。

        委托 ``OutputFilter.filter``，``output_filter.enabled=False``
        或异常时返回 ``(text, 0)``（fail-open）。

        参数:
            text: 待过滤的 LLM 响应文本。

        返回:
            ``(filtered_text, replacements_count)`` 二元组。
            ``output_filter.enabled=False`` 或异常时返回 ``(text, 0)``。
        """
        if not self._output_filter_enabled:
            return text, 0
        try:
            return self._output_filter.filter(text)
        except Exception:
            logger.warning(
                "GuardrailEngine.filter_output 异常，fail-open 返回原值",
                exc_info=True,
            )
            return text, 0

    # ------------------------------------------------------------------
    # 辅助属性（供调用方/测试查询状态）
    # ------------------------------------------------------------------
    @property
    def input_scan_enabled(self) -> bool:
        """输入扫描是否启用。"""
        return self._input_scan_enabled

    @property
    def sanitizer_enabled(self) -> bool:
        """工具返回值脱敏是否启用。"""
        return self._sanitizer_enabled

    @property
    def output_filter_enabled(self) -> bool:
        """输出过滤是否启用。"""
        return self._output_filter_enabled

    @property
    def injection_guard(self) -> InjectionGuard:
        """内部 InjectionGuard 实例（供测试与扩展查询）。"""
        return self._injection_guard

    @property
    def output_filter(self) -> OutputFilter:
        """内部 OutputFilter 实例（供测试与扩展查询）。"""
        return self._output_filter
