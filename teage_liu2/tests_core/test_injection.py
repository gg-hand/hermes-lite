"""多层级注入平台专项测试(I1+I2,计划 §2)。

验收覆盖:
- 多层级正确组装(L0-L4):STABLE_SYSTEM/SYSTEM 进 system;PREFIX 最前;
  MID 历史中间按位插入;BEFORE_INPUT 当前输入前
- 分层预算裁剪(层内 priority 降序 + 注册序稳定,超预算整段丢弃不截半)
- key 去重(同 key 后声明覆盖先声明)
- 注入永不落盘(对话后库中无注入内容)
- 轮次间注入点(I2):loop 每轮 step 前调用 inject_round(骨架)
"""

from __future__ import annotations

import asyncio

from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.injection import (
    DEFAULT_LAYER_BUDGETS,
    L_BEFORE_INPUT,
    L_MID,
    L_PREFIX,
    L_STABLE_SYSTEM,
    L_SYSTEM,
    Injection,
    assemble_injections,
)
from teage_liu2.core.pipeline import ChatPipeline
from .fake_llm import FakeLLMClient


# ---------------------------------------------------------------------------
# 纯函数组装层
# ---------------------------------------------------------------------------
def test_assemble_all_layers():
    """验收:五层全组装 —— system 稳定区/末位;messages 前置/中间/输入前。"""
    injections = [
        Injection(L_STABLE_SYSTEM, "画像:用户是工程师"),
        Injection(L_SYSTEM, "会话指令:使用中文"),
        Injection(L_PREFIX, "检索记忆:昨天讨论了架构"),
        Injection(L_MID, "背景:这是长对话"),
        Injection(L_BEFORE_INPUT, "意图引导:请简洁回答"),
    ]
    system = "基础系统提示词"
    messages = [
        {"role": "user", "content": "历史问题1"},
        {"role": "assistant", "content": "历史回答1"},
        {"role": "user", "content": "当前输入"},
    ]

    eff_system, eff_msgs = assemble_injections(injections, system, messages)

    # system:基础在前,稳定区先于 system 末位
    assert eff_system.startswith("基础系统提示词")
    assert eff_system.index("画像") < eff_system.index("会话指令")

    # messages:PREFIX 最前 / MID 历史中间 / BEFORE_INPUT 输入前;
    # 组装末尾合并相邻 user 消息(API 交替要求)——相邻注入与 user 级联合并
    contents = [m["content"] for m in eff_msgs]
    assert contents == [
        "检索记忆:昨天讨论了架构\n\n历史问题1\n\n背景:这是长对话",  # L2+L3 级联合并
        "历史回答1",
        "意图引导:请简洁回答\n\n当前输入",       # L4 输入前 + 当前输入合并
    ]
    # 合并后 user/assistant 交替(LLM API 合法性)
    assert [m["role"] for m in eff_msgs] == ["user", "assistant", "user"]


def test_empty_injections_keep_inputs():
    """验收:零注入 → system/messages 原样。"""
    eff_system, eff_msgs = assemble_injections([], "基础", [{"role": "user", "content": "hi"}])
    assert eff_system == "基础"
    assert eff_msgs == [{"role": "user", "content": "hi"}]


def test_budget_trims_low_priority_whole():
    """验收:层内 priority 降序裁剪,超预算低优先整段丢(不截半)。"""
    injections = [
        Injection(L_PREFIX, "A" * 6, priority=100),
        Injection(L_PREFIX, "B" * 6, priority=0),
    ]
    budgets = {**DEFAULT_LAYER_BUDGETS, L_PREFIX: 10}
    _, eff_msgs = assemble_injections(injections, "", [{"role": "user", "content": "u"}], budgets=budgets)
    assert [m["content"] for m in eff_msgs] == ["AAAAAA\n\nu"]  # 低优先整段丢;注入与 u 合并


def test_budget_single_item_over_budget_dropped_whole():
    """验收:单条超预算 → 整条丢弃,不截半。"""
    injections = [Injection(L_PREFIX, "C" * 20, priority=100)]
    budgets = {**DEFAULT_LAYER_BUDGETS, L_PREFIX: 10}
    _, eff_msgs = assemble_injections(injections, "", [{"role": "user", "content": "u"}], budgets=budgets)
    assert [m["content"] for m in eff_msgs] == ["u"]


def test_key_dedupe_later_wins():
    """验收:同 key 后声明覆盖先声明。"""
    injections = [
        Injection(L_PREFIX, "旧值", key="memory"),
        Injection(L_PREFIX, "新值", key="memory"),
    ]
    _, eff_msgs = assemble_injections(injections, "", [{"role": "user", "content": "u"}])
    assert [m["content"] for m in eff_msgs] == ["新值\n\nu"]


# ---------------------------------------------------------------------------
# pipeline 集成:注入永不落盘 + I2 轮次注入点
# ---------------------------------------------------------------------------
class InjectBranch(Branch):
    """声明多层级注入的枝干。"""

    name = "injector"

    async def build_injections(self, snapshot):
        return [
            Injection(L_PREFIX, "敏感注入内容-永不落盘", priority=10),
            Injection(L_BEFORE_INPUT, "输入前注入", priority=10),
        ]


def test_injection_never_persisted(tmp_path):
    """验收:注入内容永不落盘(库中无注入内容)。"""
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "回答"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    hooks.register(InjectBranch())
    store = SQLiteHistoryStore(str(tmp_path / "inj.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    async def _run():
        return [ev async for ev in pipeline.chat_stream("s_inj", "问题")]

    asyncio.run(_run())
    msgs = store.get_session_messages("s_inj")
    assert all("永不落盘" not in m["content"] for m in msgs)
    assert all("输入前注入" not in m["content"] for m in msgs)
    # LLM 确实收到了注入(仅内存,合并进相邻 user 消息)
    assert "敏感注入内容-永不落盘" in llm.last_messages[0]["content"]


class RoundBranch(Branch):
    """记录 inject_round 调用并返回本轮注入声明(Injection)。"""

    name = "round_injector"

    def __init__(self):
        self.round_calls = 0

    async def inject_round(self, snapshot):
        self.round_calls += 1
        # §4.2:layer 强制 BEFORE_INPUT;语义变更(§17):str → Injection
        return Injection(L_BEFORE_INPUT, "本轮提示")

    async def on_tool_call(self, snapshot, tool_name, tool_input):
        if tool_name != "echo":
            return NotImplemented
        return "回声"


def test_inject_round_called_per_step(tmp_path):
    """验收(I2 骨架):loop 每轮 step 前调用 inject_round,本轮注入置于输入前。"""
    llm = FakeLLMClient([
        {
            "content": [{"type": "tool_use", "id": "t1", "name": "echo", "input": {}}],
            "stop_reason": "tool_use",
        },
        {"content": [{"type": "text", "text": "收尾"}], "stop_reason": "end_turn"},
    ])
    hooks = HookChain()
    round_branch = RoundBranch()
    hooks.register(round_branch)
    store = SQLiteHistoryStore(str(tmp_path / "round.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    async def _run():
        return [ev async for ev in pipeline.chat_stream("s_round", "调工具")]

    asyncio.run(_run())
    # 两轮 LLM 调用 → inject_round 每轮一次
    assert round_branch.round_calls == 2
    # 第二轮 LLM 收到本轮注入(与 tool_results 合并为一条 user 消息:
    # 文本块前置 + tool_result 块,保持交替不破坏回喂结构)
    second = llm.last_messages
    last_content = second[-1]["content"]
    assert last_content[0]["type"] == "text"
    assert last_content[0]["text"] == "本轮提示"
    assert last_content[1]["type"] == "tool_result"
