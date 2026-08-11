"""Director 接口协议测试（Task 5，含 D6 timestamp 修复）。

测试 DirectorProtocol ABC + UserDirector + ScriptDirector + AgentDirector。
D6 修复：detect_anomaly 基于消息 timestamp 判断超时，测试用过去时间戳而非 time.sleep。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from teage_liu.multiagent.blackboard import (
    append_collab_message,
    read_collab_messages,
)
from teage_liu.multiagent.director_protocol import DirectorProtocol
from teage_liu.multiagent.directors.agent_director import AgentDirector
from teage_liu.multiagent.directors.script_director import ScriptDirector
from teage_liu.multiagent.directors.user_director import UserDirector


@pytest.fixture
def bb_root(tmp_path):
    """临时黑板根目录"""
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


# ========== DirectorProtocol ABC ==========


def test_director_protocol_is_abstract():
    """DirectorProtocol 是抽象类，不能实例化"""
    with pytest.raises(TypeError):
        DirectorProtocol()


# ========== UserDirector ==========


def test_user_director_inject_directive(bb_root):
    """UserDirector 注入 directive 到 collaboration.md"""
    director = UserDirector(bb_root)
    asyncio.run(director.inject_directive(
        content="按顺序读取弹幕",
        rule_type="ordering",
        target="*",
    ))

    messages = asyncio.run(read_collab_messages(bb_root))
    assert len(messages) == 1
    assert messages[0]["type"] == "directive"
    assert messages[0]["from"] == "director"
    assert messages[0]["rule_type"] == "ordering"
    assert messages[0]["content"] == "按顺序读取弹幕"
    # 字段级强化
    assert messages[0]["issued_by"] == "UserDirector"
    assert "timestamp" in messages[0]  # D6: 自动添加 timestamp


def test_user_directive_inject_to_specific_target(bb_root):
    """UserDirector 注入 directive 到特定 agent"""
    director = UserDirector(bb_root)
    asyncio.run(director.inject_directive(
        content="暂停协作",
        rule_type="intervention",
        target="agent_A",
    ))

    messages = asyncio.run(read_collab_messages(bb_root))
    assert messages[0]["target"] == "agent_A"
    assert messages[0]["rule_type"] == "intervention"


def test_user_director_inject_with_priority_and_deadline(bb_root):
    """UserDirector 注入带 priority/deadline 的 directive"""
    director = UserDirector(bb_root)
    asyncio.run(director.inject_directive(
        content="紧急排序",
        rule_type="ordering",
        target="*",
        priority="high",
        deadline=300,
    ))

    messages = asyncio.run(read_collab_messages(bb_root))
    assert messages[0]["priority"] == "high"
    assert messages[0]["deadline"] == 300


def test_user_director_handle_arbitration(bb_root):
    """UserDirector 仲裁请求返回 pending_user 状态"""
    director = UserDirector(bb_root)
    result = asyncio.run(director.handle_arbitration({"issue": "分歧"}))
    assert result["status"] == "pending_user"
    assert result["request"] == {"issue": "分歧"}


def test_user_director_observe(bb_root):
    """UserDirector observe 返回协作状态"""
    asyncio.run(append_collab_message(bb_root, {"from": "A", "type": "announce", "content": "1"}))
    director = UserDirector(bb_root)
    state = asyncio.run(director.observe())
    assert state["message_count"] == 1


# ========== ScriptDirector ==========


def test_script_director_observe(bb_root):
    """ScriptDirector observe 返回协作状态"""
    asyncio.run(append_collab_message(bb_root, {"from": "A", "type": "announce", "content": "上线"}))
    asyncio.run(append_collab_message(bb_root, {"from": "B", "type": "relay", "content": "你好"}))

    director = ScriptDirector(bb_root)
    state = asyncio.run(director.observe())
    assert state["message_count"] == 2
    assert "messages" in state


def test_script_director_inject_directive(bb_root):
    """ScriptDirector 也能注入 directive（issued_by=ScriptDirector）"""
    director = ScriptDirector(bb_root)
    asyncio.run(director.inject_directive(
        content="检测到异常，暂停",
        rule_type="intervention",
    ))

    messages = asyncio.run(read_collab_messages(bb_root))
    assert messages[0]["type"] == "directive"
    assert messages[0]["issued_by"] == "ScriptDirector"


def test_script_director_detect_anomaly_no_timeout(bb_root):
    """没有超时的 request 不报异常"""
    director = ScriptDirector(bb_root, timeout_seconds=300)
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A",
        "type": "request",
        "content": "需要协作",
        "collab_type": "instant",
    }))
    # 写一个 result 响应
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B",
        "type": "result",
        "content": "完成",
        "reply_to": 1,
    }))

    anomalies = asyncio.run(director.detect_anomaly())
    assert len(anomalies) == 0


def test_script_director_detect_timeout(bb_root):
    """ScriptDirector 检测超时（D6 修复：基于 timestamp，不用 time.sleep）"""
    director = ScriptDirector(bb_root, timeout_seconds=1)

    # 写入一个即时型 request（CollabWriter 会自动添加 timestamp）
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A",
        "type": "request",
        "content": "需要协作",
        "collab_type": "instant",
    }))

    # 手动改写消息的 timestamp 为过去时间（模拟超时，不用 time.sleep）
    messages = asyncio.run(read_collab_messages(bb_root))
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    messages[0]["timestamp"] = past_time

    # 重写 collaboration.md 让 detect_anomaly 读到过去时间戳
    import yaml
    from teage_liu.multiagent.blackboard import _get_collab_file
    file_path = _get_collab_file(bb_root, None)
    content = ""
    for m in messages:
        content += f"---\n{yaml.safe_dump(m, allow_unicode=True, default_flow_style=False, sort_keys=False)}---\n\n"
    file_path.write_text(content, encoding="utf-8")

    anomalies = asyncio.run(director.detect_anomaly())
    assert len(anomalies) > 0
    assert anomalies[0]["type"] == "timeout"


def test_script_director_handle_arbitration(bb_root):
    """ScriptDirector 仲裁请求返回 auto_resolved 状态"""
    director = ScriptDirector(bb_root)
    result = asyncio.run(director.handle_arbitration({"issue": "冲突"}))
    assert result["status"] == "auto_resolved"


# ========== AgentDirector ==========


def test_agent_director_inject_directive(bb_root):
    """AgentDirector 注入 directive（issued_by=AgentDirector）"""
    director = AgentDirector(bb_root)
    asyncio.run(director.inject_directive(
        content="建议按能力分工",
        rule_type="constraint",
    ))

    messages = asyncio.run(read_collab_messages(bb_root))
    assert messages[0]["type"] == "directive"
    assert messages[0]["issued_by"] == "AgentDirector"


def test_agent_director_handle_arbitration_no_orchestrator(bb_root):
    """AgentDirector 无 orchestrator 时返回 no_orchestrator 状态"""
    director = AgentDirector(bb_root, orchestrator=None)
    result = asyncio.run(director.handle_arbitration({"issue": "分歧"}))
    assert result["status"] == "no_orchestrator"


def test_agent_director_handle_arbitration_with_orchestrator(bb_root):
    """AgentDirector 有 orchestrator 时调用 LLM 处理仲裁"""
    from unittest.mock import AsyncMock

    orchestrator = AsyncMock()
    orchestrator.chat.return_value = "仲裁结果：A 优先"
    director = AgentDirector(bb_root, orchestrator=orchestrator)

    result = asyncio.run(director.handle_arbitration({"issue": "分歧"}))
    assert result["status"] == "llm_resolved"
    assert result["result"] == "仲裁结果：A 优先"
    orchestrator.chat.assert_called_once()


def test_agent_director_observe(bb_root):
    """AgentDirector observe 返回协作状态"""
    asyncio.run(append_collab_message(bb_root, {"from": "A", "type": "relay", "content": "hi"}))
    director = AgentDirector(bb_root)
    state = asyncio.run(director.observe())
    assert state["message_count"] == 1
