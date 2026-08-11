"""Director Engine 观察者模式测试（Task 7，P2 签名 + D6 timestamp 修复）。"""
import asyncio

import pytest
from datetime import datetime, timezone, timedelta

from teage_liu.multiagent.director_engine import DirectorEngine
from teage_liu.multiagent.directors.script_director import ScriptDirector


@pytest.fixture
def bb_root(tmp_path):
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


def test_director_engine_observe(bb_root):
    """DirectorEngine 观察协作状态（委托给 director_protocol.observe）"""
    director = ScriptDirector(bb_root)
    engine = DirectorEngine(
        bb_root, config={"multiagent": {}}, director_protocol=director
    )

    from teage_liu.multiagent.blackboard import append_collab_message
    asyncio.run(append_collab_message(bb_root, {"from": "A", "type": "announce", "action": "online"}))
    asyncio.run(append_collab_message(bb_root, {"from": "B", "type": "relay", "content": "2"}))

    state = asyncio.run(engine.observe_collab())
    assert state["message_count"] == 2


def test_director_engine_no_dispatch(bb_root):
    """DirectorEngine 不再分派任务（移除 _dispatch_tasks）"""
    director = ScriptDirector(bb_root)
    engine = DirectorEngine(
        bb_root, config={"multiagent": {}}, director_protocol=director
    )
    assert not hasattr(engine, "_dispatch_tasks")


def test_director_engine_detect_anomaly(bb_root):
    """DirectorEngine 检测异常（D6 修复：基于 timestamp，不用 time.sleep）"""
    director = ScriptDirector(bb_root, timeout_seconds=1)
    engine = DirectorEngine(
        bb_root, config={"multiagent": {}}, director_protocol=director
    )

    from teage_liu.multiagent.blackboard import append_collab_message, read_collab_messages, _get_collab_file
    import yaml

    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A",
        "type": "request",
        "content": "需要协作",
        "collab_type": "instant",
    }))

    # 手动改写消息 timestamp 为过去时间（模拟超时，不用 time.sleep）
    messages = asyncio.run(read_collab_messages(bb_root))
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    messages[0]["timestamp"] = past_time

    file_path = _get_collab_file(bb_root, None)
    content = ""
    for m in messages:
        content += f"---\n{yaml.safe_dump(m, allow_unicode=True, default_flow_style=False, sort_keys=False)}---\n\n"
    file_path.write_text(content, encoding="utf-8")

    anomalies = asyncio.run(engine.detect_anomaly())
    assert len(anomalies) > 0
    assert anomalies[0]["type"] == "timeout"


def test_director_engine_observe_without_protocol(bb_root):
    """无 director_protocol 时 observe_collab 直接读 collab 消息"""
    engine = DirectorEngine(bb_root, config={"multiagent": {}})

    from teage_liu.multiagent.blackboard import append_collab_message
    asyncio.run(append_collab_message(bb_root, {"from": "A", "type": "relay", "content": "hi"}))

    state = asyncio.run(engine.observe_collab())
    assert state["message_count"] == 1


def test_director_engine_detect_anomaly_without_protocol(bb_root):
    """无 director_protocol 时 detect_anomaly 返回空列表"""
    engine = DirectorEngine(bb_root, config={"multiagent": {}})
    anomalies = asyncio.run(engine.detect_anomaly())
    assert anomalies == []


def test_director_engine_preserves_infrastructure(bb_root):
    """P1：DirectorEngine 保留现有基础设施方法"""
    engine = DirectorEngine(bb_root, config={"multiagent": {}})
    # 验证保留的关键基础设施方法存在
    assert hasattr(engine, "_acquire_mutex_lock")
    assert hasattr(engine, "_try_hard_preempt")
    assert hasattr(engine, "_increment_epoch")
    assert hasattr(engine, "_broadcast_started")
    assert hasattr(engine, "_check_worker_heartbeats")
    assert hasattr(engine, "_update_trust_score")
    assert hasattr(engine, "start")
    assert hasattr(engine, "stop")


def test_director_engine_constructor_backward_compatible(bb_root):
    """P2：构造签名向后兼容（lifespan.py 调用方式不破坏）"""
    # lifespan.py 调用方式：DirectorEngine(bb_root=bb_root, config=config, agent_id=director_agent_id)
    engine = DirectorEngine(
        bb_root=bb_root,
        config={"multiagent": {"director": {"heartbeat_timeout_seconds": 30}}},
        agent_id="director_001",
    )
    assert engine._agent_id == "director_001"
    assert engine._director_protocol is None
