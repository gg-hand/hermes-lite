"""向后兼容 shim：实际代码已迁移到 ``tools/`` 包。

历史导入路径 ``from .builtin_tools import register_builtin_tools`` 等
仍可通过本 shim 正常工作，因为本模块通过 ``from .tools import *``
re-export 了 ``tools`` 包的所有公开 API。

新代码应直接从 ``src.agent.tools`` 或其子模块导入，例如：
    from .agent.tools import register_builtin_tools, BUILTIN_TOOLS
    from .agent.tools.shell_tools import execute_command, kill_running_process
    from .agent.tools.web_tools import http_request, web_search
    from .agent.tools.file_tools import read_file, write_file, register_file_tools
    from .agent.tools.memory_tools import register_memory_tools
    from .agent.tools.plan_tools import register_plan_tools

私有名（以 ``_`` 开头）不会通过 ``*`` re-export，需直接从对应子模块导入。
"""
from .tools import *  # noqa: F401, F403
from .tools import (  # noqa: F401
    BUILTIN_TOOLS,
    register_builtin_tools,
    register_bash_tool,
    register_file_tools,
    register_memory_tools,
    register_plan_tools,
)
