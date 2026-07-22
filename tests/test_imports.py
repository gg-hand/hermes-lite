"""验证项目所有 src/ 下的模块可正常导入（不依赖外部 API）。

策略：
1. 列出 src/ 下所有 .py 文件（排除 __init__.py 也一并验证导入）
2. 通过 sys.modules 注入 mock 依赖（chromadb/numpy/sentence_transformers/uvicorn），
   使缺失可选依赖时仍可导入
3. 逐一 import 验证，收集失败项

运行方式：
    python tests/test_imports.py
    python -m unittest tests.test_imports -v
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 在导入 src 模块前，先为缺失的可选依赖注入 mock
from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()


# ---------------------------------------------------------------------------
# 列出 src/ 下所有 .py 模块
# ---------------------------------------------------------------------------

def _collect_src_modules() -> list:
    """收集 src/ 下所有 .py 文件对应的模块全限定名列表。

    返回:
        模块全限定名列表，如 ["teage_liu.config", "teage_liu.llm.client", ...]，
        按 ASCII 字典序排列。
    """
    src_root = os.path.join(_PROJECT_ROOT, "teage_liu")
    modules = []

    for dirpath, _dirnames, filenames in os.walk(src_root):
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            file_path = os.path.join(dirpath, filename)
            # 转换为模块全限定名：src/agent/react_loop.py → src.agent.react_loop
            rel_path = os.path.relpath(file_path, _PROJECT_ROOT)
            module_name = rel_path.replace(os.sep, ".")[:-3]  # 去掉 .py
            modules.append(module_name)

    return sorted(modules)


# 预先收集所有模块（在测试开始前完成，便于调试）
ALL_SRC_MODULES = _collect_src_modules()


class TestImports(unittest.TestCase):
    """验证所有 src/ 下的 .py 模块可正常导入。"""

    def test_src_modules_collected(self):
        """验证收集到了预期数量的模块。"""
        self.assertGreaterEqual(
            len(ALL_SRC_MODULES), 15,
            f"应至少收集到 15 个模块，实际 {len(ALL_SRC_MODULES)}",
        )
        # 验证关键模块都在列表中
        expected_modules = [
            "teage_liu.config",
            "teage_liu.llm.client",
            "teage_liu.llm.prompts",
            "teage_liu.orchestrator.__init__",
            "teage_liu.server",
            "teage_liu.agent.react_loop",
            "teage_liu.agent.tool_registry",
            "teage_liu.agent.tools.__init__",
            "teage_liu.memory.consolidation",
            "teage_liu.memory.memory_md",
            "teage_liu.memory.retrieval",
            "teage_liu.memory.context_manager",
            "teage_liu.memory.condenser",
            "teage_liu.memory.decay",
            "teage_liu.storage.chroma_store",
            "teage_liu.storage.history_buffer",
            "teage_liu.storage.sqlite_log",
        ]
        for mod in expected_modules:
            self.assertIn(
                mod, ALL_SRC_MODULES,
                f"应包含关键模块 {mod}",
            )

    def test_all_modules_importable(self):
        """逐一验证所有 src/ 下的模块可正常导入。"""
        failures = []
        for module_name in ALL_SRC_MODULES:
            try:
                importlib.import_module(module_name)
            except Exception as e:
                failures.append((module_name, type(e).__name__, str(e)))

        if failures:
            # 生成清晰的失败报告
            detail = "\n".join(
                f"  - {name}: {exc}: {msg}" for name, exc, msg in failures
            )
            self.fail(
                f"以下 {len(failures)} 个模块导入失败:\n{detail}"
            )


if __name__ == "__main__":
    # 直接运行时先打印收集到的模块列表
    print(f"收集到 {len(ALL_SRC_MODULES)} 个 src 模块:")
    for m in ALL_SRC_MODULES:
        print(f"  - {m}")
    print()
    unittest.main(verbosity=2)
