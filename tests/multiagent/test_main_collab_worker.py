"""接收端主会话协作处理测试（worker_adapter 的 main-collab 通道）。

覆盖：
- channel=main_session 的 request 走 _handle_main_collab_message（不进 worker 路由）
- request 触发独立 main-collab LLM（session=main_collab_{agent_id} + 服务性上下文）
- 响应写回 channel=main_session + to=from（请求方）
- from==self 的请求跳过（不响应自己的请求）
- end 消息触发归档
"""
from __future__ import annotations

from pathlib import Path

import pytest

from teage_liu.multiagent.worker_adapter import WorkerAdapter


class FakeOrchestrator:
    def __init__(self):
        self.calls = []

    async def chat(self, session_id, user_input, **kwargs):
        self.calls.append((session_id, user_input, kwargs))
        return "总结完成：项目是 FastAPI 多 Agent 服务"


class FakeA2A:
    def __init__(self):
        self.calls = []

    async def call_all_endpoints(self, method, params):
        self.calls.append((method, params))
        return {"worker-1": {"ok": True, "seq": 1}}


def _make_config(agent_id="teagent-lu"):
    return {
        "multiagent": {
            "enabled": True,
            "blackboard_dir": str(Path("/tmp/bb")),
            "worker": {
                "agent_id": agent_id,
                "heartbeat_interval_seconds": 10,
                "persist_state": False,
                "director_v2_enabled": True,
            },
            "collab": {"poll_interval_seconds": 1, "idle_timeout_seconds": 60},
            "director": {"heartbeat_timeout_seconds": 30},
        }
    }


def _make_adapter(bb_root: Path, orch=None, a2a=None):
    adapter = WorkerAdapter(
        bb_root,
        _make_config(),
        agent_id="teagent-lu",
        orchestrator=orch or FakeOrchestrator(),
        a2a_client=a2a or FakeA2A(),
    )
    return adapter


@pytest.mark.asyncio
async def test_main_collab_request_triggers_independent_llm(bb_root):
    """channel=main_session request → 独立 main-collab LLM + 响应写回。"""
    orch = FakeOrchestrator()
    a2a = FakeA2A()
    adapter = _make_adapter(bb_root, orch, a2a)
    cid = "main_test001"

    await adapter._handle_collab_message({
        "channel": "main_session",
        "type": "request",
        "from": "teagent-liu-2",
        "to": "teagent-lu",
        "content": "请总结项目结构",
        "collab_id": cid,
        "message_id": "main_msg_100",
        "seq": 1,
    })

    # 独立会话 + 服务性响应上下文
    assert orch.calls, "应触发 main-collab LLM"
    session_id, prompt, kwargs = orch.calls[0]
    assert session_id == "main_collab_teagent-lu"
    assert "协作请求" in prompt
    assert kwargs.get("system_prompt_override") is not None
    assert "服务" in kwargs["system_prompt_override"] or "协作请求" in kwargs["system_prompt_override"]

    # 响应写回 collabs/main_{cid}.md（channel=main_session, to=请求方）
    from teage_liu.multiagent.blackboard import read_collab_messages

    msgs = await read_collab_messages(bb_root, collab_id=cid)
    resp = [m for m in msgs if m.get("type") == "response"]
    assert len(resp) == 1
    assert resp[0]["channel"] == "main_session"
    assert resp[0]["from"] == "teagent-lu"
    assert resp[0]["to"] == "teagent-liu-2"
    assert "总结完成" in resp[0]["content"]

    # A2A 回传
    assert a2a.calls and a2a.calls[0][0] == "collab_message"


@pytest.mark.asyncio
async def test_main_collab_self_request_skipped(bb_root):
    """from==self 的 request 跳过（不响应自己的请求）。"""
    orch = FakeOrchestrator()
    adapter = _make_adapter(bb_root, orch)
    await adapter._handle_collab_message({
        "channel": "main_session",
        "type": "request",
        "from": "teagent-lu",  # 自己
        "to": "teagent-liu-2",
        "content": "自己的请求",
        "collab_id": "main_self001",
        "message_id": "main_msg_101",
        "seq": 1,
    })
    assert orch.calls == []


@pytest.mark.asyncio
async def test_main_collab_end_archives(bb_root):
    """end 消息触发归档。"""
    orch = FakeOrchestrator()
    adapter = _make_adapter(bb_root, orch)
    cid = "main_end001"
    # 先写 request 建 index（通过 _trigger 或直接 append）
    from teage_liu.multiagent.blackboard import append_collab_message

    await append_collab_message(bb_root, {
        "channel": "main_session", "type": "request",
        "from": "teagent-liu-2", "to": "teagent-lu",
        "content": "发起", "collab_id": cid,
        "message_id": "main_msg_102",
    }, collab_id=cid)

    await adapter._handle_collab_message({
        "channel": "main_session",
        "type": "end",
        "from": "teagent-lu",
        "to": "teagent-liu-2",
        "content": "协作完成",
        "collab_id": cid,
        "message_id": "main_msg_103",
        "seq": 2,
    })
    from teage_liu.multiagent.blackboard import read_collab_index

    entries = await read_collab_index(bb_root)
    entry = next((e for e in entries if e.get("collab_id") == cid), None)
    assert entry is not None
    assert entry.get("status") == "archived"


@pytest.mark.asyncio
async def test_worker_collab_not_routed_to_main_handler(bb_root):
    """无 channel（worker 协作）的 request 不进 main-collab 通道。"""
    orch = FakeOrchestrator()
    adapter = _make_adapter(bb_root, orch)
    # worker 协作 request（无 channel）→ 走 _handle_request（普通队列/搭便车），
    # 不触发 main-collab 独立会话
    await adapter._handle_collab_message({
        "type": "request",
        "from": "teagent-liu-2",
        "to": "teagent-lu",
        "content": "worker 协作请求",
        "collab_id": "collab_worker001",
        "message_id": "worker_msg_1",
        "seq": 1,
    })
    # main-collab 会话未被调用（worker 请求可能入普通队列而非立即触发）
    assert not any(c[0] == "main_collab_teagent-lu" for c in orch.calls)
