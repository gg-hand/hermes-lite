"""Worker 适配器单元测试（Task 2）。

覆盖：
- Worker 注册流程（写 agent_card + audit）
- 心跳循环（更新 last_heartbeat）
- 优雅退出（释放锁 + 更新状态 + audit）
- 自治模式（Director 超时进入 / Director 恢复退出 / 再次崩溃回滚）
- 自治期拒绝 Director 写入
- 自治模式时间片轮转
- 轮次校验（freeform 不阻断 / 非本机轮次写 pending + 抛 NotMyTurnError）
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from watchdog.events import FileModifiedEvent

from teage_liu.multiagent.blackboard import (
    Blackboard,
    atomic_write,
    cas_write_status,
    read_audit_records,
    read_json,
    read_yaml_frontmatter,
)
from teage_liu.multiagent.collab_sanitize import SANITIZED_PLACEHOLDER, sanitize_collab_content
from teage_liu.multiagent.exceptions import NotMyTurnError
from teage_liu.multiagent.worker_adapter import AutonomousModeController, WorkerAdapter


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def worker_config() -> dict:
    return {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": "/tmp/bb",
            "worker": {
                "agent_id": "worker_001",
                "heartbeat_interval_seconds": 10,
                "capabilities": ["file_read", "file_write", "web_search"],
                "dangerous_tools": ["execute_command", "write_file", "call_tool"],
            },
            "director": {
                "heartbeat_timeout_seconds": 30,
                "degraded_threshold_seconds": 20,
            },
        }
    }


def _make_worker_card(agent_id: str = "worker_001") -> dict:
    """构造 worker agent_card 字典。"""
    return {
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "protocol_version": "1.0.0",
        "created_at": "2026-07-20T09:55:00Z",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "heartbeat_interval_seconds": 10,
        "status": "active",
        "role": "worker",
        "endpoint": "http://localhost:8000",
        "owner": "user_a",
        "capabilities": ["file_read"],
        "specialties": [],
        "auth_method": "local",
        "trust_score": 100,
        "trust_history": [],
        "extensions": {},
        "leave_reason": "",
        "left_at": "",
    }


class FakeOrchestrator:
    """模拟 Orchestrator 用于测试。"""
    def __init__(self):
        self.calls = []

    async def chat(self, session_id: str, user_input: str, **kwargs) -> str:
        self.calls.append((session_id, user_input))
        return f"已处理: {user_input}"


class TestWorkerRegistration:
    """Worker 注册流程测试。"""

    @pytest.mark.asyncio
    async def test_worker_register_writes_agent_card(self, bb_root: Path, worker_config):
        """Worker 注册时写入 agents/{id}.md。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        card_path = bb_root / "agents" / "worker_001.md"
        assert card_path.exists()

        card, _ = read_yaml_frontmatter(card_path)
        assert card["agent_id"] == "worker_001"
        assert card["role"] == "worker"
        assert card["status"] == "active"
        assert "file_read" in card["capabilities"]

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_worker_register_appends_audit(self, bb_root: Path, worker_config):
        """注册时追加 audit 记录。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        records = await read_audit_records(bb_root, limit=200)
        register_audits = [r for r in records if r.get("action") == "register"]
        assert len(register_audits) >= 1
        assert register_audits[-1]["actor"] == "worker_001"

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_worker_heartbeat_loop_updates_last_heartbeat(self, bb_root: Path, worker_config):
        """心跳循环更新 last_heartbeat 字段。"""
        worker_config["multiagent"]["worker"]["heartbeat_interval_seconds"] = 0.1
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 等待 2 次心跳
        await asyncio.sleep(0.25)

        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        agents = await registry.list_active_agents()
        worker = next(a for a in agents if a["agent_id"] == "worker_001")
        heartbeat_time = datetime.fromisoformat(
            worker["last_heartbeat"].replace("Z", "+00:00")
        )
        age = datetime.now(timezone.utc) - heartbeat_time
        assert age < timedelta(seconds=1)

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_worker_graceful_leave(self, bb_root: Path, worker_config):
        """优雅退出：释放锁 + 更新状态 + audit。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 获取一个锁
        from teage_liu.multiagent.file_lock import LockManager

        lock_manager = LockManager(bb_root, agent_id="worker_001")
        await lock_manager.acquire("messages", holder="worker_001", ttl_seconds=30)

        await adapter.stop()  # 优雅退出

        # 锁应已释放
        status = read_json(bb_root / "status.json")
        assert "messages" not in status.get("locks", {}) or (
            status["locks"]["messages"].get("holder") != "worker_001"
        )

        # agent_card.status 应为 offline
        card, _ = read_yaml_frontmatter(bb_root / "agents" / "worker_001.md")
        assert card["status"] == "offline"

        # audit 应有 leave 记录
        records = await read_audit_records(bb_root, limit=200)
        leave_audits = [r for r in records if r.get("action") == "leave"]
        assert len(leave_audits) >= 1


class TestAutonomousMode:
    """自治模式测试。"""

    @pytest.mark.asyncio
    async def test_enter_autonomous_mode_on_director_timeout(self, bb_root: Path, worker_config):
        """Director 心跳超时 → Worker 进入自治模式。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 模拟 Director 心跳超时
        stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            f'last_director_tick: "{stale_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        # 触发心跳检测
        await adapter._check_director_health()

        assert adapter._autonomous_mode is True
        assert adapter._autonomous_epoch == 1

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_mode_rejects_director_writes(self, bb_root: Path, worker_config):
        """自治期 Director 写入被拒绝。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 进入自治模式
        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 模拟 Director 写入（epoch=1，与自治期相同）
        status = adapter._read_status()
        status["current_turn"] = {"agent_id": "worker_002", "epoch": 1}

        # 应被拒绝
        result = await adapter._validate_director_write(status, writer_epoch=1)
        assert result is False

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_exit_autonomous_mode_on_director_recovery(self, bb_root: Path, worker_config):
        """Director 恢复 → Worker 退出自治模式。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 进入自治模式
        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 模拟 Director 恢复（epoch 递增 + tick 新鲜）
        fresh_tick = datetime.now(timezone.utc).isoformat()
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 2\n"
            f'last_director_tick: "{fresh_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        await adapter._check_director_recovery()

        assert adapter._autonomous_mode is False

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_exit_rollback_on_director_re_crash(self, bb_root: Path, worker_config):
        """自治退出时 Director 再次崩溃 → 回滚自治。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 第一次检测：Director 恢复（新鲜 tick + epoch 递增）
        fresh_tick = datetime.now(timezone.utc).isoformat()
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 2\n"
            f'last_director_tick: "{fresh_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        # 在退出过程中，Director 再次崩溃（写入过期 tick）
        stale_tick = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 2\n"
            f'last_director_tick: "{stale_tick}"\n'
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        await adapter._check_director_recovery()

        # 应回滚为自治模式
        assert adapter._autonomous_mode is True
        assert adapter._autonomous_epoch == 1

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_autonomous_mode_time_slicing(self, bb_root: Path, worker_config):
        """自治模式时间片轮转（agent_id 字典序，每片 30 秒）。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # adapter.start() 已注册 worker_001，补充注册 worker_002 / worker_003
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register(_make_worker_card("worker_002"))
        await registry.register(_make_worker_card("worker_003"))

        adapter._autonomous_mode = True
        adapter._autonomous_epoch = 1

        # 进入自治模式后，current_turn 应按字典序轮转
        current = await adapter._get_autonomous_current_turn()
        assert current in ["worker_001", "worker_002", "worker_003"]

        await adapter.stop()


class TestWorkerTurnCheck:
    """Worker 轮次校验测试。"""

    @pytest.mark.asyncio
    async def test_before_speak_freeform_mode_no_block(self, bb_root: Path, worker_config):
        """freeform 模式不阻断。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 设置 freeform 模式
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            'last_director_tick: "2026-07-21T10:00:00+00:00"\n'
            "turn_policy:\n  mode: freeform\n"
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        # 非本机轮次也应通过
        message = {"content": "hello", "from": "worker_001"}
        await adapter._before_speak("worker_001", message)  # 不抛异常

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_before_speak_not_my_turn_writes_pending(self, bb_root: Path, worker_config):
        """非本机轮次 → 写 messages.pending.md + 抛 NotMyTurnError。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        # 设置 round_robin 模式，当前轮次是 worker_002
        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            'last_director_tick: "2026-07-21T10:00:00+00:00"\n'
            "turn_policy:\n  mode: round_robin\n"
            "  order: ['worker_001', 'worker_002']\n"
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        status = read_json(bb_root / "status.json")
        status["current_turn"] = {
            "agent_id": "worker_002",
            "started_at": "2026-07-21T10:00:00+00:00",
            "epoch": 1,
        }
        await cas_write_status(
            bb_root, status.get("version", 0), status, writer_signature="director"
        )

        message = {"content": "hello", "from": "worker_001"}
        with pytest.raises(NotMyTurnError):
            await adapter._before_speak("worker_001", message)

        # 消息应写入 pending
        pending_path = bb_root / "messages.pending.md"
        assert pending_path.exists()
        pending_content = pending_path.read_text(encoding="utf-8")
        assert "hello" in pending_content

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_before_speak_my_turn_passes(self, bb_root: Path, worker_config):
        """本机轮次 → 通过。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        await adapter.start()

        director_md_path = bb_root / "director.md"
        content = (
            "---\n"
            "director_id: director_001\n"
            "current_epoch: 1\n"
            'last_director_tick: "2026-07-21T10:00:00+00:00"\n'
            "turn_policy:\n  mode: round_robin\n"
            "  order: ['worker_001', 'worker_002']\n"
            "heartbeat:\n  interval_seconds: 10\n  timeout_seconds: 30\n"
            "---\n\n# Director Protocol\n"
        )
        await atomic_write(director_md_path, content)

        status = read_json(bb_root / "status.json")
        status["current_turn"] = {
            "agent_id": "worker_001",
            "started_at": "2026-07-21T10:00:00+00:00",
            "epoch": 1,
        }
        await cas_write_status(
            bb_root, status.get("version", 0), status, writer_signature="director"
        )

        message = {"content": "hello", "from": "worker_001"}
        await adapter._before_speak("worker_001", message)  # 不抛异常

        await adapter.stop()


@pytest.mark.asyncio
async def test_worker_injects_orchestrator_and_activates(bb_root, worker_config):
    """WorkerAdapter 接受 orchestrator 参数，注册后状态推进到 active。"""
    from teage_liu.multiagent.blackboard import Blackboard
    from teage_liu.multiagent.worker_adapter import WorkerAdapter

    bb = Blackboard(bb_root)
    await bb.init_blackboard()

    fake_orch = FakeOrchestrator()
    worker = WorkerAdapter(
        bb_root=bb_root, config=worker_config,
        agent_id="worker_001", orchestrator=fake_orch,
    )
    await worker.start()
    try:
        assert worker._orchestrator is fake_orch

        # 验证 agent 状态为 active
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        agents = await registry.list_active_agents()
        worker_agents = [a for a in agents if a.get("agent_id") == "worker_001"]
        assert len(worker_agents) == 1
        assert worker_agents[0]["status"] == "active"
    finally:
        await worker.stop()


class FakeOrchestratorCapturing:
    """捕获 chat() 调用参数的 FakeOrchestrator。"""
    def __init__(self, response: str = "协作响应内容"):
        self.response = response
        self.captured_calls = []

    async def chat(self, session_id: str, user_input: str, **kwargs) -> str:
        self.captured_calls.append({
            "session_id": session_id,
            "user_input": user_input,
            "kwargs": kwargs,
        })
        return self.response


class TestUrgentLLMCallSignature:
    """Task 2: 验证 _trigger_urgent_llm 正确调用 Orchestrator.chat()。"""

    @pytest.mark.asyncio
    async def test_trigger_urgent_llm_uses_correct_signature(
        self, bb_root: Path, worker_config
    ):
        """_trigger_urgent_llm 用 multiagent_{agent_id} 作为 session_id，
        传 extra_system_prompt= 而非 system_prompt=。"""
        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )

        context_msg = {
            "from": "director",
            "type": "request",
            "content": "ciallo~",
            "seq": 5,
        }
        await adapter._trigger_urgent_llm(
            prompt="Director 广播协作请求：ciallo~",
            context_msg=context_msg,
        )

        assert len(fake_orch.captured_calls) == 1, "应调用 chat() 一次"
        call = fake_orch.captured_calls[0]
        assert call["session_id"] == "multiagent_worker_001", \
            f"session_id 应为 multiagent_worker_001，实际: {call['session_id']}"
        assert call["user_input"] == "Director 广播协作请求：ciallo~"
        assert "extra_system_prompt" in call["kwargs"], "应传 extra_system_prompt 参数"
        assert call["kwargs"]["extra_system_prompt"], "extra_system_prompt 不应为空"
        assert "system_prompt" not in call["kwargs"], \
            "不应传 system_prompt（旧 Bug 参数）"

    @pytest.mark.asyncio
    async def test_trigger_urgent_llm_writes_response_for_request(
        self, bb_root: Path, worker_config
    ):
        """request 类型消息触发后写 response 消息到 collaboration.md。"""
        from teage_liu.multiagent.blackboard import read_collab_messages

        fake_orch = FakeOrchestratorCapturing(response="已收到广播，准备参与协作")
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )

        context_msg = {
            "from": "director",
            "type": "request",
            "content": "收到请回复",
            "seq": 10,
        }
        await adapter._trigger_urgent_llm(
            prompt="Director 广播协作请求：收到请回复",
            context_msg=context_msg,
        )

        messages = await read_collab_messages(bb_root)
        response_msgs = [
            m for m in messages
            if m.get("type") == "response" and m.get("from") == "worker_001"
        ]
        assert len(response_msgs) == 1, "应写一条 response 消息"
        assert response_msgs[0]["content"] == "已收到广播，准备参与协作"
        assert response_msgs[0].get("reply_to") == 10
        assert response_msgs[0].get("accept") is True

    @pytest.mark.asyncio
    async def test_trigger_urgent_llm_writes_error_on_exception(
        self, bb_root: Path, worker_config
    ):
        """_trigger_urgent_llm 异常时（collab_retry 关闭）写 error 消息，不静默丢弃。"""
        from teage_liu.multiagent.blackboard import read_collab_messages

        class FailingOrchestrator:
            async def chat(self, session_id, user_input, **kwargs):
                raise RuntimeError("LLM 服务不可用")

        # Phase2 L-1：collab_retry.enabled=False 时走 error 写入回退路径
        import copy
        cfg = copy.deepcopy(worker_config)
        cfg["multiagent"]["worker"]["collab_retry"] = {"enabled": False}

        adapter = WorkerAdapter(
            bb_root=bb_root, config=cfg,
            agent_id="worker_001", orchestrator=FailingOrchestrator(),
        )

        context_msg = {
            "from": "director",
            "type": "request",
            "content": "test",
            "seq": 20,
        }
        await adapter._trigger_urgent_llm(
            prompt="test", context_msg=context_msg,
        )

        messages = await read_collab_messages(bb_root)
        error_msgs = [m for m in messages if m.get("error") is True]
        assert len(error_msgs) == 1, "应写一条 error 消息"
        assert "[协作响应失败]" in error_msgs[0]["content"]
        assert error_msgs[0].get("reply_to") == 20
        assert error_msgs[0].get("accept") is False


class TestWatchdogWakeupAndLLMLock:
    """任务 2.1 + 2.3：watchdog 唤醒协作轮询 + LLM 调用串行化测试。"""

    @pytest.mark.asyncio
    async def test_watchdog_wakes_collab_poll_loop(self, bb_root: Path, worker_config):
        """watchdog 事件唤醒协作轮询，< 100ms 延迟（不等 10s 超时）。"""
        worker_config["multiagent"]["worker"]["director_v2_enabled"] = True
        # 长间隔（10s），证明靠 watchdog 事件唤醒而非超时
        worker_config["multiagent"]["collab"] = {"poll_interval_seconds": 10}
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._running = True

        # mock _poll_collab_once 计数（返回 False = 无新消息，不触发休眠）
        adapter._poll_collab_once = AsyncMock(return_value=False)
        # mock _director_injector 避免真实文件操作
        adapter._director_injector = MagicMock()
        adapter._director_injector.poll_and_enqueue_new_directives = AsyncMock()

        task = asyncio.create_task(adapter._collab_poll_loop())
        try:
            # 等待首次轮询完成
            await asyncio.sleep(0.15)
            initial_count = adapter._poll_collab_once.call_count
            assert initial_count >= 1, "首次轮询应已执行"

            # 触发 watchdog 事件
            event = FileModifiedEvent(str(bb_root / "collaboration.md"))
            adapter._on_collab_file_changed(event)

            # 等待唤醒（< 100ms 目标，给 0.5s 余量）
            await asyncio.sleep(0.5)

            # 断言 _poll_collab_once 被再次调用（唤醒后立即调用，不等 10s 超时）
            assert adapter._poll_collab_once.call_count > initial_count, (
                f"watchdog 事件应唤醒轮询，调用次数应 > {initial_count}，"
                f"实际 {adapter._poll_collab_once.call_count}"
            )
        finally:
            adapter._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_collab_poll_loop_timeout_still_runs(self, bb_root: Path, worker_config):
        """无事件时，timeout 后 _poll_collab_once 仍被调用。"""
        worker_config["multiagent"]["worker"]["director_v2_enabled"] = True
        worker_config["multiagent"]["collab"] = {"poll_interval_seconds": 0.1}
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._running = True

        adapter._poll_collab_once = AsyncMock(return_value=False)
        adapter._director_injector = MagicMock()
        adapter._director_injector.poll_and_enqueue_new_directives = AsyncMock()

        task = asyncio.create_task(adapter._collab_poll_loop())
        try:
            # 等待足够时间让 timeout 触发多次（0.1s 间隔 × 0.5s ≈ 5 次）
            await asyncio.sleep(0.5)

            # 断言 _poll_collab_once 被调用多次（timeout 触发轮询）
            assert adapter._poll_collab_once.call_count >= 3, (
                f"无事件时 timeout 应继续触发轮询，实际调用 "
                f"{adapter._poll_collab_once.call_count} 次"
            )
        finally:
            adapter._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_llm_call_does_not_block_poll_loop(self, bb_root: Path, worker_config):
        """_trigger_urgent_llm 阻塞时，_collab_poll_loop 仍能继续轮询。

        asyncio.Lock 串行化 LLM 调用，但 _collab_poll_loop 不持有锁，故不被阻塞。
        """
        worker_config["multiagent"]["worker"]["director_v2_enabled"] = True
        worker_config["multiagent"]["collab"] = {"poll_interval_seconds": 0.1}

        chat_event = asyncio.Event()

        class BlockingOrchestrator:
            """chat 阻塞直到 chat_event.set()，模拟长 LLM 调用。"""
            async def chat(self, session_id, user_input, **kwargs):
                await chat_event.wait()
                return "done"

        adapter = WorkerAdapter(
            bb_root, worker_config, agent_id="worker_001",
            orchestrator=BlockingOrchestrator(),
        )
        adapter._running = True

        # mock _poll_collab_once 计数（不实际调用 _trigger_urgent_llm）
        adapter._poll_collab_once = AsyncMock(return_value=False)
        adapter._director_injector = MagicMock()
        adapter._director_injector.poll_and_enqueue_new_directives = AsyncMock()
        adapter._director_injector.drain_pending_directives = MagicMock(return_value="")

        # 启动轮询
        poll_task = asyncio.create_task(adapter._collab_poll_loop())
        # 启动 _trigger_urgent_llm（会阻塞在 chat 上，持有 _llm_lock）
        urgent_task = asyncio.create_task(
            adapter._trigger_urgent_llm(
                prompt="test",
                context_msg={
                    "type": "request", "seq": 1,
                    "from": "director", "content": "test",
                },
            )
        )
        try:
            # 等待 urgent_task 开始并阻塞在 chat 上
            await asyncio.sleep(0.3)

            # 断言 _poll_collab_once 仍被调用（轮询未被 LLM 锁阻塞）
            assert adapter._poll_collab_once.call_count >= 2, (
                f"_trigger_urgent_llm 阻塞时轮询应继续，实际调用 "
                f"{adapter._poll_collab_once.call_count} 次"
            )
        finally:
            # 解除 chat 阻塞
            chat_event.set()
            await urgent_task
            adapter._running = False
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass


class TestWorkerCollabDecentralized:
    """改动点 2/3/4：worker 协作去 director 中心化模式测试。

    Spec: docs/superpowers/specs/2026-07-31-worker协作模式去director中心化-design.md
    """

    def _make_partner_card(self, agent_id: str, capabilities: list[str]) -> dict:
        """构造在线 agent_card（写到 agents/{id}.md）。"""
        return {
            "agent_id": agent_id,
            "agent_version": "1.0.0",
            "protocol_version": "1.0.0",
            "created_at": "2026-07-31T10:00:00Z",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "heartbeat_interval_seconds": 10,
            "status": "active",
            "role": "worker",
            "endpoint": f"http://localhost:8001",
            "owner": "user_a",
            "capabilities": capabilities,
            "specialties": [],
            "auth_method": "local",
            "trust_score": 100,
            "trust_history": [],
            "extensions": {},
            "leave_reason": "",
            "left_at": "",
        }

    async def _register_partner(
        self, bb_root: Path, agent_id: str, capabilities: list[str]
    ) -> None:
        """在 agents/ 目录注册一个在线伙伴。"""
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator

        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register(self._make_partner_card(agent_id, capabilities))

    @pytest.mark.asyncio
    async def test_build_collab_partner_context_empty_when_no_partners(
        self, bb_root: Path, worker_config
    ):
        """无其他在线 worker 时返回提示字符串（不报错）。"""
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=FakeOrchestrator(),
        )

        context = await adapter._build_collab_partner_context()

        assert context == "（当前无其他在线协作伙伴）"

    @pytest.mark.asyncio
    async def test_build_collab_partner_context_excludes_self(
        self, bb_root: Path, worker_config
    ):
        """收集的伙伴列表不含本机 agent_id（只列其他在线 worker）。"""
        # 注册 worker_001（自己）+ worker_002（伙伴）
        await self._register_partner(
            bb_root, "worker_001", ["file_read"]
        )
        await self._register_partner(
            bb_root, "worker_002", ["file_write", "web_search"]
        )

        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=FakeOrchestrator(),
        )

        context = await adapter._build_collab_partner_context()

        # 应包含 worker_002，不含 worker_001
        assert "worker_002" in context
        assert "worker_001" not in context
        # 应包含 capabilities 信息
        assert "file_write" in context
        assert "web_search" in context

    @pytest.mark.asyncio
    async def test_build_collab_partner_context_caps_per_partner(
        self, bb_root: Path, worker_config
    ):
        """每个伙伴最多 max_msgs_per_partner（默认 3）条表态。"""
        from teage_liu.multiagent.blackboard import append_collab_message

        await self._register_partner(bb_root, "worker_002", ["file_read"])

        # 写入 5 条 worker_002 的表态
        for i in range(5):
            await append_collab_message(bb_root, {
                "from": "worker_002",
                "to": "*",
                "type": "response",
                "content": f"表态 {i}",
            })

        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=FakeOrchestrator(),
        )

        context = await adapter._build_collab_partner_context(max_msgs_per_partner=3)

        # 应只展示最近 3 条（seq 3, 4, 5），不含表态 0、1
        assert "表态 2" in context  # seq=3
        assert "表态 3" in context  # seq=4
        assert "表态 4" in context  # seq=5
        assert "表态 0" not in context
        assert "表态 1" not in context

    @pytest.mark.asyncio
    async def test_director_broadcast_prompt_includes_partner_context(
        self, bb_root: Path, worker_config
    ):
        """Director 广播触发的 prompt 包含协作伙伴上下文 + "不要回复 Director"。

        验证改动点 2：worker 收到 director 广播后的 prompt 不再说
        "请决定是否参与并回复"，而是注入协作伙伴上下文 + 引导直接协商 + 不回复 Director。
        """
        from teage_liu.multiagent.blackboard import append_collab_message

        # 注册一个在线伙伴
        await self._register_partner(bb_root, "worker_002", ["file_read"])
        # 伙伴最近表态
        await append_collab_message(bb_root, {
            "from": "worker_002", "to": "*",
            "type": "response", "content": "我已准备就绪",
        })

        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )

        # 写入 director 广播 request
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "request", "content": "你们玩猜数字游戏",
        })

        # 触发轮询（应走 _handle_request from=director 分支）
        await adapter._poll_collab_once()

        # 验证：LLM 被调用一次
        assert len(fake_orch.captured_calls) == 1, "应调用 chat() 一次"
        prompt = fake_orch.captured_calls[0]["user_input"]

        # prompt 应包含协作伙伴上下文
        assert "在线协作伙伴" in prompt
        assert "worker_002" in prompt
        # prompt 应明确引导不要回复 / 请示 Director
        assert "不要回复 Director" in prompt
        assert "不要请示 Director" in prompt
        # prompt 不应包含旧的"请决定是否参与并回复"
        assert "请决定是否参与并回复" not in prompt
        # 应保留 director 任务背景
        assert "你们玩猜数字游戏" in prompt

    @pytest.mark.asyncio
    async def test_director_broadcast_response_has_no_reply_to_director(
        self, bb_root: Path, worker_config
    ):
        """worker 对 director 广播的响应消息不含 reply_to 字段（或不是 director seq）。

        验证改动点 4：去中心化模式下，worker 响应不指回 director 的 seq。
        """
        from teage_liu.multiagent.blackboard import read_collab_messages

        # 注册一个在线伙伴（避免 _build_collab_partner_context 报错）
        await self._register_partner(bb_root, "worker_002", ["file_read"])

        fake_orch = FakeOrchestratorCapturing(response="好的，我直接和 worker_002 协商")
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )

        # 写入 director 广播 request（seq=1）
        director_msg = {
            "from": "director", "to": "*",
            "type": "request", "content": "你们玩猜数字游戏",
        }
        from teage_liu.multiagent.blackboard import append_collab_message
        await append_collab_message(bb_root, director_msg)

        # 触发轮询
        await adapter._poll_collab_once()

        # 验证：worker_001 写了 response
        messages = await read_collab_messages(bb_root)
        worker_responses = [
            m for m in messages
            if m.get("from") == "worker_001" and m.get("type") == "response"
        ]
        assert len(worker_responses) == 1, "应写一条 worker_001 的 response"

        # 验证：response 的 reply_to 字段不指回 director 的 seq（=1）
        # 去中心化模式下，reply_to 应不存在或不是 director seq
        reply_to = worker_responses[0].get("reply_to")
        assert reply_to != 1, (
            f"reply_to 不应指回 director seq=1，实际: {reply_to}"
        )
        # 严格验证：去中心化模式下 reply_to 字段不应存在
        assert "reply_to" not in worker_responses[0], (
            f"去中心化模式下 response 不应有 reply_to 字段，"
            f"实际: {worker_responses[0]}"
        )


# ========== P3-1: sanitize_collab_content 剥离 LLM 思考性开头（迁移自 _filter_meta_language） ==========

class TestFilterMetaLanguage:
    """sanitize_collab_content 应剥离 LLM 思考性开头段落，保留实质内容。

    迁移回归 guard：原 WorkerAdapter._filter_meta_language 已删除，逻辑下沉至
    collab_sanitize.sanitize_collab_content 并在 blackboard 入口统一净化。
    """

    def test_strips_leading_let_me_paragraph(self):
        text = (
            "Let me check the available tools first.\n\n"
            "[teagent-lu] 我的答案是 42。"
        )
        filtered = sanitize_collab_content(text)
        assert "我的答案是 42" in filtered
        assert "Let me check" not in filtered

    def test_strips_leading_ill_paragraph(self):
        text = (
            "I'll respond to the question now.\n"
            "Since the puzzle is about numbers.\n\n"
            "答案是 7。"
        )
        filtered = sanitize_collab_content(text)
        assert "答案是 7" in filtered
        assert "I'll respond" not in filtered

    def test_strips_leading_chinese_meta(self):
        text = (
            "让我先看看有哪些工具可用。\n\n"
            "根据工具列表查询结果，我可以回答。\n\n"
            "[teagent-liu-2] 谜底是「水」。"
        )
        filtered = sanitize_collab_content(text)
        assert "谜底是「水」" in filtered
        assert "让我先看看" not in filtered
        assert "根据工具列表查询" not in filtered

    def test_strips_actually_looking_since(self):
        text = (
            "Actually, looking at the previous message,\n"
            "Since the other agent asked a question.\n\n"
            "我的回复：猜「云」。"
        )
        filtered = sanitize_collab_content(text)
        assert "我的回复" in filtered
        assert "Actually" not in filtered

    def test_preserves_substantive_content_unchanged(self):
        text = "[teagent-lu] 这是一个直接的协作回复，没有思考性开头。"
        filtered = sanitize_collab_content(text)
        assert filtered == text

    def test_all_meta_language_returns_placeholder(self):
        """全是思考性内容时返回占位符（P3-1：不再保留原文，避免污染协作流）。"""
        text = "Let me think about this.\nSince I have no answer yet."
        filtered = sanitize_collab_content(text)
        assert filtered == SANITIZED_PLACEHOLDER

    def test_strips_multiple_leading_meta_paragraphs(self):
        text = (
            "I'll check the tools.\n\n"
            "Let me look at the history.\n\n"
            "Actually, the answer is clear.\n\n"
            "[teagent-lu] 最终答案：3.14。"
        )
        filtered = sanitize_collab_content(text)
        assert "最终答案：3.14" in filtered
        assert "I'll check" not in filtered
        assert "Let me look" not in filtered
        assert "Actually" not in filtered

    def test_empty_string_returns_empty(self):
        assert sanitize_collab_content("") == ""

    def test_does_not_strip_meta_in_middle(self):
        """中间段落出现的思考性内容不剥离（只剥离开头）。"""
        text = (
            "[teagent-lu] 这是实质回复。\n\n"
            "Let me add: 补充说明。"
        )
        filtered = sanitize_collab_content(text)
        assert "实质回复" in filtered
        assert "补充说明" in filtered


# ========== P3-2: _detect_consensus 误判修复（先 sanitize 再检测） ==========

class TestDetectConsensusIgnoresToolMeta:
    """fallback 共识检测必须先 sanitize 再 _detect_consensus。

    验收失败场景：LLM 输出工具元语言含"继续达成共识"字样，旧逻辑直接对原文
    跑 _detect_consensus 误判命中，把元语言原文写成 consensus 类型（seq3/seq4
    含元语言的直接原因）。修复：worker_adapter fallback 先 sanitize_collab_content
    再 _detect_consensus。此处用 `self._w._detect_consensus(sanitize(...))`
    镜像生产调用路径。
    """

    @pytest.fixture(autouse=True)
    def _make_consensus_worker(self, tmp_path: Path):
        """Phase4 H-2:_detect_consensus 改为实例方法(读 self._config),
        此处构造默认配置 worker 供纯文本检测测试使用。"""
        bb = tmp_path / "bb"
        bb.mkdir()
        (bb / "agents").mkdir()
        cfg = {"multiagent": {"worker": {"persist_state": False}}}
        self._w = WorkerAdapter(bb_root=bb, agent_id="w1", config=cfg, orchestrator=None)

    def test_detect_consensus_genuine_signal(self):
        """实质内容含真实"达成共识" → 净化保留 → 检测命中。"""
        text = "我们经过讨论，达成共识：今晚吃火锅。"
        assert self._w._detect_consensus(sanitize_collab_content(text)) is True

    def test_detect_consensus_ignores_tool_meta(self):
        """工具元语言句含"继续达成共识" → 净化剥离该句 → 不误判 consensus。"""
        text = (
            "我来调用 send_remote_message 工具继续达成共识。"
            "今晚的菜单还没有定下来。"
        )
        # 净化后"继续达成共识"随工具元语言句被剥离
        cleaned = sanitize_collab_content(text)
        assert "继续达成共识" not in cleaned
        assert self._w._detect_consensus(cleaned) is False

    def test_detect_consensus_all_meta_no_false_positive(self):
        """100% 工具元语言（含"达成共识"）→ 净化返回占位符 → 不误判 consensus。"""
        text = "我来调用工具继续达成共识。工具调用被拦截。"
        cleaned = sanitize_collab_content(text)
        assert cleaned == SANITIZED_PLACEHOLDER
        assert self._w._detect_consensus(cleaned) is False

    def test_detect_consensus_negation_excluded(self):
        """含否定词"未达成共识" → 不命中（既有否定排除逻辑不回归）。"""
        text = "我们尚未达成共识，需要继续讨论。"
        assert self._w._detect_consensus(sanitize_collab_content(text)) is False

    def test_detect_consensus_intent_phrase_excluded(self):
        """意向短语"探讨达成共识/争取达成共识" → 不命中（尚未达成的意向，
        非事实）。修复 teagent-liu-2 round1 输出"探讨达成共识"被误判为
        consensus、单轮即终止协作的问题。"""
        # 直接调 _detect_consensus（已 sanitize 的干净文本）
        assert self._w._detect_consensus("现在等 teagent-lu 回应，探讨达成共识。") is False
        assert self._w._detect_consensus("我们争取在2轮内达成共识。") is False
        assert self._w._detect_consensus("以便达成共识，我先提出方案。") is False
        assert self._w._detect_consensus("推动达成共识需要双方努力。") is False
        # 评估性短语（非事实）：容易/可以/能够 + 达成共识
        assert self._w._detect_consensus("火锅适合讨论氛围，容易达成共识。你觉得如何？") is False
        assert self._w._detect_consensus("我们可以达成共识，你意下如何？") is False

    def test_detect_consensus_goal_description_excluded(self):
        """目标描述"目标是...进行...讨论并达成共识" → 不命中（描述任务目标，
        非已达成的事实）。修复协作2第1轮 teagent-lu 输出被误判为 consensus、
        创建幽灵终止信号导致后续真正共识被熔断、协作停滞的问题。"""
        # 完整复现协作2 seq3 的关键句
        assert self._w._detect_consensus(
            "目标是和 teagent-liu-2 进行 7 轮以上的实质讨论并达成共识。"
        ) is False
        # 目标描述变体
        assert self._w._detect_consensus("目标是达成共识，请开始讨论。") is False
        # 并列结构："讨论并达成共识"是任务流程描述
        assert self._w._detect_consensus("我们需要进行讨论并达成共识。") is False
        assert self._w._detect_consensus("双方协商并达成一致后结束。") is False

    def test_detect_consensus_tail_signal_detected(self):
        """LLM 在文本末尾表达共识（超出前300字符）→ 末尾200字符检测命中。
        修复协作2第6轮 seq12/13 实际已达成共识但共识信号在末尾、前300字符
        未命中导致 type=response、协作未正常终止的问题。"""
        # 模拟 seq13：长文本 + 末尾"达成共识，终止"
        long_prefix = "这是一段很长的讨论内容。" * 20  # > 300 字符
        tail = "无补充点，确认为头案共识。以 consensus 终止本轮协作。本轮协作达成共识，终止。"
        text = long_prefix + tail
        assert self._w._detect_consensus(text) is True

    def test_detect_consensus_genuine_after_discussion(self):
        """真实共识"经过讨论，达成共识" → 命中（"讨论"非意向动词，
        且有标点分隔，不受意向排除影响）。"""
        text = "我们经过讨论，达成共识：今晚吃火锅。"
        assert self._w._detect_consensus(text) is True

    def test_detect_consensus_expanded_signals(self):
        """扩展共识信号检测：覆盖"收敛共识""达成完全共识""协作终止"等
        LLM 常用变体（协作 0b11ed517e1f 中 LLM 反复说这些短语但原信号列表不匹配）。"""
        assert self._w._detect_consensus("三点全部对齐，就此收敛共识。") is True
        assert self._w._detect_consensus("双方达成完全共识，无分歧。") is True
        assert self._w._detect_consensus("共识清晰明确，无遗留分歧。") is True
        assert self._w._detect_consensus("协作到此终止。") is True
        assert self._w._detect_consensus("终止本次协作。") is True

    def test_detect_consensus_terminate_intent(self):
        """终止意向检测：LLM 说"发送 consensus 终止""以 consensus 收尾"等
        明确终止意向时，即使没有"达成共识"字样，也应识别为 consensus。
        这些短语表明 LLM 想发 consensus 但没调工具，fallback 应代写 consensus。"""
        assert self._w._detect_consensus("发送 consensus 终止本次协作。") is True
        assert self._w._detect_consensus("以 consensus 收尾。") is True
        assert self._w._detect_consensus("请发 consensus 终止本次协作。") is True

    def test_genuine_consensus_after_meta_sentence(self):
        """元语言句 + 真实共识句 → 净化保留共识句 → 检测命中。"""
        text = "我来调用工具查询菜单。我们达成共识：吃红烧肉。"
        cleaned = sanitize_collab_content(text)
        assert "达成共识：吃红烧肉" in cleaned
        assert self._w._detect_consensus(cleaned) is True

    def test_detect_consensus_empty_and_placeholder(self):
        """空串与占位符均不命中共识检测。"""
        assert self._w._detect_consensus("") is False
        assert self._w._detect_consensus(SANITIZED_PLACEHOLDER) is False


# ========== P1-4: collab_round 计数修正（换 agent 才 +1，体现一来一回） ==========

class TestCollabRoundCounting:
    """_compute_outgoing_collab_round 应让同一轮次内收发双方共享 round 号，
    仅当本 worker 上一轮已发送时才 +1 推进，避免 round 爆涨。"""

    def test_first_send_is_round_1(self, bb_root, worker_config):
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        # 本 worker 首次发出（对端无消息，peer_round=0）→ round 1
        assert adapter._compute_outgoing_collab_round("collab_a", 0) == 1

    def test_same_turn_when_peer_ahead(self, bb_root, worker_config):
        """模拟 B 视角：A 已发 round 1，B 首次回应应共享 round 1（同一回合）。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_002")
        # B 从未发送（last_sent=0），对端 A 的 round=1 > 0 → B 共享 round 1
        assert adapter._compute_outgoing_collab_round("collab_a", 1) == 1

    def test_advances_when_peer_not_ahead(self, bb_root, worker_config):
        """A 视角完整来回：A1 → B1 → A2 → B2 → A3，A 的发出 round 依次 1,2,3。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        # A 首次：peer=0, last=0 → 1
        assert adapter._compute_outgoing_collab_round("collab_a", 0) == 1
        # A 回应 B 的 round1：peer=1, last=1（A 已发过 1）→ 1 不大于 1 → last+1=2
        assert adapter._compute_outgoing_collab_round("collab_a", 1) == 2
        # A 回应 B 的 round2：peer=2, last=2 → 3
        assert adapter._compute_outgoing_collab_round("collab_a", 2) == 3

    def test_simulated_full_back_and_forth_max_round_half_of_messages(
        self, bb_root, worker_config,
    ):
        """双 agent 完整一来一回 6 条消息，max round 应为 3 而非 6。"""
        a = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        b = WorkerAdapter(bb_root, worker_config, agent_id="worker_002")
        rounds = []
        # A1: peer=0
        rounds.append(a._compute_outgoing_collab_round("c1", 0))   # 1
        # B1: peer=1
        rounds.append(b._compute_outgoing_collab_round("c1", 1))   # 1
        # A2: peer=1
        rounds.append(a._compute_outgoing_collab_round("c1", 1))   # 2
        # B2: peer=2
        rounds.append(b._compute_outgoing_collab_round("c1", 2))   # 2
        # A3: peer=2
        rounds.append(a._compute_outgoing_collab_round("c1", 2))   # 3
        # B3: peer=3
        rounds.append(b._compute_outgoing_collab_round("c1", 3))   # 3
        assert rounds == [1, 1, 2, 2, 3, 3]
        assert max(rounds) == 3, "6 条消息一来一回，max round 应为 3（旧逻辑会到 6）"

    def test_different_collabs_isolated(self, bb_root, worker_config):
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        # c1: 本 worker 发出 round 1
        assert adapter._compute_outgoing_collab_round("c1", 0) == 1
        # c2: 独立计数，首次也是 round 1
        assert adapter._compute_outgoing_collab_round("c2", 0) == 1
        # c1 推进：peer=1, last=1 → 2
        assert adapter._compute_outgoing_collab_round("c1", 1) == 2
        # c2 不受 c1 影响：c2 的 last_sent 仍为 1，peer=2 > 1 → 共享 2
        assert adapter._compute_outgoing_collab_round("c2", 2) == 2

    def test_none_cid_safe(self, bb_root, worker_config):
        """cid=None 时不报错（不持久化 last_sent），返回 peer_round 或 last+1。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        assert adapter._compute_outgoing_collab_round(None, 0) == 1

    def test_zero_peer_when_last_sent_ahead_advances(self, bb_root, worker_config):
        """Phase5 L-3:本 worker 已发 round 2，对端落后发 round 0 → 对齐到 0（同回合共享，
        不再 +1 跳跃，避免接收方 round 越推越高）。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._compute_outgoing_collab_round("c1", 0)  # 1
        adapter._compute_outgoing_collab_round("c1", 1)  # 2
        # Phase5 L-3:对端 round 0 < last_sent 2 → 对齐到 peer_round=0（不跳到 3）
        assert adapter._compute_outgoing_collab_round("c1", 0) == 0


# ========== P3-3: 同 round 连发闸门 ==========

class TestSameRoundGate:
    """response 分支的同 round 闸门：本 worker 在该 round 已发过消息时，
    对端同 round 回流的 response 不再触发 LLM，改入 _normal_queue。"""

    def _make_partner_card(self, agent_id: str) -> dict:
        return {
            "agent_id": agent_id, "agent_version": "1.0.0",
            "protocol_version": "1.0.0",
            "created_at": "2026-07-31T10:00:00Z",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "heartbeat_interval_seconds": 10, "status": "active", "role": "worker",
            "endpoint": "http://localhost:8001", "owner": "user_a",
            "capabilities": ["file_read"], "specialties": [], "auth_method": "local",
            "trust_score": 100, "trust_history": [], "extensions": {},
            "leave_reason": "", "left_at": "",
        }

    async def _register_partner(self, bb_root: Path, agent_id: str) -> None:
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register(self._make_partner_card(agent_id))

    @pytest.mark.asyncio
    async def test_gate_blocks_peer_response_from_past_round(
        self, bb_root: Path, worker_config,
    ):
        """本 worker 已在 round 2 发过，对端 round 1 response（旧回合）被闸门拦截，
        不触发 LLM。修复后闸门条件为 my_last_sent > current_round（严格大于），
        避免误拦截同 round 消息。"""
        await self._register_partner(bb_root, "worker_002")
        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )
        # 模拟本 worker 已在 round 2 发过消息（领先于对端的 round 1）
        adapter._collab_last_sent_round["collab_gate_1"] = 2

        msg = {
            "from": "worker_002", "to": "worker_001",
            "type": "response", "content": "旧回合回流",
            "collab_id": "collab_gate_1", "collab_round": 1,
            "seq": 5, "message_id": "peer_r1_1",
        }
        await adapter._handle_collab_message(msg)

        # 闸门拦截 → LLM 不应被调用
        assert len(fake_orch.captured_calls) == 0, "旧回合回流应被闸门拦截，不触发 LLM"
        # 消息应进入 _normal_queue
        assert len(adapter._normal_queue) == 1
        assert adapter._normal_queue[0]["message_id"] == "peer_r1_1"

    @pytest.mark.asyncio
    async def test_gate_allows_advance_when_both_sent_same_round(
        self, bb_root: Path, worker_config,
    ):
        """双方都在 round 1 发过消息后，本 worker 收到对端 round 1 response 时
        不应被闸门拦截——_compute_outgoing_collab_round 会推进到 round 2，
        让协作正常进入下一轮。修复 >= 导致的 round1 死锁。"""
        await self._register_partner(bb_root, "worker_002")
        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )
        # 本 worker 已在 round 1 发过（与对端同 round）
        adapter._collab_last_sent_round["collab_gate_1b"] = 1

        msg = {
            "from": "worker_002", "to": "worker_001",
            "type": "response", "content": "同回合表态",
            "collab_id": "collab_gate_1b", "collab_round": 1,
            "seq": 5, "message_id": "peer_r1_1b",
        }
        await adapter._handle_collab_message(msg)

        # 闸门不拦截 → LLM 应被调用（推进到 round 2）
        assert len(fake_orch.captured_calls) == 1, "同 round 双方已发应推进到下一轮，不拦截"
        assert len(adapter._normal_queue) == 0

    @pytest.mark.asyncio
    async def test_gate_allows_peer_ahead_response(
        self, bb_root: Path, worker_config,
    ):
        """本 worker last_sent=0，对端 round 1 response 不被拦截，正常触发 LLM。"""
        await self._register_partner(bb_root, "worker_002")
        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )
        # 本 worker 从未发送（last_sent=0）
        msg = {
            "from": "worker_002", "to": "worker_001",
            "type": "response", "content": "对端领先表态",
            "collab_id": "collab_gate_2", "collab_round": 1,
            "seq": 3, "message_id": "peer_r1_2",
        }
        await adapter._handle_collab_message(msg)

        # 闸门不拦截 → LLM 应被调用
        assert len(fake_orch.captured_calls) == 1, "对端领先 response 应正常触发 LLM"
        assert len(adapter._normal_queue) == 0

    @pytest.mark.asyncio
    async def test_gate_allows_zero_peer_round(
        self, bb_root: Path, worker_config,
    ):
        """有效 collab_round=0 + 双方同回合(my_last_sent=0)时不触发闸门。

        Phase1 I-2 已把「缺 collab_round 字段」(前置检查拒绝)与「有效 round=0」解耦。
        本测试验证:有效 round=0 且本 worker 也在 round 0 时,闸门
        `my_last_sent > current_round` = `0 > 0` = False → 不拦截 → LLM 正常调用。
        (my_last_sent=2 + peer_round=0 是过时回合消息,应被拦截,见其它用例。)
        """
        await self._register_partner(bb_root, "worker_002")
        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )
        adapter._collab_last_sent_round["collab_gate_3"] = 0  # 双方都在 round 0
        msg = {
            "from": "worker_002", "to": "worker_001",
            "type": "response", "content": "同回合 round=0",
            "collab_id": "collab_gate_3", "collab_round": 0,
            "seq": 1, "message_id": "peer_r0",
        }
        await adapter._handle_collab_message(msg)
        assert len(fake_orch.captured_calls) == 1, "同回合 round=0 不应被闸门拦截"


class TestErrorFuse:
    """P3-5：LLM 失败熔断——对端 error 消息（type=response, error=True）
    不触发本 worker LLM，避免 error→LLM→error 无限循环堆积。"""

    def _make_partner_card(self, agent_id: str) -> dict:
        return {
            "agent_id": agent_id, "agent_version": "1.0.0",
            "protocol_version": "1.0.0",
            "created_at": "2026-07-31T10:00:00Z",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "heartbeat_interval_seconds": 10, "status": "active", "role": "worker",
            "endpoint": "http://localhost:8001", "owner": "user_a",
            "capabilities": ["file_read"], "specialties": [], "auth_method": "local",
            "trust_score": 100, "trust_history": [], "extensions": {},
            "leave_reason": "", "left_at": "",
        }

    async def _register_partner(self, bb_root: Path, agent_id: str) -> None:
        from teage_liu.multiagent.agent_registry import AgentRegistry
        from teage_liu.multiagent.schema_validator import SchemaValidator
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register(self._make_partner_card(agent_id))

    @pytest.mark.asyncio
    async def test_error_msg_does_not_trigger_llm(
        self, bb_root: Path, worker_config,
    ):
        """对端 error=True 的 response 消息被熔断，不触发 LLM，入队搭便车。"""
        await self._register_partner(bb_root, "worker_002")
        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )
        msg = {
            "from": "worker_002", "to": "worker_001",
            "type": "response", "content": "[协作响应失败] APIStatusError: 402",
            "collab_id": "collab_err_1", "collab_round": 1,
            "seq": 7, "message_id": "err_1", "error": True,
        }
        await adapter._handle_collab_message(msg)

        # 熔断 → LLM 不应被调用
        assert len(fake_orch.captured_calls) == 0, "error 消息应被熔断，不触发 LLM"
        # 消息应进入 _normal_queue（搭便车，不丢失）
        assert len(adapter._normal_queue) == 1
        assert adapter._normal_queue[0]["message_id"] == "err_1"
        # 应标记为已处理（避免重启后重复触发）
        dk = adapter._dk("collab_err_1", 7, "err_1")
        assert dk in adapter._processed_msg_seqs

    @pytest.mark.asyncio
    async def test_normal_response_still_triggers_llm(
        self, bb_root: Path, worker_config,
    ):
        """非 error 的正常 response 消息不受熔断影响，正常触发 LLM。"""
        await self._register_partner(bb_root, "worker_002")
        fake_orch = FakeOrchestratorCapturing()
        adapter = WorkerAdapter(
            bb_root=bb_root, config=worker_config,
            agent_id="worker_001", orchestrator=fake_orch,
        )
        msg = {
            "from": "worker_002", "to": "worker_001",
            "type": "response", "content": "我建议吃红烧肉",
            "collab_id": "collab_err_2", "collab_round": 1,
            "seq": 3, "message_id": "ok_1",
        }
        await adapter._handle_collab_message(msg)

        assert len(fake_orch.captured_calls) == 1, "正常 response 应触发 LLM"
        assert len(adapter._normal_queue) == 0


class TestToolLayerSameRoundGate:
    """P3-3 工具层同回合闸门：单次 _trigger_urgent_llm 调用内
    （_current_collab_round 非 None）仅允许发 1 条协作消息，第 2 次
    send_remote_message 调用被拦截，避免 LLM 单次响应连发风暴。

    与 TestSameRoundGate 互补：后者在 LLM 触发前拦截同回合回流，本测试覆盖
    LLM 单次响应内多次调用工具的场景（line 1280 闸门无法约束）。
    """

    @pytest.mark.asyncio
    async def test_second_send_in_same_round_blocked(self, bb_root: Path):
        from teage_liu.agent.tools.a2a_tools import (
            register_a2a_tools, _current_collab_id,
            _current_collab_round, _collab_round_sent,
        )
        from teage_liu.agent.tool_registry import ToolRegistry
        from teage_liu.multiagent.blackboard import read_collab_messages

        registry = ToolRegistry()
        register_a2a_tools(
            registry, bb_root, a2a_client=None, local_agent_id="worker_001",
        )
        collab_id = "collab_p33_gate"

        # 模拟 _trigger_urgent_llm 设置的协作上下文（round=1）
        tok_id = _current_collab_id.set(collab_id)
        tok_round = _current_collab_round.set(1)
        tok_sent = _collab_round_sent.set(False)
        try:
            # 第 1 次发送：应成功
            r1 = registry.execute_tool("send_remote_message", {
                "target_agent_id": "worker_002",
                "content": "我提议火锅",
                "msg_type": "response",
            })
            r1_obj = json.loads(r1)
            assert r1_obj["ok"] is True, f"首次发送应成功: {r1}"

            # 第 2 次发送（同一 LLM 响应内）：应被同回合闸门拦截
            r2 = registry.execute_tool("send_remote_message", {
                "target_agent_id": "worker_002",
                "content": "再补充一句",
                "msg_type": "response",
            })
            r2_obj = json.loads(r2)
            assert r2_obj.get("ok") is False, "同回合第 2 次发送应被拦截"
            assert r2_obj.get("blocked") == "same_round_gate"
        finally:
            _current_collab_id.reset(tok_id)
            _current_collab_round.reset(tok_round)
            _collab_round_sent.reset(tok_sent)

        # 仅 1 条消息落盘（第 2 次被拦截，未写入）
        msgs = await read_collab_messages(bb_root, collab_id=collab_id)
        assert len(msgs) == 1, f"同回合应只落盘 1 条，实际 {len(msgs)}"
        assert msgs[0]["content"] == "我提议火锅"

    @pytest.mark.asyncio
    async def test_non_collab_context_not_blocked(self, bb_root: Path):
        """非协作上下文（_current_collab_round=None）不触发闸门，可多次发送。"""
        from teage_liu.agent.tools.a2a_tools import (
            register_a2a_tools, _current_collab_round,
        )
        from teage_liu.agent.tool_registry import ToolRegistry

        registry = ToolRegistry()
        register_a2a_tools(
            registry, bb_root, a2a_client=None, local_agent_id="worker_001",
        )
        # _current_collab_round 默认 None → 非协作上下文，闸门不激活
        assert _current_collab_round.get() is None
        r1 = registry.execute_tool("send_remote_message", {
            "target_agent_id": "worker_002",
            "content": "msg1", "msg_type": "request",
        })
        r2 = registry.execute_tool("send_remote_message", {
            "target_agent_id": "worker_002",
            "content": "msg2", "msg_type": "request",
        })
        assert json.loads(r1)["ok"] is True
        assert json.loads(r2)["ok"] is True, "非协作上下文不应被闸门拦截"

    @pytest.mark.asyncio
    async def test_new_round_resets_gate(self, bb_root: Path):
        """新一轮 _trigger_urgent_llm 调用重置闸门，可再次发送 1 条。

        模拟两次独立的 _trigger_urgent_llm 调用（_collab_round_sent 每次 reset
        为 False），每次都能成功发送首条消息。
        """
        from teage_liu.agent.tools.a2a_tools import (
            register_a2a_tools, _current_collab_id,
            _current_collab_round, _collab_round_sent,
        )
        from teage_liu.agent.tool_registry import ToolRegistry
        from teage_liu.multiagent.blackboard import read_collab_messages

        registry = ToolRegistry()
        register_a2a_tools(
            registry, bb_root, a2a_client=None, local_agent_id="worker_001",
        )
        collab_id = "collab_p33_reset"

        # 第 1 轮 LLM 调用
        tok_id = _current_collab_id.set(collab_id)
        tok_round1 = _current_collab_round.set(1)
        tok_sent1 = _collab_round_sent.set(False)
        try:
            r1 = registry.execute_tool("send_remote_message", {
                "target_agent_id": "worker_002",
                "content": "round1 消息", "msg_type": "response",
            })
            assert json.loads(r1)["ok"] is True
        finally:
            _current_collab_id.reset(tok_id)
            _current_collab_round.reset(tok_round1)
            _collab_round_sent.reset(tok_sent1)

        # 第 2 轮 LLM 调用（_collab_round_sent 重新 reset 为 False）
        tok_id2 = _current_collab_id.set(collab_id)
        tok_round2 = _current_collab_round.set(2)
        tok_sent2 = _collab_round_sent.set(False)
        try:
            r2 = registry.execute_tool("send_remote_message", {
                "target_agent_id": "worker_002",
                "content": "round2 消息", "msg_type": "response",
            })
            assert json.loads(r2)["ok"] is True, "新一轮应重置闸门，允许首条发送"
        finally:
            _current_collab_id.reset(tok_id2)
            _current_collab_round.reset(tok_round2)
            _collab_round_sent.reset(tok_sent2)

        # 两轮各 1 条消息落盘
        msgs = await read_collab_messages(bb_root, collab_id=collab_id)
        assert len(msgs) == 2


# ========== P3-4: _rebuild_state_from_history 重建 _processed_msg_seqs ==========

class TestRebuildStateRepopulatesProcessedSeqs:
    """重启后旧协作重处理修复：_rebuild_state_from_history 重建 _processed_msg_seqs。

    验收失败场景：state.json 缺失时 rebuild 仅重建 responded_request_seqs/executed_op_ids，
    _processed_msg_seqs 留空 → 首次轮询把旧 active 协作的 peer response 当新消息重新触发
    LLM → collab 被重新写入（collab_7183541dc877/bbb090f30c9e）。修复：按"已参与协作"
    启发式重建——本 worker 已发过 response/consensus 的协作中所有消息 key 加入集合。
    """

    @pytest.mark.asyncio
    async def test_rebuild_state_repopulates_processed_seqs(
        self, bb_root: Path, worker_config,
    ):
        """已参与协作的所有消息 key 进入 processed_msg_seqs；未参与协作不进入。"""
        from teage_liu.multiagent.blackboard import (
            append_collab_message, read_collab_messages,
        )

        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._persist_state = False  # 内存模式，不写 state.json

        # collab A：本 worker 已参与（发了 response + consensus）
        cid_a = "collab_p34_a"
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "request", "content": "晚餐吃什么", "collab_id": cid_a,
        }, collab_id=cid_a)
        await append_collab_message(bb_root, {
            "from": "worker_002", "to": "*",
            "type": "response", "content": "我提议火锅", "collab_id": cid_a,
        }, collab_id=cid_a)
        await append_collab_message(bb_root, {
            "from": "worker_001", "to": "*",
            "type": "response", "content": "同意火锅",
            "collab_id": cid_a, "reply_to": 1, "accept": True,
        }, collab_id=cid_a)
        await append_collab_message(bb_root, {
            "from": "worker_001", "to": "*",
            "type": "consensus", "content": "达成共识：火锅", "collab_id": cid_a,
        }, collab_id=cid_a)

        # collab B：本 worker 未参与（只有 director 请求 + 他方 response）
        cid_b = "collab_p34_b"
        await append_collab_message(bb_root, {
            "from": "director", "to": "*",
            "type": "request", "content": "明天的任务", "collab_id": cid_b,
        }, collab_id=cid_b)
        await append_collab_message(bb_root, {
            "from": "worker_002", "to": "*",
            "type": "response", "content": "我来做", "collab_id": cid_b,
        }, collab_id=cid_b)

        state = await adapter._rebuild_state_from_history()

        # 读回消息拿到 seq + message_id，构造期望的 dedup key
        msgs_a = await read_collab_messages(bb_root, collab_id=cid_a)
        msgs_b = await read_collab_messages(bb_root, collab_id=cid_b)

        # collab A：4 条消息全部应在 processed_msg_seqs（已参与协作，全跳过防重触发）
        for m in msgs_a:
            key = adapter._dk(m.get("collab_id"), m["seq"], m.get("message_id"))
            assert key in state.processed_msg_seqs, (
                f"已参与协作 A 的消息 seq={m['seq']} 应在 processed_msg_seqs"
            )

        # collab B：消息不应在 processed_msg_seqs（未参与，保留响应新广播能力）
        for m in msgs_b:
            key = adapter._dk(m.get("collab_id"), m["seq"], m.get("message_id"))
            assert key not in state.processed_msg_seqs, (
                f"未参与协作 B 的消息 seq={m['seq']} 不应在 processed_msg_seqs"
            )

        # responded_request_seqs：worker_001 在 A 中发过 accept=True response(reply_to=1)
        assert 1 in state.responded_request_seqs

    @pytest.mark.asyncio
    async def test_rebuild_no_participation_keeps_empty(
        self, bb_root: Path, worker_config,
    ):
        """本 worker 从未发过 response/consensus 时，processed_msg_seqs 保持空。"""
        from teage_liu.multiagent.blackboard import append_collab_message

        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._persist_state = False

        cid = "collab_p34_nopart"
        await append_collab_message(bb_root, {
            "from": "director", "to": "*", "type": "request",
            "content": "广播", "collab_id": cid,
        }, collab_id=cid)
        await append_collab_message(bb_root, {
            "from": "worker_002", "to": "*", "type": "response",
            "content": "响应", "collab_id": cid,
        }, collab_id=cid)

        state = await adapter._rebuild_state_from_history()
        assert state.processed_msg_seqs == set(), "未参与协作时 processed_msg_seqs 应为空"

    @pytest.mark.asyncio
    async def test_rebuild_empty_history(self, bb_root: Path, worker_config):
        """无任何协作消息时，processed_msg_seqs 为空且不报错。"""
        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._persist_state = False
        state = await adapter._rebuild_state_from_history()
        assert state.processed_msg_seqs == set()
        assert state.responded_request_seqs == set()

    @pytest.mark.asyncio
    async def test_rebuild_uses_message_id_dedup_key(
        self, bb_root: Path, worker_config,
    ):
        """重建的 key 必须用 _dk（优先 message_id），与轮询判断一致。"""
        from teage_liu.multiagent.blackboard import append_collab_message

        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._persist_state = False

        cid = "collab_p34_mid"
        # 先写 request 注册 collab 到 index（否则 read_all_active_collab_messages 找不到）
        await append_collab_message(bb_root, {
            "from": "director", "to": "*", "type": "request",
            "content": "注册协作", "collab_id": cid,
        }, collab_id=cid)
        # 自定义 message_id，验证 key 为 "mid:{message_id}" 而非 "cid:seq"
        await append_collab_message(bb_root, {
            "from": "worker_001", "to": "*", "type": "response",
            "content": "我的响应", "collab_id": cid, "message_id": "custom_mid_1",
        }, collab_id=cid)
        await append_collab_message(bb_root, {
            "from": "worker_002", "to": "*", "type": "response",
            "content": "对端响应", "collab_id": cid, "message_id": "custom_mid_2",
        }, collab_id=cid)

        state = await adapter._rebuild_state_from_history()
        # worker_001 发过 response → 已参与 → 两条 response 的 key 应为 mid: 前缀
        assert "mid:custom_mid_1" in state.processed_msg_seqs
        assert "mid:custom_mid_2" in state.processed_msg_seqs
        # 不应为 cid:seq 形式
        assert f"{cid}:1" not in state.processed_msg_seqs
        assert f"{cid}:2" not in state.processed_msg_seqs

    @pytest.mark.asyncio
    async def test_rebuild_global_collab_participation(
        self, bb_root: Path, worker_config,
    ):
        """全局协作（collab_id=None，消息在 collaboration.md）同样适用已参与启发式。"""
        from teage_liu.multiagent.blackboard import append_collab_message

        adapter = WorkerAdapter(bb_root, worker_config, agent_id="worker_001")
        adapter._persist_state = False

        # 全局协作（collab_id=None）
        await append_collab_message(bb_root, {
            "from": "worker_002", "to": "*", "type": "response",
            "content": "全局对端响应",
        })
        await append_collab_message(bb_root, {
            "from": "worker_001", "to": "*", "type": "response",
            "content": "全局我的响应",
        })

        state = await adapter._rebuild_state_from_history()
        # worker_001 在全局协作发过 response → 全局协作视为已参与 → 全局消息应被标记
        assert len(state.processed_msg_seqs) >= 2, (
            "全局协作已参与时，其消息应进入 processed_msg_seqs"
        )
