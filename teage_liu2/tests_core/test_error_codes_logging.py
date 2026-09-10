"""错误码"日志面"契约（2026-09-10 深度审计 F-3）。

errors 域 18 码此前几乎全是"文档态"：只有 2 个以日志前缀形式出现。
本文件锁定 hooks 层的两码必须以 `CODE: message` 前缀出现在日志中，
供行为套件用 logging 捕获做协议级断言。
"""
from __future__ import annotations

import asyncio
import logging

from teage_liu2.core.errors import HOOK_EXCEPTION, HOOK_TIMEOUT
from teage_liu2.core.hooks import Branch, HookChain


class _Boom(Branch):
    name = "boom"

    async def before(self, snapshot):  # noqa: ANN001 - 契约测试桩
        raise RuntimeError("脚本化异常")


class _Slow(Branch):
    name = "slow"

    async def before(self, snapshot):  # noqa: ANN001 - 契约测试桩
        await asyncio.sleep(0.05)
        return []


def test_hook_exception_logs_code(caplog):
    chain = HookChain()
    boom = _Boom()
    chain.register(boom)
    with caplog.at_level(logging.ERROR, logger="teage_liu2.core.hooks"):
        asyncio.run(chain._call(boom, "before", None))
    assert HOOK_EXCEPTION in caplog.text


def test_hook_timeout_logs_code(caplog):
    chain = HookChain(hook_timeout=0.01)
    slow = _Slow()
    chain.register(slow)
    with caplog.at_level(logging.ERROR, logger="teage_liu2.core.hooks"):
        asyncio.run(chain._call(slow, "before", None))
    assert HOOK_TIMEOUT in caplog.text
