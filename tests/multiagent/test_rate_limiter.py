"""限流器测试。"""
import time
import pytest

from hermes.multiagent.rate_limiter import RateLimiter


class TestRateLimiter:
    """限流器测试。"""

    def test_under_limit_allowed(self):
        """低于阈值允许。"""
        limiter = RateLimiter(rate_per_second=10)
        for _ in range(10):
            assert limiter.check("127.0.0.1") is True

    def test_over_limit_rejected(self):
        """超过阈值拒绝。"""
        limiter = RateLimiter(rate_per_second=2)
        assert limiter.check("127.0.0.1") is True
        assert limiter.check("127.0.0.1") is True
        assert limiter.check("127.0.0.1") is False

    def test_different_ips_independent(self):
        """不同 IP 独立计数。"""
        limiter = RateLimiter(rate_per_second=2)
        assert limiter.check("127.0.0.1") is True
        assert limiter.check("127.0.0.1") is True
        # 不同 IP 重新计数
        assert limiter.check("127.0.0.2") is True

    def test_window_resets_after_one_second(self):
        """1 秒后窗口重置。"""
        limiter = RateLimiter(rate_per_second=2)
        limiter.check("127.0.0.1")
        limiter.check("127.0.0.1")
        assert limiter.check("127.0.0.1") is False
        # 等待窗口重置（测试中用 _advance_time 模拟）
        limiter._advance_time("127.0.0.1", 1.1)
        assert limiter.check("127.0.0.1") is True

    def test_concurrent_thread_safety(self):
        """并发安全。"""
        import threading
        limiter = RateLimiter(rate_per_second=100)
        results = []
        lock = threading.Lock()

        def worker():
            r = limiter.check("127.0.0.1")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 100 个 True，100 个 False
        assert results.count(True) == 100
        assert results.count(False) == 100
