"""轻量级指标采集模块。

提供 MetricsCollector 类，用于在进程内采集 LLM 调用、记忆检索、工具调用
等关键路径的计数器与延迟直方图指标。仅依赖 Python 标准库（threading、copy），
无外部依赖，适合嵌入 hermes-lite 主进程随业务代码同步运行。

采集的指标可通过 snapshot() 导出为纯字典，供健康检查端点或日志输出使用。
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List

# 直方图 bucket 边界（毫秒）
_LATENCY_BUCKETS: List[float] = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000]


def _new_histogram() -> Dict[str, Any]:
    """创建初始直方图结构。

    buckets 数组长度 = 边界数 + 1，末位为 +Inf bucket。
    初始时 count=0，min=0，max=0。
    """
    return {
        "buckets": [0] * (len(_LATENCY_BUCKETS) + 1),
        "count": 0,
        "sum": 0.0,
        "min": 0.0,
        "max": 0.0,
    }


class MetricsCollector:
    """线程安全的轻量级指标采集器。

    使用 threading.Lock 保护所有读写操作，可在多线程环境下安全调用。
    适用于 hermes-lite 主进程内随业务代码同步采集 LLM 调用、记忆检索、
    工具调用等关键路径的计数器与延迟直方图指标。

    典型用法::

        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 100, "output_tokens": 50}, 350.0
        )
        collector.observe_memory_retrieval(hit=True)
        collector.observe_tool_call("file_read", success=True, latency_ms=5.0)
        snapshot = collector.snapshot()
    """

    def __init__(self) -> None:
        """初始化所有计数器与直方图为初始状态。"""
        self._lock = threading.Lock()
        # 计数器
        self._llm_calls_total: int = 0
        self._llm_tokens_input_total: int = 0
        self._llm_tokens_output_total: int = 0
        self._llm_cache_creation_tokens_total: int = 0
        self._llm_cache_read_tokens_total: int = 0
        self._memory_retrieval_hits_total: int = 0
        self._memory_retrieval_misses_total: int = 0
        self._tool_calls_total: Dict[str, int] = {}
        self._tool_calls_errors_total: Dict[str, int] = {}
        # 直方图
        self._llm_latency_ms: Dict[str, Any] = _new_histogram()
        self._tool_latency_ms: Dict[str, Any] = _new_histogram()
        # 反馈机制计数器（Phase 1 反馈监控扩展）
        self._termination_reasons_total: Dict[str, int] = {}
        self._tool_error_classes_total: Dict[str, Dict[str, int]] = {}
        self._tool_retries_total: Dict[str, int] = {}
        # Phase 2 反馈监控：审批决策计数器
        self._approval_decisions_total: Dict[str, int] = {}
        # OCR 分层引擎指标：按 engine 分桶（paddle/tesseract/vision_llm）
        self._ocr_calls_total: Dict[str, int] = {}
        self._ocr_errors_total: Dict[str, int] = {}
        self._ocr_latency_ms: Dict[str, Dict[str, float]] = {}
        # reasoning_tokens 累计（spec integrate-llm-reasoning-mode Task 15）
        self._llm_reasoning_tokens_total: int = 0
        # intent_classifier 监控（spec agent-metacognition-uplift Task 5）
        # 按 intent 类型分桶的计数器（simple_qa/knowledge_lookup/multi_step_task/out_of_scope）
        self._intent_classifications_total: Dict[str, int] = {}
        # 降级原因分桶（llm_failure/timeout/parse_failure/low_confidence/cron_skip）
        self._intent_fallbacks_total: Dict[str, int] = {}
        # intent 分类延迟统计（毫秒）
        self._intent_latency_ms: Dict[str, float] = {"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0}
        # cron 会话跳过计数（is_cron=True 时短路）
        self._intent_cron_skips_total: int = 0

    # ------------------------------------------------------------------
    # 公开采集方法
    # ------------------------------------------------------------------
    def observe_llm_usage(self, usage: dict, latency_ms: float) -> None:
        """记录一次 LLM 调用的用量与延迟。

        从 usage 字典安全提取 input_tokens / output_tokens /
        cache_creation_input_tokens / cache_read_input_tokens / reasoning_tokens
        （缺失为 0），累加到对应计数器，并将 latency_ms 记入 llm_latency_ms 直方图。

        reasoning_tokens 计量关系（SubTask 15.2）：
        - profile.reasoning_tokens_included_in_output=True（默认，Anthropic/OpenAI o3）：
          reasoning_tokens 已包含在 output_tokens 内，**不重复累加**到 output_total，
          避免双重计数；
        - =False（独立计数）：reasoning_tokens **同时累加**到 output_total，
          避免总成本统计偏低。
        当前实现：reasoning_tokens 单独累加到 _llm_reasoning_tokens_total，
        不影响 output_tokens 累加（output_tokens 由 backend 已正确处理包含关系）。

        参数:
            usage: LLM 响应中的 usage 字典，含各类 token 计数。
            latency_ms: 本次 LLM 调用延迟（毫秒）。
        """
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        reasoning_tokens = usage.get("reasoning_tokens", 0) or 0

        with self._lock:
            self._llm_calls_total += 1
            self._llm_tokens_input_total += input_tokens
            self._llm_tokens_output_total += output_tokens
            self._llm_cache_creation_tokens_total += cache_creation
            self._llm_cache_read_tokens_total += cache_read
            self._llm_reasoning_tokens_total += reasoning_tokens
            self._observe_histogram("llm_latency_ms", latency_ms)

    def observe_memory_retrieval(self, hit: bool) -> None:
        """记录一次记忆检索结果。

        参数:
            hit: True 表示命中，False 表示未命中。
        """
        with self._lock:
            if hit:
                self._memory_retrieval_hits_total += 1
            else:
                self._memory_retrieval_misses_total += 1

    def observe_tool_call(
        self, tool_name: str, success: bool, latency_ms: float
    ) -> None:
        """记录一次工具调用结果与延迟。

        参数:
            tool_name: 工具名称，用于分桶。
            success: 是否调用成功；失败时同时累加 tool_calls_errors_total。
            latency_ms: 本次工具调用延迟（毫秒）。
        """
        with self._lock:
            self._tool_calls_total[tool_name] = (
                self._tool_calls_total.get(tool_name, 0) + 1
            )
            if not success:
                self._tool_calls_errors_total[tool_name] = (
                    self._tool_calls_errors_total.get(tool_name, 0) + 1
                )
            self._observe_histogram("tool_latency_ms", latency_ms)

    # ------------------------------------------------------------------
    # 反馈机制采集方法（Phase 1 反馈监控扩展）
    # ------------------------------------------------------------------
    def observe_termination(self, reason: str) -> None:
        """记录一次循环终止原因。

        参数:
            reason: 终止原因，取值 normal/user_cancel/tool_permanent_fail/max_loops。
        """
        with self._lock:
            self._termination_reasons_total[reason] = (
                self._termination_reasons_total.get(reason, 0) + 1
            )

    def observe_tool_error_class(self, tool_name: str, error_class: str) -> None:
        """记录一次工具错误分类计数。

        参数:
            tool_name: 工具名称，用于分桶。
            error_class: 错误分类字符串，取值（18 类 + 1 兼容）：
                - pre_execution(7): param_error, tool_not_found, policy_denied,
                  user_rejected, non_stream_hil, stuck_detected, cancelled
                - execution(8): not_found, permission, timeout, transient,
                  permanent, anti_crawler, auth_required, internal_error
                - protocol(3): orphan_tool_result, llm_failure,
                  reasoning_config_invalid（spec integrate-llm-reasoning-mode
                  Task 16：LLM reasoning 配置非法，由 backend 400 异常路径
                  进入 ErrorClassifier 识别）
                - 兼容(1): unknown（历史数据 / ErrorClassifier 兜底）
        """
        with self._lock:
            bucket = self._tool_error_classes_total.setdefault(tool_name, {})
            bucket[error_class] = bucket.get(error_class, 0) + 1

    def observe_tool_retry(self, tool_name: str) -> None:
        """记录一次工具重试（Phase 2 调用，Phase 1 预留接口）。

        参数:
            tool_name: 工具名称，用于分桶。
        """
        with self._lock:
            self._tool_retries_total[tool_name] = (
                self._tool_retries_total.get(tool_name, 0) + 1
            )

    def observe_approval_decision(self, decision: str) -> None:
        """记录一次审批决策（Phase 2 反馈监控）。

        参数:
            decision: 决策类型，取值 approve/deny/timeout。
        """
        with self._lock:
            self._approval_decisions_total[decision] = (
                self._approval_decisions_total.get(decision, 0) + 1
            )

    def observe_ocr_call(self, engine: str, success: bool, latency_ms: float) -> None:
        """记录一次 OCR 引擎调用结果与延迟。

        按 engine 分桶（paddle/tesseract/vision_llm），用于监控分层 OCR
        各引擎命中率、错误率与延迟分布。

        参数:
            engine: OCR 引擎名（paddle/tesseract/vision_llm）。
            success: 是否调用成功；失败时同时累加 ocr_errors_total。
            latency_ms: 本次 OCR 调用延迟（毫秒）。
        """
        with self._lock:
            self._ocr_calls_total[engine] = (
                self._ocr_calls_total.get(engine, 0) + 1
            )
            if not success:
                self._ocr_errors_total[engine] = (
                    self._ocr_errors_total.get(engine, 0) + 1
                )
            stats = self._ocr_latency_ms.setdefault(
                engine, {"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0}
            )
            stats["count"] += 1
            stats["sum"] += latency_ms
            if stats["count"] == 1:
                stats["min"] = latency_ms
                stats["max"] = latency_ms
            else:
                stats["min"] = min(stats["min"], latency_ms)
            stats["max"] = max(stats["max"], latency_ms)

    def observe_intent(
        self,
        intent_type: Optional[str] = None,
        confidence: float = 0.0,
        fallback_reason: Optional[str] = None,
        latency_ms: Optional[float] = None,
        is_cron_skip: bool = False,
    ) -> None:
        """记录一次 intent_classifier 分类结果或降级事件。

        spec agent-metacognition-uplift Task 5 监控要求：记录 intent 分类
        分布、降级率、低置信度回退率、延迟统计，便于在 /monitor 页面展示。

        三种调用场景：
        1. 正常分类成功：intent_type="simple_qa", confidence=0.9, fallback_reason=None
        2. 降级（LLM 失败/超时/解析失败）：intent_type=None, fallback_reason="llm_failure"
        3. cron 跳过：is_cron_skip=True（不消耗 LLM 调用）

        参数:
            intent_type: IntentType 枚举的 value（如 "simple_qa"），降级时为 None。
            confidence: 分类置信度 0.0-1.0，降级时为 0.0。
            fallback_reason: 降级原因，取值：
                - "llm_failure": LLM 调用抛异常
                - "timeout": LLM 调用超时
                - "parse_failure": LLM 输出解析失败
                - "low_confidence": 置信度 < 0.6 触发回退
                - None: 正常分类成功
            latency_ms: intent 分类耗时（毫秒），cron 跳过时为 None。
            is_cron_skip: 是否为 cron 会话跳过（is_cron=True 短路）。
        """
        with self._lock:
            if is_cron_skip:
                self._intent_cron_skips_total += 1
                return
            if intent_type is not None:
                self._intent_classifications_total[intent_type] = (
                    self._intent_classifications_total.get(intent_type, 0) + 1
                )
            if fallback_reason is not None:
                self._intent_fallbacks_total[fallback_reason] = (
                    self._intent_fallbacks_total.get(fallback_reason, 0) + 1
                )
            if latency_ms is not None:
                stats = self._intent_latency_ms
                stats["count"] += 1
                stats["sum"] += latency_ms
                if stats["count"] == 1:
                    stats["min"] = latency_ms
                    stats["max"] = latency_ms
                else:
                    stats["min"] = min(stats["min"], latency_ms)
                    stats["max"] = max(stats["max"], latency_ms)

    # ------------------------------------------------------------------
    # 导出与重置
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """返回所有指标的深拷贝。

        直方图额外包含计算字段 avg = sum / count（count=0 时 avg=0）。
        返回的字典为深拷贝，调用方可安全修改。

        返回:
            包含全部计数器与直方图的字典。
        """
        with self._lock:
            llm_latency = copy.deepcopy(self._llm_latency_ms)
            tool_latency = copy.deepcopy(self._tool_latency_ms)
            llm_latency["avg"] = (
                llm_latency["sum"] / llm_latency["count"]
                if llm_latency["count"]
                else 0
            )
            tool_latency["avg"] = (
                tool_latency["sum"] / tool_latency["count"]
                if tool_latency["count"]
                else 0
            )
            return {
                "llm_calls_total": self._llm_calls_total,
                "llm_tokens_input_total": self._llm_tokens_input_total,
                "llm_tokens_output_total": self._llm_tokens_output_total,
                "llm_cache_creation_tokens_total": self._llm_cache_creation_tokens_total,
                "llm_cache_read_tokens_total": self._llm_cache_read_tokens_total,
                "llm_reasoning_tokens_total": self._llm_reasoning_tokens_total,
                "memory_retrieval_hits_total": self._memory_retrieval_hits_total,
                "memory_retrieval_misses_total": self._memory_retrieval_misses_total,
                "tool_calls_total": copy.deepcopy(self._tool_calls_total),
                "tool_calls_errors_total": copy.deepcopy(self._tool_calls_errors_total),
                "llm_latency_ms": llm_latency,
                "tool_latency_ms": tool_latency,
                "termination_reasons_total": copy.deepcopy(self._termination_reasons_total),
                "tool_error_classes_total": copy.deepcopy(self._tool_error_classes_total),
                "tool_retries_total": copy.deepcopy(self._tool_retries_total),
                "approval_decisions_total": copy.deepcopy(self._approval_decisions_total),
                "ocr_calls_total": copy.deepcopy(self._ocr_calls_total),
                "ocr_errors_total": copy.deepcopy(self._ocr_errors_total),
                "ocr_latency_ms": copy.deepcopy(self._ocr_latency_ms),
                "intent_classifications_total": copy.deepcopy(self._intent_classifications_total),
                "intent_fallbacks_total": copy.deepcopy(self._intent_fallbacks_total),
                "intent_latency_ms": copy.deepcopy(self._intent_latency_ms),
                "intent_cron_skips_total": self._intent_cron_skips_total,
            }

    def get_reasoning_tokens(self) -> int:
        """返回累计的 reasoning_tokens 总数（SubTask 15.4）。"""
        with self._lock:
            return self._llm_reasoning_tokens_total

    def reset(self) -> None:
        """清空所有计数器并重置直方图为初始状态。"""
        with self._lock:
            self._llm_calls_total = 0
            self._llm_tokens_input_total = 0
            self._llm_tokens_output_total = 0
            self._llm_cache_creation_tokens_total = 0
            self._llm_cache_read_tokens_total = 0
            self._llm_reasoning_tokens_total = 0
            self._memory_retrieval_hits_total = 0
            self._memory_retrieval_misses_total = 0
            self._tool_calls_total = {}
            self._tool_calls_errors_total = {}
            self._llm_latency_ms = _new_histogram()
            self._tool_latency_ms = _new_histogram()
            self._termination_reasons_total = {}
            self._tool_error_classes_total = {}
            self._tool_retries_total = {}
            self._approval_decisions_total = {}
            self._ocr_calls_total = {}
            self._ocr_errors_total = {}
            self._ocr_latency_ms = {}
            self._intent_classifications_total = {}
            self._intent_fallbacks_total = {}
            self._intent_latency_ms = {"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0}
            self._intent_cron_skips_total = 0

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _observe_histogram(self, hist_key: str, latency_ms: float) -> None:
        """将一次延迟观测记入指定直方图（调用方须已持有锁）。

        更新 buckets / count / sum / min / max：
        - latency_ms 落入第一个 边界 >= latency_ms 的 bucket；
        - 若 latency_ms > 最大边界(30000)，落入 +Inf bucket（数组末位）。
        - 首次观测（count 由 0 变 1）时 min/max 初始化为当前 latency_ms，
          后续取最小/最大值。

        参数:
            hist_key: 直方图字段名，"llm_latency_ms" 或 "tool_latency_ms"。
            latency_ms: 观测延迟（毫秒）。
        """
        if hist_key == "llm_latency_ms":
            hist = self._llm_latency_ms
        else:
            hist = self._tool_latency_ms

        # 定位 bucket：第一个 边界 >= latency_ms；若全部边界 < latency_ms 则落入 +Inf
        bucket_idx = len(_LATENCY_BUCKETS)
        for i, boundary in enumerate(_LATENCY_BUCKETS):
            if latency_ms <= boundary:
                bucket_idx = i
                break

        hist["buckets"][bucket_idx] += 1
        hist["count"] += 1
        hist["sum"] += latency_ms
        if hist["count"] == 1:
            hist["min"] = latency_ms
            hist["max"] = latency_ms
        else:
            hist["min"] = min(hist["min"], latency_ms)
            hist["max"] = max(hist["max"], latency_ms)
