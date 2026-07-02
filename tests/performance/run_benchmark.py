#!/usr/bin/env python3
"""Hermes Lite Performance Benchmark Suite — Consolidated Runner.

Measures end-to-end latency, subsystem overhead, memory, GC, and lock contention
across 5 benchmark scenarios. Uses a MockBackend to eliminate LLM API variability.

Usage:
    cd hermes-lite
    python tests/performance/run_benchmark.py [--profile] [--quick]
"""

from __future__ import annotations

import cProfile
import gc
import json
import logging
import os
import pstats
import shutil
import statistics
import sys
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

logging.basicConfig(
    level=logging.WARNING,  # Suppress internal logs for cleaner output
    format="%(levelname)s %(message)s",
)
logger = logging.getLogger("benchmark")
logger.setLevel(logging.INFO)

# Add console handler with our format
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(console)

SRC_DIR = str(Path(__file__).resolve().parent.parent.parent / "src")
PROJ_DIR = str(Path(__file__).resolve().parent.parent.parent)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if PROJ_DIR not in sys.path:
    sys.path.insert(0, PROJ_DIR)

# ---- Globals ---------------------------------------------------------
RESULTS_DIR = Path(__file__).resolve().parent / "results"
os.makedirs(RESULTS_DIR, exist_ok=True)

# ---- Patch LLMClient -------------------------------------------------
# Need to add test module path for MockBackend
_TEST_DIR = str(Path(__file__).resolve().parent.parent)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from llm import client as llm_client_module
from performance.mock_backend import MockBackend, LLMResponse

_original_create_backend = llm_client_module._create_backend


def _mock_create_backend(provider: str, model: str, api_key: str, base_url: str = None):
    """Inject MockBackend for 'mock' provider."""
    if provider.strip().lower() == "mock":
        return MockBackend(model=model, api_key=api_key or "mock", base_url=base_url)
    return _original_create_backend(provider, model, api_key, base_url)


llm_client_module._create_backend = _mock_create_backend


def _patched_llm_init(self, config_path=None, config=None, metrics_collector=None):
    """Bypass API key validation for mock provider."""
    if config is None:
        from config import load_config
        config = load_config(config_path)
    llm_cfg = config.get("llm", {})
    self._metrics_collector = metrics_collector
    self.main_provider = llm_cfg.get("main_provider", "mock")
    self.main_model = llm_cfg.get("main_model", "mock")
    self.consolidation_provider = llm_cfg.get("consolidation_provider", "mock")
    self.consolidation_model = llm_cfg.get("consolidation_model", "mock")
    self.max_context_tokens = int(llm_cfg.get("max_context_tokens", 200000))
    self.context_threshold = float(llm_cfg.get("context_threshold", 0.8))
    self._main_backend = llm_client_module._create_backend(
        self.main_provider, self.main_model,
        llm_cfg.get("main_api_key"), llm_cfg.get("main_base_url"),
    )
    self._consolidation_backend = llm_client_module._create_backend(
        self.consolidation_provider, self.consolidation_model,
        llm_cfg.get("consolidation_api_key"), llm_cfg.get("consolidation_base_url"),
    )


llm_client_module.LLMClient.__init__ = _patched_llm_init


def _patch_load_config():
    """Make load_config return our test configuration."""
    import config as config_module

    _test_config = None

    def _make_test_config(data_dir: str, max_loops: int = 1, consolidation_threshold: int = 999):
        nonlocal _test_config
        _test_config = {
            "llm": {
                "main_provider": "mock", "main_model": "mock-main",
                "main_api_key": "mock-key",
                "consolidation_provider": "mock", "consolidation_model": "mock-consolidation",
                "consolidation_api_key": "mock-key",
                "max_context_tokens": 200000, "context_threshold": 0.8,
            },
            "memory": {
                "chroma_path": os.path.join(data_dir, "chroma"),
                "memory_md_path": os.path.join(data_dir, "memory.md"),
                "consolidation_threshold": consolidation_threshold,
                "dedup_similarity_threshold": 0.85,
                "retrieval_top_k": 5,
                "surprise_gate_enabled": True,
                "surprise_similarity_threshold": 0.85,
                "surprise_skip_threshold": 0.92,
                "decay_rate": 0.01, "frequency_weight": 0.5,
                "history_max_turns": 50,
                "archive_turns_on_evict": True,
                "condenser": {
                    "enabled": True, "strategy": "masking",
                    "keep_recent_n": 6, "keep_first": 2,
                    "llm_summary_threshold": 100000,
                },
            },
            "server": {"host": "0.0.0.0", "port": 8000},
            "storage": {
                "sqlite_path": os.path.join(data_dir, "sessions.db"),
                "session_ttl_days": 30, "cleanup_interval_hours": 24,
            },
            "history": {"persistence_dir": os.path.join(data_dir, "history")},
            "tools": {"max_react_loops": max_loops, "defer_loading_threshold": 20},
            "monitoring": {"enabled": True},
            "skills": {"hermes": [], "mcp": []},
            "security": {"enabled": True, "approval_timeout_seconds": 300, "rules": []},
            "tasks": {}, "schedules": [],
        }
        return _test_config

    def _patched_load_config(path):
        nonlocal _test_config
        if _test_config is not None:
            return _test_config
        return config_module.load_config(path)

    config_module.load_config = _patched_load_config
    return _make_test_config


make_test_config = _patch_load_config()


# ---- Session Profiling Helpers ---------------------------------------

class BenchmarkSession:
    """Collects latency, system metrics, CPU profile, and memory traces."""

    def __init__(self, name: str):
        self.name = name
        self.dir = RESULTS_DIR / name
        self.dir.mkdir(parents=True, exist_ok=True)

        self.latencies: List[float] = []
        self.phase_times: Dict[str, List[float]] = {}
        self.process = psutil.Process()
        self.cpu_samples: List[float] = []
        self.mem_samples: List[Tuple[float, int]] = []
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._profiler: Optional[cProfile.Profile] = None
        self._trace_snapshots = []

    def start_monitor(self, interval: float = 0.5):
        self._monitoring = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(interval,), daemon=True
        )
        self._monitor_thread.start()

    def stop_monitor(self):
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

    def start_profile(self):
        self._profiler = cProfile.Profile()
        self._profiler.enable()

    def stop_profile(self):
        if self._profiler:
            self._profiler.disable()
            path = self.dir / "cprofile.out"
            self._profiler.dump_stats(str(path))
            txt_path = self.dir / "cprofile_top.txt"
            with open(txt_path, "w") as f:
                p = pstats.Stats(self._profiler, stream=f)
                p.sort_stats("cumtime")
                p.print_stats(40)
                f.write("\n\n=== By tottime ===\n")
                p.sort_stats("tottime")
                p.print_stats(40)
            logger.info(f"    -> Profile: {path}")

    def start_tracemalloc(self):
        tracemalloc.start(25)

    def trace_snapshot(self):
        if tracemalloc.is_tracing():
            self._trace_snapshots.append(tracemalloc.take_snapshot())

    def stop_tracemalloc(self) -> Optional[Dict]:
        if not tracemalloc.is_tracing() or len(self._trace_snapshots) < 2:
            tracemalloc.stop()
            return None
        snap = tracemalloc.take_snapshot()
        stats = snap.compare_to(self._trace_snapshots[0], "lineno")
        top_leaks = [
            {
                "traceback": str(s.traceback),
                "size_diff_bytes": s.size_diff,
                "count_diff": s.count_diff,
            }
            for s in stats[:20]
        ]
        tracemalloc.stop()
        return {"top_leaks": top_leaks}

    def log_latency(self, ms: float):
        self.latencies.append(ms)

    def log_phase(self, phase: str, ms: float):
        self.phase_times.setdefault(phase, []).append(ms)

    @staticmethod
    def stats(vals: List[float]) -> Dict[str, float]:
        if not vals:
            return {"min": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0, "n": 0}
        s = sorted(vals)
        n = len(s)
        return {
            "min": round(s[0], 3),
            "p50": round(s[int(n * 0.50)], 3),
            "p95": round(s[int(n * 0.95)], 3),
            "p99": round(s[int(n * 0.99)], 3),
            "max": round(s[-1], 3),
            "mean": round(sum(s) / n, 3),
            "stdev": round(statistics.stdev(s) if n > 1 else 0, 3),
            "n": n,
        }

    def save(self, extra: Optional[Dict] = None) -> Path:
        gc.collect()
        gc_stats = gc.get_stats()
        report = {
            "scenario": self.name,
            "timestamp": datetime.now().isoformat(),
            "latency_ms": self.stats(self.latencies),
            "phase_times": {p: self.stats(v) for p, v in self.phase_times.items()},
            "system": {
                "cpu_mean": statistics.mean(self.cpu_samples) if self.cpu_samples else 0,
                "cpu_max": max(self.cpu_samples) if self.cpu_samples else 0,
                "mem_peak_mb": (
                    max(m[1] for m in self.mem_samples) / (1024 * 1024)
                    if self.mem_samples else 0
                ),
                "mem_final_mb": (
                    self.mem_samples[-1][1] / (1024 * 1024) if self.mem_samples else 0
                ),
            },
            "gc": {
                "collected": sum(
                    g["collected"] for g in gc_stats
                ),
            },
            "process_rss_mb": self.process.memory_info().rss / (1024 * 1024),
        }
        trace = self.stop_tracemalloc()
        if trace:
            report["memory_leaks"] = trace
        if extra:
            report.update(extra)

        path = self.dir / "report.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info(f"  Report: {path}")
        return path


# ---- Seed Data ---------------------------------------------------------

def seed_test_data(data_dir: str):
    """Pre-seed test data stores for consistent baselines."""
    from storage.chroma_store import ChromaMemoryStore
    from storage.sqlite_log import SessionLogger

    logger.info("Seeding test data...")

    # ChromaDB
    chroma_path = os.path.join(data_dir, "chroma")
    if os.path.exists(chroma_path):
        shutil.rmtree(chroma_path)
    os.makedirs(chroma_path, exist_ok=True)
    store = ChromaMemoryStore(persist_path=chroma_path)
    topics = ["programming", "python", "data science", "ml", "web", "database"]
    batch_docs, batch_ids, batch_metadatas = [], [], []
    for i in range(200):
        topic = topics[i % len(topics)]
        batch_docs.append(f"Test memory {i} about {topic}.")
        batch_ids.append(f"mem_{i}")
        batch_metadatas.append({
            "importance": round((i % 10) / 10, 2),
            "type": "fact",
            "namespace": "user",
        })
    store.collection.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metadatas)
    logger.info(f"  ChromaDB: {store.collection.count()} vectors")

    # SQLite
    db_path = os.path.join(data_dir, "sessions.db")
    if os.path.exists(db_path):
        os.unlink(db_path)
    sl = SessionLogger(str(db_path))
    for sid in range(3):
        session_id = f"seed_session_{sid}"
        sl.create_session(session_id)
        for mid in range(10):
            role = "user" if mid % 2 == 0 else "assistant"
            sl.log_message(session_id, role, f"Seed message {mid} in session {sid}")
    logger.info(f"  SQLite: 3 sessions, 30 messages")

    # memory.md
    md_path = os.path.join(data_dir, "memory.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Profile\n\nUser is a software engineer.\n")
    logger.info(f"  Memory.md: created")

    # History
    hist_dir = os.path.join(data_dir, "history")
    os.makedirs(hist_dir, exist_ok=True)
    for sid in range(2):
        with open(os.path.join(hist_dir, f"seed_session_{sid}.jsonl"), "w") as f:
            for mid in range(20):
                entry = {
                    "role": "user" if mid % 2 == 0 else "assistant",
                    "content": f"History message {mid}",
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info(f"  History: 2 sessions, 40 messages")


# ---- Benchmark Scenarios ------------------------------------------------

def run_scenario_a(orc, data_dir: str, n: int = 50):
    """Simple Q&A — baseline latency with no tools."""
    logger.info(f"\n{'='*60}")
    logger.info("Scenario A: Simple Q&A")
    logger.info(f"  {n} iterations, mock latency=50ms, max_loops=1")
    logger.info(f"{'='*60}")

    sess = BenchmarkSession("scenario_a")
    sess.start_tracemalloc()
    sess.trace_snapshot()

    mb = orc.llm_client._main_backend
    mb.scenario = "simple_qa"
    mb.simulated_latency_ms = 50

    for i in range(n):
        t0 = time.perf_counter()
        orc.chat(f"scenario_a_{i}", f"测试消息第{i}条。")
        sess.log_latency((time.perf_counter() - t0) * 1000)

    sess.trace_snapshot()

    # Phase breakdown: measure sub-components
    # Context building
    t0 = time.perf_counter()
    orc._build_enhanced_context("breakdown_test", "test", [])
    sess.log_phase("context_build_ms", (time.perf_counter() - t0) * 1000)

    # SQLite log (user + assistant = 2 messages)
    t0 = time.perf_counter()
    orc.session_logger.log_message("breakdown_test", "user", "test message")
    orc.session_logger.log_message("breakdown_test", "assistant", "response")
    sess.log_phase("sqlite_log_2msg_ms", (time.perf_counter() - t0) * 1000)

    logger.info(f"  Latency: {sess.stats(sess.latencies)}")
    sess.save()
    return sess


def run_scenario_b(orc, data_dir: str, n_sessions: int = 15):
    """Tool-intensive — stress policy engine, tool exec, audit."""
    logger.info(f"\n{'='*60}")
    logger.info("Scenario B: Tool-intensive")
    logger.info(f"  {n_sessions} sessions, mock latency=50ms")
    logger.info(f"{'='*60}")

    sess = BenchmarkSession("scenario_b")
    sess.start_monitor(interval=0.3)

    mb = orc.llm_client._main_backend
    mb.scenario = "tool_intensive"
    mb.simulated_latency_ms = 50
    mb.call_count = 0

    for i in range(n_sessions):
        mb.call_count = 0
        mb._response_index = 0
        mb._responses.clear()

        t0 = time.perf_counter()
        orc.chat(
            f"scenario_b_{i}",
            "读取 /tmp/test.txt，写入备份，然后执行 ls 命令。",
        )
        sess.log_latency((time.perf_counter() - t0) * 1000)

        if (i + 1) % 5 == 0:
            logger.info(f"  Progress: {i+1}/{n_sessions}")

    sess.stop_monitor()
    logger.info(f"  Latency: {sess.stats(sess.latencies)}")
    sess.save()
    return sess


def run_scenario_c(orc, data_dir: str, n_sessions: int = 2):
    """Memory-intensive — long history + consolidation."""
    logger.info(f"\n{'='*60}")
    logger.info("Scenario C: Memory-intensive (consolidation + ChromaDB)")
    logger.info(f"  {n_sessions} sessions, threshold=3")
    logger.info(f"{'='*60}")

    sess = BenchmarkSession("scenario_c")
    sess.start_monitor(interval=0.5)
    sess.start_tracemalloc()
    sess.trace_snapshot()

    mb = orc.llm_client._main_backend
    mb.scenario = "consolidation"
    mb.simulated_latency_ms = 50

    cb = orc.llm_client._consolidation_backend
    cb.scenario = "consolidation"
    cb.simulated_latency_ms = 100  # consolidation model is slower

    # Lower consolidation threshold to trigger frequently
    if orc.consolidation_engine:
        orc.consolidation_engine.info_counter = 100  # Near threshold

    for sid in range(n_sessions):
        session_id = f"scenario_c_{sid}"

        # Pre-fill history buffer
        if orc.history_buffer:
            for h in range(40):
                orc.history_buffer.add_message(
                    session_id,
                    "user" if h % 2 == 0 else "assistant",
                    f"Pre-fill message {h}",
                )

        for turn in range(30):
            t0 = time.perf_counter()
            orc.chat(session_id, f"第{turn}轮：记忆测试消息。")
            sess.log_latency((time.perf_counter() - t0) * 1000)

        sess.trace_snapshot()
        logger.info(f"  Session {sid+1}/{n_sessions} done")

    sess.stop_monitor()
    logger.info(f"  Latency: {sess.stats(sess.latencies)}")

    # Measure consolidation overhead specifically
    if orc.consolidation_engine:
        t0 = time.perf_counter()
        orc.consolidation_engine.should_consolidate()
        sess.log_phase("consolidation_check_ms", (time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        try:
            orc.consolidation_engine.consolidate()
        except Exception:
            pass
        sess.log_phase("consolidation_run_ms", (time.perf_counter() - t0) * 1000)

    # ChromaDB query overhead
    if orc.chroma_store:
        t0 = time.perf_counter()
        orc.chroma_store.query_memory("test query", top_k=5)
        sess.log_phase("chromadb_query_5_ms", (time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        try:
            orc.chroma_store.find_duplicates("test fact", threshold=0.85)
        except Exception:
            pass
        sess.log_phase("chromadb_find_dups_ms", (time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        try:
            orc.chroma_store.reinforce("mem_0")
        except Exception:
            pass
        sess.log_phase("chromadb_reinforce_ms", (time.perf_counter() - t0) * 1000)

    sess.save()
    return sess


def run_scenario_d(data_dir: str, n_users: int = 8, n_requests: int = 5):
    """Multi-user concurrent load."""
    import concurrent.futures

    logger.info(f"\n{'='*60}")
    logger.info("Scenario D: Multi-user concurrent load")
    logger.info(f"  {n_users} concurrent users, {n_requests} requests each")
    logger.info(f"{'='*60}")

    sess = BenchmarkSession("scenario_d")
    sess.start_monitor(interval=0.2)

    # Create one orchestrator per user (to measure lock contention)
    def _make_orc():
        orc = _create_orchestrator(data_dir, "simple_qa", max_loops=1)
        orc.llm_client._main_backend.simulated_latency_ms = 20
        orc.llm_client._main_backend.scenario = "simple_qa"
        return orc

    def _user_loop(uid: int):
        orc = _make_orc()
        local_lat = []
        for i in range(n_requests):
            t0 = time.perf_counter()
            orc.chat(f"scenario_d_u{uid}_{i}", f"并发测试第{i}条消息。")
            local_lat.append((time.perf_counter() - t0) * 1000)
        return local_lat

    t_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_users) as ex:
        futures = [ex.submit(_user_loop, uid) for uid in range(n_users)]
        for f in concurrent.futures.as_completed(futures):
            for lat in f.result():
                sess.log_latency(lat)

    elapsed = time.perf_counter() - t_start
    total_reqs = n_users * n_requests
    throughput = total_reqs / elapsed

    sess.stop_monitor()
    logger.info(f"  Latency: {sess.stats(sess.latencies)}")
    logger.info(f"  Throughput: {throughput:.1f} req/s ({total_reqs} in {elapsed:.1f}s)")
    sess.save({"throughput_req_per_sec": round(throughput, 2), "concurrent_users": n_users})
    return sess


def run_scenario_e(data_dir: str, n: int = 3):
    """Cold-start analysis."""
    logger.info(f"\n{'='*60}")
    logger.info("Scenario E: Cold start")
    logger.info(f"  {n} cold starts + warm comparison")
    logger.info(f"{'='*60}")

    sess = BenchmarkSession("scenario_e")

    for i in range(n):
        # Clean ChromaDB for fresh start (close any open connections first)
        chroma_dir = os.path.join(data_dir, "chroma")
        if os.path.exists(chroma_dir):
            # On Windows, ChromaDB keeps SQLite lock; use retry
            for retry in range(3):
                try:
                    shutil.rmtree(chroma_dir)
                    break
                except PermissionError:
                    time.sleep(0.5)
                    # Try to force-close by deleting individual files
                    for f in os.listdir(chroma_dir):
                        try:
                            os.unlink(os.path.join(chroma_dir, f))
                        except Exception:
                            pass

        t0 = time.perf_counter()
        orc = _create_orchestrator(data_dir, "simple_qa", max_loops=1)
        init_ms = (time.perf_counter() - t0) * 1000
        sess.log_phase("orchestrator_init_ms", init_ms)

        t0 = time.perf_counter()
        orc.llm_client._main_backend.simulated_latency_ms = 0
        orc.chat(f"scenario_e_cold_{i}", "冷启动测试。")
        chat_ms = (time.perf_counter() - t0) * 1000
        sess.log_phase("first_chat_ms", chat_ms)

        sess.log_latency(init_ms + chat_ms)
        logger.info(f"  Cold start {i+1}: init={init_ms:.1f}ms, first_chat={chat_ms:.1f}ms")

    # Warm measurement
    orc = _create_orchestrator(data_dir, "simple_qa", max_loops=1)
    t0 = time.perf_counter()
    _ = orc.llm_client._main_backend
    _.simulated_latency_ms = 0
    _.scenario = "simple_qa"
    orc.chat("scenario_e_warm", "热启动测试。")
    warm_ms = (time.perf_counter() - t0) * 1000
    sess.log_phase("warm_first_chat_ms", warm_ms)
    logger.info(f"  Warm first chat: {warm_ms:.1f}ms")

    sess.save()
    return sess


def _create_orchestrator(data_dir: str, scenario: str, max_loops: int = 1, consolidation_threshold: int = 9999):
    """Factory: create Orchestrator with test config."""
    from orchestrator import Orchestrator

    # Update global test config with given threshold
    make_test_config(data_dir, max_loops=max_loops, consolidation_threshold=consolidation_threshold)
    return Orchestrator(config_path="ignored.yaml")


# ---- Main ---------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hermes Lite Performance Benchmark")
    parser.add_argument("--quick", action="store_true", help="Fewer iterations for faster run")
    parser.add_argument("--profile", action="store_true", help="Enable cProfile")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Hermes Lite — Performance Benchmark Suite")
    logger.info("=" * 60)

    data_dir = "data/test_profiling"
    os.makedirs(data_dir, exist_ok=True)
    seed_test_data(data_dir)

    orc = _create_orchestrator(data_dir, "simple_qa", max_loops=1)

    # Warmup
    mb = orc.llm_client._main_backend
    mb.simulated_latency_ms = 0
    mb.scenario = "simple_qa"
    orc.chat("warmup", "预热消息。")
    logger.info("Warmup complete\n")

    results = {}

    # --- SCENARIO A ---
    s = run_scenario_a(orc, data_dir, n=10 if args.quick else 50)
    results["scenario_a"] = str(s.save())

    # --- SCENARIO B ---
    # Need to recreate orchestrator with max_react_loops=5
    orc_b = _create_orchestrator(data_dir, "tool_intensive", max_loops=5)
    mb_b = orc_b.llm_client._main_backend
    mb_b.simulated_latency_ms = 50
    mb_b.scenario = "tool_intensive"
    s = run_scenario_b(orc_b, data_dir, n_sessions=5 if args.quick else 15)
    results["scenario_b"] = str(s.save())

    # --- SCENARIO C ---
    # Recreate orchestrator with low consolidation threshold
    orc_c = _create_orchestrator(data_dir, "consolidation", max_loops=3, consolidation_threshold=2)
    mb_c = orc_c.llm_client._main_backend
    mb_c.simulated_latency_ms = 50
    mb_c.scenario = "consolidation"
    cb_c = orc_c.llm_client._consolidation_backend
    cb_c.simulated_latency_ms = 100
    cb_c.scenario = "consolidation"  # Returns proper JSON with facts
    s = run_scenario_c(orc_c, data_dir, n_sessions=1 if args.quick else 2)
    results["scenario_c"] = str(s.save())

    # --- SCENARIO D ---
    s = run_scenario_d(data_dir, n_users=4 if args.quick else 8, n_requests=3 if args.quick else 5)
    results["scenario_d"] = str(s.save())

    # --- SCENARIO E ---
    s = run_scenario_e(data_dir, n=2 if args.quick else 3)
    results["scenario_e"] = str(s.save())

    # Master index
    master = RESULTS_DIR / "master_index.json"
    with open(master, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "python": sys.version,
            "platform": sys.platform,
            "results": results,
        }, f, indent=2)

    logger.info(f"\n{'='*60}")
    logger.info("All scenarios complete!")
    logger.info(f"Results: {RESULTS_DIR}")
    logger.info(f"Master:  {master}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
