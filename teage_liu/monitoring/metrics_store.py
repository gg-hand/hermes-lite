"""监控指标按天持久化存储。

将 :class:`MetricsCollector` 的累计快照转换为每日增量并持久化到 SQLite。
支持重启合并：进程重启后当天已持久化的增量不丢失，新增量叠加合并。
复用 ``data/sessions.db``，独立连接（WAL 模式支持多连接并发）。

存储模型：
- 每天一行记录，``date`` 为主键（ISO 日期字符串 "2026-07-04"）
- 标量计数器存当日增量（int）
- 直方图存 count/sum/min/max（不存 buckets）
- Dict 指标存 JSON 字符串
- ``upsert_daily`` 采用 read-modify-write 模式实现增量合并
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


# ------------------------------------------------------------------
# 模块级 delta/merge 函数
# ------------------------------------------------------------------

def _compute_hist_delta(cur: dict, base: dict) -> Dict[str, Any]:
    """计算直方图增量。

    - count/sum：current - baseline（clamp >= 0）
    - min/max：baseline.count == 0 时直接取 current 的值（重启后或初始状态）；
      否则检测新极端值（current.min < baseline.min 时取 current.min，否则 None）
    """
    cur_count = cur.get("count", 0)
    base_count = base.get("count", 0)
    delta_count = max(0, cur_count - base_count)
    delta_sum = max(0.0, cur.get("sum", 0.0) - base.get("sum", 0.0))

    if base_count == 0:
        # baseline 为空（重启后或初始），current 代表全部活动
        delta_min = cur.get("min", 0.0) if cur_count > 0 else None
        delta_max = cur.get("max", 0.0) if cur_count > 0 else None
    else:
        # 检测新极端值
        cur_min = cur.get("min", 0.0)
        base_min = base.get("min", 0.0)
        delta_min = cur_min if cur_min < base_min else None

        cur_max = cur.get("max", 0.0)
        base_max = base.get("max", 0.0)
        delta_max = cur_max if cur_max > base_max else None

    return {"count": delta_count, "sum": delta_sum, "min": delta_min, "max": delta_max}


def _merge_hist(existing: dict, delta: dict) -> Dict[str, Any]:
    """合并直方图。count/sum 相加，min 取较小（delta.min 非 None 时），max 取较大。"""
    merged_count = existing.get("count", 0) + delta.get("count", 0)
    merged_sum = existing.get("sum", 0.0) + delta.get("sum", 0.0)

    ex_min = existing.get("min")
    d_min = delta.get("min")
    if d_min is not None:
        merged_min = min(ex_min, d_min) if ex_min is not None and ex_min > 0 else d_min
    else:
        merged_min = ex_min

    ex_max = existing.get("max")
    d_max = delta.get("max")
    if d_max is not None:
        merged_max = max(ex_max, d_max) if ex_max is not None else d_max
    else:
        merged_max = ex_max

    return {"count": merged_count, "sum": merged_sum, "min": merged_min, "max": merged_max}


def _merge_dict(existing: dict, delta: dict) -> Dict[str, int]:
    """Dict 按 key 累加。"""
    result = dict(existing)
    for key, val in delta.items():
        result[key] = result.get(key, 0) + val
    return result


def compute_delta(current: dict, baseline: dict) -> dict:
    """计算两个快照间的增量。

    标量计数器：``max(0, current - baseline)``（clamp 处理 reset 场景）。
    直方图：见 :func:`_compute_hist_delta`。
    Dict 指标：按 key 取 ``max(0, diff)``。
    """
    delta: Dict[str, Any] = {}

    # 标量计数器
    scalar_keys = [
        "llm_calls_total",
        "llm_tokens_input_total",
        "llm_tokens_output_total",
        "llm_cache_creation_tokens_total",
        "llm_cache_read_tokens_total",
        "memory_retrieval_hits_total",
        "memory_retrieval_misses_total",
        "intent_cron_skips_total",
    ]
    for key in scalar_keys:
        cur_val = current.get(key, 0)
        base_val = baseline.get(key, 0)
        delta[key] = max(0, cur_val - base_val)

    # 直方图
    delta["llm_latency_ms"] = _compute_hist_delta(
        current.get("llm_latency_ms", {}), baseline.get("llm_latency_ms", {})
    )
    delta["tool_latency_ms"] = _compute_hist_delta(
        current.get("tool_latency_ms", {}), baseline.get("tool_latency_ms", {})
    )
    delta["intent_latency_ms"] = _compute_hist_delta(
        current.get("intent_latency_ms", {}), baseline.get("intent_latency_ms", {})
    )

    # Dict 指标
    dict_keys = [
        "tool_calls_total",
        "tool_calls_errors_total",
        "termination_reasons_total",
        "tool_retries_total",
        "approval_decisions_total",
        "intent_classifications_total",
        "intent_fallbacks_total",
    ]
    for key in dict_keys:
        cur_dict = current.get(key, {})
        base_dict = baseline.get(key, {})
        d: Dict[str, int] = {}
        for k, v in cur_dict.items():
            d[k] = max(0, v - base_dict.get(k, 0))
        delta[key] = d

    # tool_error_classes_total 是嵌套 Dict
    cur_err_classes = current.get("tool_error_classes_total", {})
    base_err_classes = baseline.get("tool_error_classes_total", {})
    err_delta: Dict[str, Dict[str, int]] = {}
    for tool, classes in cur_err_classes.items():
        base_classes = base_err_classes.get(tool, {})
        tool_delta: Dict[str, int] = {}
        for cls, cnt in classes.items():
            tool_delta[cls] = max(0, cnt - base_classes.get(cls, 0))
        err_delta[tool] = tool_delta
    delta["tool_error_classes_total"] = err_delta

    return delta


# ------------------------------------------------------------------
# MetricsStore 类
# ------------------------------------------------------------------

class MetricsStore:
    """监控指标按天持久化存储器。

    通过 ``threading.Lock`` 保护 SQLite 写操作，与 SessionLogger /
    UploadManager 共享同一 DB 文件（sessions.db），各自维护独立连接
    与锁（WAL 模式支持并发读写）。
    """

    # DB 列名映射：snapshot key → DB 列名
    _SCALAR_COLS = [
        "llm_calls_total",
        "llm_tokens_input_total",
        "llm_tokens_output_total",
        "llm_cache_creation_tokens_total",
        "llm_cache_read_tokens_total",
        "memory_retrieval_hits_total",
        "memory_retrieval_misses_total",
        "intent_cron_skips_total",
    ]
    _HIST_COLS = {
        "llm_latency_ms": "llm_latency",
        "tool_latency_ms": "tool_latency",
        "intent_latency_ms": "intent_latency",
    }
    _DICT_COLS = {
        "tool_calls_total": "tool_calls_total_json",
        "tool_calls_errors_total": "tool_calls_errors_total_json",
        "termination_reasons_total": "termination_reasons_total_json",
        "tool_error_classes_total": "tool_error_classes_total_json",
        "tool_retries_total": "tool_retries_total_json",
        "approval_decisions_total": "approval_decisions_total_json",
        "intent_classifications_total": "intent_classifications_total_json",
        "intent_fallbacks_total": "intent_fallbacks_total_json",
    }

    def __init__(self, db_path: str) -> None:
        """初始化，建立 SQLite 连接并创建表。"""
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self) -> None:
        """创建 metrics_daily 表（如不存在）并迁移旧库缺失列。"""
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics_daily (
                    date TEXT PRIMARY KEY,
                    llm_calls_total INTEGER DEFAULT 0,
                    llm_tokens_input_total INTEGER DEFAULT 0,
                    llm_tokens_output_total INTEGER DEFAULT 0,
                    llm_cache_creation_tokens_total INTEGER DEFAULT 0,
                    llm_cache_read_tokens_total INTEGER DEFAULT 0,
                    memory_retrieval_hits_total INTEGER DEFAULT 0,
                    memory_retrieval_misses_total INTEGER DEFAULT 0,
                    intent_cron_skips_total INTEGER DEFAULT 0,
                    llm_latency_count INTEGER DEFAULT 0,
                    llm_latency_sum REAL DEFAULT 0,
                    llm_latency_min REAL DEFAULT 0,
                    llm_latency_max REAL DEFAULT 0,
                    tool_latency_count INTEGER DEFAULT 0,
                    tool_latency_sum REAL DEFAULT 0,
                    tool_latency_min REAL DEFAULT 0,
                    tool_latency_max REAL DEFAULT 0,
                    intent_latency_count INTEGER DEFAULT 0,
                    intent_latency_sum REAL DEFAULT 0,
                    intent_latency_min REAL DEFAULT 0,
                    intent_latency_max REAL DEFAULT 0,
                    tool_calls_total_json TEXT DEFAULT '{}',
                    tool_calls_errors_total_json TEXT DEFAULT '{}',
                    termination_reasons_total_json TEXT DEFAULT '{}',
                    tool_error_classes_total_json TEXT DEFAULT '{}',
                    tool_retries_total_json TEXT DEFAULT '{}',
                    approval_decisions_total_json TEXT DEFAULT '{}',
                    intent_classifications_total_json TEXT DEFAULT '{}',
                    intent_fallbacks_total_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_daily_date ON metrics_daily(date);"
            )
            # 兼容旧库：缺失的 intent 列通过 ALTER TABLE ADD COLUMN 补齐
            existing_cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(metrics_daily)").fetchall()
            }
            alter_specs = [
                ("intent_cron_skips_total", "INTEGER DEFAULT 0"),
                ("intent_latency_count", "INTEGER DEFAULT 0"),
                ("intent_latency_sum", "REAL DEFAULT 0"),
                ("intent_latency_min", "REAL DEFAULT 0"),
                ("intent_latency_max", "REAL DEFAULT 0"),
                ("intent_classifications_total_json", "TEXT DEFAULT '{}'"),
                ("intent_fallbacks_total_json", "TEXT DEFAULT '{}'"),
            ]
            for col_name, col_type in alter_specs:
                if col_name not in existing_cols:
                    self._conn.execute(
                        f"ALTER TABLE metrics_daily ADD COLUMN {col_name} {col_type}"
                    )
            self._conn.commit()

    def upsert_daily(self, date_str: str, delta: dict) -> None:
        """合并写入当日增量（核心方法）。

        读取现有记录 → 在 Python 中 merge → INSERT 或 UPDATE。
        全程持锁，保证 read-modify-write 原子性。
        """
        with self._lock:
            now_iso = datetime.now().isoformat()
            row = self._conn.execute(
                "SELECT * FROM metrics_daily WHERE date = ?", (date_str,)
            ).fetchone()

            if row is None:
                # 首次写入：直接 INSERT
                self._insert_new(date_str, delta, now_iso)
            else:
                # 合并写入：UPDATE
                self._merge_update(row, delta, now_iso)
            self._conn.commit()

    def _insert_new(self, date_str: str, delta: dict, now_iso: str) -> None:
        """首次插入当天记录。"""
        cols = ["date", "created_at", "updated_at"]
        vals: List[Any] = [date_str, now_iso, now_iso]

        for key in self._SCALAR_COLS:
            cols.append(key)
            vals.append(int(delta.get(key, 0)))

        for snap_key, col_prefix in self._HIST_COLS.items():
            hist = delta.get(snap_key, {})
            for field in ["count", "sum", "min", "max"]:
                cols.append(f"{col_prefix}_{field}")
                val = hist.get(field)
                if field in ("count",):
                    vals.append(int(val or 0))
                elif field == "sum":
                    vals.append(float(val or 0.0))
                else:
                    vals.append(float(val) if val is not None else 0.0)

        for snap_key, col_name in self._DICT_COLS.items():
            cols.append(col_name)
            vals.append(json.dumps(delta.get(snap_key, {}), ensure_ascii=False))

        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        self._conn.execute(
            f"INSERT INTO metrics_daily ({col_names}) VALUES ({placeholders})", vals
        )

    def _merge_update(self, row: sqlite3.Row, delta: dict, now_iso: str) -> None:
        """合并增量到现有记录。"""
        updates: List[str] = []
        vals: List[Any] = []

        for key in self._SCALAR_COLS:
            existing = row[key] if key in row.keys() else 0
            merged = int(existing) + int(delta.get(key, 0))
            updates.append(f"{key} = ?")
            vals.append(merged)

        for snap_key, col_prefix in self._HIST_COLS.items():
            hist_delta = delta.get(snap_key, {})
            existing_hist = {
                "count": row[f"{col_prefix}_count"],
                "sum": row[f"{col_prefix}_sum"],
                "min": row[f"{col_prefix}_min"],
                "max": row[f"{col_prefix}_max"],
            }
            merged = _merge_hist(existing_hist, hist_delta)
            for field in ["count", "sum", "min", "max"]:
                updates.append(f"{col_prefix}_{field} = ?")
                val = merged.get(field)
                if field == "count":
                    vals.append(int(val or 0))
                elif field == "sum":
                    vals.append(float(val or 0.0))
                else:
                    vals.append(float(val) if val is not None else 0.0)

        for snap_key, col_name in self._DICT_COLS.items():
            existing_dict = json.loads(row[col_name] or "{}")
            delta_dict = delta.get(snap_key, {})
            if snap_key == "tool_error_classes_total":
                # 嵌套 Dict
                merged_dict = self._merge_nested_dict(existing_dict, delta_dict)
            else:
                merged_dict = _merge_dict(existing_dict, delta_dict)
            updates.append(f"{col_name} = ?")
            vals.append(json.dumps(merged_dict, ensure_ascii=False))

        updates.append("updated_at = ?")
        vals.append(now_iso)
        vals.append(row["date"])

        set_clause = ", ".join(updates)
        self._conn.execute(
            f"UPDATE metrics_daily SET {set_clause} WHERE date = ?", vals
        )

    @staticmethod
    def _merge_nested_dict(
        existing: Dict[str, Dict[str, int]], delta: Dict[str, Dict[str, int]]
    ) -> Dict[str, Dict[str, int]]:
        """合并嵌套 Dict（tool_error_classes_total 用）。"""
        result = {k: dict(v) for k, v in existing.items()}
        for tool, classes in delta.items():
            if tool not in result:
                result[tool] = {}
            for cls, cnt in classes.items():
                result[tool][cls] = result[tool].get(cls, 0) + cnt
        return result

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """将 DB 行转换为前端友好的 dict（解析 _json 字段，计算 avg）。"""
        result: Dict[str, Any] = {"date": row["date"]}

        for key in self._SCALAR_COLS:
            result[key] = row[key]

        for snap_key, col_prefix in self._HIST_COLS.items():
            count = row[f"{col_prefix}_count"]
            sum_val = row[f"{col_prefix}_sum"]
            result[snap_key] = {
                "count": count,
                "sum": sum_val,
                "min": row[f"{col_prefix}_min"],
                "max": row[f"{col_prefix}_max"],
                "avg": (sum_val / count) if count > 0 else 0.0,
            }

        for snap_key, col_name in self._DICT_COLS.items():
            result[snap_key] = json.loads(row[col_name] or "{}")

        result["created_at"] = row["created_at"]
        result["updated_at"] = row["updated_at"]
        return result

    def get_daily(self, date_str: str) -> Optional[dict]:
        """获取某天的指标记录，含计算字段 avg。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM metrics_daily WHERE date = ?", (date_str,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_history(self, days: int = 30) -> List[dict]:
        """获取最近 N 天的历史记录（按日期升序，便于前端绘图）。"""
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM metrics_daily WHERE date >= ? ORDER BY date ASC",
                (cutoff,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete_old_metrics(self, ttl_days: int) -> int:
        """删除超过 TTL 的旧记录（同 delete_old_sessions 模式）。"""
        cutoff = (datetime.now() - timedelta(days=ttl_days)).date().isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM metrics_daily WHERE date < ?", (cutoff,)
            )
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()
