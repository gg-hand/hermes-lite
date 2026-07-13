"""Task 8: 验证 Orchestrator 不再暴露 CronIsolator 委托方法。"""
import sys

sys.path.insert(0, "hermes")


def test_no_build_cron_isolation():
    from hermes.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_build_cron_isolation")


def test_no_build_cron_enhanced_context():
    from hermes.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_build_cron_enhanced_context")


def test_no_build_cron_tools():
    from hermes.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "_build_cron_tools")


def test_no_set_cron_dependencies():
    from hermes.orchestrator import Orchestrator
    assert not hasattr(Orchestrator, "set_cron_dependencies")
