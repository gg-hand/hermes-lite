"""端到端集成测试：Director + Worker + Orchestrator mock。

验证完整任务分派链路：
用户提交 task → Director 分派 assign → Worker 执行 → status(processing) → result(completed)
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
import yaml


class FakeOrchestrator:
    """模拟 Orchestrator。"""
    def __init__(self):
        self.calls = []

    async def chat(self, session_id: str, user_input: str, **kwargs) -> str:
        self.calls.append((session_id, user_input))
        return f"已处理: {user_input}"


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录（覆盖 conftest.py 的同步版本）。"""
    from teage_liu.multiagent.blackboard import Blackboard

    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.mark.asyncio
async def test_e2e_task_dispatch_full_chain(bb_root):
    """端到端：task → assign → status(processing) → result(completed)。"""
    from teage_liu.multiagent.agent_registry import AgentRegistry
    from teage_liu.multiagent.blackboard import append_message, read_messages
    from teage_liu.multiagent.director_engine import DirectorEngine
    from teage_liu.multiagent.schema_validator import SchemaValidator
    from teage_liu.multiagent.worker_adapter import WorkerAdapter

    # 1. 注册 active worker
    registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
    agent_card = {
        "agent_id": "worker_001", "role": "worker", "status": "registering",
        "protocol_version": "1.0.0", "agent_version": "1.0.0",
        "capabilities": ["file_read"],
        "last_heartbeat": "2026-07-23T10:00:00+00:00",
        "heartbeat_interval_seconds": 10, "trust_score": 100,
    }
    await registry.register(agent_card)
    await registry.update_agent_status("worker_001", "active")

    # 2. 写入 director.md
    director_md = {
        "protocol_version": "1.0.0", "director_version": "1.0.0",
        "current_epoch": 1, "epoch_started_at": "2026-07-23T10:00:00+00:00",
        "last_director_tick": "2026-07-23T10:00:00+00:00",
        "director_id": "director_001", "director_implementation": "script",
        "heartbeat": {"interval_seconds": 10, "timeout_seconds": 30},
        "turn_policy": {"mode": "freeform", "order": []},
    }
    yaml_str = yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)
    (bb_root / "director.md").write_text(
        f"---\n{yaml_str}---\n\n# Director Protocol\n", encoding="utf-8",
    )

    # 3. 构造 Director 和 Worker
    config = {"multiagent": {"director": {"turn_timeout_seconds": 30}}}
    director = DirectorEngine(bb_root=bb_root, config=config, agent_id="director_001")

    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root, config=config, agent_id="worker_001", orchestrator=fake_orch,
    )

    # 4. 用户提交 task 消息
    await append_message(bb_root, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "task", "content": "帮我读取 data/test.txt",
        "task_op_id": "e2e-task-001", "target_agents": [], "mode": "dispatch",
    }, validate=True)

    # 5. Director 分派任务
    await director._dispatch_tasks()

    # 6. Worker 拾取并执行
    await worker._poll_once()

    # 7. 验证消息链
    messages = await read_messages(bb_root)
    task_msgs = [m for m in messages if m.get("task_op_id") == "e2e-task-001"]
    assert len(task_msgs) == 4, f"期望 4 条消息（task+assign+status+result），实际 {len(task_msgs)}"

    types = [m["type"] for m in task_msgs]
    assert types == ["task", "assign", "status", "result"]

    # 8. 验证 assign 消息
    assign_msg = task_msgs[1]
    assert assign_msg["from"] == "director_001"
    assert assign_msg["to"] == "worker_001"
    assert assign_msg["assigned_to"] == "worker_001"
    assert assign_msg["reply_to"] == task_msgs[0]["seq"]

    # 9. 验证 status 消息
    status_msg = task_msgs[2]
    assert status_msg["from"] == "worker_001"
    assert status_msg["status"] == "processing"

    # 10. 验证 result 消息
    result_msg = task_msgs[3]
    assert result_msg["from"] == "worker_001"
    assert result_msg["status"] == "completed"
    assert "已处理" in result_msg["content"]

    # 11. 验证 Orchestrator 被调用
    assert len(fake_orch.calls) == 1
    assert fake_orch.calls[0][1] == "帮我读取 data/test.txt"


@pytest.mark.asyncio
async def test_e2e_all_messages_pass_schema_validation(bb_root):
    """端到端：所有写入的消息通过 schema 验证。"""
    from teage_liu.multiagent.agent_registry import AgentRegistry
    from teage_liu.multiagent.blackboard import append_message, read_messages
    from teage_liu.multiagent.director_engine import DirectorEngine
    from teage_liu.multiagent.schema_validator import SchemaValidator
    from teage_liu.multiagent.worker_adapter import WorkerAdapter

    # 注册 active worker
    registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
    agent_card = {
        "agent_id": "worker_001", "role": "worker", "status": "registering",
        "protocol_version": "1.0.0", "agent_version": "1.0.0",
        "capabilities": [],
        "last_heartbeat": "2026-07-23T10:00:00+00:00",
        "heartbeat_interval_seconds": 10, "trust_score": 100,
    }
    await registry.register(agent_card)
    await registry.update_agent_status("worker_001", "active")

    # director.md
    director_md = {
        "protocol_version": "1.0.0", "director_version": "1.0.0",
        "current_epoch": 1, "epoch_started_at": "2026-07-23T10:00:00+00:00",
        "last_director_tick": "2026-07-23T10:00:00+00:00",
        "director_id": "director_001", "director_implementation": "script",
        "heartbeat": {"interval_seconds": 10, "timeout_seconds": 30},
        "turn_policy": {"mode": "freeform", "order": []},
    }
    yaml_str = yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)
    (bb_root / "director.md").write_text(
        f"---\n{yaml_str}---\n\n# Director Protocol\n", encoding="utf-8",
    )

    config = {"multiagent": {"director": {"turn_timeout_seconds": 30}}}
    director = DirectorEngine(bb_root=bb_root, config=config, agent_id="director_001")
    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root, config=config, agent_id="worker_001", orchestrator=fake_orch,
    )

    await append_message(bb_root, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "task", "content": "schema 验证测试",
        "task_op_id": "e2e-schema-001", "target_agents": [], "mode": "dispatch",
    }, validate=True)

    await director._dispatch_tasks()
    await worker._poll_once()

    # 验证所有消息通过 schema
    validator = SchemaValidator(enabled=True)
    messages = await read_messages(bb_root)
    for msg in messages:
        # 跳过缺失 seq 的旧消息（init_blackboard 可能写入的空消息）
        if "seq" not in msg:
            continue
        validator.validate_messages_record(msg)  # 不抛异常即通过


@pytest.mark.asyncio
async def test_e2e_get_task_status_returns_full_timeline(bb_root):
    """端到端：get_task_status 返回完整的状态流转时间线。"""
    from teage_liu.api.multiagent_routes import _infer_task_status
    from teage_liu.multiagent.agent_registry import AgentRegistry
    from teage_liu.multiagent.blackboard import append_message, read_messages
    from teage_liu.multiagent.director_engine import DirectorEngine
    from teage_liu.multiagent.schema_validator import SchemaValidator
    from teage_liu.multiagent.worker_adapter import WorkerAdapter

    registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
    agent_card = {
        "agent_id": "worker_001", "role": "worker", "status": "registering",
        "protocol_version": "1.0.0", "agent_version": "1.0.0",
        "capabilities": [],
        "last_heartbeat": "2026-07-23T10:00:00+00:00",
        "heartbeat_interval_seconds": 10, "trust_score": 100,
    }
    await registry.register(agent_card)
    await registry.update_agent_status("worker_001", "active")

    director_md = {
        "protocol_version": "1.0.0", "director_version": "1.0.0",
        "current_epoch": 1, "epoch_started_at": "2026-07-23T10:00:00+00:00",
        "last_director_tick": "2026-07-23T10:00:00+00:00",
        "director_id": "director_001", "director_implementation": "script",
        "heartbeat": {"interval_seconds": 10, "timeout_seconds": 30},
        "turn_policy": {"mode": "freeform", "order": []},
    }
    yaml_str = yaml.safe_dump(director_md, sort_keys=False, allow_unicode=True)
    (bb_root / "director.md").write_text(
        f"---\n{yaml_str}---\n\n# Director Protocol\n", encoding="utf-8",
    )

    config = {"multiagent": {"director": {"turn_timeout_seconds": 30}}}
    director = DirectorEngine(bb_root=bb_root, config=config, agent_id="director_001")
    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root, config=config, agent_id="worker_001", orchestrator=fake_orch,
    )

    await append_message(bb_root, {
        "from": "user_dispatch", "to": "*",
        "timestamp": "2026-07-23T10:00:01+00:00",
        "type": "task", "content": "时间线测试",
        "task_op_id": "e2e-timeline-001", "target_agents": [], "mode": "dispatch",
    }, validate=True)

    await director._dispatch_tasks()
    await worker._poll_once()

    # 模拟 get_task_status 端点逻辑
    messages = await read_messages(bb_root)
    task_msgs = [m for m in messages if m.get("task_op_id") == "e2e-timeline-001"]
    assert len(task_msgs) == 4

    latest = task_msgs[-1]
    inferred_status = _infer_task_status(latest)
    assert inferred_status == "completed"

    # 验证 timeline
    timeline = [
        {
            "ts": m.get("timestamp") or m.get("ts", ""),
            "from": m.get("from", ""),
            "type": m.get("type", ""),
            "status": m.get("status") or _infer_task_status(m),
        }
        for m in task_msgs
    ]
    assert timeline[0]["type"] == "task"
    assert timeline[0]["status"] == "pending"
    assert timeline[1]["type"] == "assign"
    assert timeline[1]["status"] == "assigned"
    assert timeline[2]["type"] == "status"
    assert timeline[2]["status"] == "processing"
    assert timeline[3]["type"] == "result"
    assert timeline[3]["status"] == "completed"
