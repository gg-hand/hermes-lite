"""pytest 全局配置：确保 hermes 包可导入。

在所有测试模块导入之前，将项目根目录加入 sys.path，
使 ``from hermes.xxx import ...`` 绝对导入在测试中可用。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERMES_ROOT = Path(__file__).resolve().parent.parent
HERMES_PKG = HERMES_ROOT / "hermes"

if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

if str(HERMES_PKG) not in sys.path:
    sys.path.insert(0, str(HERMES_PKG))
