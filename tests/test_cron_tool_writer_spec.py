"""write_cron_tool 工具 description 规范测试（Phase 8 Task 5.4 spec）。

验证 ``write_cron_tool`` 的 description 已嵌入完整 cron_tool 写法规范：
- TOOL.md 模板（含 frontmatter 字段 name/version/description/author/
  timeout/input_schema）
- run.py 模板（含 stdin JSON 解析与 stdout JSON 输出）
- 接口契约（stdin/stdout/JSON/result/error/timeout）
- echo_text 示例引用

设计要点：
- description 必须是**静态字符串**（字节级稳定），不能含动态变量。
  测试通过两次注册取 description 比对，确保无动态拼接。
- 只验证 write_cron_tool 的 description，不影响其他 Core Tier 工具。

运行方式:
    python -m unittest tests.test_cron_tool_writer_spec -v
    python -m pytest tests/ -v -k cron_tool
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.agent.cron_tool_writer import register_write_cron_tool  # noqa: E402


# ---------------------------------------------------------------------------
# Mock ToolRegistry：记录 register_core 调用
# ---------------------------------------------------------------------------


class _MockToolRegistry:
    """记录 register_core 调用的 mock ToolRegistry。"""

    def __init__(self) -> None:
        self.tools: dict = {}

    def register_core(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler,
    ) -> None:
        self.tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }


# ---------------------------------------------------------------------------
# 规范测试
# ---------------------------------------------------------------------------


class TestCronToolWriterSpec(unittest.TestCase):
    """验证 write_cron_tool description 含完整 cron_tool 写法规范。"""

    def setUp(self):
        """注册 write_cron_tool 到 mock registry，捕获 description。"""
        self.registry = _MockToolRegistry()
        register_write_cron_tool(self.registry, base_dir="/tmp/test_cron_tool_spec")
        self.assertIn("cron_tool_create", self.registry.tools)
        self.description = self.registry.tools["cron_tool_create"]["description"]

    def test_write_cron_tool_description_contains_template(self):
        """测试 write_cron_tool description 含模板。"""
        # 验证 description 含 "TOOL.md 模板"、"run.py 模板"、"接口契约"
        self.assertIn("TOOL.md 模板", self.description)
        self.assertIn("run.py 模板", self.description)
        self.assertIn("接口契约", self.description)

    def test_write_cron_tool_description_contains_frontmatter(self):
        """测试 description 含 frontmatter 字段说明。"""
        # 验证含 name/version/description/author/timeout/input_schema
        self.assertIn("name:", self.description)
        self.assertIn("version:", self.description)
        self.assertIn("description:", self.description)
        self.assertIn("author:", self.description)
        self.assertIn("timeout:", self.description)
        self.assertIn("input_schema:", self.description)
        # 验证 frontmatter 起止分隔符
        self.assertIn("---", self.description)

    def test_write_cron_tool_description_contains_interface(self):
        """测试 description 含接口契约。"""
        # 验证含 stdin/stdout/JSON/result/error
        self.assertIn("stdin", self.description)
        self.assertIn("stdout", self.description)
        self.assertIn("JSON", self.description)
        self.assertIn("result", self.description)
        self.assertIn("error", self.description)
        # 验证 timeout 由 frontmatter 配置的说明
        self.assertIn("超时", self.description)
        self.assertIn("timeout", self.description)

    def test_write_cron_tool_description_contains_run_py_template(self):
        """测试 description 含 run.py 模板的关键代码片段。"""
        # 验证 run.py 模板含关键导入与主函数结构
        self.assertIn("from __future__ import annotations", self.description)
        self.assertIn("import json, sys", self.description)
        self.assertIn("def main() -> None:", self.description)
        self.assertIn("sys.stdin.read()", self.description)
        self.assertIn("json.loads", self.description)
        self.assertIn('json.dumps({"result": result}', self.description)
        self.assertIn('if __name__ == "__main__":', self.description)

    def test_write_cron_tool_description_contains_echo_text_example(self):
        """测试 description 含 echo_text 示例引用。"""
        self.assertIn("echo_text", self.description)
        self.assertIn("cron_tool/echo_text/", self.description)

    def test_write_cron_tool_description_contains_input_schema_format(self):
        """测试 description 含 input_schema JSON Schema 格式说明。"""
        # 验证 input_schema 模板含 JSON Schema 结构
        self.assertIn("type: object", self.description)
        self.assertIn("properties:", self.description)
        self.assertIn("required:", self.description)

    def test_write_cron_tool_description_is_static_string(self):
        """测试 description 为静态字符串（字节级稳定）。

        两次注册取 description 比对，确保无动态变量拼接。
        """
        registry2 = _MockToolRegistry()
        register_write_cron_tool(registry2, base_dir="/tmp/another_path")
        description2 = registry2.tools["cron_tool_create"]["description"]
        # 两次 description 必须字节级一致（不受 base_dir 等参数影响）
        self.assertEqual(self.description, description2)

    def test_write_cron_tool_input_schema_unchanged(self):
        """测试 input_schema 字段未被修改（仍含 5 个参数）。"""
        schema = self.registry.tools["cron_tool_create"]["input_schema"]
        self.assertEqual(schema["type"], "object")
        self.assertIn("tool_name", schema["properties"])
        self.assertIn("tool_md", schema["properties"])
        self.assertIn("run_script", schema["properties"])
        self.assertIn("run_ext", schema["properties"])
        self.assertIn("llm_explanation", schema["properties"])
        self.assertEqual(
            schema["required"], ["tool_name", "tool_md", "run_script"]
        )

    def test_write_cron_tool_description_length_expanded(self):
        """测试 description 已扩充（远超原 200 token 简短说明）。"""
        # 新 description 含完整模板，长度应显著大于 500 字符
        self.assertGreater(len(self.description), 500)


if __name__ == "__main__":
    unittest.main()
