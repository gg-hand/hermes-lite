"""标准 A2A v1.0 合规测试。

合规主依据（双保险）：
1. 官方 a2a-sdk 互操作测试客户端——用官方 SDK 客户端打我们的端点，
   跨实现互操作是最强的合规证明（test-only 依赖，不进生产 requirements）。
2. @a2a-compliance/cli 加分项（社区工具非规范本身；npx 不可用时跳过）。

附：程序化规范清单（方法集 / 错误码 / 生命周期 / card 字段）。
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("a2a")  # a2a-sdk 未安装时跳过整个互操作模块

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from teage_liu.multiagent.a2a_std.router import create_a2a_std_router  # noqa: E402


def _config(bb_root: Path) -> dict:
    return {
        "a2a": {
            "enabled": True,
            "standard": {
                "enabled": True,
                "name": "teagent-lu",
                "description": "test agent",
                "capabilities": {"streaming": True, "push_notifications": False},
                "task_store_path": str(bb_root / "tasks" / "a2a" / "tasks.json"),
            },
        },
        "multiagent": {
            "role": "worker",
            "worker": {"agent_id": "teagent-lu"},
            "blackboard_dir": str(bb_root),
        },
        "security": {"api_key": ""},
        "server": {"port": 8000},
    }


@pytest.fixture
def a2a_server(tmp_path) -> Iterator[tuple[int, str, object]]:
    """起真实 uvicorn 服务，产出 (port, base_url, router)，teardown 关停。

    注：官方 SDK 1.1.2 的 JsonRpcTransport 解析端点 URL 后实际请求
    localhost:8000（默认端口），因此绑 127.0.0.1:8000 保证 SDK 请求命中；
    8000 被占用时跳过（SDK 路径不可测）。
    """
    bb_root = tmp_path / "bb"
    bb_root.mkdir(exist_ok=True)
    app = FastAPI()
    router = create_a2a_std_router(bb_root, _config(bb_root))
    app.include_router(router)

    port = 8000
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.skip("端口 8000 不可用，跳过 SDK 互操作测试")
    try:
        yield port, f"http://127.0.0.1:{port}", router
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class TestOfficialSdkInterop:
    """官方 a2a-sdk 客户端 ↔ 本项目标准端点。"""

    def test_sdk_client_full_flow(self, a2a_server):
        """官方客户端：卡片发现 → message/send → tasks/get → cancel。

        注：a2a-sdk 1.1.2 的 protobuf 方言与规范 JSON schema 有差异
        （Message.message_id / Part 无 kind 判别字段 / AgentCard 无 protocolVersion），
        card 断言用我们规范正确的原始 JSON；请求用 SDK 方言构造。
        """
        import asyncio

        from a2a.client import ClientConfig, create_client
        from a2a.types import (
            GetTaskRequest,
            Message,
            Part,
            Role,
            SendMessageRequest,
            TaskState,
        )
        from a2a.utils import TransportProtocol

        port, base, router = a2a_server
        adapter = router.a2a_std_engine_adapter
        manager = router.a2a_std_task_manager

        def _run(coro):
            return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)

        # 注：SDK httpx 客户端绑定事件循环，全流程必须跑在同一个 loop 里
        async def _scenario():
            # 显式声明仅支持 JSONRPC 绑定（避免 SDK 默认尝试 REST/gRPC）
            client_cfg = ClientConfig(
                supported_protocol_bindings=[TransportProtocol.JSONRPC.value],
            )
            client = await create_client(agent=base, client_config=client_cfg)
            try:
                # 1. Agent Card 发现
                card_json = (await client._httpx_client.get(
                    f"{base}/.well-known/agent-card.json")).json() if hasattr(client, "_httpx_client") else httpx.get(f"{base}/.well-known/agent-card.json").json()
                assert card_json["protocolVersion"] == "1.0"
                assert card_json["preferredTransport"] == "JSONRPC"
                assert card_json["url"].endswith("/a2a/std/jsonrpc")
                assert card_json["capabilities"]["streaming"] is True

                # 2. message/send（官方 SDK 方言构造请求 → 我们的端点，流式）
                msg = Message(
                    role=Role.ROLE_USER,
                    message_id="interop_1",
                    parts=[Part(text="official sdk interop")],
                )
                send_req = SendMessageRequest(message=msg)

                async def _send():
                    responses = []
                    async for resp in client.send_message(send_req):
                        responses.append(resp)
                    return responses

                async def _drive_to_terminal():
                    """流打开期间并发驱动任务到终态（stream 才会关闭）。"""
                    from teage_liu.multiagent.blackboard import append_collab_message

                    # 按 message_id 轮询等任务创建（避免竞态）
                    target = None
                    for _ in range(50):
                        tasks = await manager.list_tasks()
                        target = next((
                            t for t in tasks
                            if any(getattr(h, "message_id", None) == "interop_1"
                                   for h in t.history)
                        ), None)
                        if target is not None:
                            break
                        await asyncio.sleep(0.1)
                    assert target is not None, "SDK 创建的任务应可见"
                    await append_collab_message(
                        adapter._bb_root,
                        {"from": "teagent-lu", "to": "*", "type": "end",
                         "content": "end", "message_id": f"interop_end_{target.id}"},
                        collab_id=target.context_id,
                    )
                    await adapter.tick_once()

                send_task = asyncio.create_task(_send())
                await _drive_to_terminal()
                responses = await asyncio.wait_for(send_task, timeout=15)
                assert responses, "官方客户端应收到响应"
                task = None
                for r in responses:
                    t = r.task if hasattr(r, "task") else None
                    if t is not None and t.id:
                        task = t
                        break
                assert task is not None, "响应应包含 Task"
                assert task.id.startswith("t_")

                # 3. tasks/get（已驱动到终态）
                got = await client.get_task(GetTaskRequest(id=task.id))
                assert got.id == task.id
                assert got.status.state == TaskState.TASK_STATE_COMPLETED

                # 4a. 终态任务取消 → SDK 正确映射我们的 -32002 为 TaskNotCancelableError
                from a2a.utils.errors import TaskNotCancelableError as SdkTaskNotCancelableError

                try:
                    await client.cancel_task(GetTaskRequest(id=task.id))
                    assert False, "终态任务取消应被拒绝"
                except SdkTaskNotCancelableError:
                    pass  # 错误码互操作成立

                # 4b. 新任务（不驱动）→ 取消成功
                msg2 = Message(
                    role=Role.ROLE_USER,
                    message_id="interop_cancel",
                    parts=[Part(text="cancel me")],
                )
                async for r in client.send_message(SendMessageRequest(message=msg2)):
                    task2 = r.task if hasattr(r, "task") else None
                    if task2 is not None and task2.id:
                        break
                else:
                    task2 = None
                assert task2 is not None, "第二个任务应创建"
                cancelled = await client.cancel_task(GetTaskRequest(id=task2.id))
                assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
            finally:
                client.close()

        _run(_scenario())

    def test_sdk_schema_parses_our_wire(self, a2a_server):
        """官方 SDK 的 protobuf 模式能解析我们的线格式（结构合规）。"""
        from google.protobuf import json_format

        from a2a.types import AgentCard, Task

        port, base, _ = a2a_server

        # Agent Card 解析（SDK 1.1.2 protobuf 与规范 JSON schema 存在字段差异：
        # securitySchemes 建模为 map、无 protocolVersion——解析共用字段并保留
        # 我们规范正确的原始 JSON 断言）
        card_json = httpx.get(f"{base}/.well-known/agent-card.json").json()
        card_json.pop("securitySchemes", None)
        card = json_format.ParseDict(card_json, AgentCard(), ignore_unknown_fields=True)
        assert card.name == "teagent-lu"
        assert card_json["protocolVersion"] == "1.0"  # 线格式按规范 JSON schema

        # Task 解析（注：SDK protobuf 枚举名为 UPPER_SNAKE，规范线值为小写，
        # protobuf JSON 无法还原小写枚举——结构解析校验 id/contextId，
        # 状态值从原始 JSON 断言，线格式以规范为准）
        payload = {
            "jsonrpc": "2.0", "method": "message/send", "id": 1,
            "params": {"message": {
                "role": "user", "messageId": "interop_2",
                "parts": [{"kind": "text", "text": "hi"}],
            }},
        }
        r = httpx.post(f"{base}/a2a/std/jsonrpc", json=payload)
        task_json = r.json()["result"]
        task = json_format.ParseDict(task_json, Task(), ignore_unknown_fields=True)
        assert task.id.startswith("t_")
        assert task.context_id.startswith("a2a_")
        assert task_json["status"]["state"] == "working"  # 线值 = 规范小写
        assert task_json["id"].startswith("t_")


class TestComplianceCli:
    """@a2a-compliance/cli 加分项（社区工具；npx 不可用/无网络时跳过）。"""

    @pytest.mark.slow
    def test_compliance_cli(self, a2a_server):
        import subprocess
        import sys

        port, base, _ = a2a_server
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True, timeout=15,
            )
            npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
            result = subprocess.run(
                [npx_cmd, "--yes", "@a2a-compliance/cli", "card", base],
                capture_output=True, text=True, timeout=180,
                encoding="utf-8", errors="replace",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            pytest.skip(f"npx 不可用或超时: {e}")
        if result.returncode != 0 and "network" in (result.stderr or "").lower():
            pytest.skip("网络受限，跳过合规 CLI")
        assert result.returncode == 0, f"合规 CLI 失败:\n{result.stdout}\n{result.stderr}"


class TestSpecChecklist:
    """程序化规范清单。"""

    def test_full_standard_method_set_registered(self, a2a_server):
        """全部标准方法已实现（调用不返回 -32601）。"""
        port, base, _ = a2a_server
        required = {
            "message/send", "message/stream", "tasks/get", "tasks/list",
            "tasks/cancel", "tasks/resubscribe",
            "tasks/pushNotificationConfig/set", "tasks/pushNotificationConfig/get",
            "tasks/pushNotificationConfig/list", "tasks/pushNotificationConfig/delete",
            "agent/getAuthenticatedExtendedCard",
        }
        for method in sorted(required):
            r = httpx.post(
                f"{base}/a2a/std/jsonrpc",
                json={"jsonrpc": "2.0", "method": method, "params": {}, "id": 1},
            )
            body = r.json()
            if "error" in body:
                assert body["error"]["code"] != -32601, f"{method} 未实现"

    def test_standard_error_codes(self):
        """A2A 标准库错误码映射。"""
        from teage_liu.multiagent.a2a_std import exceptions as exc

        assert exc.TaskNotFoundError().code == -32001
        assert exc.TaskNotCancelableError().code == -32002
        assert exc.PushNotificationNotSupportedError().code == -32003
        assert exc.UnsupportedOperationError().code == -32004
        assert exc.ContentTypeNotSupportedError().code == -32005
        assert exc.MessageExceptionError().code == -32006

    def test_task_state_wire_values(self):
        """TaskState 线值与终态语义。"""
        from teage_liu.multiagent.a2a_std.models import TaskState

        assert TaskState.SUBMITTED.value == "submitted"
        assert TaskState.WORKING.value == "working"
        assert TaskState.INPUT_REQUIRED.value == "input-required"
        assert TaskState.AUTH_REQUIRED.value == "auth-required"
        assert TaskState.COMPLETED.value == "completed"
        assert TaskState.FAILED.value == "failed"
        assert TaskState.CANCELED.value == "canceled"
        assert TaskState.REJECTED.value == "rejected"
        for s in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED):
            assert s.is_terminal
