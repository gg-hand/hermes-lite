"""钩子链契约测试(阶段 2 协议化):顺序 / 隔离 / 超时 / 异常跳过 / 拦截 / 注入 / 工具派发。

新协议签名:钩子收到不可变 Snapshot,通过返回 Action 变更对话状态;
before_all 返回 (最新快照, stop_reason or None)。
"""

from __future__ import annotations

import asyncio

import pytest

from teage_liu2.core.actions import SetExtra, SetStop
from teage_liu2.core.hooks import Branch, HookChain, no_executor
from teage_liu2.core.injection import L_PREFIX, Injection
from teage_liu2.core.types import Snapshot


def _snapshot(user_input: str = "hi") -> Snapshot:
    return Snapshot(session_id="s", user_input=user_input)


# ---------------------------------------------------------------------------
# 顺序契约
# ---------------------------------------------------------------------------
class RecordBranch(Branch):
    """记录调用序列的枝干。"""

    name = "record"

    def __init__(self, log, tag):
        self.log = log
        self.tag = tag

    async def setup(self, config, core):
        self.log.append(f"{self.tag}.setup")

    async def teardown(self):
        self.log.append(f"{self.tag}.teardown")

    async def build_injections(self, snapshot):
        self.log.append(f"{self.tag}.build_injections")
        return [Injection(layer=L_PREFIX, content=f"注入-{self.tag}")]

    async def before(self, snapshot):
        self.log.append(f"{self.tag}.before")
        return []

    async def after(self, snapshot, response):
        self.log.append(f"{self.tag}.after")
        return []


def test_hooks_order_forward_before_reverse_after():
    """验收:before 正序(注册序),after 逆序(洋葱),teardown 逆序。"""
    log = []
    chain = HookChain()
    chain.register(RecordBranch(log, "a"))
    chain.register(RecordBranch(log, "b"))

    snapshot = _snapshot()
    asyncio.run(chain.build_injections_all(snapshot))
    asyncio.run(chain.before_all(snapshot))
    asyncio.run(chain.after_all(snapshot, None))
    asyncio.run(chain.teardown_all())

    build_injections_calls = [x for x in log if x.endswith("build_injections")]
    assert build_injections_calls == ["a.build_injections", "b.build_injections"]
    before_calls = [x for x in log if x.endswith("before")]
    assert before_calls == ["a.before", "b.before"]
    after_calls = [x for x in log if x.endswith("after")]
    assert after_calls == ["b.after", "a.after"]
    teardown_calls = [x for x in log if x.endswith("teardown")]
    assert teardown_calls == ["b.teardown", "a.teardown"]


def test_build_injections_collect():
    """验收:build_injections 汇总全部枝干的注入声明(注册序)。"""
    chain = HookChain()
    chain.register(RecordBranch([], "a"))
    chain.register(RecordBranch([], "b"))

    snapshot = _snapshot()
    items = asyncio.run(chain.build_injections_all(snapshot))

    assert [i.content for i in items] == ["注入-a", "注入-b"]
    assert all(i.layer == L_PREFIX for i in items)


# ---------------------------------------------------------------------------
# 异常隔离与超时
# ---------------------------------------------------------------------------
class FailingBranch(Branch):
    """before 抛异常 / 超时的枝干,不应影响对话。"""

    name = "failing"

    def __init__(self, mode="raise"):
        self.mode = mode

    async def before(self, snapshot):
        if self.mode == "raise":
            raise RuntimeError("枝干故障")
        if self.mode == "timeout":
            await asyncio.sleep(10)
        return []


def test_hook_exception_skips_branch_only():
    """验收:枝干运行时异常 → 跳过该枝干,其余枝干正常执行。"""
    log = []
    chain = HookChain()
    chain.register(RecordBranch(log, "a"))
    chain.register(FailingBranch(mode="raise"))
    chain.register(RecordBranch(log, "c"))

    snapshot, _ = asyncio.run(chain.before_all(_snapshot()))
    assert not snapshot.stop  # 对话未被破坏
    assert log.count("a.before") == 1
    assert log.count("c.before") == 1


def test_hook_timeout_skips_branch():
    """验收:钩子超时 → 跳过该枝干(默认 5s,测试用短超时)。"""
    log = []
    chain = HookChain(hook_timeout=0.05)
    chain.register(RecordBranch(log, "a"))
    chain.register(FailingBranch(mode="timeout"))
    chain.register(RecordBranch(log, "c"))

    snapshot, _ = asyncio.run(chain.before_all(_snapshot()))
    assert not snapshot.stop
    assert log.count("c.before") == 1  # 超时枝干被跳过,c 正常执行


# ---------------------------------------------------------------------------
# 拦截(SetStop 短路)与 Action 应用
# ---------------------------------------------------------------------------
class StopBranch(Branch):
    """before 返回 SetStop 拦截对话。"""

    name = "stopper"

    async def before(self, snapshot):
        return [SetStop(reason="test")]


def test_before_stop_intercepts():
    """验收:枝干返回 SetStop → before 链提前终止,后续枝干不被调用。"""
    log = []
    chain = HookChain()
    chain.register(RecordBranch(log, "a"))
    chain.register(StopBranch())
    chain.register(RecordBranch(log, "c"))

    snapshot, stop_reason = asyncio.run(chain.before_all(_snapshot()))
    assert snapshot.stop is True
    assert stop_reason == "test"
    assert log.count("c.before") == 0  # SetStop 后不再调用后续枝干


def test_before_extra_applied_to_snapshot():
    """验收:before 返回 SetExtra → action 立即应用进快照(后扩展可见)。"""
    class ExtraBranch(Branch):
        name = "extra"

        async def before(self, snapshot):
            return [SetExtra(key="a.b", value=1)]

    chain = HookChain()
    chain.register(ExtraBranch())
    snapshot, _ = asyncio.run(chain.before_all(_snapshot()))
    assert snapshot.extra == {"a.b": 1}


# ---------------------------------------------------------------------------
# 工具派发
# ---------------------------------------------------------------------------
class ToolBranch(Branch):
    """实现 on_tool_call 的枝干。"""

    name = "tools"

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    async def on_tool_call(self, snapshot, tool_name, tool_input):
        self.calls.append((tool_name, tool_input))
        if tool_name in self.results:
            return self.results[tool_name]
        return NotImplemented


def test_dispatch_tool_call_first_executor_wins():
    """验收:工具派发按注册序,首个非 NotImplemented 者执行。"""
    chain = HookChain()
    chain.register(ToolBranch(results={}))
    chain.register(ToolBranch(results={"web_search": "结果B"}))

    snapshot = _snapshot()
    # 第一个枝干未实现 web_search → 交给第二个
    result = asyncio.run(chain.dispatch_tool_call(snapshot, "web_search", {"q": "x"}))
    assert result == "结果B"

    # 全部未实现 → no_executor 哨兵
    result2 = asyncio.run(chain.dispatch_tool_call(snapshot, "unknown_tool", {}))
    assert no_executor(result2)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
def test_setup_all_propagates_failure():
    """验收:setup 失败向上抛(启动失败),不静默吞错。"""
    class BadSetupBranch(Branch):
        name = "bad_setup"

        async def setup(self, config, core):
            raise RuntimeError("初始化失败:依赖缺失")

    chain = HookChain()
    chain.register(BadSetupBranch())
    with pytest.raises(RuntimeError, match="初始化失败"):
        asyncio.run(chain.setup_all({}, None))
