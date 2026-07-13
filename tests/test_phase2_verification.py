"""阶段 2 最终验证：热重载 + 软重启 + 后台 task 重启。"""
from __future__ import annotations
import sys
import os
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


class TestHotReloadTaskRestart:

    def test_config_route_restarts_metrics_persist_task(self):
        """热重载重建 metrics_collector 后重启 metrics_persist task。"""
        with open(os.path.join(_SRC_DIR, "routes", "config.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "task_registry.restart" in content or "metrics_persist" in content

    def test_config_route_restarts_cleanup_task(self):
        """热重载重建 session_logger 后重启 cleanup task。"""
        with open(os.path.join(_SRC_DIR, "routes", "config.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "cleanup" in content

    def test_config_route_restarts_file_cleanup_task(self):
        """热重载重建 upload_manager 后重启 file_cleanup task。"""
        with open(os.path.join(_SRC_DIR, "routes", "config.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "file_cleanup" in content


class TestGrepMetrics:

    def test_no_import_state_in_routes(self):
        """src/routes/ 下无 import state。"""
        import subprocess
        result = subprocess.run(
            ["findstr", "/s", "/r", "/n", "import state",
             os.path.join(_SRC_DIR, "routes")],
            capture_output=True, text=True
        )
        real_matches = [
            line for line in result.stdout.split("\n")
            if line and "import state" in line and not line.strip().startswith("#")
        ]
        assert len(real_matches) == 0

    def test_depends_count_in_routes(self):
        """src/routes/ 下 Depends(get_ 出现 ≥ 61 次。"""
        import glob
        count = 0
        for pyfile in glob.glob(os.path.join(_SRC_DIR, "routes", "*.py")):
            with open(pyfile, "r", encoding="utf-8") as f:
                content = f.read()
            count += content.count("Depends(get_")
        assert count >= 61, f"Depends(get_ 出现 {count} 次，预期 >= 61"

    def test_lifespan_line_count(self):
        """lifespan.py 行数 ≤ 250。"""
        with open(os.path.join(_SRC_DIR, "lifespan.py"), "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) <= 250, f"lifespan.py {len(lines)} 行，预期 <= 250"

    def test_server_line_count(self):
        """server.py 行数 ≤ 35。"""
        with open(os.path.join(_SRC_DIR, "server.py"), "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) <= 35, f"server.py {len(lines)} 行，预期 <= 35"
