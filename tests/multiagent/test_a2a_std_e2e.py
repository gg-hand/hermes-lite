"""标准 A2A e2e：两个真实 uvicorn 实例经 StdA2AClient 跨机协作。"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from teage_liu.multiagent.a2a_std.client import StdA2AClient
from teage_liu.multiagent.a2a_std.models import Message, TextPart
from teage_liu.multiagent.a2a_std.router import create_a2a_std_router
from teage_liu.multiagent.blackboard import append_collab_message


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_config(bb_root: Path, port: int, peer_port: int, agent_id: str) -> dict:
    return {
        "a2a": {
            "enabled": True,
            "remote_endpoints": [
                {"name": "peer", "url": f"http://127.0.0.1:{peer_port}"},
            ],
            "timeout_seconds": 5,
            "retry_count": 1,
            "standard": {
                "enabled": True,
                "name": agent_id,
                "base_url": f"http://127.0.0.1:{port}",
                "capabilities": {"streaming": True, "push_notifications": False},
                "task_store_path": str(bb_root / "tasks.json"),
            },
        },
        "multiagent": {
            "role": "worker",
            "worker": {"agent_id": agent_id},
            "blackboard_dir": str(bb_root),
        },
        "security": {"api_key": ""},
        "server": {"port": port},
    }


class _Server:
    def __init__(self, bb_root: Path, config: dict):
        self.bb_root = bb_root
        self.config = config
        self.app = FastAPI()
        self.router = create_a2a_std_router(bb_root, config)
        self.app.include_router(self.router)
        self.port = config["server"]["port"]
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning",
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        assert self.server.started, f"服务 {self.port} 未启动"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def dual_servers(tmp_path):
    port_a = _free_port()
    port_b = _free_port()
    while port_b == port_a:
        port_b = _free_port()
    bb_a = tmp_path / "bb_a"
    bb_b = tmp_path / "bb_b"
    bb_a.mkdir(exist_ok=True)
    bb_b.mkdir(exist_ok=True)
    server_a = _Server(bb_a, _make_config(bb_a, port_a, port_b, "teagent-lu"))
    server_b = _Server(bb_b, _make_config(bb_b, port_b, port_a, "teagent-server"))
    try:
        yield server_a, server_b
    finally:
        server_a.stop()
        server_b.stop()


class TestDualInstance:
    """双实例标准 A2A 协作。"""

    @pytest.mark.e2e
    def test_cross_instance_task_lifecycle(self, dual_servers):
        server_a, server_b = dual_servers

        # A 的客户端 → B 的端点
        client = StdA2AClient(server_a.config)

        # 1. Agent Card 发现
        card = _run(client.fetch_agent_card("peer"))
        assert card["name"] == "teagent-server"

        # 2. message/send（A → B）
        msg = Message(
            role="user", message_id="e2e_1",
            parts=[TextPart(text="跨机协作任务")],
            metadata={"from": "teagent-lu", "to": "teagent-server", "type": "request"},
        )
        task = _run(client.message_send("peer", msg, context_id="e2e_collab"))
        task_id = task["id"]
        assert task["status"]["state"] == "working"

        # 3. B 侧引擎推进完成（同进程直连 B 的 adapter）
        async def drive_b():
            await append_collab_message(
                server_b.bb_root,
                {"from": "teagent-server", "to": "teagent-lu", "type": "response",
                 "content": "B 的回复内容", "message_id": "e2e_resp"},
                collab_id="e2e_collab",
            )
            await append_collab_message(
                server_b.bb_root,
                {"from": "director", "to": "*", "type": "end",
                 "content": "end", "message_id": "e2e_end"},
                collab_id="e2e_collab",
            )
            await server_b.router.a2a_std_engine_adapter.tick_once()
        _run(drive_b())

        # 4. wait_for_terminal（A 轮询 tasks/get 直到终态）
        terminal = _run(client.wait_for_terminal("peer", task_id, context_id="e2e_collab", timeout=15))
        assert terminal["status"]["state"] == "completed"
        artifacts = terminal.get("artifacts") or []
        assert any(a.get("parts") and a["parts"][0].get("text") == "B 的回复内容"
                   for a in artifacts)

        # 5. tasks/list 可见
        tasks = _run(client.tasks_list("peer"))
        assert any(t["id"] == task_id for t in tasks)

        _run(client.close())

    @pytest.mark.e2e
    def test_bidirectional_discovery(self, dual_servers):
        """双向发现：A 的客户端能看到 B，B 的客户端能看到 A。"""
        server_a, server_b = dual_servers
        client_a = StdA2AClient(server_a.config)
        client_b = StdA2AClient(server_b.config)

        card_b = _run(client_a.fetch_agent_card("peer"))
        card_a = _run(client_b.fetch_agent_card("peer"))
        assert card_b["name"] == "teagent-server"
        assert card_a["name"] == "teagent-lu"

        _run(client_a.close())
        _run(client_b.close())
