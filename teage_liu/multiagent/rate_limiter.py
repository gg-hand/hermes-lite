"""滑动窗口限流器：基于 per-IP 计数 + 线程安全。

特性：
- 每 IP 独立计数
- 滑动窗口（1 秒）
- 线程安全（threading.Lock）
- 支持时间模拟（测试用）
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """滑动窗口限流器。"""

    def __init__(self, rate_per_second: int = 100):
        self._rate = rate_per_second
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """检查是否允许请求。

        Args:
            key: 限流键（通常为客户端 IP）。

        Returns:
            True 允许，False 拒绝。
        """
        now = time.monotonic()
        with self._lock:
            window = self._windows[key]
            # 清理过期记录（1 秒前）
            while window and window[0] < now - 1.0:
                window.popleft()
            if len(window) >= self._rate:
                return False
            window.append(now)
            return True

    def _advance_time(self, key: str, seconds: float) -> None:
        """测试用：模拟时间推进。"""
        with self._lock:
            window = self._windows[key]
            for i in range(len(window)):
                window[i] -= seconds

    def reset(self, key: str | None = None) -> None:
        """重置限流状态。"""
        with self._lock:
            if key is None:
                self._windows.clear()
            else:
                self._windows.pop(key, None)
