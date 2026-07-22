"""Task 6: 验证 Orchestrator 不再暴露 SessionManager/ContextBuilder 委托方法。"""
import sys

sys.path.insert(0, "teage_liu")


def test_orchestrator_has_no_maybe_generate_title_async():
    """_maybe_generate_title_async 已移除。"""
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_maybe_generate_title_async")


def test_orchestrator_has_no_generate_title_task():
    """_generate_title_task 已移除。"""
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_generate_title_task")


def test_orchestrator_has_no_ensure_session():
    """_ensure_session 已移除。"""
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_ensure_session")


def test_orchestrator_has_no_build_environment_section():
    """_build_environment_section 已移除。"""
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_build_environment_section")


def test_orchestrator_has_no_format_todo_for_injection():
    """_format_todo_for_injection 已移除。"""
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_format_todo_for_injection")


def test_orchestrator_has_no_has_unfinished_steps():
    """_has_unfinished_steps 已移除。"""
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_has_unfinished_steps")


def test_orchestrator_has_no_build_continuation_message():
    """_build_continuation_message 已移除。"""
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_build_continuation_message")
