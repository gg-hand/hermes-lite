"""pytest 全局配置：确保 teage_liu 包可导入。

在所有测试模块导入之前，将项目根目录加入 sys.path，
使 ``from teage_liu.xxx import ...`` 绝对导入在测试中可用。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = PROJECT_ROOT / "teage_liu"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if str(PKG_DIR) not in sys.path:
    sys.path.insert(0, str(PKG_DIR))
