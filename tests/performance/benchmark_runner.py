"""Teage Liu Performance Benchmark Runner.

Runs 5 benchmark scenarios against the orchestrator with mock LLM backend,
collects latency, memory, GC, and lock contention metrics.

Usage:
    python tests/performance/benchmark_runner.py [--scenario all|a|b|c|d|e] [--profile]
"""

from __future__ import annotations

import atexit
import cProfile
import gc
import json
import logging
import os
import pstats
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Ensure we can import from teage_liu-lite src
SRC_DIR = str(Path(__file__).resolve().parent.parent / "teage_liu")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Profiling results directory
RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ============================================================
# Monkey-patch: Inject MockBackend into LLMClient
# ============================================================

def _patch_llm_client():
    """Replace LLMClient._create_backend to return MockBackend for 'mock' provider."""
    from teage_liu.llm import client as llm_client_module
    from tests.performance.mock_backend import MockBackend

    _original_create_backend = llm_client_module.LLMClient._create_backend

    def _patched_create_backend(self, provider, model, api_key, base_url=None):
        provider_lower = provider.strip().lower()
        if provider_lower == "mock":
            return MockBackend(
                model=model,
                api_key=api_key or "mock-key",
                base_url=base_url,
            )
        return _original_create_backend(self, provider, model, api_key, base_url)

    llm_client_module.LLMClient._create_backend = _patched_create_backend

    # Also patch LLMClient to accept mock provider config without validation errors
    _original_init = llm_client_module.LLMClient.__init__

    def _patched_init(self, config_path=None, config=None, metrics_collector=None):
        """Skip API key validation for mock provider."""
        from teage_liu.llm.client import _PROVIDER_DEFAULT_ENV_KEY

        if config is None:
            from teage_liu.config import load_config
            config = load_config(config_path)

        llm_config = config.get("llm", {})

        # Store config for later use
        self._metrics_collector = metrics_collector
        self.main_provider = llm_config.get("main_provider", "mock")
        self.main_model = llm_config.get("main_model", "mock-model")
        self.consolidation_provider = llm_config.get("consolidation_provider", "mock")
        self.consolidation_model = llm_config.get("consolidation_model", "mock-model")
        self.max_context_tokens = int(llm_config.get("max_context_tokens", 200000))
        self.context_threshold = float(llm_config.get("context_threshold", 0.8))

        # Create backends - skip API key check for mock
        self._main_backend = self._create_backend(
            self.main_provider, self.main_model,
            llm_config.get("main_api_key", "mock-key"),
            llm_config.get("main_base_url"),
        )
        self._consolidation_backend = self._create_backend(
            self.consolidation_provider, self.consolidation_model,
            llm_config.get("consolidation_api_key", "mock-key"),
            llm_config.get("consolidation_base_url"),
        )

    llm_client_module.LLMClient.__init__ = _patched_init
    logger.info("LLMClient patched: 'mock' provider -> MockBackend")


def _patch_sqlite_logger_with_lock_profiler():
    """Wrap SessionLogger locks with ProfiledLock."""
    from tests.performance.lock_profiler import get_lock
    from teage_liu.storage import sqlite_log as sqlite_log_module

    if hasattr(sqlite_log_module.SessionLogger, '_lock') and not hasattr(sqlite_log_module.SessionLogger, '_profiled'):
        original_lock = sqlite_log_module.SessionLogger._lock
        profiled = get_lock("SessionLogger._lock")
        # Only patch if not already profiled
        if not isinstance(original_lock, type(profiled)):
            sqlite_log_module.SessionLogger._lock = profiled
            sqlite_log_module.SessionLogger._profiled = True
            logger.info("SessionLogger._lock wrapped with ProfiledLock")


def _patch_metrics_lock():
    """Wrap MetricsCollector lock with ProfiledLock."""
    try:
        from tests.performance.lock_profiler import get_lock
        from teage_liu.monitoring import metrics as metrics_module

        if hasattr(metrics_module.MetricsCollector, '_lock') and not hasattr(metrics_module.MetricsCollector, '_profiled'):
            metrics_module.MetricsCollector._profiled = True
            # The lock is created in __init__, so we patch at instance level
            logger.info("MetricsCollector lock profiling available")
    except ImportError:
        pass


# ============================================================
# Profiling Helpers
# ============================================================

class ProfilingSession:
    """Manages profiling context for a benchmark run."""

    def __init__(self, scenario_name: str):
        self.scenario_name = scenario_name
        self.scenario_dir = RESULTS_DIR / scenario_name
        self.scenario_dir.mkdir(parents=True, exist_ok=True)

        # Timing data
        self.latencies: List[float] = []
        self.phase_times: Dict[str, List[float]] = {}

        # System metrics
        self.process = psutil.Process()
        self.cpu_samples: List[float] = []
        self.mem_samples: List[Tuple[float, int]] = []  # (timestamp, RSS bytes)
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None

        # Profiling
        self._cprofiler: Optional[cProfile.Profile] = None
        self._trace_snapshots: List[Any] = []

    def start_monitoring(self, interval: float = 0.5):
        """Start background CPU/memory monitoring."""
        self._monitoring = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(interval,), daemon=True
        )
        self._monitor_thread.start()

    def stop_monitoring(self):
        """Stop background monitoring."""
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

    def _monitor_loop(self, interval: float):
        while self._monitoring:
            try:
                mem = self.process.memory_info().rss
                cpu = self.process.cpu_percent(interval=0)
                self.cpu_samples.append(cpu)
                self.mem_samples.append((time.time(), mem))
            except Exception:
                pass
            time.sleep(interval)

    def start_cprofile(self):
        """Start cProfile profiling."""
        self._cprofiler = cProfile.Profile()
        self._cprofiler.enable()

    def stop_cprofile(self):
        """Stop cProfile and save stats."""
        if self._cprofiler:
            self._cprofiler.disable()
            prof_path = self.scenario_dir / "cprofile.out"
            self._cprofiler.dump_stats(str(prof_path))

            # Save human-readable stats
            stats_path = self.scenario_dir / "cprofile_stats.txt"
            with open(stats_path, "w") as f:
                p = pstats.Stats(self._cprofiler, stream=f)
                p.sort_stats("cumtime")
                p.print_stats(60)
                f.write("\n\n=== By tottime ===\n")
                p.sort_stats("tottime")
                p.print_stats(60)
            logger.info(f"  cProfile saved: {prof_path} / {stats_path}")

    def start_tracemalloc(self):
        """Start tracemalloc for memory leak detection."""
        tracemalloc.start(25)

    def take_trace_snapshot(self):
        """Take a tracemalloc snapshot."""
        if tracemalloc.is_tracing():
            self._trace_snapshots.append(tracemalloc.take_snapshot())

    def stop_tracemalloc(self) -> Optional[Dict]:
        """Stop tracemalloc and compute diff from first snapshot."""
        if not tracemalloc.is_tracing() or len(self._trace_snapshots) < 2:
            tracemalloc.stop()
            return None

        snap2 = tracemalloc.take_snapshot()
        stats = snap2.compare_to(self._trace_snapshots[0], 'lineno')

        top_leaks = []
        for stat in stats[:30]:
            top_leaks.append({
                "traceback": str(stat.traceback),
                "size_diff_bytes": stat.size_diff,
                "count_diff": stat.count_diff,
            })

        tracemalloc.stop()
        return {"top_leaks": top_leaks}

    def log_latency(self, latency_ms: float):
        self.latencies.append(latency_ms)

    def log_phase(self, phase: str, elapsed_ms: float):
        if phase not in self.phase_times:
            self.phase_times[phase] = []
        self.phase_times[phase].append(elapsed_ms)

    def compute_stats(self, values: List[float]) -> Dict[str, float]:
        if not values:
            return {"min": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0, "count": 0}
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        return {
            "min": sorted_vals[0],
            "p50": sorted_vals[int(n * 0.50)],
            "p95": sorted_vals[int(n * 0.95)],
            "p99": sorted_vals[int(n * 0.99)],
            "max": sorted_vals[-1],
            "mean": sum(sorted_vals) / n,
            "count": n,
        }

    def save_report(self, extra: Optional[Dict] = None):
        """Save all collected metrics as JSON."""
        report = {
            "scenario": self.scenario_name,
            "timestamp": datetime.now().isoformat(),
            "latency_ms": self.compute_stats(self.latencies),
            "phase_times": {
                phase: self.compute_stats(times)
                for phase, times in self.phase_times.items()
            },
            "system": {
                "cpu_samples": {
                    "count": len(self.cpu_samples),
                    "mean": sum(self.cpu_samples) / max(1, len(self.cpu_samples)),
                    "max": max(self.cpu_samples) if self.cpu_samples else 0,
                },
                "memory_rss_mb": {
                    "samples": len(self.mem_samples),
                    "peak_mb": max(m[1] for m in self.mem_samples) / (1024 * 1024) if self.mem_samples else 0,
                    "final_mb": self.mem_samples[-1][1] / (1024 * 1024) if self.mem_samples else 0,
                },
            },
        }

        # Add GC stats
        gc_stats = gc.get_stats()
        report["gc"] = {
            "collections": gc_stats,
            "total_collected": sum(s["collected"] for gen in gc_stats for s in [gen]),
        }

        # Add lock profiling data
        try:
            from tests.performance.lock_profiler import get_registry
            report["locks"] = get_registry().snapshot()
        except ImportError:
            pass

        # Add tracemalloc results
        trace_result = self.stop_tracemalloc()
        if trace_result:
            report["memory_leaks"] = trace_result

        if extra:
            report.update(extra)

        report_path = self.scenario_dir / "report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info(f"Report saved: {report_path}")
        return report_path


# ============================================================
# Orchestrator Factory
# ============================================================

def create_test_config(data_dir: str, scenario: str, max_react_loops: int = 5) -> dict:
    """Create a test configuration dict."""
    return {
        "llm": {
            "main_provider": "mock",
            "main_model": f"mock-{scenario}",
            "main_api_key": "mock-key",
            "consolidation_provider": "mock",
            "consolidation_model": f"mock-consolidation-{scenario}",
            "consolidation_api_key": "mock-key",
            "max_context_tokens": 200000,
            "context_threshold": 0.8,
        },
        "memory": {
            "chroma_path": os.path.join(data_dir, "chroma"),
            "memory_md_path": os.path.join(data_dir, "memory.md"),
            "consolidation_threshold": 3,
            "dedup_similarity_threshold": 0.85,
            "retrieval_top_k": 5,
            "surprise_gate_enabled": True,
            "surprise_similarity_threshold": 0.85,
            "surprise_skip_threshold": 0.92,
            "decay_rate": 0.01,
            "frequency_weight": 0.5,
            "history_max_turns": 50,
            "archive_turns_on_evict": True,
            "condenser": {
                "enabled": True,
                "strategy": "masking",
                "keep_recent_n": 6,
                "keep_first": 2,
                "llm_summary_threshold": 100000,
            },
        },
        "server": {"host": "0.0.0.0", "port": 8000},
        "storage": {
            "sqlite_path": os.path.join(data_dir, "sessions.db"),
            "session_ttl_days": 30,
            "cleanup_interval_hours": 24,
        },
        "history": {
            "persistence_dir": os.path.join(data_dir, "history"),
        },
        "tools": {
            "max_react_loops": max_react_loops,
            "defer_loading_threshold": 20,
        },
        "monitoring": {"enabled": True},
        "skills": {"teage_liu": [], "mcp": []},
        "security": {
            "enabled": True,
            "approval_timeout_seconds": 300,
            "rules": [],
        },
        "tasks": {},
        "schedules": [],
    }


def create_orchestrator(data_dir: str, scenario: str, max_react_loops: int = 5):
    """Create an Orchestrator with mock backend and test config."""
    from teage_liu.orchestrator import Orchestrator

    config = create_test_config(data_dir, scenario, max_react_loops)

    # Pre-load and seed test data is handled by calling seed_data separately

    return Orchestrator(config=config)


# ============================================================
# Benchmark Scenarios
# ============================================================

def _warmup(orc, session_id: str = "warmup") -> float:
    """Warmup run to initialize caches and JIT."""
    t0 = time.perf_counter()
    try:
        orc.chat(session_id, "预热测试消息。")
    except Exception as e:
        logger.warning(f"Warmup error (may be expected): {e}")
    return (time.perf_counter() - t0) * 1000


def run_scenario_a(orc, session: ProfilingSession, n_runs: int = 50):
    """Scenario A: Simple Q&A (no tool calls, 1-turn)."""
    logger.info(f"  Running {n_runs} iterations...")

    session.start_monitoring(interval=0.2)

    for i in range(n_runs):
        t0 = time.perf_counter()
        try:
            result = orc.chat(f"bench_a_{i}", "你好，今天天气怎么样？")
            elapsed = (time.perf_counter() - t0) * 1000
            session.log_latency(elapsed)
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.warning(f"  Iteration {i} error after {elapsed:.1f}ms: {e}")
            session.log_latency(elapsed)

        if (i + 1) % 10 == 0:
            logger.info(f"    Completed {i + 1}/{n_runs}")

    session.stop_monitoring()


def run_scenario_b(orc, session: ProfilingSession, n_sessions: int = 20):
    """Scenario B: Tool-intensive session."""
    logger.info(f"  Running {n_sessions} tool-intensive sessions...")

    # Set up mock backend for tool_intensive scenario
    mock_backend = orc.llm_client._main_backend
    mock_backend.scenario = "tool_intensive"
    mock_backend.call_count = 0

    session.start_monitoring(interval=0.2)

    for i in range(n_sessions):
        mock_backend.call_count = 0
        mock_backend._response_index = 0
        mock_backend._responses.clear()

        t0 = time.perf_counter()
        try:
            result = orc.chat(
                f"bench_b_{i}",
                "读取 /tmp/test.txt 文件，然后执行 echo 命令。"
            )
            elapsed = (time.perf_counter() - t0) * 1000
            session.log_latency(elapsed)
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.warning(f"  Session {i} error after {elapsed:.1f}ms: {e}")
            session.log_latency(elapsed)

        if (i + 1) % 5 == 0:
            logger.info(f"    Completed {i + 1}/{n_sessions} sessions")

    session.stop_monitoring()


def run_scenario_c(orc, session: ProfilingSession, n_sessions: int = 3):
    """Scenario C: Memory-intensive session with long history and consolidation."""
    logger.info(f"  Running {n_sessions} memory-intensive sessions (200 turns each)...")

    mock_backend = orc.llm_client._main_backend
    mock_backend.scenario = "consolidation"

    session.start_monitoring(interval=0.5)
    session.start_tracemalloc()
    session.take_trace_snapshot()

    # Pre-populate history for each session
    sample_msgs = [
        "介绍一下你自己。",
        "帮我写一个Python装饰器。",
        "什么是闭包？",
        "解释一下异步编程。",
        "如何优化SQL查询？",
        "推荐一些设计模式。",
    ]

    for sid in range(n_sessions):
        session_id = f"bench_c_{sid}"

        # Pre-fill history with 50 turns
        logger.info(f"    Session {sid}: Pre-filling history...")
        for h in range(50):
            role = "user" if h % 2 == 0 else "assistant"
            msg = sample_msgs[h % len(sample_msgs)]
            # Use internal history buffer directly
            if orc.history_buffer:
                orc.history_buffer.add_message(session_id, {"role": role, "content": msg})
            if orc.session_logger:
                try:
                    orc.session_logger.log_message(session_id, role, msg)
                except Exception:
                    pass

        # Run the actual benchmark: send messages that trigger consolidation
        logger.info(f"    Session {sid}: Running benchmark turns...")
        for turn in range(150):
            msg = f"第{turn}轮：{sample_msgs[turn % len(sample_msgs)]}"

            t0 = time.perf_counter()
            try:
                result = orc.chat(session_id, msg)
                elapsed = (time.perf_counter() - t0) * 1000
                session.log_latency(elapsed)
            except Exception as e:
                elapsed = (time.perf_counter() - t0) * 1000
                logger.warning(f"      Turn {turn} error: {e}")
                session.log_latency(elapsed)

            if (turn + 1) % 50 == 0:
                logger.info(f"      Completed turn {turn + 1}/150")

        session.take_trace_snapshot()

    session.stop_monitoring()


def run_scenario_d(orc, session: ProfilingSession, duration_secs: int = 60):
    """Scenario D: Multi-user concurrent load."""
    import concurrent.futures

    n_workers = 10
    logger.info(f"  Running {n_workers} concurrent users for {duration_secs}s...")

    mock_backend = orc.llm_client._main_backend
    mock_backend.scenario = "simple_qa"

    session.start_monitoring(interval=0.2)

    def _user_loop(user_id: int, n_requests: int):
        local_latencies = []
        for i in range(n_requests):
            t0 = time.perf_counter()
            try:
                orc.chat(f"bench_d_user{user_id}_req{i}", "并发测试消息。")
                elapsed = (time.perf_counter() - t0) * 1000
                local_latencies.append(elapsed)
            except Exception as e:
                elapsed = (time.perf_counter() - t0) * 1000
                local_latencies.append(elapsed)
        return local_latencies

    t_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
        # Each user sends requests for the duration
        futures = []
        for uid in range(n_workers):
            futures.append(executor.submit(_user_loop, uid, 5))

        all_latencies = []
        for f in concurrent.futures.as_completed(futures):
            all_latencies.extend(f.result())

    elapsed_total = time.perf_counter() - t_start
    for lat in all_latencies:
        session.log_latency(lat)

    throughput = len(all_latencies) / elapsed_total
    session.stop_monitoring()

    return {"throughput_req_per_sec": throughput, "concurrent_users": n_workers}


def run_scenario_e(data_dir: str, session: ProfilingSession, n_restarts: int = 5):
    """Scenario E: Cold-start analysis."""
    import subprocess

    logger.info(f"  Running {n_restarts} cold start measurements...")

    for i in range(n_restarts):
        # Clean data for fresh start
        chroma_dir = os.path.join(data_dir, "chroma")
        if os.path.exists(chroma_dir):
            shutil.rmtree(chroma_dir)
        os.makedirs(chroma_dir, exist_ok=True)

        t0 = time.perf_counter()
        try:
            orc = create_orchestrator(data_dir, "simple_qa", max_react_loops=1)
            init_time = (time.perf_counter() - t0) * 1000
            session.log_phase("orchestrator_init_ms", init_time)

            # First chat (cold cache)
            t1 = time.perf_counter()
            result = orc.chat("bench_e_cold", "冷启动测试消息。")
            first_chat_time = (time.perf_counter() - t1) * 1000
            session.log_phase("first_chat_cold_ms", first_chat_time)

            session.log_latency(init_time + first_chat_time)

        except Exception as e:
            logger.warning(f"  Restart {i} error: {e}")

        logger.info(f"    Completed restart {i + 1}/{n_restarts}")

    # Also measure warm start for comparison
    t0 = time.perf_counter()
    orc = create_orchestrator(data_dir, "simple_qa", max_react_loops=1)
    warm_init = (time.perf_counter() - t0) * 1000
    session.log_phase("warm_init_ms", warm_init)

    t1 = time.perf_counter()
    result = orc.chat("bench_e_warm", "热启动测试消息。")
    warm_chat = (time.perf_counter() - t1) * 1000
    session.log_phase("warm_first_chat_ms", warm_chat)


# ============================================================
# Main Orchestrator
# ============================================================

def run_all_scenarios(profile_cpu: bool = False):
    """Run all 5 benchmark scenarios and generate reports."""
    from tests.performance.seed_data import (
        prepare_directories, seed_chromadb, seed_sqlite,
        seed_memory_md, seed_history,
    )

    # Setup
    logger.info("=" * 60)
    logger.info("Teage Liu Performance Benchmark Suite")
    logger.info("=" * 60)

    # Create test data directory
    data_dir = str(prepare_directories("data/test_profiling"))
    logger.info(f"Test data directory: {data_dir}")

    # Seed test data
    logger.info("Seeding test data...")
    seed_chromadb(os.path.join(data_dir, "chroma"), num_vectors=1000)
    seed_sqlite(os.path.join(data_dir, "sessions.db"))
    seed_memory_md(os.path.join(data_dir, "memory.md"))
    seed_history(os.path.join(data_dir, "history"))
    results_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Creating orchestrator with mock backend...")
    orc = create_orchestrator(data_dir, "simple_qa", max_react_loops=5)

    # Warmup
    logger.info("Warmup...")
    _warmup(orc)
    logger.info("Warmup complete.\n")

    results = {}

    # --- Scenario A: Simple Q&A ---
    logger.info(f"\n{'='*60}")
    logger.info("Scenario A: Simple Q&A (no tools, 1-turn)")
    logger.info(f"{'='*60}")
    sess_a = ProfilingSession("scenario_a")
    if profile_cpu:
        sess_a.start_cprofile()
    run_scenario_a(orc, sess_a, n_runs=50)
    if profile_cpu:
        sess_a.stop_cprofile()
    results["scenario_a"] = str(sess_a.save_report())
    del sess_a

    # --- Scenario B: Tool-intensive ---
    logger.info(f"\n{'='*60}")
    logger.info("Scenario B: Tool-intensive session")
    logger.info(f"{'='*60}")
    sess_b = ProfilingSession("scenario_b")
    if profile_cpu:
        sess_b.start_cprofile()
    run_scenario_b(orc, sess_b, n_sessions=20)
    if profile_cpu:
        sess_b.stop_cprofile()
    results["scenario_b"] = str(sess_b.save_report())
    del sess_b

    # --- Scenario C: Memory-intensive ---
    logger.info(f"\n{'='*60}")
    logger.info("Scenario C: Memory-intensive (long history + consolidation)")
    logger.info(f"{'='*60}")
    sess_c = ProfilingSession("scenario_c")
    if profile_cpu:
        sess_c.start_cprofile()
    run_scenario_c(orc, sess_c, n_sessions=1)  # 1 session for speed
    if profile_cpu:
        sess_c.stop_cprofile()
    results["scenario_c"] = str(sess_c.save_report())
    del sess_c

    # --- Scenario D: Concurrent load ---
    logger.info(f"\n{'='*60}")
    logger.info("Scenario D: Multi-user concurrent load")
    logger.info(f"{'='*60}")
    sess_d = ProfilingSession("scenario_d")
    if profile_cpu:
        sess_d.start_cprofile()
    extra_d = run_scenario_d(orc, sess_d, duration_secs=30)
    if profile_cpu:
        sess_d.stop_cprofile()
    results["scenario_d"] = str(sess_d.save_report(extra_d))
    del sess_d

    # --- Scenario E: Cold start ---
    logger.info(f"\n{'='*60}")
    logger.info("Scenario E: Cold start analysis")
    logger.info(f"{'='*60}")
    sess_e = ProfilingSession("scenario_e")
    run_scenario_e(data_dir, sess_e, n_restarts=3)
    results["scenario_e"] = str(sess_e.save_report())
    del sess_e

    # Save master results index
    master_path = RESULTS_DIR / "master_index.json"
    with open(master_path, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "python_version": sys.version,
            "platform": sys.platform,
            "results": results,
        }, f, indent=2)

    logger.info(f"\n{'='*60}")
    logger.info("All scenarios complete!")
    logger.info(f"Master index: {master_path}")
    logger.info(f"Results directory: {results_dir}")
    logger.info(f"{'='*60}")

    return master_path


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Teage Liu Performance Benchmark")
    parser.add_argument(
        "--scenario", choices=["all", "a", "b", "c", "d", "e"],
        default="all", help="Scenario to run (default: all)"
    )
    parser.add_argument("--profile", action="store_true", help="Enable cProfiling")
    args = parser.parse_args()

    # Apply patches
    _patch_llm_client()
    _patch_sqlite_logger_with_lock_profiler()

    if args.scenario == "all":
        run_all_scenarios(profile_cpu=args.profile)
    else:
        logger.info("Single-scenario mode not yet implemented, running all.")
        run_all_scenarios(profile_cpu=args.profile)


if __name__ == "__main__":
    main()
