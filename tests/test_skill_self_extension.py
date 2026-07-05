"""Integration test for full skill lifecycle.

Tests the 5 skill management tools (skill__template / propose_skill / reload_skill /
toggle_skill / list_skills) registered by register_skill_tools, along with
ToolRegistry.disable_skill / enable_skill / is_skill_disabled.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

# Add both src/ and its parent to sys.path so that relative imports used
# inside src/agent/skill_tools.py (e.g. ``from ..skill.loader import ...``)
# resolve correctly: ``..`` goes from the ``agent`` package up to ``src``,
# which is a proper package (it has __init__.py).
_src = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(_src.parent))  # hermes-lite/  — enables src.agent etc.
sys.path.insert(0, str(_src))         # hermes-lite/src/  — enables direct agent.*

from src.agent.tool_registry import ToolRegistry
from src.agent.skill_tools import register_skill_tools
from src.skill.loader import Skill, SkillLoader


class TestSkillLifecycle(TestCase):
    """Integration test for the full skill lifecycle."""

    def setUp(self):
        """Create a fresh ToolRegistry and a mocked SkillLoader, then register
        the 5 skill management tools as Core Tier."""
        self.registry = ToolRegistry()
        self.skill_loader = MagicMock(spec=SkillLoader)
        # Override auto-created MagicMock attributes with real empty containers
        # so that hasattr / in / iteration all behave as expected.
        self.skill_loader._metas = {}
        self.skill_loader._skills = {}
        register_skill_tools(self.registry, self.skill_loader)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _call(self, tool_name: str, **kwargs) -> dict:
        """Invoke a registered Core tool and parse its JSON result."""
        raw = self.registry.execute_tool(tool_name, kwargs)
        return json.loads(raw)

    # ------------------------------------------------------------------
    # The full lifecycle
    # ------------------------------------------------------------------

    def test_full_lifecycle(self):
        """Exercise every step of the skill lifecycle in order."""

        # ==================================================================
        # 1. register_skill_tools  —  all 5 tools are Core-registered
        # ==================================================================
        for name in ("skill__template", "skill__propose", "skill__reload",
                     "skill__toggle", "skill__list"):
            self.assertIn(name, self.registry._core_tools,
                          f"Core tool {name!r} should be registered")

        # ==================================================================
        # 2. skill__template  —  returns template JSON
        # ==================================================================
        tmpl = self._call("skill__template")
        self.assertIn("fields", tmpl)
        self.assertIn("scripts_template", tmpl)
        self.assertIn("skill_md_template", tmpl)
        # skill_md_template 含 name: my_skill frontmatter
        self.assertIn("my_skill", tmpl["skill_md_template"])
        # scripts_template 含 my_handler 函数定义
        self.assertIn("my_handler", tmpl["scripts_template"])

        # ==================================================================
        # 3. Prepare a mock Skill object  —  used by propose_skill,
        #    toggle_skill (enable), and reload_skill.
        # ==================================================================
        mock_skill = Skill(
            name="test_skill",
            tools=[
                {
                    "name": "greet",
                    "description": "Greet someone",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Name to greet"},
                        },
                        "required": ["name"],
                    },
                    "handler": "greet",
                },
            ],
            system_prompt="You are a test greeting skill.",
            handlers={
                "greet": lambda name="": f"Hello, {name}!",
            },
        )
        self.skill_loader.load.return_value = mock_skill

        # ==================================================================
        # 4. propose_skill  —  create skill on disk, register to deferred,
        #    return status "activated"
        # ==================================================================
        skill_body = "# Test Skill\n"
        tools_py = (
            "TOOLS = [\n"
            '    {\n'
            '        "name": "greet",\n'
            '        "description": "Greet someone",\n'
            '        "handler": "greet",\n'
            '        "input_schema": {\n'
            '            "type": "object",\n'
            '            "properties": {\n'
            '                "name": {"type": "string", "description": "Name to greet"},\n'
            '            },\n'
            '            "required": ["name"],\n'
            '        },\n'
            '    },\n'
            "]\n"
            "\n"
            "def greet(name):\n"
            '    return f"Hello, {name}!"\n'
        )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Point both the file-writing path and the skill-dir check to the
            # same temp directory so the existing-check passes.
            self.skill_loader.skill_dir = tmp

            with patch("src.agent.skill_tools.SKILL_BASE_DIR", tmp), \
                 patch("src.agent.skill_tools.SKILL_STATE_PATH", tmp / "state.json"):

                # ---- propose ----
                result = self._call(
                    "skill__propose",
                    name="test_skill",
                    description="A test greeting skill",
                    version="0.1.0",
                    skill_body=skill_body,
                    tools_py=tools_py,
                )
                self.assertEqual(
                    result["status"], "activated",
                    "propose_skill should return status 'activated'",
                )
                self.assertGreaterEqual(
                    len(result.get("tools", [])), 1,
                    "activated skill should report its tools",
                )

                # Files were written to disk
                self.assertTrue(
                    (tmp / "test_skill" / "SKILL.md").exists(),
                    "SKILL.md should be written to disk",
                )
                self.assertTrue(
                    (tmp / "test_skill" / "tools.py").exists(),
                    "tools.py should be written to disk",
                )

                # The deferred tool was registered
                self.assertIn(
                    "skill__test_skill__greet",
                    self.registry._deferred_tools,
                    "Skill tool should be registered in deferred tier",
                )

                # ==========================================================
                # 5. disable_skill  ->  is_skill_disabled True
                # ==========================================================
                self.registry.disable_skill("test_skill")
                self.assertTrue(
                    self.registry.is_skill_disabled("test_skill"),
                )

                # ==========================================================
                # 6. enable_skill  ->  is_skill_disabled False
                # ==========================================================
                self.registry.enable_skill("test_skill")
                self.assertFalse(
                    self.registry.is_skill_disabled("test_skill"),
                )

                # ==========================================================
                # 7. reload_skill  ->  status "reloaded"
                # ==========================================================
                result = self._call("skill__reload", name="test_skill")
                self.assertEqual(result["status"], "reloaded")

                # verify tool is still registered after reload
                self.assertIn(
                    "skill__test_skill__greet",
                    self.registry._deferred_tools,
                )

                # ==========================================================
                # 8. list_skills  ->  response has skill_name in loaded list
                # ==========================================================
                self.skill_loader._skills = {"test_skill": mock_skill}
                result = self._call("skill__list")
                loaded = result.get("loaded_skills", [])
                loaded_names = [s["name"] for s in loaded]
                self.assertIn(
                    "test_skill", loaded_names,
                    "list_skills should report test_skill as loaded",
                )

                # ==========================================================
                # 9. disable skill and verify schema contains enabled:False
                # ==========================================================
                # P1 改造：通过 skill__toggle action=disable 触发软禁用
                # （而非直接调 registry.disable_skill），验证端到端流程
                toggle_result = self._call(
                    "skill__toggle", name="test_skill", action="disable"
                )
                self.assertEqual(
                    toggle_result["status"], "disabled",
                    "skill__toggle action=disable 应返回 status=disabled",
                )
                schemas = self.registry.get_tools_schema()
                deferred_entry = None
                for s in schemas:
                    if s.get("name") == "skill__test_skill__greet":
                        deferred_entry = s
                        break
                self.assertIsNotNone(
                    deferred_entry,
                    "Deferred tool should appear in schema even when disabled",
                )
                self.assertIn("enabled", deferred_entry)
                self.assertFalse(deferred_entry["enabled"])
