"""Phase D 测试：双路径工具标准分支 / 审批桥 / 适配器标准模式。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import pytest

from teage_liu.multiagent.a2a_std.models import Message, Task, TaskState, TaskStatus, TextPart
from teage_liu.multiagent.a2a_std.task_manager import A2ATaskManager
from teage_liu.multiagent.a2a_std.task_store import TaskEventHub, TaskStore


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _FakeStdClient:
    """记录调用的 StdA2AClient 替身。"""

    def __init__(self, card_name: str = "teagent-server"):
        self.endpoints = ["server"]
        self._card_name = card_name
        self.sent: list[dict] = []
        self._terminal_task: Optional[dict] = None

    async def fetch_agent_card(self, endpoint_name: str) -> dict:
        return {
            "protocolVersion": "1.0",
            "name": self._card_name,
            "url": "http://server/a2a/std/jsonrpc",
        }

    async def message_send(self, endpoint_name, message: Message, context_id=None) -> dict:
        self.sent.append({
            "endpoint": endpoint_name,
            "message": message.model_dump(by_alias=True),
            "context_id": context_id,
        })
        task_id = f"t_std_{len(self.sent)}"
        self._terminal_task = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "completed"},
            "artifacts": [{
                "name": "response_1", "mimeType": "text/plain",
                "parts": [{"kind": "text", "text": "标准通道响应内容"}],
            }],
            "history": [],
        }
        return {"id": task_id, "status": {"state": "working"}}

    async def wait_for_terminal(self, endpoint_name, task_id, context_id=None, timeout=60.0):
        return self._terminal_task or {"id": task_id, "status": {"state": "completed"}, "artifacts": []}

    async def tasks_get(self, endpoint_name, task_id, context_id=None) -> dict:
        return self._terminal_task or {"id": task_id, "status": {"state": "working"}}


def _msc_cfg() -> dict:
    return {
        "enabled": True, "inject_prompt": True, "register_tools": True,
        "default_timeout": 60, "max_timeout": 300, "max_result_chars": 4000,
    }


class _Registry:
    def __init__(self):
        self.tools = {}

    def register_core(self, name, description, input_schema, handler, **kwargs):
        self.tools[name] = handler


class TestMainSessionCollabStandard:
    """主会话协作工具标准分支。"""

    def test_request_collaboration_standard(self, tmp_path):
        from teage_liu.agent.tools.main_session_collab_tools import (
            register_main_session_collab_tools,
        )

        registry = _Registry()
        std_client = _FakeStdClient(card_name="teagent-server")
        register_main_session_collab_tools(
            registry, tmp_path, "teagent-lu", None, _msc_cfg(), std_client=std_client,
        )
        handler = registry.tools["request_collaboration"]
        out = handler("teagent-server", "帮我查一下资料", 30)
        result = json.loads(out)
        assert result["ok"] is True
        assert result["via"] == "a2a-standard"
        assert result["summary"] == "标准通道响应内容"
        # message/send 被调用，携带 main_session 元数据
        sent = std_client.sent[-1]
        assert sent["context_id"].startswith("main_")
        assert sent["message"]["metadata"]["channel"] == "main_session"
        assert sent["message"]["metadata"]["to"] == "teagent-server"

    def test_list_collab_agents_standard_discovery(self, tmp_path):
        from teage_liu.agent.tools.main_session_collab_tools import (
            register_main_session_collab_tools,
        )

        registry = _Registry()
        std_client = _FakeStdClient(card_name="teagent-server")
        register_main_session_collab_tools(
            registry, tmp_path, "teagent-lu", None, _msc_cfg(), std_client=std_client,
        )
        out = registry.tools["list_collab_agents"]()
        result = json.loads(out)
        assert result["remote"]["server"]["agents"][0]["agent_id"] == "teagent-server"
        assert result["remote"]["server"]["agents"][0]["via"] == "a2a-card"

    def test_legacy_path_unchanged_without_std_client(self, tmp_path):
        """无 std_client 时保持 legacy 行为（无标准调用记录）。"""
        from teage_liu.agent.tools.main_session_collab_tools import (
            register_main_session_collab_tools,
        )

        registry = _Registry()
        register_main_session_collab_tools(
            registry, tmp_path, "teagent-lu", None, _msc_cfg(), std_client=None,
        )
        out = registry.tools["list_collab_agents"]()
        result = json.loads(out)
        assert result["remote"] == {}
        assert "via" not in json.loads(registry.tools["request_collaboration"](
            "teagent-server", "任务", 10,
        ))


class TestA2aToolsStandard:
    """worker 协作工具标准分支。"""

    def test_send_remote_message_standard(self, tmp_path):
        from teage_liu.agent.tools.a2a_tools import register_a2a_tools

        registry = _Registry()
        std_client = _FakeStdClient(card_name="teagent-server")
        register_a2a_tools(
            registry, tmp_path, None, "teagent-lu", std_client=std_client,
        )
        out = registry.tools["send_remote_message"]("teagent-server", "你好", "collab_1")
        result = json.loads(out)
        assert result["ok"] is True
        assert result["via"] == "a2a-standard"
        assert result["forwarded"][0]["via"] == "a2a-standard"
        sent = std_client.sent[-1]
        assert sent["context_id"] == "collab_1"
        assert sent["message"]["parts"][0]["text"] == "你好"


class TestApprovalBridge:
    """审批 → input-required 桥。"""

    def test_create_request_marks_task_input_required(self, tmp_path):
        import time

        from teage_liu.multiagent.a2a_std.approval_bridge import A2AApprovalBridge
        from teage_liu.multiagent.blackboard import read_collab_messages

        class _FakeApproval:
            def __init__(self):
                self.created = []

            def create_request(self, tool_name, tool_input, reason, risk_level, tool_kind="generic"):
                self.created.append(tool_name)
                return "approval_1"

        store = TaskStore(tmp_path / "tasks.json")
        manager = A2ATaskManager(store, TaskEventHub())
        approval = _FakeApproval()

        # 先创建一个 main_* 上下文的任务
        msg = Message(role="user", message_id="m_bridge", parts=[TextPart(text="审批任务")])
        task = _run(manager.create_task(msg, "main_abc"))

        bridge = A2AApprovalBridge(approval, manager, tmp_path)
        bridge.install()
        approval.create_request("write_file", {}, "危险操作", "high")

        # 后台线程异步推进，轮询等待
        deadline = time.time() + 5
        state = None
        while time.time() < deadline:
            got = _run(manager.get_task(task.id))
            state = got.status.state
            if state == TaskState.INPUT_REQUIRED:
                break
            time.sleep(0.1)
        assert state == TaskState.INPUT_REQUIRED

        # collab 标记
        msgs = _run(read_collab_messages(tmp_path, collab_id="main_abc"))
        assert any(m.get("status") == "input_required" for m in msgs)


class TestRemoteAgentAdapterStandard:
    """适配器标准模式：卡片发现，不注册/心跳。"""

    def test_standard_mode_uses_card_discovery(self, tmp_path):
        from teage_liu.multiagent.remote_agent_adapter import RemoteAgentAdapter

        config = {
            "a2a": {
                "remote_endpoints": [{"name": "server", "url": "http://server"}],
            },
            "multiagent": {"worker": {"agent_id": "teagent-lu"}},
        }
        adapter = RemoteAgentAdapter(
            tmp_path, config, "teagent-lu", standard_mode=True,
        )
        # 替换 std_client 为 fake（避免真实 HTTP）
        adapter._std_client = _FakeStdClient(card_name="teagent-server")
        adapter._a2a_client._endpoints = [{"name": "server", "url": "http://server"}]

        async def scenario():
            await adapter.start()
            return adapter._registered, adapter._heartbeat_task

        registered, hb_task = _run(scenario())
        assert "server" in registered  # 卡片发现成功
        assert hb_task is None  # 不启动心跳
        _run(adapter.stop())
