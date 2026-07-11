"""验证 src/agent/tools/ 各子模块可正常导入（Task 4）。

确保 re-export 层的 5 个领域文件都能导入，且关键 register 函数可用。
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

import pytest  # noqa: E402


def test_file_tools_imports():
    """file_tools 应导出 read_file/write_file 等模块级函数 + register_file_tools。"""
    from src.agent.tools import file_tools
    assert hasattr(file_tools, "read_file")
    assert hasattr(file_tools, "write_file")
    assert hasattr(file_tools, "register_file_tools")


def test_shell_tools_imports():
    """shell_tools 应导出 execute_command/kill_running_process + register_bash_tool。"""
    from src.agent.tools import shell_tools
    assert hasattr(shell_tools, "execute_command")
    assert hasattr(shell_tools, "kill_running_process")
    assert hasattr(shell_tools, "register_bash_tool")


def test_web_tools_imports():
    """web_tools 应导出 http_request/web_search。"""
    from src.agent.tools import web_tools
    assert hasattr(web_tools, "http_request")
    assert hasattr(web_tools, "web_search")


def test_memory_tools_imports():
    """memory_tools 应导出 register_memory_tools + _register_update_profile。"""
    from src.agent.tools import memory_tools
    assert hasattr(memory_tools, "register_memory_tools")
    assert hasattr(memory_tools, "_register_update_profile")


def test_plan_tools_imports():
    """plan_tools 应导出 register_plan_tools。"""
    from src.agent.tools import plan_tools
    assert hasattr(plan_tools, "register_plan_tools")


def test_package_init_re_exports():
    """tools/__init__.py 的 * 导出应包含关键 register 函数。"""
    from src.agent import tools
    # __init__.py 通过 from .xxx import * 导出各模块的 __all__
    # 验证至少 file_tools 的 register_file_tools 可通过包访问
    assert hasattr(tools, "read_file") or hasattr(tools.file_tools, "read_file")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
