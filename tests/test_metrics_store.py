"""MetricsStore 单元测试 — 验证按天持久化、增量合并、重启容错、TTL 清理等核心逻辑。

运行方式:
    python -m unittest tests.test_metrics_store -v
    python tests/test_metrics_store.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.monitoring.metrics_store import (  # noqa: E402
    MetricsStore,
    _merge_dict,
    _merge_hist,
    compute_delta,
)


def _hist(count=0, sum_val=0.0, min_val=0.0, max_val=0.0):
    """构造直方图快照。"""
    return {
        "buckets": [0] * 10,
        "count": count,
        "sum": sum_val,
        "min": min_val,
        "max": max_val,
    }


def _snapshot(
    llm_calls=0,
    tokens_in=0,
    tokens_out=0,
    llm_hist=None,
    tool_hist=None,
    tool_calls=None,
    tool_errors=None,
    err_classes=None,
    intent_cron_skips=0,
    intent_classifications=None,
    intent_fallbacks=None,
    intent_hist=None,
):
    """构造 MetricsCollector snapshot 风格的快照。"""
    return {
        "llm_calls_total": llm_calls,
        "llm_tokens_input_total": tokens_in,
        "llm_tokens_output_total": tokens_out,
        "llm_cache_creation_tokens_total": 0,
        "llm_cache_read_tokens_total": 0,
        "memory_retrieval_hits_total": 0,
        "memory_retrieval_misses_total": 0,
        "llm_latency_ms": llm_hist or _hist(),
        "tool_latency_ms": tool_hist or _hist(),
        "tool_calls_total": tool_calls or {},
        "tool_calls_errors_total": tool_errors or {},
        "termination_reasons_total": {},
        "tool_error_classes_total": err_classes or {},
        "tool_retries_total": {},
        "approval_decisions_total": {},
        "intent_cron_skips_total": intent_cron_skips,
        "intent_classifications_total": intent_classifications or {},
        "intent_fallbacks_total": intent_fallbacks or {},
        "intent_latency_ms": intent_hist or _hist(),
    }


class TestMetricsStoreBasic(unittest.TestCase):
    """验证基本写入与查询。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self.store = MetricsStore(db_path=self._db_path)

    def tearDown(self):
        self.store.close()
        for f in os.listdir(self._tmpdir):
            os.unlink(os.path.join(self._tmpdir, f))
        os.rmdir(self._tmpdir)

    def test_first_write_creates_record(self):
        """首次 upsert 创建记录，值正确。"""
        delta = compute_delta(
            _snapshot(llm_calls=10, tokens_in=500, tokens_out=200,
                      llm_hist=_hist(count=10, sum_val=2000.0, min_val=100.0, max_val=500.0)),
            _snapshot(),
        )
        self.store.upsert_daily("2026-07-01", delta)
        record = self.store.get_daily("2026-07-01")
        self.assertIsNotNone(record)
        self.assertEqual(record["llm_calls_total"], 10)
        self.assertEqual(record["llm_tokens_input_total"], 500)
        self.assertEqual(record["llm_latency_ms"]["count"], 10)
        self.assertEqual(record["llm_latency_ms"]["min"], 100.0)
        self.assertEqual(record["llm_latency_ms"]["max"], 500.0)
        self.assertAlmostEqual(record["llm_latency_ms"]["avg"], 200.0)

    def test_merge_same_day(self):
        """同一天两次 upsert，值叠加。"""
        delta1 = compute_delta(
            _snapshot(llm_calls=10), _snapshot()
        )
        self.store.upsert_daily("2026-07-01", delta1)

        delta2 = compute_delta(
            _snapshot(llm_calls=25), _snapshot(llm_calls=10)
        )
        self.store.upsert_daily("2026-07-01", delta2)

        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["llm_calls_total"], 25)  # 10 + 15

    def test_separate_days(self):
        """不同 date 写入不同行。"""
        self.store.upsert_daily("2026-07-01", compute_delta(_snapshot(llm_calls=10), _snapshot()))
        self.store.upsert_daily("2026-07-02", compute_delta(_snapshot(llm_calls=20), _snapshot()))

        r1 = self.store.get_daily("2026-07-01")
        r2 = self.store.get_daily("2026-07-02")
        self.assertEqual(r1["llm_calls_total"], 10)
        self.assertEqual(r2["llm_calls_total"], 20)


class TestMetricsStoreRestartMerge(unittest.TestCase):
    """验证重启合并逻辑。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self.store = MetricsStore(db_path=self._db_path)

    def tearDown(self):
        self.store.close()
        for f in os.listdir(self._tmpdir):
            os.unlink(os.path.join(self._tmpdir, f))
        os.rmdir(self._tmpdir)

    def test_restart_merge(self):
        """模拟重启：先写 delta1，新 baseline=0，再写 delta2，验证 DB = delta1 + delta2。"""
        # 重启前：80 次调用
        delta1 = compute_delta(_snapshot(llm_calls=80), _snapshot())
        self.store.upsert_daily("2026-07-01", delta1)

        # 重启后：baseline 归零，新增 20 次调用
        delta2 = compute_delta(_snapshot(llm_calls=20), _snapshot())
        self.store.upsert_daily("2026-07-01", delta2)

        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["llm_calls_total"], 100)  # 80 + 20


class TestHistogramDeltaAndMerge(unittest.TestCase):
    """验证直方图 delta 计算与合并。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self.store = MetricsStore(db_path=self._db_path)

    def tearDown(self):
        self.store.close()
        for f in os.listdir(self._tmpdir):
            os.unlink(os.path.join(self._tmpdir, f))
        os.rmdir(self._tmpdir)

    def test_baseline_count_zero_takes_current_min_max(self):
        """baseline.count==0 时 delta min/max 取 current 值（P0 修复验证）。"""
        # 重启后 baseline = 全零（count=0, min=0.0, max=0.0）
        baseline = _snapshot(llm_hist=_hist(count=0, min_val=0.0, max_val=0.0))
        # current 有实际观测
        current = _snapshot(llm_hist=_hist(count=5, sum_val=1000.0, min_val=150.0, max_val=400.0))

        delta = compute_delta(current, baseline)
        self.assertEqual(delta["llm_latency_ms"]["count"], 5)
        self.assertEqual(delta["llm_latency_ms"]["min"], 150.0)
        self.assertEqual(delta["llm_latency_ms"]["max"], 400.0)

        # 写入 DB 验证
        self.store.upsert_daily("2026-07-01", delta)
        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["llm_latency_ms"]["min"], 150.0)
        self.assertEqual(record["llm_latency_ms"]["max"], 400.0)

    def test_hist_min_max_merge_new_extreme(self):
        """新极端值被捕获，无新极端值时保留旧值。"""
        # 第一次：min=200, max=500
        delta1 = compute_delta(
            _snapshot(llm_hist=_hist(count=5, sum_val=1500.0, min_val=200.0, max_val=500.0)),
            _snapshot(),
        )
        self.store.upsert_daily("2026-07-01", delta1)

        # 第二次：min=150（新极端）, max=400（非新极端）
        delta2 = compute_delta(
            _snapshot(llm_hist=_hist(count=8, sum_val=2400.0, min_val=150.0, max_val=500.0)),
            _snapshot(llm_hist=_hist(count=5, sum_val=1500.0, min_val=200.0, max_val=500.0)),
        )
        self.store.upsert_daily("2026-07-01", delta2)

        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["llm_latency_ms"]["min"], 150.0)  # 新极端值
        self.assertEqual(record["llm_latency_ms"]["max"], 500.0)  # 保留旧值
        self.assertEqual(record["llm_latency_ms"]["count"], 8)


class TestDictMerge(unittest.TestCase):
    """验证 Dict 指标合并。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self.store = MetricsStore(db_path=self._db_path)

    def tearDown(self):
        self.store.close()
        for f in os.listdir(self._tmpdir):
            os.unlink(os.path.join(self._tmpdir, f))
        os.rmdir(self._tmpdir)

    def test_dict_merge_new_key(self):
        """Dict 新 key 出现正确合并。"""
        delta1 = compute_delta(
            _snapshot(tool_calls={"file_read": 5}),
            _snapshot(),
        )
        self.store.upsert_daily("2026-07-01", delta1)

        delta2 = compute_delta(
            _snapshot(tool_calls={"file_read": 8, "bash_exec": 3}),
            _snapshot(tool_calls={"file_read": 5}),
        )
        self.store.upsert_daily("2026-07-01", delta2)

        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["tool_calls_total"]["file_read"], 8)
        self.assertEqual(record["tool_calls_total"]["bash_exec"], 3)

    def test_merge_dict_function(self):
        """_merge_dict 按 key 累加。"""
        result = _merge_dict({"a": 5, "b": 3}, {"a": 2, "c": 7})
        self.assertEqual(result, {"a": 7, "b": 3, "c": 7})


class TestResetClamp(unittest.TestCase):
    """验证 reset 场景的 clamp 逻辑。"""

    def test_reset_clamp_to_zero(self):
        """current < baseline 时 delta clamp 到 0。"""
        baseline = _snapshot(llm_calls=100)
        current = _snapshot(llm_calls=30)  # reset 后
        delta = compute_delta(current, baseline)
        self.assertEqual(delta["llm_calls_total"], 0)  # clamp 到 0


class TestIntentFieldsDelta(unittest.TestCase):
    """验证 compute_delta 中 intent 字段增量计算（Task 1.4）。"""

    def test_intent_cron_skips_total_scalar_delta(self):
        """intent_cron_skips_total 标量增量计算，含 reset 场景 clamp。"""
        # 正常增量
        delta = compute_delta(
            _snapshot(intent_cron_skips=10),
            _snapshot(intent_cron_skips=3),
        )
        self.assertEqual(delta["intent_cron_skips_total"], 7)

        # reset 场景 clamp 到 0
        delta_reset = compute_delta(
            _snapshot(intent_cron_skips=2),
            _snapshot(intent_cron_skips=5),
        )
        self.assertEqual(delta_reset["intent_cron_skips_total"], 0)

    def test_intent_classifications_total_dict_delta(self):
        """intent_classifications_total 按 key 取 max(0, diff)。"""
        delta = compute_delta(
            _snapshot(
                intent_classifications={"simple_qa": 8, "multi_step_task": 5, "react_task": 2},
            ),
            _snapshot(
                intent_classifications={"simple_qa": 3, "multi_step_task": 5},
            ),
        )
        self.assertEqual(delta["intent_classifications_total"]["simple_qa"], 5)
        self.assertEqual(delta["intent_classifications_total"]["multi_step_task"], 0)
        self.assertEqual(delta["intent_classifications_total"]["react_task"], 2)

        # 缺失 key 兜底为 0
        delta_missing = compute_delta(
            _snapshot(intent_classifications={"simple_qa": 5}),
            _snapshot(),
        )
        self.assertEqual(delta_missing["intent_classifications_total"]["simple_qa"], 5)

    def test_intent_latency_ms_hist_delta(self):
        """intent_latency_ms 直方图增量，含 baseline.count==0 的 min/max 兜底。"""
        # baseline 非空：正常差值
        delta = compute_delta(
            _snapshot(intent_hist=_hist(count=10, sum_val=2000.0, min_val=100.0, max_val=500.0)),
            _snapshot(intent_hist=_hist(count=4, sum_val=800.0, min_val=200.0, max_val=400.0)),
        )
        self.assertEqual(delta["intent_latency_ms"]["count"], 6)
        self.assertAlmostEqual(delta["intent_latency_ms"]["sum"], 1200.0)
        # cur_min=100 < base_min=200 → 取 100
        self.assertEqual(delta["intent_latency_ms"]["min"], 100.0)
        # cur_max=500 > base_max=400 → 取 500
        self.assertEqual(delta["intent_latency_ms"]["max"], 500.0)

        # baseline.count==0（重启后）：min/max 直接取 current
        delta_restart = compute_delta(
            _snapshot(intent_hist=_hist(count=5, sum_val=1000.0, min_val=150.0, max_val=400.0)),
            _snapshot(intent_hist=_hist(count=0)),
        )
        self.assertEqual(delta_restart["intent_latency_ms"]["count"], 5)
        self.assertEqual(delta_restart["intent_latency_ms"]["min"], 150.0)
        self.assertEqual(delta_restart["intent_latency_ms"]["max"], 400.0)


class TestIntentFieldsPersistence(unittest.TestCase):
    """验证 MetricsStore 持久化 intent 字段（Task 2.9）。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self.store = MetricsStore(db_path=self._db_path)

    def tearDown(self):
        self.store.close()
        for f in os.listdir(self._tmpdir):
            os.unlink(os.path.join(self._tmpdir, f))
        os.rmdir(self._tmpdir)

    def test_first_write_intent_fields(self):
        """首次插入 intent 字段值正确。"""
        delta = compute_delta(
            _snapshot(
                intent_cron_skips=3,
                intent_classifications={"simple_qa": 5, "multi_step_task": 2},
                intent_fallbacks={"llm_failure": 1},
                intent_hist=_hist(count=7, sum_val=1400.0, min_val=120.0, max_val=350.0),
            ),
            _snapshot(),
        )
        self.store.upsert_daily("2026-07-01", delta)
        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["intent_cron_skips_total"], 3)
        self.assertEqual(record["intent_classifications_total"]["simple_qa"], 5)
        self.assertEqual(record["intent_classifications_total"]["multi_step_task"], 2)
        self.assertEqual(record["intent_fallbacks_total"]["llm_failure"], 1)
        self.assertEqual(record["intent_latency_ms"]["count"], 7)
        self.assertEqual(record["intent_latency_ms"]["min"], 120.0)
        self.assertEqual(record["intent_latency_ms"]["max"], 350.0)
        self.assertAlmostEqual(record["intent_latency_ms"]["avg"], 200.0)

    def test_merge_intent_fields_same_day(self):
        """同一天两次 upsert，intent 字段累加合并。"""
        delta1 = compute_delta(
            _snapshot(
                intent_cron_skips=2,
                intent_classifications={"simple_qa": 3},
                intent_hist=_hist(count=4, sum_val=800.0, min_val=200.0, max_val=400.0),
            ),
            _snapshot(),
        )
        self.store.upsert_daily("2026-07-01", delta1)

        delta2 = compute_delta(
            _snapshot(
                intent_cron_skips=5,
                intent_classifications={"simple_qa": 7, "react_task": 2},
                intent_hist=_hist(count=8, sum_val=1600.0, min_val=100.0, max_val=500.0),
            ),
            _snapshot(
                intent_cron_skips=2,
                intent_classifications={"simple_qa": 3},
                intent_hist=_hist(count=4, sum_val=800.0, min_val=200.0, max_val=400.0),
            ),
        )
        self.store.upsert_daily("2026-07-01", delta2)

        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["intent_cron_skips_total"], 5)  # 2 + 3
        self.assertEqual(record["intent_classifications_total"]["simple_qa"], 7)  # 3 + 4
        self.assertEqual(record["intent_classifications_total"]["react_task"], 2)
        self.assertEqual(record["intent_latency_ms"]["count"], 8)  # 4 + 4
        self.assertEqual(record["intent_latency_ms"]["min"], 100.0)  # 新极端值
        self.assertEqual(record["intent_latency_ms"]["max"], 500.0)  # 新极端值

    def test_alter_table_migration_for_old_db(self):
        """旧 DB 缺 intent 列时启动时 ALTER TABLE 补齐。"""
        # 先用旧 schema 建表（不含 intent 列）
        self.store.close()
        old_conn = sqlite3.connect(self._db_path)
        old_conn.execute("DROP TABLE IF EXISTS metrics_daily")
        old_conn.execute(
            """
            CREATE TABLE metrics_daily (
                date TEXT PRIMARY KEY,
                llm_calls_total INTEGER DEFAULT 0,
                llm_tokens_input_total INTEGER DEFAULT 0,
                llm_tokens_output_total INTEGER DEFAULT 0,
                llm_cache_creation_tokens_total INTEGER DEFAULT 0,
                llm_cache_read_tokens_total INTEGER DEFAULT 0,
                memory_retrieval_hits_total INTEGER DEFAULT 0,
                memory_retrieval_misses_total INTEGER DEFAULT 0,
                llm_latency_count INTEGER DEFAULT 0,
                llm_latency_sum REAL DEFAULT 0,
                llm_latency_min REAL DEFAULT 0,
                llm_latency_max REAL DEFAULT 0,
                tool_latency_count INTEGER DEFAULT 0,
                tool_latency_sum REAL DEFAULT 0,
                tool_latency_min REAL DEFAULT 0,
                tool_latency_max REAL DEFAULT 0,
                tool_calls_total_json TEXT DEFAULT '{}',
                tool_calls_errors_total_json TEXT DEFAULT '{}',
                termination_reasons_total_json TEXT DEFAULT '{}',
                tool_error_classes_total_json TEXT DEFAULT '{}',
                tool_retries_total_json TEXT DEFAULT '{}',
                approval_decisions_total_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 插入一条旧记录（无 intent 字段）
        old_conn.execute(
            "INSERT INTO metrics_daily (date, llm_calls_total, created_at, updated_at) "
            "VALUES ('2026-06-30', 5, '2026-06-30T00:00:00', '2026-06-30T00:00:00')"
        )
        old_conn.commit()
        old_conn.close()

        # 重新初始化 MetricsStore，触发 ALTER 迁移
        self.store = MetricsStore(db_path=self._db_path)

        # 旧记录仍在，intent 字段默认值
        old_record = self.store.get_daily("2026-06-30")
        self.assertIsNotNone(old_record)
        self.assertEqual(old_record["llm_calls_total"], 5)
        self.assertEqual(old_record["intent_cron_skips_total"], 0)
        self.assertEqual(old_record["intent_classifications_total"], {})
        self.assertEqual(old_record["intent_fallbacks_total"], {})
        self.assertEqual(old_record["intent_latency_ms"]["count"], 0)

        # 新记录可正常写入 intent 字段
        self.store.upsert_daily(
            "2026-07-01",
            compute_delta(
                _snapshot(intent_cron_skips=2, intent_classifications={"simple_qa": 1}),
                _snapshot(),
            ),
        )
        new_record = self.store.get_daily("2026-07-01")
        self.assertEqual(new_record["intent_cron_skips_total"], 2)
        self.assertEqual(new_record["intent_classifications_total"]["simple_qa"], 1)

    def test_row_to_dict_reads_intent_fields(self):
        """_row_to_dict 返回的记录含完整 intent 字段（含 avg 计算）。"""
        delta = compute_delta(
            _snapshot(
                intent_cron_skips=4,
                intent_classifications={"simple_qa": 3, "react_task": 1},
                intent_fallbacks={"parse_failure": 2},
                intent_hist=_hist(count=4, sum_val=800.0, min_val=150.0, max_val=250.0),
            ),
            _snapshot(),
        )
        self.store.upsert_daily("2026-07-01", delta)

        record = self.store.get_daily("2026-07-01")
        # 标量
        self.assertEqual(record["intent_cron_skips_total"], 4)
        # dict
        self.assertEqual(record["intent_classifications_total"], {"simple_qa": 3, "react_task": 1})
        self.assertEqual(record["intent_fallbacks_total"], {"parse_failure": 2})
        # hist 含 avg
        self.assertEqual(record["intent_latency_ms"]["count"], 4)
        self.assertEqual(record["intent_latency_ms"]["sum"], 800.0)
        self.assertEqual(record["intent_latency_ms"]["min"], 150.0)
        self.assertEqual(record["intent_latency_ms"]["max"], 250.0)
        self.assertAlmostEqual(record["intent_latency_ms"]["avg"], 200.0)

    def test_empty_delta_default_values(self):
        """空 delta（无 intent 字段）写入时使用默认值兜底。"""
        # 只含 llm_calls，不含 intent 字段
        delta = compute_delta(_snapshot(llm_calls=5), _snapshot())
        # intent 字段在 delta 中应为默认值
        self.assertEqual(delta.get("intent_cron_skips_total", 0), 0)
        self.assertEqual(delta.get("intent_classifications_total", {}), {})

        self.store.upsert_daily("2026-07-01", delta)
        record = self.store.get_daily("2026-07-01")
        self.assertEqual(record["llm_calls_total"], 5)
        self.assertEqual(record["intent_cron_skips_total"], 0)
        self.assertEqual(record["intent_classifications_total"], {})
        self.assertEqual(record["intent_fallbacks_total"], {})
        self.assertEqual(record["intent_latency_ms"]["count"], 0)


class TestTtlCleanup(unittest.TestCase):
    """验证 TTL 清理。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self.store = MetricsStore(db_path=self._db_path)

    def tearDown(self):
        self.store.close()
        for f in os.listdir(self._tmpdir):
            os.unlink(os.path.join(self._tmpdir, f))
        os.rmdir(self._tmpdir)

    def test_delete_old_metrics(self):
        """插入旧日期记录，delete_old_metrics 正确删除。"""
        from datetime import datetime, timedelta

        old_date = (datetime.now() - timedelta(days=35)).date().isoformat()
        recent_date = (datetime.now() - timedelta(days=5)).date().isoformat()

        self.store.upsert_daily(old_date, compute_delta(_snapshot(llm_calls=10), _snapshot()))
        self.store.upsert_daily(recent_date, compute_delta(_snapshot(llm_calls=20), _snapshot()))

        deleted = self.store.delete_old_metrics(30)
        self.assertEqual(deleted, 1)
        self.assertIsNone(self.store.get_daily(old_date))
        self.assertIsNotNone(self.store.get_daily(recent_date))


class TestGetHistory(unittest.TestCase):
    """验证 get_history 返回格式。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self.store = MetricsStore(db_path=self._db_path)

    def tearDown(self):
        self.store.close()
        for f in os.listdir(self._tmpdir):
            os.unlink(os.path.join(self._tmpdir, f))
        os.rmdir(self._tmpdir)

    def test_get_history_ascending_with_json_parsed(self):
        """get_history 返回按日期升序，_json 字段已解析为 dict 且去掉后缀。"""
        self.store.upsert_daily("2026-07-01", compute_delta(
            _snapshot(tool_calls={"file_read": 5}), _snapshot()
        ))
        self.store.upsert_daily("2026-07-02", compute_delta(
            _snapshot(tool_calls={"bash_exec": 3}), _snapshot()
        ))

        history = self.store.get_history(30)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["date"], "2026-07-01")
        self.assertEqual(history[1]["date"], "2026-07-02")
        # _json 字段应被解析为 dict，且无 _json 后缀
        self.assertIsInstance(history[0]["tool_calls_total"], dict)
        self.assertEqual(history[0]["tool_calls_total"]["file_read"], 5)
        self.assertNotIn("tool_calls_total_json", history[0])

    def test_get_daily_with_avg(self):
        """get_daily 返回含计算字段 avg。"""
        delta = compute_delta(
            _snapshot(llm_hist=_hist(count=10, sum_val=2500.0, min_val=100.0, max_val=600.0)),
            _snapshot(),
        )
        self.store.upsert_daily("2026-07-01", delta)

        record = self.store.get_daily("2026-07-01")
        self.assertAlmostEqual(record["llm_latency_ms"]["avg"], 250.0)
        self.assertEqual(record["llm_latency_ms"]["count"], 10)


if __name__ == "__main__":
    unittest.main()
