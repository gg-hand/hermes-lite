# tests/test_routes_batch1_no_state.py
r"""测试 routes 批次 1 无 import state。

spec 2026-07-13 阶段 2：health/memory/cron_tools/approvals 改 Depends。

注意：``state.`` 检查使用负向 lookbehind 正则 ``(?<!\.)state\.``
以排除 FastAPI 标准的 ``request.app.state.xxx`` 访问模式（``app.state``
是 FastAPI 应用状态，与 ``state`` 模块无关）。
"""
from __future__ import annotations
import re
import sys
import os

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# 匹配 state. 但不匹配 .state.（如 request.app.state.xxx）
# 负向 lookbehind：state 前一个字符不能是 '.'
_STATE_MODULE_ACCESS = re.compile(r"(?<!\.)state\.")


class TestRoutesBatch1NoState:

    def test_health_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "health.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content
        assert _STATE_MODULE_ACCESS.search(content) is None

    def test_memory_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "memory.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content
        assert _STATE_MODULE_ACCESS.search(content) is None

    def test_cron_tools_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "cron_tools.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content
        assert _STATE_MODULE_ACCESS.search(content) is None

    def test_approvals_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "approvals.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content
        assert _STATE_MODULE_ACCESS.search(content) is None

    def test_health_uses_depends(self):
        with open(os.path.join(_SRC_DIR, "routes", "health.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "Depends(get_" in content

    def test_health_reset_metrics_uses_event(self):
        """reset_metrics 端点使用 app.state.metrics_reset_event。"""
        with open(os.path.join(_SRC_DIR, "routes", "health.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "metrics_reset_event" in content
        assert "request.app.state" in content or "app.state" in content
