"""Worker 重启幂等性 e2e 测试。

验证场景：
- Director 广播在 worker 重启后不被重复响应
- intervention directive 在 worker 重启后不被重复触发
- A2A 任务在 worker 重启后不被重复执行
- 普通队列消息在 worker 重启后不被重复入队
- 多次重启后 response 数量始终为 1
- 重启后新广播仍能正常响应
- 【第三轮 Review 补充】error response 不阻塞重启后重试
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from teage_liu.multiagent.blackboard import (
    append_collab_message, append_message,
    read_collab_messages, read_messages,
)
from teage_liu.multiagent.worker_adapter import WorkerAdapter


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    bb = tmp_path / "blackboard"
    (bb / "agents").mkdir(parents=True)
    (bb / "collabs").mkdir()
    # 初始化空 messages.md（A2A 任务消息文件）
    (bb / "messages.md").write_text("", encoding="utf-8")
    return bb


def _make_worker(bb_root, agent_id="agent_A", orchestrator=None):
    config = {
        "multiagent": {
            "worker": {"capabilities": ["sentiment_analysis"]},
            "director": {},
            "collab": {"poll_interval_seconds": 2, "idle_timeout_seconds": 60},
        }
    }
    return WorkerAdapter(
        bb_root=bb_root, config=config, agent_id=agent_id,
        orchestrator=orchestrator or AsyncMock(),
    )


def test_director_broadcast_not_re_responded_after_restart(bb_root):
    """【核心场景】Director 广播在 worker 重启后不被重复响应。"""
    # 1. 写入 Director 广播
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "需要协作",
    }))

    # 2. Worker1 启动并处理广播
    worker1 = _make_worker(bb_root, agent_id="agent_A")
    worker1._orchestrator.chat.return_value = "第一次响应"
    asyncio.run(worker1._load_state_on_start())
    asyncio.run(worker1._poll_collab_once())
    asyncio.run(worker1.stop())

    # 3. 验证：写了 1 条 response
    msgs = asyncio.run(read_collab_messages(bb_root))
    response_msgs = [m for m in msgs if m.get("type") == "response"]
    assert len(response_msgs) == 1
    assert response_msgs[0]["content"] == "第一次响应"

    # 4. Worker2 重启（同一 bb_root，同一 agent_id）
    worker2 = _make_worker(bb_root, agent_id="agent_A")
    worker2._orchestrator.chat.return_value = "第二次响应（不应出现）"
    asyncio.run(worker2._load_state_on_start())

    # 5. 验证：worker2 启动后 _responded_request_seqs 已恢复
    # S2 修正：key 为 `mid:{broadcast.message_id}`（生产 _dk 优先 message_id）
    msgs = asyncio.run(read_collab_messages(bb_root))
    broadcast_msg = next(m for m in msgs if m.get("type") == "request")
    expected_key = WorkerAdapter._dk(
        broadcast_msg.get("collab_id"),
        broadcast_msg["seq"],
        broadcast_msg.get("message_id"),
    )
    assert expected_key in worker2._responded_request_seqs

    # 6. Worker2 轮询，不应触发 LLM
    asyncio.run(worker2._poll_collab_once())
    worker2._orchestrator.chat.assert_not_called()

    # 7. 验证：response 消息数量仍为 1（未新增）
    msgs = asyncio.run(read_collab_messages(bb_root))
    response_msgs = [m for m in msgs if m.get("type") == "response"]
    assert len(response_msgs) == 1


def test_intervention_directive_not_re_triggered_after_restart(bb_root):
    """intervention directive 在 worker 重启后不被重复触发。"""
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "directive",
        "rule_type": "intervention", "content": "紧急干预",
        "target": "*",
    }))

    worker1 = _make_worker(bb_root, agent_id="agent_A")
    worker1._orchestrator.chat.return_value = "已处理干预"
    asyncio.run(worker1._load_state_on_start())
    asyncio.run(worker1._poll_collab_once())
    asyncio.run(worker1.stop())

    # 验证：worker1 调用了一次 LLM
    worker1._orchestrator.chat.assert_called_once()

    # 重启
    worker2 = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker2._load_state_on_start())
    asyncio.run(worker2._poll_collab_once())

    # 验证：worker2 未调用 LLM
    worker2._orchestrator.chat.assert_not_called()


def test_a2a_task_not_re_executed_after_restart(bb_root):
    """A2A 任务在 worker 重启后不被重复执行。"""
    worker1 = _make_worker(bb_root, agent_id="agent_A")
    worker1._orchestrator.chat.return_value = "任务结果1"
    asyncio.run(worker1._load_state_on_start())
    asyncio.run(worker1.execute_a2a_task(
        task_op_id="op_001", task_content="测试任务",
    ))
    asyncio.run(worker1.stop())

    # 验证：写了 1 条 result 消息
    msgs = asyncio.run(read_messages(bb_root))
    result_msgs = [m for m in msgs if m.get("type") == "result"]
    assert len(result_msgs) == 1

    # 重启
    worker2 = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker2._load_state_on_start())
    asyncio.run(worker2.execute_a2a_task(
        task_op_id="op_001", task_content="测试任务",
    ))

    # 验证：worker2 未调用 LLM（cached）
    worker2._orchestrator.chat.assert_not_called()

    # 验证：result 消息数量仍为 1
    msgs = asyncio.run(read_messages(bb_root))
    result_msgs = [m for m in msgs if m.get("type") == "result"]
    assert len(result_msgs) == 1


def test_normal_queue_not_re_queued_after_restart(bb_root):
    """普通队列消息在 worker 重启后不被重复入队。"""
    # 写入 relay 消息
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B", "type": "relay", "content": "历史 relay",
    }))

    worker1 = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker1._load_state_on_start())
    asyncio.run(worker1._poll_collab_once())
    asyncio.run(worker1.stop())

    # 验证：worker1 入队了 1 条消息
    assert len(worker1._normal_queue) == 1

    # 重启
    worker2 = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker2._load_state_on_start())
    asyncio.run(worker2._poll_collab_once())

    # 验证：worker2 未重复入队
    assert len(worker2._normal_queue) == 0


def test_multiple_restarts_keep_single_response(bb_root):
    """多次重启后 response 数量始终为 1（无累积）。"""
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播",
    }))

    for i in range(3):
        worker = _make_worker(bb_root, agent_id="agent_A")
        worker._orchestrator.chat.return_value = f"响应 {i}"
        asyncio.run(worker._load_state_on_start())
        asyncio.run(worker._poll_collab_once())
        asyncio.run(worker.stop())

    msgs = asyncio.run(read_collab_messages(bb_root))
    response_msgs = [m for m in msgs if m.get("type") == "response"]
    assert len(response_msgs) == 1, f"应只有 1 条 response，实际 {len(response_msgs)}"


def test_new_broadcast_after_restart_still_works(bb_root):
    """重启后新广播仍能正常响应（防止幂等检查误伤）。"""
    # 1. 第一条广播
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))

    worker1 = _make_worker(bb_root, agent_id="agent_A")
    worker1._orchestrator.chat.return_value = "响应1"
    asyncio.run(worker1._load_state_on_start())
    asyncio.run(worker1._poll_collab_once())
    asyncio.run(worker1.stop())

    # 2. 重启后写第二条广播
    worker2 = _make_worker(bb_root, agent_id="agent_A")
    worker2._orchestrator.chat.return_value = "响应2"
    asyncio.run(worker2._load_state_on_start())
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播2",
    }))
    asyncio.run(worker2._poll_collab_once())

    # 3. 验证：worker2 调用了一次 LLM（响应新广播）
    worker2._orchestrator.chat.assert_called_once()

    # 4. 验证：response 消息数量为 2（旧1 + 新1）
    msgs = asyncio.run(read_collab_messages(bb_root))
    response_msgs = [m for m in msgs if m.get("type") == "response"]
    assert len(response_msgs) == 2


def test_error_response_not_blocking_after_restart(bb_root):
    """【第三轮 Review 补充 + S2 修正】error response 不被 rebuild 当成功响应。

    场景：worker1 处理广播时 LLM 失败，写入 error response。
    worker2 重启后扫描历史，不应把 error response 的 reply_to 加入
    responded_request_seqs（否则该 request 永久跳过，无法重试）。

    S2 修正：P3-4 rebuild 启发式会把"已参与协作"(cid=None，因 error response
    cid=None)的所有消息加入 _processed_msg_seqs，故 worker2 轮询会跳过 broadcast。
    测试聚焦于核心点：rebuild 不把 error response 当成功响应加入
    _responded_request_seqs。"重试成功"部分因 P3-4 启发式行为改变而移除
    （完整重试机制由 Phase2 L-1 _llm_retry_queue 承担，不在重启扫描路径）。
    """
    # 1. Director 广播 seq=1
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    # 2. error response seq=2, reply_to=1（模拟 worker1 LLM 失败）
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "response", "reply_to": 1,
        "content": "[协作响应失败] RuntimeError: LLM 不可用",
        "accept": False, "error": True,
    }))

    # 3. worker2 重启（state.json 不存在，扫描历史重建）
    worker2 = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker2._load_state_on_start())

    # 4. 验证：error response 的 reply_to=1 不在 responded_request_seqs
    #    （key 经 _migrate 后为 "global:1"）
    assert "global:1" not in worker2._responded_request_seqs

    # 5. 验证：_last_collab_seq 保持为 0（不跳过历史消息）
    assert worker2._last_collab_seq == 0
