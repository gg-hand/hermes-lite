"""钩子链契约测试:顺序 / 隔离 / 超时 / 异常跳过 / 拦截 / 注入 / 工具派发。"""

from __future__ import annotations

import asyncio

import pytest

from teage_liu2.core.hooks import Branch, BranchContext, HookChain, no_executor


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

    async def build_system(self, ctx):
        self.log.append(f"{self.tag}.build_system")
        ctx.system_text += f"[{self.tag}]"

    async def build_injection(self, ctx):
        self.log.append(f"{self.tag}.build_injection")
        return f"注入-{self.tag}"

    async def before(self, ctx):
        self.log.append(f"{self.tag}.before")

    async def after(self, ctx, response):
        self.log.append(f"{self.tag}.after")


def test_hooks_order_forward_before_reverse_after():
    """验收:before 正序(注册序),after 逆序(洋葱),teardown 逆序。"""
    log = []
    chain = HookChain()
    chain.register(RecordBranch(log, "a"))
    chain.register(RecordBranch(log, "b"))

    ctx = BranchContext("s", "hi")
    asyncio.run(chain.build_system_all(ctx))
    asyncio.run(chain.before_all(ctx))
    asyncio.run(chain.after_all(ctx, None))
    asyncio.run(chain.teardown_all())

    build_system_calls = [x for x in log if x.endswith("build_system")]
    assert build_system_calls == ["a.build_system", "b.build_system"]
    before_calls = [x for x in log if x.endswith("before")]
    assert before_calls == ["a.before", "b.before"]
    after_calls = [x for x in log if x.endswith("after")]
    assert after_calls == ["b.after", "a.after"]
    teardown_calls = [x for x in log if x.endswith("teardown")]
    assert teardown_calls == ["b.teardown", "a.teardown"]


def test_build_system_and_injection():
    """验收:build_system 追加稳定前缀;build_injection 汇总注入文本。"""
    chain = HookChain()
    chain.register(RecordBranch([], "a"))
    chain.register(RecordBranch([], "b"))

    ctx = BranchContext("s", "hi")
    asyncio.run(chain.build_system_all(ctx))
    injection = asyncio.run(chain.build_injection_all(ctx))

    assert ctx.system_text == "[a][b]"
    assert injection == "注入-a\n\n注入-b"


# ---------------------------------------------------------------------------
# 异常隔离与超时
# ---------------------------------------------------------------------------
class FailingBranch(Branch):
    """before 抛异常 / 超时的枝干,不应影响对话。"""

    name = "failing"

    def __init__(self, mode="raise"):
        self.mode = mode

    async def before(self, ctx):
        if self.mode == "raise":
            raise RuntimeError("枝干故障")
        if self.mode == "timeout":
            await asyncio.sleep(10)


def test_hook_exception_skips_branch_only():
    """验收:枝干运行时异常 → 跳过该枝干,其余枝干正常执行。"""
    log = []
    chain = HookChain()
    chain.register(RecordBranch(log, "a"))
    chain.register(FailingBranch(mode="raise"))
    chain.register(RecordBranch(log, "c"))

    ctx = BranchContext("s", "hi")
    asyncio.run(chain.before_all(ctx))
    assert not ctx.stop  # 对话未被破坏
    assert log.count("a.before") == 1
    assert log.count("c.before") == 1


def test_hook_timeout_skips_branch():
    """验收:钩子超时 → 跳过该枝干(默认 5s,测试用短超时)。"""
    log = []
    chain = HookChain(hook_timeout=0.05)
    chain.register(RecordBranch(log, "a"))
    chain.register(FailingBranch(mode="timeout"))
    chain.register(RecordBranch(log, "c"))

    ctx = BranchContext("s", "hi")
    asyncio.run(chain.before_all(ctx))
    assert not ctx.stop
    assert log.count("c.before") == 1  # 超时枝干被跳过,c 正常执行


# ---------------------------------------------------------------------------
# 拦截与工具派发
# ---------------------------------------------------------------------------
class StopBranch(Branch):
    """before 置 stop 拦截对话。"""

    name = "stopper"

    async def before(self, ctx):
        ctx.stop = True


def test_before_stop_intercepts():
    """验收:枝干置 ctx.stop → before 链提前终止。"""
    log = []
    chain = HookChain()
    chain.register(RecordBranch(log, "a"))
    chain.register(StopBranch())
    chain.register(RecordBranch(log, "c"))

    ctx = BranchContext("s", "hi")
    asyncio.run(chain.before_all(ctx))
    assert ctx.stop is True
    assert log.count("c.before") == 0  # stop 后不再调用后续枝干


class ToolBranch(Branch):
    """实现 on_tool_call 的枝干。"""

    name = "tools"

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    async def on_tool_call(self, ctx, tool_name, tool_input):
        self.calls.append((tool_name, tool_input))
        if tool_name in self.results:
            return self.results[tool_name]
        return NotImplemented


def test_dispatch_tool_call_first_executor_wins():
    """验收:工具派发按注册序,首个非 NotImplemented 者执行。"""
    chain = HookChain()
    chain.register(ToolBranch(results={}))
    chain.register(ToolBranch(results={"web_search": "结果B"}))

    ctx = BranchContext("s", "hi")
    # 第一个枝干未实现 web_search → 交给第二个
    result = asyncio.run(chain.dispatch_tool_call(ctx, "web_search", {"q": "x"}))
    assert result == "结果B"

    # 全部未实现 → no_executor 哨兵
    result2 = asyncio.run(chain.dispatch_tool_call(ctx, "unknown_tool", {}))
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
