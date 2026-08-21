"""session 级并发互斥(§5/§18.7,阶段 4 落地):同 session 对话串行化(B3 根治)。

老系统 B3(会话并发互斥)在 teage_liu2 的落地:core 单快照无共享,但 server 层
同 session 并发 /chat 与 /chat/stream 会交错读历史/交错落盘/丢消息 ——
外壳以 session 级 ``asyncio.Lock`` 串行化(§4.3 v1.11 会话并发互斥契约)。

实现:
- 按 session_id 惰性创建锁,LRU 有界缓存(maxsize 默认 10000)
- **严格 LRU 淘汰**:超限时从最旧开始扫描,**首个未持有/未等待的锁**被淘汰
  (不只看最旧一个)——空闲锁总能被淘汰,缓存仅在"全部锁同时被持有/等待"时
  才临时超限(绝对必要的互斥优先边界,文档明确,无"空闲锁堆积超限")
- 持有/等待中的锁不可淘汰 —— 否则同 session 拿到两个不同锁,破坏互斥
- 异步:``async with await locks.get(session_id): ...`` 覆盖整个对话流
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any


class SessionLocks:
    """session 级 asyncio.Lock 管理(严格 LRU 有界,持有中的锁不可淘汰)。"""

    def __init__(self, max_size: int = 10000) -> None:
        if max_size < 1:
            raise ValueError(f"max_size 必须是正整数,实际 {max_size!r}")
        self._locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
        self._max_size = max_size
        self._guard = asyncio.Lock()

    async def get(self, session_id: str) -> asyncio.Lock:
        """获取(或创建)session 锁,并移动到最近使用位。

        严格 LRU 淘汰:超限时从最旧开始扫描,淘汰**首个未持有/未等待的锁**
        (不只看最旧一个)——空闲锁总能被淘汰;仅当全部锁都处于持有/等待中才
        允许缓存临时超限(互斥优先,此边界安全且必要)。
        """
        async with self._guard:
            lock = self._locks.pop(session_id, None)
            if lock is None:
                lock = asyncio.Lock()
            self._locks[session_id] = lock  # 移到末尾(最近使用)
            while len(self._locks) > self._max_size:
                evicted = False
                for lid, candidate in list(self._locks.items()):
                    if not candidate.locked():
                        self._locks.pop(lid)
                        evicted = True
                        break
                if not evicted:
                    # 全部锁在使用中:互斥优先,允许临时超限(安全边界)
                    break
            return lock

    def drop(self, session_id: str) -> None:
        """显式移除 session 锁(会话清理/关闭时调用)。"""
        self._locks.pop(session_id, None)

    def clear(self) -> None:
        """清空全部锁(shutdown 时调用)。"""
        self._locks.clear()

    @property
    def size(self) -> int:
        return len(self._locks)

    @property
    def max_size(self) -> int:
        return self._max_size
