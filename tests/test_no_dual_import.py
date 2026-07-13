# tests/test_no_dual_import.py
"""测试无双重 try/except 导入样板。"""
from __future__ import annotations
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_HERMES = os.path.join(_ROOT, "hermes")


class TestNoDualImport:

    def test_no_dual_try_except_import(self):
        """hermes/ 下无双重 try/except 导入样板。

        模式：try: from .x import Y / except ImportError: from x import Y
        """
        dual_import_pattern = re.compile(
            r"try:\s*\n\s*from \.\w+ import.*\nexcept ImportError.*\n\s*from \w+ import",
            re.MULTILINE
        )

        offenders = []
        for root, dirs, files in os.walk(_HERMES):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                matches = dual_import_pattern.findall(content)
                if matches:
                    offenders.append(f"{fpath}: {len(matches)} 处")

        # 允许必要的可选降级（chromadb 等），但双重 try/except 样板应为 0
        assert len(offenders) == 0, f"仍有双重 try/except 导入样板: {offenders}"
