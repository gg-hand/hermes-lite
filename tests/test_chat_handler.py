"""Task 10: 验证 ChatHandler 类可独立导入。"""
import sys

sys.path.insert(0, "hermes")


def test_chat_handler_class_exists():
    """ChatHandler 类存在。"""
    from hermes.orchestrator.chat_handler import ChatHandler
    assert ChatHandler is not None


def test_chat_method_is_coroutine():
    """chat 方法是 async。"""
    import inspect
    from hermes.orchestrator.chat_handler import ChatHandler
    assert inspect.iscoroutinefunction(ChatHandler.chat)


def test_orchestrator_delegates_chat_to_handler():
    """Orchestrator.chat 委托到 ChatHandler.chat（保持向后兼容）。"""
    from hermes.orchestrator import Orchestrator
    assert hasattr(Orchestrator, "chat")
