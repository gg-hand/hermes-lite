﻿# tests/test_package_entry.py
"""测试 hermes 包入口。"""
from __future__ import annotations
import sys
import os

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_HERMES = os.path.join(_ROOT, "hermes")


class TestPackageEntry:

    def test_hermes_dir_exists(self):
        """hermes/ 目录存在。"""
        assert os.path.isdir(_HERMES), "hermes/ 目录不存在"

    def test_src_dir_deleted(self):
        """src/ 目录已重命名。"""
        src_dir = os.path.join(_ROOT, "src")
        assert not os.path.isdir(src_dir), "src/ 应已重命名为 hermes/"

    def test_main_module_exists(self):
        """hermes/__main__.py 存在。"""
        assert os.path.exists(os.path.join(_HERMES, "__main__.py"))

    def test_init_module_exists(self):
        """hermes/__init__.py 存在。"""
        assert os.path.exists(os.path.join(_HERMES, "__init__.py"))

    def test_app_no_main_block(self):
        """app.py 不包含 __main__ 块。"""
        with open(os.path.join(_HERMES, "app.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert '__name__ == "__main__"' not in content

    def test_server_no_main_block(self):
        """server.py 不包含 __main__ 块。"""
        with open(os.path.join(_HERMES, "server.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert '__name__ == "__main__"' not in content
