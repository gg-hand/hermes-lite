"""Phase 4 Task 10: SkillLoader 测试。"""

from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from skill.loader import SkillLoader, SkillMeta, Skill, load_skill_to_registry


class TestSkillLoader(unittest.TestCase):
    """SkillLoader 测试。"""

    def _create_skill(self, tmp_path: Path, name: str, frontmatter: str, tools_py: str = ""):
        """在 tmp_path 下创建一个 Skill 目录。"""
        skill_dir = tmp_path / name
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(frontmatter, encoding="utf-8")
        if tools_py:
            (skill_dir / "tools.py").write_text(tools_py, encoding="utf-8")
        return skill_dir

    def test_discover_finds_skill(self):
        """扫描到 SKILL.md 并解析 frontmatter。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "test-skill", """---
name: test-skill
version: 1.0.0
description: 测试 Skill
requires: []
---

# Test Skill
""")
            loader = SkillLoader(skill_dir=tmp)
            metas = loader.discover()
            self.assertEqual(len(metas), 1)
            self.assertEqual(metas[0].name, "test-skill")
            self.assertEqual(metas[0].version, "1.0.0")

    def test_discover_skips_no_frontmatter(self):
        """SKILL.md 不以 --- 开头时跳过。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "no-fm", "This is a skill without frontmatter.")
            loader = SkillLoader(skill_dir=tmp)
            metas = loader.discover()
            self.assertEqual(len(metas), 0)

    def test_discover_skips_no_skill_md(self):
        """目录无 SKILL.md 时跳过。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "empty-skill").mkdir()
            loader = SkillLoader(skill_dir=tmp)
            metas = loader.discover()
            self.assertEqual(len(metas), 0)

    def test_load_dynamic_import(self):
        """加载 Skill 并提取 handler。"""
        import tempfile
        tools_py = '''
def add(a, b):
    return str(float(a) + float(b))

TOOLS = [
    {
        "name": "add",
        "handler": "add",
        "description": "加法",
        "input_schema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    }
]
'''
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "calc", """---
name: calc
version: 1.0.0
description: 计算器
requires: []
---

# Calc
""", tools_py)
            loader = SkillLoader(skill_dir=tmp)
            skill = loader.load("calc")
            self.assertIsNotNone(skill)
            self.assertEqual(len(skill.tools), 1)
            self.assertIn("add", skill.handlers)
            self.assertEqual(skill.handlers["add"](a=1, b=2), "3.0")

    def test_load_cached(self):
        """重复调用 load 不重复 import。"""
        import tempfile
        tools_py = '''
TOOLS = []
'''
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "cached", """---
name: cached
version: 1.0.0
description: 缓存测试
requires: []
---

# Cached
""", tools_py)
            loader = SkillLoader(skill_dir=tmp)
            skill1 = loader.load("cached")
            skill2 = loader.load("cached")
            # 同一实例（缓存命中）
            self.assertIs(skill1, skill2)

    def test_load_missing_skill_returns_none(self):
        """加载不存在的 Skill 返回 None。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            loader = SkillLoader(skill_dir=tmp)
            skill = loader.load("nonexistent")
            self.assertIsNone(skill)

    def test_parse_meta_invalid_yaml(self):
        """YAML 格式错误时返回 None。"""
        import tempfile
        # 故意构造无效 YAML（frontmatter 内有格式错误）
        invalid_yaml = """---
name: [unclosed
version: 1.0.0
---

# Invalid
"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "invalid", invalid_yaml)
            loader = SkillLoader(skill_dir=tmp)
            metas = loader.discover()
            # 解析失败应跳过，返回空列表
            self.assertEqual(len(metas), 0)


class TestLoadSkillToRegistry(unittest.TestCase):
    """load_skill_to_registry 函数测试。"""

    def test_register_skill_tools_to_deferred(self):
        """Skill 工具注册到 Deferred Tier。"""
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry()
        skill = Skill(
            name="test-skill",
            tools=[
                {
                    "name": "tool1",
                    "description": "工具1",
                    "input_schema": {"type": "object"},
                }
            ],
            system_prompt="",
            handlers={"tool1": lambda **kw: "result1"},
        )
        load_skill_to_registry(registry, skill)
        # 应在 _deferred_tools 中
        self.assertIn("skill__test-skill__tool1", registry._deferred_tools)
        # 工具描述应含 [Skill: test-skill] 后缀
        tool = registry._deferred_tools["skill__test-skill__tool1"]
        self.assertIn("[Skill: test-skill]", tool.description)


class TestSkillLoaderReload(unittest.TestCase):
    """SkillLoader 重新加载与存在性检查测试。"""

    def _create_skill(self, tmp_path: Path, name: str, frontmatter: str, tools_py: str = ""):
        """在 tmp_path 下创建一个 Skill 目录。"""
        skill_dir = tmp_path / name
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(frontmatter, encoding="utf-8")
        if tools_py:
            (skill_dir / "tools.py").write_text(tools_py, encoding="utf-8")
        return skill_dir

    def test_reload_clears_cache_and_reimports(self):
        """reload 清除缓存并重新导入模块。"""
        import tempfile
        tools_py_old = '''
def echo(msg):
    return "old:" + msg

TOOLS = [
    {
        "name": "echo",
        "handler": "echo",
        "description": "Echo",
        "input_schema": {"type": "object", "properties": {"msg": {"type": "string"}}},
    }
]
'''
        tools_py_new = '''
def echo(msg):
    return "new:" + msg

TOOLS = [
    {
        "name": "echo",
        "handler": "echo",
        "description": "Echo",
        "input_schema": {"type": "object", "properties": {"msg": {"type": "string"}}},
    }
]
'''
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "reload-test", """---
name: reload-test
version: 1.0.0
description: Reload Test
requires: []
---

# Reload Test
""", tools_py_old)
            loader = SkillLoader(skill_dir=tmp)

            # 首次加载
            skill = loader.load("reload-test")
            self.assertIsNotNone(skill)
            self.assertIn("reload-test", loader._skills)
            self.assertEqual(skill.handlers["echo"](msg="hello"), "old:hello")

            # 记录缓存中的旧对象
            old_skill = loader._skills["reload-test"]

            # 修改磁盘上的 tools.py 以返回新值
            (tmp / "reload-test" / "tools.py").write_text(tools_py_new, encoding="utf-8")

            # 重新加载
            skill2 = loader.reload("reload-test")
            self.assertIsNotNone(skill2)
            self.assertEqual(skill2.handlers["echo"](msg="hello"), "new:hello")

            # 缓存已更新为新实例，旧实例已被替换
            self.assertIn("reload-test", loader._skills)
            self.assertIs(loader._skills["reload-test"], skill2)
            self.assertIsNot(skill2, old_skill)

            # reload 已清理 sys.modules 中 skills.reload-test 相关的条目
            self.assertNotIn("skills.reload-test", sys.modules)
            for mod_name in list(sys.modules):
                self.assertFalse(mod_name.startswith("skills.reload-test."),
                                 f"sys.modules 中存在残留模块: {mod_name}")

    def test_reload_nonexistent_returns_none(self):
        """reload 不存在的 Skill 返回 None。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            loader = SkillLoader(skill_dir=tmp)
            result = loader.reload("nonexistent")
            self.assertIsNone(result)

    def test_list_loaded(self):
        """list_loaded 返回已加载的 Skill 名称列表。"""
        import tempfile
        tools_py = "TOOLS = []\n"
        frontmatter = """---
name: {name}
version: 1.0.0
description: Test Skill
requires: []
---

# {name}
"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "skill-a", frontmatter.format(name="skill-a"), tools_py)
            self._create_skill(tmp, "skill-b", frontmatter.format(name="skill-b"), tools_py)
            loader = SkillLoader(skill_dir=tmp)

            # 未加载时列表为空
            self.assertEqual(loader.list_loaded(), [])

            loader.load("skill-a")
            self.assertEqual(loader.list_loaded(), ["skill-a"])

            loader.load("skill-b")
            self.assertCountEqual(loader.list_loaded(), ["skill-a", "skill-b"])

    def test_skill_exists_loaded(self):
        """加载后 skill_exists 返回 True（即使磁盘上已移除）。"""
        import tempfile
        import shutil
        tools_py = "TOOLS = []\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "exists-loaded", """---
name: exists-loaded
version: 1.0.0
description: Test
requires: []
---

# Exists Loaded
""", tools_py)
            loader = SkillLoader(skill_dir=tmp)
            loader.load("exists-loaded")

            # 移除磁盘上的目录
            shutil.rmtree(tmp / "exists-loaded")
            self.assertFalse((tmp / "exists-loaded").exists())

            # 应仍返回 True（存在于 _skills 缓存中）
            self.assertTrue(loader.skill_exists("exists-loaded"))

    def test_skill_exists_on_disk(self):
        """目录存在但未加载时 skill_exists 返回 True。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._create_skill(tmp, "disk-only", """---
name: disk-only
version: 1.0.0
description: On Disk
requires: []
---

# Disk Only
""")
            loader = SkillLoader(skill_dir=tmp)
            # 未加载，但目录和 SKILL.md 存在
            self.assertNotIn("disk-only", loader._skills)
            self.assertTrue(loader.skill_exists("disk-only"))

    def test_skill_exists_not_found(self):
        """不存在的 Skill 返回 False。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            loader = SkillLoader(skill_dir=tmp)
            self.assertFalse(loader.skill_exists("nonexistent"))


if __name__ == "__main__":
    unittest.main()
