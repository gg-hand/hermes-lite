"""Task 10: 验证 ChatHandler 类可独立导入。"""
import sys

sys.path.insert(0, "teage_liu")


def test_chat_handler_class_exists():
    """ChatHandler 类存在。"""
    from teage_liu.orchestrator.chat_handler import ChatHandler
    assert ChatHandler is not None


def test_chat_method_is_coroutine():
    """chat 方法是 async。"""
    import inspect
    from teage_liu.orchestrator.chat_handler import ChatHandler
    assert inspect.iscoroutinefunction(ChatHandler.chat)


def test_orchestrator_delegates_chat_to_handler():
    """Orchestrator.chat 委托到 ChatHandler.chat（保持向后兼容）。"""
    from teage_liu.orchestrator import Orchestrator
    assert hasattr(Orchestrator, "chat")


def test_chat_accepts_extra_system_prompt_param():
    """chat() 方法接受 extra_system_prompt 参数（默认 None）。"""
    import inspect
    from teage_liu.orchestrator.chat_handler import ChatHandler
    sig = inspect.signature(ChatHandler.chat)
    assert "extra_system_prompt" in sig.parameters
    assert sig.parameters["extra_system_prompt"].default is None


def test_orchestrator_chat_accepts_extra_system_prompt_param():
    """Orchestrator.chat() 也接受 extra_system_prompt 参数。"""
    import inspect
    from teage_liu.orchestrator import Orchestrator
    sig = inspect.signature(Orchestrator.chat)
    assert "extra_system_prompt" in sig.parameters
    assert sig.parameters["extra_system_prompt"].default is None


import pytest
from unittest.mock import MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_extra_system_prompt_injected_to_history():
    """extra_system_prompt 作为独立 system 消息注入 enhanced_history（缓存失效区），
    不进入 system_text（缓存命中区）。"""
    from teage_liu.orchestrator.chat_handler import ChatHandler

    mock_orch = MagicMock()
    mock_orch._current_session_id = None
    mock_orch._current_intent_result = None
    mock_orch._pending_interrupt_notices = {}
    mock_orch._consecutive_empty_runs = {}
    mock_orch.msg_persistence = MagicMock()
    mock_orch.msg_persistence.maybe_flush_on_switch = AsyncMock()
    mock_orch.history_buffer = None
    mock_orch.guardrail_engine = None
    mock_orch.metrics = None
    mock_orch.llm_client = None
    mock_orch.cron_isolator = None
    mock_orch.audit_logger = None
    mock_orch.session_logger = MagicMock()
    mock_orch.session_logger.log_user_input = AsyncMock()
    mock_orch.session_logger.log_assistant_response = AsyncMock()
    mock_orch.consolidation = None
    mock_orch.intent_classifier = None
    mock_orch.memory_engine = None

    captured_history = []
    captured_system_text = []

    mock_orch.enhanced_context_builder = MagicMock()
    async def fake_build(sid, ui, hist):
        st = "BASE_SYSTEM_PROMPT"
        captured_system_text.append(st)
        return (st, hist, None)
    mock_orch.enhanced_context_builder.build = fake_build

    mock_orch.react_loop = MagicMock()
    mock_orch.react_loop.max_loops = 5
    async def fake_run(**kwargs):
        captured_history.extend(kwargs.get("history", []))
        return ("response", [], True, "complete")
    mock_orch.react_loop.run = fake_run

    handler = ChatHandler(mock_orch)
    await handler.chat(
        session_id="test_session",
        user_input="hello",
        extra_system_prompt="[协作上下文] 收到用户广播请求",
    )

    system_msgs = [m for m in captured_history if m.get("role") == "system"]
    assert any("[协作上下文]" in m.get("content", "") for m in system_msgs), \
        "extra_system_prompt 未注入到 enhanced_history"
    assert captured_system_text == ["BASE_SYSTEM_PROMPT"], \
        "system_text（缓存命中区）不应被 extra_system_prompt 污染"
