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


if __name__ == "__main__":
    unittest.main()
