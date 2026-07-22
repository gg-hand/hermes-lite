"""Task 7: 验证 Orchestrator 不再暴露 SkillManager/MessagePersistence 委托方法。"""
import sys

sys.path.insert(0, "teage_liu")


def test_no_activate_skill():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "activate_skill")


def test_no_deactivate_skill():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "deactivate_skill")


def test_no_build_active_skills_section():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_build_active_skills_section")


def test_no_persist_new_messages():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_persist_new_messages")


def test_no_save_interrupt_notice():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_save_interrupt_notice")


def test_no_sanitize_history_alternation():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_sanitize_history_alternation")


def test_no_is_empty_assistant_content():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_is_empty_assistant_content")


def test_no_maybe_flush_on_session_switch():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_maybe_flush_on_session_switch")


def test_no_flush_consolidation():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "flush_consolidation")


def test_no_trigger_consolidation():
    from teage_liu.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_trigger_consolidation")
