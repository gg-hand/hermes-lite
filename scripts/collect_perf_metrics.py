"""N-worker 协作性能指标采集脚本。

用法：
    python scripts/collect_perf_metrics.py --duration 1800 --output metrics.json

前提：
    1. 双 worker 已启动（默认 http://localhost:8000 与 http://localhost:8001）
    2. Director 已在 worker-2 上启动
    3. 当前工作目录为项目根（teage-liu/），以便定位 audit.jsonl

采集内容：
    - A2A / director/status 调用延迟（p50/p95/p99，毫秒）
    - audit.jsonl 增长速率（行/分钟）
    - director 进程崩溃 + 自动重启事件计数
    - worker HTTP 可达性（采样成功率）

输出 JSON 字段说明见 metrics["thresholds"] 与 metrics["summary"]。
阈值与 N-worker 修复验收清单对齐：
    - a2a_p99_ms < 1000
    - audit_growth_per_min < 10
    - director_crash_count == 0（或自动重启成功）
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_WORKER1 = "http://localhost:8000"
DEFAULT_WORKER2 = "http://localhost:8001"
DEFAULT_AUDIT_PATH = "data/blackboard_a2a_2/audit/audit.jsonl"

# 阈值（与验收清单对齐）
THRESHOLDS = {
    "a2a_p99_ms": 1000,
    "a2a_p95_ms": 500,
    "audit_growth_per_min": 10,
    "director_unreachable_count": 0,
    "sample_success_rate": 0.95,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get_json(url: str, timeout: float = 5.0):
    """返回 (ok, data_or_err, latency_ms)。"""
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8")
        latency_ms = (time.perf_counter() - t0) * 1000
        try:
            return True, json.loads(body), latency_ms
        except json.JSONDecodeError as e:
            return False, f"json decode: {e}", latency_ms
    except urllib.error.URLError as e:
        return False, f"urllib: {e.reason}", (time.perf_counter() - t0) * 1000
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", (time.perf_counter() - t0) * 1000


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _percentile(data: list[float], pct: float) -> float:
    """简单百分位（线性插值）。data 为空返回 0。"""
    if not data:
        return 0.0
    s = sorted(data)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def collect(duration: int, worker1: str, worker2: str, audit_path: Path,
            sample_interval: int, output: str) -> dict:
    """主采集循环。"""
    metrics = {
        "start_at": _now_iso(),
        "duration_seconds": duration,
        "worker1": worker1,
        "worker2": worker2,
        "audit_path": str(audit_path),
        "sample_interval_seconds": sample_interval,
        "thresholds": dict(THRESHOLDS),
        "samples": [],            # 每次采样的快照
        "a2a_latencies_ms": [],   # worker-2 director/status 延迟
        "director_events": [],    # 崩溃/重启事件
        "audit_growth_per_min": None,
        "summary": {},
    }

    # audit 起始行数
    audit_start_lines = _count_lines(audit_path)
    audit_start_time = time.time()

    # director pid 跟踪
    last_pid = None
    director_unreachable_count = 0
    sample_total = 0
    sample_ok = 0

    end_time = time.time() + duration
    print(f"[perf] 开始采集 {duration}s，每 {sample_interval}s 采样一次")

    while time.time() < end_time:
        sample_total += 1
        snap = {
            "ts": _now_iso(),
            "elapsed": round(time.time() - audit_start_time, 1),
        }

        # 1. worker-2 director/status（A2A 健康代理指标）
        ok, data, lat = _http_get_json(f"{worker2}/api/multiagent/director/status")
        snap["w2_director_status_ok"] = ok
        snap["w2_director_latency_ms"] = round(lat, 2)
        if ok:
            sample_ok += 1
            metrics["a2a_latencies_ms"].append(round(lat, 2))
            running = data.get("running")
            pid = data.get("pid")
            state = data.get("state")
            snap["w2_director_running"] = running
            snap["w2_director_pid"] = pid
            snap["w2_director_state"] = state

            # 检测 pid 变化（崩溃后自动重启）
            if last_pid is not None and pid is not None and pid != last_pid:
                metrics["director_events"].append({
                    "ts": _now_iso(),
                    "type": "pid_changed",
                    "old_pid": last_pid,
                    "new_pid": pid,
                    "state": state,
                })
                print(f"[perf] director pid 变化: {last_pid} -> {pid} (state={state})")
            if not running:
                # director 不在运行，可能是崩溃未恢复
                metrics["director_events"].append({
                    "ts": _now_iso(),
                    "type": "not_running",
                    "pid": pid,
                    "state": state,
                })
                director_unreachable_count += 1
            last_pid = pid
        else:
            director_unreachable_count += 1
            snap["w2_director_error"] = str(data)[:200]

        # 2. worker-1 可达性
        ok1, data1, lat1 = _http_get_json(f"{worker1}/api/multiagent/status")
        snap["w1_status_ok"] = ok1
        snap["w1_latency_ms"] = round(lat1, 2)

        # 3. audit 行数快照
        snap["audit_lines"] = _count_lines(audit_path)

        metrics["samples"].append(snap)
        print(
            f"[perf] t={snap['elapsed']:.0f}s "
            f"w2_director={'ok' if ok else 'FAIL'}({lat:.0f}ms) "
            f"w1={'ok' if ok1 else 'FAIL'} "
            f"audit={snap['audit_lines']}"
        )

        # 等待下一次采样（最后一次可能略超时，不影响总时长统计）
        time.sleep(sample_interval)

    # 汇总
    audit_end_lines = _count_lines(audit_path)
    audit_elapsed = time.time() - audit_start_time
    audit_growth_per_min = (
        (audit_end_lines - audit_start_lines) / audit_elapsed * 60
        if audit_elapsed > 0 else 0.0
    )

    lats = metrics["a2a_latencies_ms"]
    summary = {
        "end_at": _now_iso(),
        "audit_start_lines": audit_start_lines,
        "audit_end_lines": audit_end_lines,
        "audit_growth_per_min": round(audit_growth_per_min, 3),
        "a2a_sample_count": len(lats),
        "a2a_p50_ms": round(_percentile(lats, 0.50), 2),
        "a2a_p95_ms": round(_percentile(lats, 0.95), 2),
        "a2a_p99_ms": round(_percentile(lats, 0.99), 2),
        "a2a_max_ms": round(max(lats), 2) if lats else 0.0,
        "director_unreachable_count": director_unreachable_count,
        "director_event_count": len(metrics["director_events"]),
        "sample_total": sample_total,
        "sample_ok": sample_ok,
        "sample_success_rate": round(sample_ok / sample_total, 4) if sample_total else 0.0,
    }

    # 阈值判定
    summary["pass"] = {
        "a2a_p99_ms": summary["a2a_p99_ms"] < THRESHOLDS["a2a_p99_ms"],
        "audit_growth_per_min": summary["audit_growth_per_min"] < THRESHOLDS["audit_growth_per_min"],
        "director_unreachable_count": summary["director_unreachable_count"] <= THRESHOLDS["director_unreachable_count"],
        "sample_success_rate": summary["sample_success_rate"] >= THRESHOLDS["sample_success_rate"],
    }
    summary["all_pass"] = all(summary["pass"].values())

    metrics["summary"] = summary

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[perf] 指标已写入 {out_path}")
    print(f"[perf] 总判定: {'PASS' if summary['all_pass'] else 'FAIL'}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return metrics


def main():
    parser = argparse.ArgumentParser(
        description="N-worker 协作性能指标采集（长跑用）"
    )
    parser.add_argument("--duration", type=int, default=1800,
                        help="采集总时长（秒），默认 1800=30 分钟")
    parser.add_argument("--worker1", default=DEFAULT_WORKER1,
                        help=f"worker-1 URL，默认 {DEFAULT_WORKER1}")
    parser.add_argument("--worker2", default=DEFAULT_WORKER2,
                        help=f"worker-2 URL，默认 {DEFAULT_WORKER2}")
    parser.add_argument("--audit-path", default=DEFAULT_AUDIT_PATH,
                        help=f"audit.jsonl 路径，默认 {DEFAULT_AUDIT_PATH}")
    parser.add_argument("--sample-interval", type=int, default=30,
                        help="采样间隔（秒），默认 30")
    parser.add_argument("--output", default="metrics.json",
                        help="输出 JSON 文件路径，默认 metrics.json")
    args = parser.parse_args()

    audit_path = Path(args.audit_path)
    collect(
        duration=args.duration,
        worker1=args.worker1,
        worker2=args.worker2,
        audit_path=audit_path,
        sample_interval=args.sample_interval,
        output=args.output,
    )


if __name__ == "__main__":
    main()
