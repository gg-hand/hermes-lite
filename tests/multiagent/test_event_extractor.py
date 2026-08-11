"""event_extractor 单元测试。"""
import pytest
from datetime import datetime, timezone

from teage_liu.multiagent.event_extractor import extract_events


def _msg(seq, type_, from_, ts, **extra):
    """构造测试消息。"""
    return {"seq": seq, "type": type_, "from": from_, "timestamp": ts, **extra}


def test_extract_events_agent_online():
    """agent announce online 消息应提取为 agent_online 事件。"""
    ts = "2026-07-27T10:00:00+00:00"
    messages = [
        _msg(1, "announce", "agent-001", ts, action="online",
             agent_name="Worker-A", capabilities=["task_exec"]),
    ]
    events = extract_events(messages)
    assert len(events) == 1
    assert events[0]["event_type"] == "agent_online"
    assert events[0]["agent_id"] == "agent-001"
    assert events[0]["ts"] == ts
    assert events[0]["seq"] == 1
    assert "Worker-A" in events[0]["summary"]


def test_extract_events_director_broadcast():
    """Director 广播（from=director, type=request）应提取为 director_broadcast 事件。"""
    ts = "2026-07-27T11:00:00+00:00"
    messages = [
        _msg(2, "request", "director", ts, content="请所有 agent 汇报进度", to="*"),
    ]
    events = extract_events(messages)
    assert len(events) == 1
    assert events[0]["event_type"] == "director_broadcast"
    assert events[0]["agent_id"] == "director"
    assert "请所有 agent 汇报进度" in events[0]["summary"]


def test_extract_events_directive_injected():
    """director directive 消息应提取为 directive_injected 事件。"""
    ts = "2026-07-27T12:00:00+00:00"
    messages = [
        _msg(3, "directive", "director", ts, content="优先处理用户请求",
             rule_type="ordering", priority="high"),
    ]
    events = extract_events(messages)
    assert len(events) == 1
    assert events[0]["event_type"] == "directive_injected"
    assert events[0]["agent_id"] == "director"
    assert "ordering" in events[0]["summary"]
    assert "high" in events[0]["summary"]


def test_extract_events_filters_untracked_types():
    """response / status / result 等非事件类型应被过滤。
    注：relay + via=a2a 已纳入事件提取（a2a_relay），此处验证不带 via 的 relay 仍被过滤。
    """
    ts = "2026-07-27T13:00:00+00:00"
    messages = [
        # 不带 via=a2a 的 relay 仍被过滤
        _msg(1, "relay", "agent-001", ts, content="hello"),
        _msg(2, "response", "agent-002", ts, content="ok"),
        _msg(3, "status", "agent-001", ts, content="running"),
    ]
    events = extract_events(messages)
    assert events == []


def test_extract_events_a2a_relay():
    """relay + via=a2a 应提取为 a2a_relay 事件（体现 A2A 协作透明度）。"""
    ts = "2026-07-27T13:00:00+00:00"
    messages = [
        _msg(10, "relay", "agent_A", ts, content="hello via a2a",
             to="agent_B", via="a2a", message_id="msg_001",
             forwarded_by="agent_A"),
    ]
    events = extract_events(messages)
    assert len(events) == 1
    assert events[0]["event_type"] == "a2a_relay"
    assert events[0]["agent_id"] == "agent_A"
    assert events[0]["seq"] == 10
    assert "agent_A" in events[0]["summary"]
    assert "agent_B" in events[0]["summary"]
    assert "hello via a2a" in events[0]["summary"]


def test_extract_events_a2a_relay_with_forwarded_by():
    """forwarded_by 与 from 不同时，summary 应包含归档方信息。"""
    ts = "2026-07-27T13:30:00+00:00"
    messages = [
        _msg(11, "relay", "agent_A", ts, content="hi",
             to="agent_B", via="a2a", message_id="msg_002",
             forwarded_by="agent_C"),
    ]
    events = extract_events(messages)
    assert len(events) == 1
    assert events[0]["event_type"] == "a2a_relay"
    assert "agent_C" in events[0]["summary"]  # 归档方出现在 summary
    assert "归档" in events[0]["summary"]


def test_extract_events_sorted_desc_by_ts():
    """事件应按时间倒序返回（最新在前）。"""
    messages = [
        _msg(1, "announce", "a1", "2026-07-27T10:00:00+00:00", action="online"),
        _msg(2, "announce", "a2", "2026-07-27T12:00:00+00:00", action="online"),
        _msg(3, "announce", "a3", "2026-07-27T11:00:00+00:00", action="online"),
    ]
    events = extract_events(messages)
    assert len(events) == 3
    assert events[0]["agent_id"] == "a2"  # 12:00 最新
    assert events[1]["agent_id"] == "a3"  # 11:00
    assert events[2]["agent_id"] == "a1"  # 10:00 最早


def test_extract_events_before_ts_pagination():
    """before_ts 游标分页：仅返回 ts 严格小于 before_ts 的事件。"""
    messages = [
        _msg(1, "announce", "a1", "2026-07-27T10:00:00+00:00", action="online"),
        _msg(2, "announce", "a2", "2026-07-27T11:00:00+00:00", action="online"),
        _msg(3, "announce", "a3", "2026-07-27T12:00:00+00:00", action="online"),
    ]
    # 取 11:00 之前的事件
    events = extract_events(messages, before_ts="2026-07-27T11:00:00+00:00")
    assert len(events) == 1
    assert events[0]["agent_id"] == "a1"


def test_extract_events_limit_caps_count():
    """limit 参数限制返回数量。"""
    messages = [
        _msg(i, "announce", f"a{i}", f"2026-07-27T{i+9:02d}:00:00+00:00", action="online")
        for i in range(1, 6)
    ]
    events = extract_events(messages, limit=3)
    assert len(events) == 3
    # 应取最新的 3 条（i=5 → 14:00 最新）
    assert events[0]["agent_id"] == "a5"


def test_extract_events_agent_offline():
    """agent offline 消息应提取为 agent_offline 事件。"""
    ts = "2026-07-27T14:00:00+00:00"
    messages = [
        _msg(1, "announce", "agent-001", ts, action="offline"),
    ]
    events = extract_events(messages)
    assert len(events) == 1
    assert events[0]["event_type"] == "agent_offline"
