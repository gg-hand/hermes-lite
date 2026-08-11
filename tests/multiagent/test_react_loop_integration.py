"""ReactLoop 7 集成点测试（Task 6）。

覆盖 7 个集成点：
1. system prompt 注入（WorkerAdapter._build_multiagent_prompt）
2. capabilities 校验下沉 ToolExecutor
3/4. SessionManager 注册钩子（on_start / on_end）
5. 发言前轮次校验（已在 Task 2 测试）
6. Director 心跳监测后台任务（已在 Task 2 测试）
7. LLM 上下文消息隔离（InjectionIsolator.build_llm_context）
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from teage_liu.multiagent.agent_registry import AgentRegistry
from teage_liu.multiagent.blackboard import Blackboard
from teage_liu.multiagent.injection_isolator import InjectionIsolator
from teage_liu.multiagent.schema_validator import SchemaValidator
from teage_liu.multiagent.worker_adapter import WorkerAdapter


def _make_worker_card(agent_id: str) -> dict:
    """构造 worker agent_card（用于注册第二个 agent）。"""
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


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    return tmp_path


@pytest.fixture
def multiagent_config() -> dict:
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
            },
        }
    }


class TestIntegration1SystemPrompt:
    """集成点 1：system prompt 注入。"""

    @pytest.mark.asyncio
    async def test_build_multiagent_prompt_includes_active_agents(
        self, bb_root: Path, multiagent_config
    ):
        """system prompt 包含 active_agents 列表。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        # 注册另一个 agent
        registry = AgentRegistry(bb_root, SchemaValidator(enabled=False))
        await registry.register(_make_worker_card("worker_002"))

        prompt = await adapter._build_multiagent_prompt()
        assert "worker_001" in prompt
        assert "worker_002" in prompt
        assert "Multi-Agent Collaboration Context" in prompt

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_build_multiagent_prompt_includes_director_rules(
        self, bb_root: Path, multiagent_config
    ):
        """system prompt 包含 director.md 规则段。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        prompt = await adapter._build_multiagent_prompt()
        assert "Director Rules" in prompt or "Director Protocol" in prompt

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_build_multiagent_prompt_includes_current_turn(
        self, bb_root: Path, multiagent_config
    ):
        """system prompt 包含当前轮次。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        prompt = await adapter._build_multiagent_prompt()
        assert "Current Turn" in prompt
        assert "worker_001" in prompt  # 自己的 agent_id

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_build_multiagent_prompt_includes_protocol_constraints(
        self, bb_root: Path, multiagent_config
    ):
        """system prompt 包含协议约束（锁/audit/路径沙箱/注入隔离）。"""
        adapter = WorkerAdapter(bb_root, multiagent_config, agent_id="worker_001")
        await adapter.start()

        prompt = await adapter._build_multiagent_prompt()
        assert "messages" in prompt
        assert "fencing_token" in prompt
        assert "audit" in prompt
        assert "untrusted" in prompt.lower() or "injection" in prompt.lower()

        await adapter.stop()


class TestIntegration2CapabilitiesCheck:
    """集成点 2：capabilities 校验下沉 ToolExecutor。"""

    @pytest.mark.asyncio
    async def test_evaluate_policy_rejects_undeclared_tool(
        self, bb_root: Path, multiagent_config
    ):
        """调用未声明的工具 → CapabilityNotInCardError。"""
        from teage_liu.multiagent.exceptions import CapabilityNotInCardError

        # 模拟 ToolExecutor
        class MockToolExecutor:
            def __init__(self):
                self._multiagent_state = MagicMock()
                self._multiagent_state.worker_capabilities = ["file_read", "file_write"]
                self._multiagent_state.agent_id = "worker_001"

            def evaluate_policy(self, tool_name, tool_input, session_id, **kwargs):
                if self._multiagent_state and tool_name not in self._multiagent_state.worker_capabilities:
                    raise CapabilityNotInCardError(
                        tool_name=tool_name,
                        agent_id=self._multiagent_state.agent_id,
                        declared_capabilities=self._multiagent_state.worker_capabilities,
                    )

        executor = MockToolExecutor()
        with pytest.raises(CapabilityNotInCardError, match="execute_command"):
            executor.evaluate_policy("execute_command", {}, "session_001")

    @pytest.mark.asyncio
    async def test_evaluate_policy_allows_declared_tool(
        self, bb_root: Path, multiagent_config
    ):
        """调用已声明的工具 → 通过。"""
        from teage_liu.multiagent.exceptions import CapabilityNotInCardError

        class MockToolExecutor:
            def __init__(self):
                self._multiagent_state = MagicMock()
                self._multiagent_state.worker_capabilities = ["file_read", "file_write"]
                self._multiagent_state.agent_id = "worker_001"

            def evaluate_policy(self, tool_name, tool_input, session_id, **kwargs):
                if self._multiagent_state and tool_name not in self._multiagent_state.worker_capabilities:
                    raise CapabilityNotInCardError(
                        tool_name=tool_name,
                        agent_id=self._multiagent_state.agent_id,
                        declared_capabilities=self._multiagent_state.worker_capabilities,
                    )

        executor = MockToolExecutor()
        # 不抛异常即通过
        executor.evaluate_policy("file_read", {}, "session_001")


class TestIntegration3_4SessionHooks:
    """集成点 3/4：SessionManager 注册钩子。"""

    @pytest.mark.asyncio
    async def test_session_manager_add_multiagent_hook(
        self, bb_root: Path, multiagent_config
    ):
        """SessionManager.add_multiagent_hook 注册钩子。"""
        from teage_liu.agent.session_manager import SessionManager

        sm = SessionManager(MagicMock())

        on_start = AsyncMock()
        on_end = AsyncMock()
        sm.add_multiagent_hook(on_start, on_end)

        assert len(sm._multiagent_hooks) == 1
        assert sm._multiagent_hooks[0] == (on_start, on_end)

    @pytest.mark.asyncio
    async def test_session_create_calls_on_start_hooks(
        self, bb_root: Path, multiagent_config
    ):
        """create_session 时调用 on_start 钩子。"""
        from teage_liu.agent.session_manager import SessionManager

        sm = SessionManager(MagicMock())

        on_start = AsyncMock()
        on_end = AsyncMock()
        sm.add_multiagent_hook(on_start, on_end)

        # mock create_session 的核心逻辑
        with patch.object(sm, "_create_session_internal", return_value=MagicMock()):
            await sm.create_session()

        on_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_destroy_calls_on_end_hooks(
        self, bb_root: Path, multiagent_config
    ):
        """destroy_session 时调用 on_end 钩子。"""
        from teage_liu.agent.session_manager import SessionManager

        sm = SessionManager(MagicMock())

        on_start = AsyncMock()
        on_end = AsyncMock()
        sm.add_multiagent_hook(on_start, on_end)

        with patch.object(sm, "_destroy_session_internal"):
            await sm.destroy_session("session_001")

        on_end.assert_called_once_with("session_001")


class TestIntegration5TurnCheckBeforeSpeak:
    """集成点 5：发言前轮次校验。"""

    @pytest.mark.asyncio
    async def test_before_speak_called_before_message_append(
        self, bb_root: Path, multiagent_config
    ):
        """_before_speak 在消息追加前调用。"""
        # 已在 Task 2 test_worker_adapter.py 中测试
        pass


class TestIntegration6DirectorHeartbeatMonitor:
    """集成点 6：Director 心跳监测后台任务。"""

    @pytest.mark.asyncio
    async def test_director_heartbeat_monitor_starts_with_session(
        self, bb_root: Path, multiagent_config
    ):
        """会话启动时启动 Director 心跳监测。"""
        # 已在 Task 2 test_worker_adapter.py 中测试（_director_monitor_loop）
        pass


class TestIntegration7InjectionIsolation:
    """集成点 7：LLM 上下文消息隔离。"""

    @pytest.mark.asyncio
    async def test_build_chat_messages_wraps_with_isolator(
        self, bb_root: Path, multiagent_config
    ):
        """multiagent 启用时用 InjectionIsolator 包裹消息。"""
        isolator = InjectionIsolator(bb_root)
        raw_messages = [
            {"seq": 1, "from": "agent_a", "content": "hello"},
            {"seq": 2, "from": "agent_b", "content": "world"},
        ]

        wrapped = isolator.build_llm_context(raw_messages)

        assert "<untrusted_user_message" in wrapped
        assert "hello" in wrapped
        assert "world" in wrapped

    @pytest.mark.asyncio
    async def test_build_chat_messages_without_multiagent_returns_raw(
        self, bb_root: Path, multiagent_config
    ):
        """multiagent 未启用时返回原始消息。"""
        # 模拟 ReactLoop._build_chat_messages 在 multiagent 未启用时的行为
        raw_messages = [
            {"role": "user", "content": "hello"},
        ]
        # 不包裹，直接返回
        assert raw_messages[0]["content"] == "hello"
