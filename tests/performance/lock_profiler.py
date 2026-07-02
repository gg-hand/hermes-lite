"""Lock contention profiler.

Wraps threading.Lock with nanosecond-resolution acquire timing to identify
lock contention hotspots under concurrent load.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional


class ProfiledLock:
    """A threading.Lock wrapper that profiles acquire wait times.

    Usage:
        lock = ProfiledLock("SessionLogger._lock")
        with lock:
            # critical section
        print(lock.stats())
    """

    def __init__(self, name: str, delegate: Optional[threading.Lock] = None):
        self._name = name
        self._lock = delegate or threading.Lock()
        self._wait_total_ns: int = 0
        self._hold_total_ns: int = 0
        self._acquire_count: int = 0
        self._contention_count: int = 0
        self._max_wait_ns: int = 0
        self._max_hold_ns: int = 0
        self._owning_thread: Optional[int] = None
        self._lock_internal = threading.Lock()  # protects stats only

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        t0 = time.perf_counter_ns()
        acquired = self._lock.acquire(blocking=blocking, timeout=timeout if timeout >= 0 else None)
        wait_ns = time.perf_counter_ns() - t0

        if acquired:
            with self._lock_internal:
                self._acquire_count += 1
                self._wait_total_ns += wait_ns
                if wait_ns > self._max_wait_ns:
                    self._max_wait_ns = wait_ns
                if wait_ns > 1_000_000:  # > 1ms is contention
                    self._contention_count += 1
                self._hold_start_ns = time.perf_counter_ns()
                self._owning_thread = threading.get_ident()

        return acquired

    def release(self) -> None:
        hold_ns = time.perf_counter_ns() - self._hold_start_ns
        with self._lock_internal:
            self._hold_total_ns += hold_ns
            if hold_ns > self._max_hold_ns:
                self._max_hold_ns = hold_ns
            self._owning_thread = None
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def locked(self) -> bool:
        return self._lock.locked()

    def stats(self) -> Dict[str, object]:
        with self._lock_internal:
            return {
                "name": self._name,
                "acquire_count": self._acquire_count,
                "contention_count": self._contention_count,
                "total_wait_ms": self._wait_total_ns / 1_000_000,
                "avg_wait_us": (self._wait_total_ns / max(1, self._acquire_count)) / 1_000,
                "max_wait_ms": self._max_wait_ns / 1_000_000,
                "total_hold_ms": self._hold_total_ns / 1_000_000,
                "avg_hold_us": (self._hold_total_ns / max(1, self._acquire_count)) / 1_000,
                "max_hold_ms": self._max_hold_ns / 1_000_000,
            }

    def reset(self) -> None:
        with self._lock_internal:
            self._wait_total_ns = 0
            self._hold_total_ns = 0
            self._acquire_count = 0
            self._contention_count = 0
            self._max_wait_ns = 0
            self._max_hold_ns = 0


class LockProfilerRegistry:
    """Registry of all profiled locks for aggregated reporting."""

    def __init__(self):
        self._locks: Dict[str, ProfiledLock] = {}

    def get_or_create(self, name: str) -> ProfiledLock:
        if name not in self._locks:
            self._locks[name] = ProfiledLock(name)
        return self._locks[name]

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        return {name: lock.stats() for name, lock in self._locks.items()}

    def reset_all(self) -> None:
        for lock in self._locks.values():
            lock.reset()

    def report_markdown(self) -> str:
        """Generate a markdown table of lock contention stats."""
        lines = [
            "| Lock | Acquires | Contentions | Avg Wait (µs) | Max Wait (ms) | Total Wait (ms) | Avg Hold (µs) | Max Hold (ms) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name, lock in sorted(self._locks.items()):
            s = lock.stats()
            lines.append(
                f"| {s['name']} | {s['acquire_count']} | {s['contention_count']} | "
                f"{s['avg_wait_us']:.1f} | {s['max_wait_ms']:.2f} | "
                f"{s['total_wait_ms']:.1f} | {s['avg_hold_us']:.1f} | {s['max_hold_ms']:.2f} |"
            )
        return "\n".join(lines)


# Global registry instance
_registry = LockProfilerRegistry()


def get_lock(name: str) -> ProfiledLock:
    """Get or create a profiled lock by name."""
    return _registry.get_or_create(name)


def get_registry() -> LockProfilerRegistry:
    return _registry
