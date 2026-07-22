"""Task 9: 验证 EnhancedContextBuilder 可独立构造并构建上下文。"""
import asyncio
import sys

sys.path.insert(0, "teage_liu")


def test_enhanced_context_builder_class_exists():
    """EnhancedContextBuilder 类存在。"""
    from teage_liu.orchestrator.enhanced_context import EnhancedContextBuilder
    assert EnhancedContextBuilder is not None


def test_build_method_signature():
    """build 方法接受 (session_id, user_input, history) 三参数。"""
    import inspect
    from teage_liu.orchestrator.enhanced_context import EnhancedContextBuilder
    sig = inspect.signature(EnhancedContextBuilder.build)
    params = list(sig.parameters.keys())
    # self 之外的参数
    assert "session_id" in params
    assert "user_input" in params
    assert "history" in params


def test_build_returns_three_tuple_for_user_session():
    """用户会话路径返回 (system_text, enhanced_history, tools_override) 三元组。"""
    from teage_liu.orchestrator.enhanced_context import EnhancedContextBuilder

    class MockBuilder:
        def __init__(self):
            self.context_manager = None
            self.memory_retriever = None
            self.metrics = None
            self.task_manager = None
            self.todo_registry = None
            self.context_builder = None
            self.skill_mgr = None
            self.cron_isolator = None
            self.condenser = None

    builder = EnhancedContextBuilder(MockBuilder())
    result = asyncio.run(builder.build("user-session", "hello", []))
    assert len(result) == 3
    system_text, enhanced_history, tools_override = result
    assert isinstance(system_text, str)
    assert isinstance(enhanced_history, list)
    assert tools_override is None
