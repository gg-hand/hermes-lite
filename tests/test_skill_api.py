from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent.tool_registry import ToolRegistry


class TestSkillAPI(TestCase):
    """Skill API 集成测试：禁用/启用/卸载。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register_deferred(
            "skill__calculator__add",
            "Add two numbers",
            {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
            handler=lambda **kw: str(kw.get("a", 0) + kw.get("b", 0)),
        )

    def test_skill_registry_state(self):
        """注册后的 skill 工具应出现在 get_tools_schema 中。"""
        schemas = self.registry.get_tools_schema()
        names = [s["name"] for s in schemas]
        self.assertIn("skill__calculator__add", names)

    def test_disable_via_registry(self):
        """disable_skill 后 schema 中对应 stub 应含 enabled: False。"""
        self.registry.disable_skill("calculator")
        schemas = self.registry.get_tools_schema()
        entry = next(s for s in schemas if s["name"] == "skill__calculator__add")
        self.assertIn("enabled", entry)
        self.assertFalse(entry["enabled"])

    def test_enable_via_registry(self):
        """禁用再启用后 schema 中不应含 enabled 字段（即默认为启用）。"""
        self.registry.disable_skill("calculator")
        self.registry.enable_skill("calculator")
        schemas = self.registry.get_tools_schema()
        entry = next(s for s in schemas if s["name"] == "skill__calculator__add")
        self.assertNotIn("enabled", entry)

    def test_unregister_skill_tools(self):
        """unregister_by_prefix 移除匹配前缀的所有工具，不匹配的工具保留。"""
        self.registry.register_deferred(
            "skill__calculator__multiply",
            "Multiply two numbers",
            {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
            handler=lambda **kw: str(kw.get("a", 0) * kw.get("b", 0)),
        )
        self.registry.register_deferred(
            "skill__weather__current",
            "Get current weather",
            {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            handler=lambda **kw: f"sunny in {kw.get('city', 'unknown')}",
        )

        removed = self.registry.unregister_by_prefix("skill__calculator__")
        self.assertEqual(removed, 2)

        schemas = self.registry.get_tools_schema()
        remaining_names = [s["name"] for s in schemas]
        self.assertNotIn("skill__calculator__add", remaining_names)
        self.assertNotIn("skill__calculator__multiply", remaining_names)
        self.assertIn("skill__weather__current", remaining_names)
