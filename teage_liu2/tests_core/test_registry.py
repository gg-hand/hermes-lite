"""registry 装配层专项测试(B1+B2+B3,计划 §3)。

验收覆盖:
- 配置声明 → 按序实例化;enabled:false 不注册;未知名启动失败
- setup 失败 = 启动失败(向上抛);setup 收到枝干自己的配置段
- registry.build 幂等可重入(L2 热重载铺路)
- after 钩子收 AfterResponse(B3)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.pipeline import ChatPipeline
from teage_liu2.core.registry import BranchRegistry
from teage_liu2.core.types import AfterResponse
from .fake_llm import FakeLLMClient


def _cfg(branches: dict) -> dict:
    return {"core": {"branches": branches}}


class LogBranch(Branch):
    """记录生命周期与收到的配置。"""

    def __init__(self, name, log):
        self.name = name
        self.log = log

    async def setup(self, config, core):
        self.log.append((self.name, "setup", dict(config)))

    async def teardown(self):
        self.log.append((self.name, "teardown"))


def test_build_order_and_enabled():
    """验收:配置顺序 = 注册顺序;enabled:false 不注册。"""
    log = []
    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("b", lambda cfg: LogBranch("b", log))

    chain = reg.build(_cfg({
        "b": {"enabled": True, "k": 1},
        "a": {"enabled": False},
    }))
    assert [b.name for b in chain.branches] == ["b"]


def test_unknown_branch_fails():
    """验收:未知名枝干(工厂未注册)→ ValueError 启动失败(防 typo 静默)。"""
    reg = BranchRegistry()
    with pytest.raises(ValueError, match="未知枝干"):
        reg.build(_cfg({"ghost": {"enabled": True}}))


def test_build_idempotent_reentrant():
    """验收(L2 铺路):build 幂等可重入 —— 每次返回全新链,互不残留。"""
    log = []
    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))

    chain1 = reg.build(_cfg({"a": {"enabled": True}}))
    chain2 = reg.build(_cfg({"a": {"enabled": True}}))
    assert chain1 is not chain2
    assert [b.name for b in chain1.branches] == ["a"]
    assert [b.name for b in chain2.branches] == ["a"]
    # 二次 build 后 setup_all 只对新链生效(旧链不残留)
    asyncio.run(reg.setup_all(_cfg({}), None))
    assert log == [("a", "setup", {"enabled": True})]


def test_setup_receives_branch_config():
    """验收:setup 收到枝干自己的配置段(枝干自校验依据)。"""
    log = []
    reg = BranchRegistry()
    reg.register_factory("memory", lambda cfg: LogBranch("memory", log))
    reg.build(_cfg({"memory": {"enabled": True, "retrieval_top_k": 5}}))
    asyncio.run(reg.setup_all(_cfg({}), SimpleNamespace()))
    assert log[0][2] == {"enabled": True, "retrieval_top_k": 5}


def test_setup_failure_propagates():
    """验收:setup 失败向上抛 = 启动失败(不静默吞错)。"""
    class BadBranch(Branch):
        name = "bad"

        async def setup(self, config, core):
            raise RuntimeError("依赖缺失")

    reg = BranchRegistry()
    reg.register_factory("bad", lambda cfg: BadBranch())
    reg.build(_cfg({"bad": {"enabled": True}}))
    with pytest.raises(RuntimeError, match="依赖缺失"):
        asyncio.run(reg.setup_all(_cfg({}), None))


def test_teardown_all_reverse_with_warning():
    """验收:teardown_all 逆序调用;单个 teardown 异常仅告警,逆序继续。"""
    log = []

    class BadTeardown(LogBranch):
        async def teardown(self):
            log.append((self.name, "teardown"))
            raise RuntimeError("teardown 失败")

    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("b", lambda cfg: BadTeardown("b", log))
    reg.build(_cfg({"a": {"enabled": True}, "b": {"enabled": True}}))
    asyncio.run(reg.teardown_all())
    # 逆序:先 b 后 a;b 抛异常仅告警,a 仍执行
    assert [x for x in log if x[1] == "teardown"] == [("b", "teardown"), ("a", "teardown")]


def test_setup_failure_rolls_back_succeeded():
    """验收(E1):setup 失败 → 逆序 teardown 已成功枝干 → 再抛错(启动原子性)。"""
    log = []

    class BadSetup(Branch):
        name = "bad"

        async def setup(self, config, core):
            log.append(("bad", "setup"))
            raise RuntimeError("依赖缺失")

    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("bad", lambda cfg: BadSetup())
    reg.register_factory("c", lambda cfg: LogBranch("c", log))
    reg.build(_cfg({"a": {"enabled": True}, "bad": {"enabled": True}, "c": {"enabled": True}}))

    with pytest.raises(RuntimeError, match="依赖缺失"):
        asyncio.run(reg.setup_all(_cfg({}), None))
    # a 已成功 → 回滚 teardown;bad 失败不 teardown;c 未 setup 不 teardown
    assert log == [
        ("a", "setup", {"enabled": True}),
        ("bad", "setup"),
        ("a", "teardown"),
    ]


def test_shutdown_idempotent_ordered():
    """验收(L1):shutdown 幂等编排 —— ①TaskRegistry 取消 → ②teardown 逆序 → ③④close。"""
    import asyncio

    from teage_liu2.core.tasks import TaskRegistry

    log = []
    tr = TaskRegistry()
    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("b", lambda cfg: LogBranch("b", log))
    reg.build(_cfg({"a": {"enabled": True}, "b": {"enabled": True}}))

    closed = []

    class FakeClose:
        def __init__(self, tag):
            self.tag = tag

        def close(self):
            closed.append(self.tag)

    ms, sp = FakeClose("ms"), FakeClose("sp")

    async def main():
        task = tr.create_task(asyncio.sleep(30))
        await reg.shutdown(task_registry=tr, message_store=ms, storage_provider=sp)
        # 幂等:第二次调用无效果(teardown/close 不再重复)
        await reg.shutdown(task_registry=tr, message_store=ms, storage_provider=sp)
        return task

    task = asyncio.run(main())
    assert task.cancelled()  # ① 后台任务已取消
    assert [x for x in log if x[1] == "teardown"] == [("b", "teardown"), ("a", "teardown")]
    assert closed == ["ms", "sp"]  # ③④ 各一次


def test_after_receives_after_response(tmp_path):
    """验收(B3):after 钩子收到 AfterResponse(text/content_blocks/usage/done_event)。"""
    seen = {}

    class AfterBranch(Branch):
        name = "after_observer"

        async def after(self, snapshot, response):
            seen["response"] = response
            return []

    llm = FakeLLMClient([
        {
            "content": [{"type": "text", "text": "完成"}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 7},
        },
    ])
    hooks = HookChain()
    hooks.register(AfterBranch())
    store = SQLiteHistoryStore(str(tmp_path / "after.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    async def _run():
        return [ev async for ev in pipeline.chat_stream("s_after", "问题")]

    asyncio.run(_run())
    resp = seen["response"]
    assert isinstance(resp, AfterResponse)
    assert resp.text == "完成"
    assert resp.done_event is not None
    assert resp.done_event["type"] == "done"
    assert resp.usage == {"output_tokens": 7}
    assert resp.content_blocks[0]["type"] == "text"
