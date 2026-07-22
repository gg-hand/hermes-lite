﻿# tests/test_routes_batch2_no_state.py
"""测试 routes 批次 2 无 import state。"""
from __future__ import annotations
import sys
import os

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "hermes")
class TestRoutesBatch2NoState:

    def test_chat_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "chat.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content

    def test_sessions_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "sessions.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content

    def test_skills_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "skills.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content

    def test_files_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "files.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content

    def test_proposals_no_import_state(self):
        with open(os.path.join(_SRC_DIR, "routes", "proposals.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content
