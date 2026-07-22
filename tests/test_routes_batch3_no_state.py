# tests/test_routes_batch3_no_state.py
"""测试 routes 批次 3 无 import state + 软重启改用容器 API。"""
from __future__ import annotations
import sys
import os

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "teage_liu")
class TestRoutesBatch3NoState:

    def test_schedules_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "schedules.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content

    def test_misc_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "misc.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content

    def test_misc_no_sync_to_server_globals(self):
        """_sync_to_server_globals 函数应已删除。"""
        with open(os.path.join(_SRC_DIR, "routes", "misc.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "_sync_to_server_globals" not in content

    def test_soft_restart_uses_container(self):
        """软重启使用 container.get() + set_instance()。"""
        with open(os.path.join(_SRC_DIR, "routes", "misc.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "container.get" in content or "container.set_instance" in content
        assert "task_registry.restart" in content

    def test_soft_restart_rebuilds_health_checker(self):
        """软重启重建 health_checker（步骤 5.5）。"""
        with open(os.path.join(_SRC_DIR, "routes", "misc.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "health_checker" in content
        assert "HealthChecker(" in content

    def test_soft_restart_rebuilds_etl_engine(self):
        """软重启重建 etl_engine（步骤 5.6）。"""
        with open(os.path.join(_SRC_DIR, "routes", "misc.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "etl_engine" in content
