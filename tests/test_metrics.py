"""MetricsCollector 单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.monitoring.metrics import MetricsCollector  # noqa: E402


class TestMetricsCollector(unittest.TestCase):
    def test_observe_llm_usage(self):
        collector = MetricsCollector()
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 0,
        }
        collector.observe_llm_usage(usage, 350.0)
        snap = collector.snapshot()
        self.assertEqual(snap["llm_calls_total"], 1)
        self.assertEqual(snap["llm_tokens_input_total"], 100)
        self.assertEqual(snap["llm_tokens_output_total"], 50)
        self.assertEqual(snap["llm_cache_creation_tokens_total"], 200)
        self.assertEqual(snap["llm_cache_read_tokens_total"], 0)
        # 350ms 落入 500 边界桶（索引 3，因为 200<350<=500）
        self.assertEqual(snap["llm_latency_ms"]["buckets"][3], 1)
        self.assertEqual(snap["llm_latency_ms"]["count"], 1)
        self.assertEqual(snap["llm_latency_ms"]["sum"], 350.0)
        self.assertEqual(snap["llm_latency_ms"]["min"], 350.0)
        self.assertEqual(snap["llm_latency_ms"]["max"], 350.0)
        self.assertEqual(snap["llm_latency_ms"]["avg"], 350.0)

    def test_observe_llm_usage_missing_fields(self):
        collector = MetricsCollector()
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
        }
        # 缺少 cache 字段不应抛异常
        collector.observe_llm_usage(usage, 100.0)
        snap = collector.snapshot()
        self.assertEqual(snap["llm_calls_total"], 1)
        self.assertEqual(snap["llm_tokens_input_total"], 100)
        self.assertEqual(snap["llm_tokens_output_total"], 50)
        self.assertEqual(snap["llm_cache_creation_tokens_total"], 0)
        self.assertEqual(snap["llm_cache_read_tokens_total"], 0)

    def test_observe_memory_retrieval(self):
        collector = MetricsCollector()
        for _ in range(3):
            collector.observe_memory_retrieval(hit=True)
        for _ in range(2):
            collector.observe_memory_retrieval(hit=False)
        snap = collector.snapshot()
        self.assertEqual(snap["memory_retrieval_hits_total"], 3)
        self.assertEqual(snap["memory_retrieval_misses_total"], 2)

    def test_observe_tool_call(self):
        collector = MetricsCollector()
        collector.observe_tool_call("file_read", success=True, latency_ms=10.0)
        collector.observe_tool_call("file_read", success=False, latency_ms=20.0)
        snap = collector.snapshot()
        self.assertEqual(snap["tool_calls_total"]["file_read"], 2)
        self.assertEqual(snap["tool_calls_errors_total"]["file_read"], 1)
        self.assertEqual(snap["tool_latency_ms"]["count"], 2)

    def test_snapshot_is_deepcopy(self):
        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 10, "output_tokens": 5}, 100.0
        )
        collector.observe_tool_call("x", success=True, latency_ms=5.0)
        snap = collector.snapshot()

        # 修改返回的 dict
        snap["llm_calls_total"] = 999
        snap["tool_calls_total"]["x"] = 999

        # 再次获取 snapshot，验证未被影响
        snap2 = collector.snapshot()
        self.assertEqual(snap2["llm_calls_total"], 1)
        self.assertEqual(snap2["tool_calls_total"]["x"], 1)

    def test_reset(self):
        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 100, "output_tokens": 50}, 350.0
        )
        collector.observe_memory_retrieval(hit=True)
        collector.observe_tool_call("file_read", success=False, latency_ms=10.0)

        collector.reset()
        snap = collector.snapshot()

        # 计数器归零
        self.assertEqual(snap["llm_calls_total"], 0)
        self.assertEqual(snap["llm_tokens_input_total"], 0)
        self.assertEqual(snap["llm_tokens_output_total"], 0)
        self.assertEqual(snap["llm_cache_creation_tokens_total"], 0)
        self.assertEqual(snap["llm_cache_read_tokens_total"], 0)
        self.assertEqual(snap["memory_retrieval_hits_total"], 0)
        self.assertEqual(snap["memory_retrieval_misses_total"], 0)
        # 字典为空
        self.assertEqual(snap["tool_calls_total"], {})
        self.assertEqual(snap["tool_calls_errors_total"], {})
        # 直方图归零
        self.assertEqual(snap["llm_latency_ms"]["count"], 0)
        self.assertEqual(snap["llm_latency_ms"]["sum"], 0.0)
        self.assertEqual(snap["llm_latency_ms"]["buckets"], [0] * 10)
        self.assertEqual(snap["tool_latency_ms"]["count"], 0)
        self.assertEqual(snap["tool_latency_ms"]["sum"], 0.0)
        self.assertEqual(snap["tool_latency_ms"]["buckets"], [0] * 10)

    def test_histogram_buckets(self):
        collector = MetricsCollector()
        # 6 次观测覆盖不同 bucket
        collector.observe_llm_usage({"input_tokens": 0, "output_tokens": 0}, 30.0)
        collector.observe_llm_usage({"input_tokens": 0, "output_tokens": 0}, 80.0)
        collector.observe_llm_usage({"input_tokens": 0, "output_tokens": 0}, 150.0)
        collector.observe_llm_usage({"input_tokens": 0, "output_tokens": 0}, 350.0)
        collector.observe_llm_usage({"input_tokens": 0, "output_tokens": 0}, 800.0)
        collector.observe_llm_usage({"input_tokens": 0, "output_tokens": 0}, 50000.0)
        snap = collector.snapshot()
        buckets = snap["llm_latency_ms"]["buckets"]
        # 30ms → bucket[0] (50 边界)
        self.assertEqual(buckets[0], 1)
        # 80ms → bucket[1] (100 边界)
        self.assertEqual(buckets[1], 1)
        # 150ms → bucket[2] (200 边界)
        self.assertEqual(buckets[2], 1)
        # 350ms → bucket[3] (500 边界)
        self.assertEqual(buckets[3], 1)
        # 800ms → bucket[4] (1000 边界)
        self.assertEqual(buckets[4], 1)
        # 50000ms → bucket[9] (+Inf)
        self.assertEqual(buckets[9], 1)
        # 其余 bucket 为 0
        self.assertEqual(buckets[5], 0)
        self.assertEqual(buckets[6], 0)
        self.assertEqual(buckets[7], 0)
        self.assertEqual(buckets[8], 0)
        # count 与 sum
        self.assertEqual(snap["llm_latency_ms"]["count"], 6)
        self.assertEqual(snap["llm_latency_ms"]["sum"], 30.0 + 80.0 + 150.0 + 350.0 + 800.0 + 50000.0)

    # ==================================================================
    # spec integrate-llm-reasoning-mode Task 22 SubTask 22.31
    # reasoning_tokens 4 处同步单测（__init__/observe_llm_usage/snapshot/reset）
    # ==================================================================

    def test_reasoning_tokens_init_zero(self):
        """__init__：reasoning_tokens 初始为 0。"""
        collector = MetricsCollector()
        self.assertEqual(collector.get_reasoning_tokens(), 0)

    def test_reasoning_tokens_accumulate(self):
        """observe_llm_usage：reasoning_tokens 正确累加。"""
        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 80}, 350.0
        )
        collector.observe_llm_usage(
            {"input_tokens": 200, "output_tokens": 100, "reasoning_tokens": 120}, 500.0
        )
        self.assertEqual(collector.get_reasoning_tokens(), 200)

    def test_reasoning_tokens_missing_defaults_zero(self):
        """observe_llm_usage：reasoning_tokens 缺失时不报错，默认 0。"""
        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 100, "output_tokens": 50}, 350.0
        )
        self.assertEqual(collector.get_reasoning_tokens(), 0)

    def test_reasoning_tokens_in_snapshot(self):
        """snapshot：包含 llm_reasoning_tokens_total 字段。"""
        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 150}, 350.0
        )
        snap = collector.snapshot()
        self.assertIn("llm_reasoning_tokens_total", snap)
        self.assertEqual(snap["llm_reasoning_tokens_total"], 150)

    def test_reasoning_tokens_reset(self):
        """reset：reasoning_tokens 清零。"""
        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 150}, 350.0
        )
        self.assertEqual(collector.get_reasoning_tokens(), 150)
        collector.reset()
        self.assertEqual(collector.get_reasoning_tokens(), 0)

    def test_reasoning_tokens_snapshot_deepcopy(self):
        """snapshot：返回深拷贝，修改不影响内部状态。"""
        collector = MetricsCollector()
        collector.observe_llm_usage(
            {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 150}, 350.0
        )
        snap = collector.snapshot()
        snap["llm_reasoning_tokens_total"] = 99999
        self.assertEqual(collector.get_reasoning_tokens(), 150)


class TestFeedbackMetrics(unittest.TestCase):
    """Phase 1 反馈监控扩展：termination_reason / error_class / retry 计数器。"""

    def test_observe_termination_accumulates_by_reason(self):
        """observe_termination 按 reason 分桶累加。"""
        collector = MetricsCollector()
        collector.observe_termination("normal")
        collector.observe_termination("normal")
        collector.observe_termination("user_cancel")
        collector.observe_termination("tool_permanent_fail")
        snap = collector.snapshot()
        self.assertEqual(snap["termination_reasons_total"]["normal"], 2)
        self.assertEqual(snap["termination_reasons_total"]["user_cancel"], 1)
        self.assertEqual(snap["termination_reasons_total"]["tool_permanent_fail"], 1)

    def test_observe_tool_error_class_nests_by_tool(self):
        """observe_tool_error_class 按 tool_name → error_class 二级分桶。"""
        collector = MetricsCollector()
        collector.observe_tool_error_class("web_fetch", "permanent")
        collector.observe_tool_error_class("web_fetch", "permanent")
        collector.observe_tool_error_class("web_fetch", "transient")
        collector.observe_tool_error_class("file_read", "unknown")
        snap = collector.snapshot()
        self.assertEqual(snap["tool_error_classes_total"]["web_fetch"]["permanent"], 2)
        self.assertEqual(snap["tool_error_classes_total"]["web_fetch"]["transient"], 1)
        self.assertEqual(snap["tool_error_classes_total"]["file_read"]["unknown"], 1)

    def test_observe_tool_retry_accumulates_by_tool(self):
        """observe_tool_retry 按 tool_name 分桶累加。"""
        collector = MetricsCollector()
        collector.observe_tool_retry("bash_exec")
        collector.observe_tool_retry("bash_exec")
        collector.observe_tool_retry("web_fetch")
        snap = collector.snapshot()
        self.assertEqual(snap["tool_retries_total"]["bash_exec"], 2)
        self.assertEqual(snap["tool_retries_total"]["web_fetch"], 1)

    def test_snapshot_returns_deepcopy_for_feedback_counters(self):
        """snapshot 返回的反馈计数器是深拷贝，外部修改不影响内部状态。"""
        collector = MetricsCollector()
        collector.observe_termination("normal")
        snap = collector.snapshot()
        snap["termination_reasons_total"]["normal"] = 999
        snap["tool_error_classes_total"]["x"] = {"y": 999}
        snap["tool_retries_total"]["z"] = 999
        # 再次 snapshot 验证内部状态未被影响
        snap2 = collector.snapshot()
        self.assertEqual(snap2["termination_reasons_total"]["normal"], 1)
        self.assertNotIn("x", snap2["tool_error_classes_total"])
        self.assertNotIn("z", snap2["tool_retries_total"])

    def test_reset_clears_feedback_counters(self):
        """reset 清空 3 个反馈计数器。"""
        collector = MetricsCollector()
        collector.observe_termination("normal")
        collector.observe_tool_error_class("web_fetch", "permanent")
        collector.observe_tool_retry("bash_exec")
        collector.reset()
        snap = collector.snapshot()
        self.assertEqual(snap["termination_reasons_total"], {})
        self.assertEqual(snap["tool_error_classes_total"], {})
        self.assertEqual(snap["tool_retries_total"], {})

    def test_initial_snapshot_has_empty_feedback_counters(self):
        """新创建的 collector snapshot 中反馈计数器为空 dict。"""
        collector = MetricsCollector()
        snap = collector.snapshot()
        self.assertEqual(snap["termination_reasons_total"], {})
        self.assertEqual(snap["tool_error_classes_total"], {})
        self.assertEqual(snap["tool_retries_total"], {})

    def test_feedback_methods_thread_safe(self):
        """反馈计数器方法在并发调用下不丢失更新（基础烟雾测试）。"""
        import threading
        collector = MetricsCollector()

        def worker():
            for _ in range(100):
                collector.observe_termination("normal")
                collector.observe_tool_error_class("web_fetch", "permanent")
                collector.observe_tool_retry("bash_exec")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = collector.snapshot()
        # 5 线程 × 100 次 = 500
        self.assertEqual(snap["termination_reasons_total"]["normal"], 500)
        self.assertEqual(snap["tool_error_classes_total"]["web_fetch"]["permanent"], 500)
        self.assertEqual(snap["tool_retries_total"]["bash_exec"], 500)


class TestIntentMetrics(unittest.TestCase):
    """Intent Classifier 监控指标测试（spec agent-metacognition-uplift Task 5）。"""

    def test_observe_intent_normal_classification(self):
        """正常分类成功：intent_type + confidence + latency 累加。"""
        collector = MetricsCollector()
        collector.observe_intent(
            intent_type="simple_qa",
            confidence=0.9,
            fallback_reason=None,
            latency_ms=150.0,
        )
        collector.observe_intent(
            intent_type="multi_step_task",
            confidence=0.85,
            fallback_reason=None,
            latency_ms=300.0,
        )
        collector.observe_intent(
            intent_type="simple_qa",
            confidence=0.95,
            fallback_reason=None,
            latency_ms=100.0,
        )
        snap = collector.snapshot()
        self.assertEqual(snap["intent_classifications_total"]["simple_qa"], 2)
        self.assertEqual(snap["intent_classifications_total"]["multi_step_task"], 1)
        self.assertEqual(snap["intent_fallbacks_total"], {})
        self.assertEqual(snap["intent_latency_ms"]["count"], 3)
        self.assertEqual(snap["intent_latency_ms"]["min"], 100.0)
        self.assertEqual(snap["intent_latency_ms"]["max"], 300.0)

    def test_observe_intent_fallback(self):
        """降级场景：fallback_reason 分桶累加。"""
        collector = MetricsCollector()
        collector.observe_intent(
            intent_type=None,
            confidence=0.0,
            fallback_reason="timeout",
            latency_ms=5000.0,
        )
        collector.observe_intent(
            intent_type=None,
            confidence=0.0,
            fallback_reason="llm_failure",
            latency_ms=100.0,
        )
        collector.observe_intent(
            intent_type=None,
            confidence=0.0,
            fallback_reason="parse_failure",
            latency_ms=200.0,
        )
        snap = collector.snapshot()
        self.assertEqual(snap["intent_fallbacks_total"]["timeout"], 1)
        self.assertEqual(snap["intent_fallbacks_total"]["llm_failure"], 1)
        self.assertEqual(snap["intent_fallbacks_total"]["parse_failure"], 1)
        self.assertEqual(snap["intent_classifications_total"], {})

    def test_observe_intent_low_confidence_fallback(self):
        """低置信度回退：intent_type 仍然记录，fallback_reason=low_confidence。"""
        collector = MetricsCollector()
        collector.observe_intent(
            intent_type="multi_step_task",
            confidence=0.4,
            fallback_reason="low_confidence",
            latency_ms=250.0,
        )
        snap = collector.snapshot()
        # 低置信度回退时，intent_type 和 fallback_reason 都记录
        self.assertEqual(snap["intent_classifications_total"]["multi_step_task"], 1)
        self.assertEqual(snap["intent_fallbacks_total"]["low_confidence"], 1)

    def test_observe_intent_cron_skip(self):
        """cron 跳过：is_cron_skip=True 时不记录其他指标。"""
        collector = MetricsCollector()
        collector.observe_intent(is_cron_skip=True)
        collector.observe_intent(is_cron_skip=True)
        collector.observe_intent(is_cron_skip=True)
        snap = collector.snapshot()
        self.assertEqual(snap["intent_cron_skips_total"], 3)
        self.assertEqual(snap["intent_classifications_total"], {})
        self.assertEqual(snap["intent_fallbacks_total"], {})
        self.assertEqual(snap["intent_latency_ms"]["count"], 0)

    def test_observe_intent_latency_stats(self):
        """延迟统计：count/sum/min/max 正确计算。"""
        collector = MetricsCollector()
        for lat in [100.0, 200.0, 150.0, 300.0, 50.0]:
            collector.observe_intent(
                intent_type="simple_qa",
                confidence=0.9,
                latency_ms=lat,
            )
        snap = collector.snapshot()
        self.assertEqual(snap["intent_latency_ms"]["count"], 5)
        self.assertEqual(snap["intent_latency_ms"]["sum"], 800.0)
        self.assertEqual(snap["intent_latency_ms"]["min"], 50.0)
        self.assertEqual(snap["intent_latency_ms"]["max"], 300.0)

    def test_observe_intent_no_latency(self):
        """latency_ms=None 时不更新延迟统计。"""
        collector = MetricsCollector()
        collector.observe_intent(
            intent_type="simple_qa",
            confidence=0.9,
            latency_ms=None,
        )
        snap = collector.snapshot()
        self.assertEqual(snap["intent_classifications_total"]["simple_qa"], 1)
        self.assertEqual(snap["intent_latency_ms"]["count"], 0)

    def test_intent_metrics_reset(self):
        """reset() 清空所有 intent 指标。"""
        collector = MetricsCollector()
        collector.observe_intent(intent_type="simple_qa", confidence=0.9, latency_ms=100.0)
        collector.observe_intent(fallback_reason="timeout", latency_ms=5000.0)
        collector.observe_intent(is_cron_skip=True)
        collector.reset()
        snap = collector.snapshot()
        self.assertEqual(snap["intent_classifications_total"], {})
        self.assertEqual(snap["intent_fallbacks_total"], {})
        self.assertEqual(snap["intent_latency_ms"]["count"], 0)
        self.assertEqual(snap["intent_cron_skips_total"], 0)

    def test_intent_metrics_snapshot_deepcopy(self):
        """snapshot 返回深拷贝，修改不影响内部状态。"""
        collector = MetricsCollector()
        collector.observe_intent(intent_type="simple_qa", confidence=0.9, latency_ms=100.0)
        snap = collector.snapshot()
        snap["intent_classifications_total"]["simple_qa"] = 999
        snap["intent_cron_skips_total"] = 999
        snap2 = collector.snapshot()
        self.assertEqual(snap2["intent_classifications_total"]["simple_qa"], 1)
        self.assertEqual(snap2["intent_cron_skips_total"], 0)

    def test_intent_metrics_thread_safe(self):
        """intent 指标方法在并发调用下不丢失更新。"""
        import threading
        collector = MetricsCollector()

        def worker():
            for _ in range(100):
                collector.observe_intent(intent_type="simple_qa", confidence=0.9, latency_ms=100.0)
                collector.observe_intent(fallback_reason="timeout", latency_ms=5000.0)
                collector.observe_intent(is_cron_skip=True)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = collector.snapshot()
        # 5 线程 × 100 次 = 500
        self.assertEqual(snap["intent_classifications_total"]["simple_qa"], 500)
        self.assertEqual(snap["intent_fallbacks_total"]["timeout"], 500)
        self.assertEqual(snap["intent_cron_skips_total"], 500)
        self.assertEqual(snap["intent_latency_ms"]["count"], 1000)  # 500 normal + 500 fallback


if __name__ == "__main__":
    unittest.main()
