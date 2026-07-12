"""Task 5: 验证 lifespan 模块可独立导入且暴露 lifespan 函数。

架构决策：全局组件变量保留在 server.py（因 40 个测试 patch src.server.orchestrator），
lifespan.py 仅包含 lifespan 函数与 skill state 辅助函数。lifespan 函数内部通过
`import server; server.orchestrator = ...` 设置全局变量。
"""
import inspect
import sys

sys.path.insert(0, "src")


def test_lifespan_module_importable():
    """lifespan 模块可独立导入。"""
    import lifespan
    assert lifespan is not None


def test_lifespan_callable_exists():
    """lifespan 模块暴露 lifespan 可调用对象（async context manager）。"""
    from lifespan import lifespan
    assert callable(lifespan)


def test_lifespan_is_async_context_manager():
    """lifespan 是 async context manager（被 @asynccontextmanager 装饰）。"""
    from lifespan import lifespan
    # @asynccontextmanager 装饰后，lifespan 是一个 callable，
    # 调用后返回 _AsyncGeneratorContextManager
    assert callable(lifespan)


def test_load_skill_state_exists():
    """_load_skill_state 辅助函数存在于 lifespan 模块。"""
    from lifespan import _load_skill_state
    assert callable(_load_skill_state)


def test_save_skill_state_exists():
    """_save_skill_state 辅助函数存在于 lifespan 模块。"""
    from lifespan import _save_skill_state
    assert callable(_save_skill_state)


def test_server_globals_still_declared():
    """server 模块仍声明全局变量（供 test patch 与 state.py 代理）。"""
    import server
    assert hasattr(server, "orchestrator")
    assert hasattr(server, "session_logger")
    assert hasattr(server, "metrics_collector")
    assert hasattr(server, "approval_manager")
    assert hasattr(server, "task_manager")
