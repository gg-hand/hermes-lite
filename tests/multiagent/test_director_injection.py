"""Director LLM 上下文注入测试。"""
from __future__ import annotations

import pytest
from pathlib import Path

from teage_liu.multiagent.director_injection import DirectorInjector
from teage_liu.multiagent.blackboard import append_collab_message


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def injector(bb_root: Path) -> DirectorInjector:
    return DirectorInjector(bb_root=bb_root, agent_id="agent_alice")


class TestDirectorInjector:
    """Director 注入机制测试。"""

    @pytest.mark.asyncio
    async def test_enqueue_directive(self, injector: DirectorInjector):
        """入队 directive。"""
        await injector.enqueue_directive({
            "from": "director",
            "type": "directive",
            "content": "请按顺序读取弹幕",
            "rule_type": "ordering",
            "target": "*",
            "seq": 5,
        })
        assert injector._pending_count() == 1

    @pytest.mark.asyncio
    async def test_drain_empty_queue_returns_empty(self, injector: DirectorInjector):
        """空队列 drain 返回空字符串。"""
        result = injector.drain_pending_directives()
        assert result == ""

    @pytest.mark.asyncio
    async def test_drain_returns_formatted_context(self, injector: DirectorInjector):
        """drain 返回格式化的 LLM 上下文片段。"""
        await injector.enqueue_directive({
            "from": "director",
            "type": "directive",
            "content": "请按顺序读取弹幕",
            "rule_type": "ordering",
            "target": "*",
            "seq": 5,
            "timestamp": "2026-07-29T10:00:00+00:00",
        })

        context = injector.drain_pending_directives()
        assert "[协作背景指导]" in context
        assert "请按顺序读取弹幕" in context
        assert "ordering" in context
        # drain 后队列清空
        assert injector._pending_count() == 0

    @pytest.mark.asyncio
    async def test_drain_multiple_directives_concatenated(
        self, injector: DirectorInjector
    ):
        """多个 directive 拼接为一个上下文片段。"""
        await injector.enqueue_directive({
            "from": "director", "type": "directive",
            "content": "规则1", "rule_type": "constraint",
            "target": "*", "seq": 1,
        })
        await injector.enqueue_directive({
            "from": "director", "type": "directive",
            "content": "规则2", "rule_type": "ordering",
            "target": "*", "seq": 2,
        })

        context = injector.drain_pending_directives()
        assert "规则1" in context
        assert "规则2" in context

    @pytest.mark.asyncio
    async def test_drain_clears_queue(self, injector: DirectorInjector):
        """drain 后再次调用返回空。"""
        await injector.enqueue_directive({
            "from": "director", "type": "directive",
            "content": "test", "rule_type": "ordering",
            "target": "*", "seq": 1,
        })
        first = injector.drain_pending_directives()
        second = injector.drain_pending_directives()
        assert first != ""
        assert second == ""

    @pytest.mark.asyncio
    async def test_poll_and_enqueue_new_directives(
        self, bb_root: Path, injector: DirectorInjector
    ):
        """轮询 collaboration.md 发现新 directive 并入队。"""
        # 写入两条 directive
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "directive",
            "content": "directive 1",
            "rule_type": "ordering",
            "target": "*",
        })
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "directive",
            "content": "directive 2",
            "rule_type": "constraint",
            "target": "agent_alice",
        })

        count = await injector.poll_and_enqueue_new_directives()
        assert count == 2
        assert injector._pending_count() == 2

    @pytest.mark.asyncio
    async def test_poll_skips_already_seen_directives(
        self, bb_root: Path, injector: DirectorInjector
    ):
        """重复轮询不重复入队已处理的 directive。"""
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "directive",
            "content": "only directive",
            "rule_type": "ordering",
            "target": "*",
        })

        first_count = await injector.poll_and_enqueue_new_directives()
        second_count = await injector.poll_and_enqueue_new_directives()

        assert first_count == 1
        assert second_count == 0

    @pytest.mark.asyncio
    async def test_poll_filters_by_target(
        self, bb_root: Path, injector: DirectorInjector
    ):
        """target 为特定 agent 时，只入队目标匹配的 directive。"""
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "directive",
            "content": "for alice",
            "rule_type": "ordering",
            "target": "agent_alice",
        })
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "directive",
            "content": "for bob",
            "rule_type": "ordering",
            "target": "agent_bob",
        })

        count = await injector.poll_and_enqueue_new_directives()
        assert count == 1  # 只入队 target=agent_alice 和 target=*
        context = injector.drain_pending_directives()
        assert "for alice" in context
        assert "for bob" not in context


class TestDecentralizedInjection:
    """改动点 1：去 director 中心化模式下注入内容测试。"""

    @pytest.mark.asyncio
    async def test_injected_block_does_not_request_director_approval(
        self, bb_root: Path
    ):
        """去中心化模式下，注入内容明确告知 worker 不要请示 Director。

        验证关键词：注入块应包含「不审批方案」「不要回复或请示 Director」，
        且标题为「协作背景指导」而非旧的「Director 引导」。
        """
        injector = DirectorInjector(
            bb_root=bb_root, agent_id="agent_alice", decentralized=True
        )
        await injector.enqueue_directive({
            "from": "director", "type": "directive",
            "content": "你们玩猜数字游戏",
            "rule_type": "ordering",
            "target": "*", "seq": 1,
        })

        context = injector.drain_pending_directives()

        # 标题改为协作背景指导（不再是 Director 引导）
        assert "[协作背景指导]" in context
        assert "[Director 引导]" not in context
        # 明确告知 Director 不审批方案
        assert "不审批方案" in context
        # 明确告知不要回复或请示 Director
        assert "不要回复或请示 Director" in context
        # 背景内容仍保留
        assert "你们玩猜数字游戏" in context
