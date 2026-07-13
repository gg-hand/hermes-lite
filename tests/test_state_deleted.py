# tests/test_state_deleted.py
"""测试 state.py 已删除 + server.py 无全局组件变量。"""
from __future__ import annotations
import sys
import os

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


class TestStateDeleted:

    def test_state_py_deleted(self):
        """state.py 文件应已删除。"""
        state_path = os.path.join(_SRC_DIR, "state.py")
        assert not os.path.exists(state_path), "state.py 应已删除"

    def test_server_no_global_components(self):
        """server.py 不应包含全局组件变量。"""
        with open(os.path.join(_SRC_DIR, "server.py"), "r", encoding="utf-8") as f:
            content = f.read()
        # 不应包含全局组件变量声明
        assert "orchestrator: Optional" not in content
        assert "session_logger: Optional" not in content
        assert "metrics_collector: Optional" not in content

    def test_server_no_config_helpers_reexport(self):
        """server.py 不应 re-export config_helpers。"""
        with open(os.path.join(_SRC_DIR, "server.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "_RESTART_REQUIRED_KEYS" not in content
        assert "_deep_merge_config" not in content

    def test_server_no_main_block(self):
        """server.py 不应包含 __main__ 块。"""
        with open(os.path.join(_SRC_DIR, "server.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "__main__" not in content

    def test_no_import_state_in_src(self):
        """src/ 目录下无 import state。"""
        import subprocess
        result = subprocess.run(
            ["findstr", "/s", "/r", "/n", "import state", _SRC_DIR],
            capture_output=True, text=True
        )
        # 排除注释中的 import state
        real_matches = [
            line for line in result.stdout.split("\n")
            if line and "import state" in line and not line.strip().startswith("#")
        ]
        assert len(real_matches) == 0, f"仍有 import state: {real_matches}"

    def test_no_get_server_globals(self):
        """src/ 目录下无 _get_server_globals。"""
        import subprocess
        result = subprocess.run(
            ["findstr", "/s", "/r", "/n", "_get_server_globals", _SRC_DIR],
            capture_output=True, text=True
        )
        assert not result.stdout.strip(), f"仍有 _get_server_globals: {result.stdout}"
