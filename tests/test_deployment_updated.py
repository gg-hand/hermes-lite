"""测试部署脚本和 conftest 已更新为 hermes 包入口（Task 15）。

验证：
- tests/conftest.py 存在并设置 hermes 包路径
- sidecar.rs（若存在）使用 ``python -m hermes``
- deploy.sh / start.sh / restart.sh / restart.ps1 / hermes-lite.service
  不再引用 ``src/server.py`` 或 ``src.server:app``，统一为 ``hermes.app:app``
  或 ``python -m hermes``
"""
from __future__ import annotations

import os

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestConftestExists:
    def test_conftest_exists(self):
        """tests/conftest.py 存在。"""
        assert os.path.exists(os.path.join(_ROOT, "tests", "conftest.py"))

    def test_conftest_sets_hermes_path(self):
        """conftest.py 设置 hermes 包路径。"""
        if not os.path.exists(os.path.join(_ROOT, "tests", "conftest.py")):
            return
        content = _read(os.path.join(_ROOT, "tests", "conftest.py"))
        assert "hermes" in content
        assert "sys.path" in content


class TestSidecarUpdated:
    def test_sidecar_uses_hermes_module(self):
        """sidecar.rs 使用 python -m hermes（若文件存在）。"""
        sidecar_path = os.path.join(
            _ROOT, "desktop", "src-tauri", "src", "sidecar.rs"
        )
        if not os.path.exists(sidecar_path):
            return  # 桌面端在独立分支管理，dev 分支可能不存在
        content = _read(sidecar_path)
        assert "-m hermes" in content or "python\" \"-m\" \"hermes" in content
        assert "src/server.py" not in content
        assert "src.server:app" not in content


class TestDeployScriptsUpdated:
    def _check_script(self, filename: str):
        path = os.path.join(_ROOT, filename)
        if not os.path.exists(path):
            return
        content = _read(path)
        # 不应再引用旧的 src/server.py 或 src.server:app
        assert "src/server.py" not in content, (
            f"{filename} 仍引用 src/server.py"
        )
        assert "src.server:app" not in content, (
            f"{filename} 仍引用 src.server:app"
        )
        # 应使用新的 hermes 入口
        assert ("hermes.app:app" in content) or ("-m hermes" in content), (
            f"{filename} 未使用 hermes.app:app 或 python -m hermes"
        )

    def test_deploy_sh(self):
        self._check_script("deploy.sh")

    def test_start_sh(self):
        self._check_script("start.sh")

    def test_restart_sh(self):
        self._check_script("restart.sh")

    def test_restart_ps1(self):
        self._check_script("restart.ps1")

    def test_hermes_lite_service(self):
        self._check_script("hermes-lite.service")


class TestHermesModuleEntry:
    def test_hermes_main_module_exists(self):
        """hermes/__main__.py 存在，支持 python -m hermes。"""
        assert os.path.exists(os.path.join(_ROOT, "hermes", "__main__.py"))

    def test_hermes_app_importable(self):
        """hermes.app:app 可导入（uvicorn hermes.app:app 可用）。"""
        import importlib

        mod = importlib.import_module("hermes.app")
        assert hasattr(mod, "app")
