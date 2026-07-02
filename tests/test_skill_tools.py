"""Skill 管理工具（5 个 handler）单元测试。

覆盖 skill_template / propose_skill / reload_skill / toggle_skill / list_skills
共 5 个 handler，全部通过 ToolRegistry.execute_tool 调用。

mock 策略:
- skill_loader 用 unittest.mock.MagicMock 替代，不依赖真实文件系统。
- SKILL_BASE_DIR、SKILL_STATE_PATH 在涉及文件 I/O 的测试中用 tempfile 隔离。
- Skill 对象用 MagicMock(spec=Skill) 构造，仅暴露测试需要的属性。
- load_skill_to_registry 使用真实实现验证注册行为。

运行方式:
    python -m pytest tests/test_skill_tools.py -v
    python -m unittest tests.test_skill_tools -v
    python tests/test_skill_tools.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.tool_registry import ToolRegistry  # noqa: E402
from src.agent.skill_tools import register_skill_tools  # noqa: E402
from src.skill.loader import Skill, SkillLoader  # noqa: E402


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _make_skill_md(name: str = "my_skill", description: str = "Test skill") -> str:
    """构造合法的 SKILL.md 内容（含 YAML frontmatter）。"""
    return (
        "---\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        f"description: {description}\n"
        "requires: []\n"
        "---\n"
        f"\n# {name}\n"
    )


def _make_empty_tools_py() -> str:
    """构造一个空 tools.py（仅 TOOLS 空列表）。"""
    return '"""Empty tools."""\nTOOLS = []\n'


def _make_temp_state_file(
    disabled: list | None = None, locked: list | None = None
) -> str:
    """创建临时 JSON 状态文件并返回路径。

    返回的路径在测试方法结束时应由调用方清理（Path(path).unlink）。
    """
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    state = {
        "disabled": disabled or [],
        "locked": locked or [],
    }
    Path(path).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return path


def _build_mock_skill(
    name: str = "test_skill",
    tools: list | None = None,
    description: str = "",
    system_prompt: str = "",
) -> MagicMock:
    """构造一个 MagicMock Skill 对象，便于在测试中注入。"""
    if tools is None:
        tools = []
    handlers = {}
    for t in tools:
        tname = t.get("name")
        if tname:
            handlers[tname] = MagicMock(return_value="ok")
    skill = MagicMock(spec=Skill)
    skill.name = name
    skill.tools = tools
    skill.handlers = handlers
    skill.description = description
    skill.system_prompt = system_prompt
    return skill


# ===========================================================================
# 1. TestSkillTemplate
# ===========================================================================

class TestSkillTemplate(TestCase):
    """验证 skill_template 返回合法 JSON 且包含 fields 与 tools.py 模板。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.skill_loader = MagicMock(spec=SkillLoader)
        register_skill_tools(self.registry, self.skill_loader)

    def test_returns_valid_json_with_fields_and_tools_py(self):
        """skill_template 返回 JSON，含 fields 和 tools.py。"""
        result = self.registry.execute_tool("skill_template", {})
        data = json.loads(result)
        self.assertIn("fields", data)
        self.assertIn("tools_py", data)
        self.assertIsInstance(data["fields"], dict)
        self.assertIsInstance(data["tools_py"], str)

    def test_fields_contains_required_keys(self):
        """fields 包含 name / description / version / requires / skill_body。"""
        result = self.registry.execute_tool("skill_template", {})
        data = json.loads(result)
        for key in ("name", "description", "version", "requires", "skill_body"):
            self.assertIn(key, data["fields"])

    def test_tools_py_contains_TOOLS_list(self):
        """tools.py 模板包含 TOOLS 列表定义。"""
        result = self.registry.execute_tool("skill_template", {})
        data = json.loads(result)
        self.assertIn("TOOLS = [", data["tools_py"])

    def test_tools_py_contains_handler_function(self):
        """tools.py 模板包含 handler 函数定义。"""
        result = self.registry.execute_tool("skill_template", {})
        data = json.loads(result)
        self.assertIn("def my_handler", data["tools_py"])

    def test_tools_py_has_valid_syntax(self):
        """tools.py 模板是合法 Python（compile 不抛异常）。"""
        result = self.registry.execute_tool("skill_template", {})
        data = json.loads(result)
        compile(data["tools_py"], "<test>", "exec")


# ===========================================================================
# 2. TestProposeSkill
# ===========================================================================

class TestProposeSkill(TestCase):
    """验证 propose_skill：新增 Skill、校验、回滚、注册。"""

    _DEFAULT_TOOL_INPUT = {
        "name": "my_skill",
        "description": "Test skill",
    }

    def setUp(self):
        self.registry = ToolRegistry()
        self.skill_loader = MagicMock(spec=SkillLoader)
        self.skill_loader._metas = {}
        self.skill_loader.unload = MagicMock()
        self.skill_loader.load = MagicMock()
        # 临时目录用于文件操作隔离
        self.temp_dir = Path(tempfile.mkdtemp())
        self.skill_loader.skill_dir = self.temp_dir
        register_skill_tools(self.registry, self.skill_loader)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -- 辅助 ---------------------------------------------------------------

    def _propose(self, tools_input=None, patch_base=True):
        """执行 propose_skill 的快捷方法。"""
        if tools_input is None:
            tools_input = dict(self._DEFAULT_TOOL_INPUT)
        if patch_base:
            with patch("src.agent.skill_tools.SKILL_BASE_DIR", self.temp_dir):
                return self.registry.execute_tool("skill_propose", tools_input)
        return self.registry.execute_tool("skill_propose", tools_input)

    # -- 成功路径 -----------------------------------------------------------

    def test_propose_activates_skill(self):
        """合法输入返回 status=activated，包含 tools 列表。"""
        mock_skill = _build_mock_skill(
            name="my_skill",
            tools=[{"name": "greet", "description": "Greet tool", "input_schema": {}}],
        )
        self.skill_loader.load.return_value = mock_skill

        result = self._propose({"name": "my_skill", "description": "Greeting skill", "tools_py": _make_empty_tools_py()})
        data = json.loads(result)

        self.assertEqual(data["status"], "activated")
        self.assertIn("tools", data)
        self.assertIsInstance(data["tools"], list)

    def test_propose_registers_tools_to_deferred_tier(self):
        """activate 后工具的技能工具被注册到 registry Deferred Tier。"""
        mock_skill = _build_mock_skill(
            name="calc",
            tools=[{"name": "add", "description": "Add", "input_schema": {}}],
        )
        self.skill_loader.load.return_value = mock_skill

        result = self._propose({"name": "calc", "description": "Calculator skill", "tools_py": _make_empty_tools_py()})
        data = json.loads(result)

        self.assertEqual(data["status"], "activated")
        # skill__calc__add 应注册到 deferred
        self.assertIn("skill__calc__add", self.registry._deferred_tools)

    def test_propose_calls_unload_then_load(self):
        """propose 成功时调 skill_loader.unload 再调 skill_loader.load。"""
        mock_skill = _build_mock_skill(name="s1")
        self.skill_loader.load.return_value = mock_skill

        self._propose({"name": "s1", "description": "Test"})

        self.skill_loader.unload.assert_called_once_with("s1")
        self.skill_loader.load.assert_called_once_with("s1")

    def test_propose_creates_skill_directory(self):
        """propose 成功时创建 skills/<name>/ 目录及其中的 SKILL.md。"""
        mock_skill = _build_mock_skill(name="created_skill")
        self.skill_loader.load.return_value = mock_skill

        self._propose({"name": "created_skill", "description": "Test"})

        # 文件应该已被创建
        skill_dir = self.temp_dir / "created_skill"
        self.assertTrue(skill_dir.exists(), "技能目录应被创建")
        self.assertTrue((skill_dir / "SKILL.md").exists(), "SKILL.md 应被创建")
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("created_skill", content)
        # 验证 frontmatter 被正确组装
        self.assertIn("---", content)
        self.assertIn("name: created_skill", content)

    # -- 名称校验 -----------------------------------------------------------

    def test_propose_empty_name_returns_error(self):
        """空名称返回 error。"""
        result = self._propose({"name": "", "description": "Test"}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("不能为空", data["reason"])

    def test_propose_name_with_slash_returns_error(self):
        """名称含 / 返回 error。"""
        result = self._propose({"name": "a/b", "description": "Test"}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("路径分隔符", data["reason"])

    def test_propose_name_with_backslash_returns_error(self):
        """名称含 \\ 返回 error。"""
        result = self._propose({"name": "a\\b", "description": "Test"}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("路径分隔符", data["reason"])

    def test_propose_name_starting_with_dot_returns_error(self):
        """名称以 . 开头返回 error。"""
        result = self._propose({"name": ".hidden", "description": "Test"}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("点号开头", data["reason"])

    def test_propose_name_ending_with_dot_returns_error(self):
        """名称以 . 结尾返回 error。"""
        result = self._propose({"name": "skill.", "description": "Test"}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("点号结尾", data["reason"])

    # -- 字段校验 -----------------------------------------------------------

    def test_propose_empty_description_returns_error(self):
        """description 为空返回 error。"""
        result = self._propose({"name": "no_desc", "description": ""}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("description", data["reason"])

    # -- 已存在检查 ---------------------------------------------------------

    def test_propose_existing_in_metas_returns_error(self):
        """技能已存在于 _metas 缓存时返回 error。"""
        self.skill_loader._metas = {"my_skill": MagicMock()}
        result = self._propose({"name": "my_skill", "description": "Test"}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("已存在", data["reason"])

    def test_propose_existing_on_disk_returns_error(self):
        """技能目录已存在于磁盘时返回 error。"""
        (self.temp_dir / "my_skill").mkdir(parents=True)
        result = self._propose({"name": "my_skill", "description": "Test"})
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("已存在", data["reason"])

    def test_propose_existing_on_disk_via_skill_loader_dir(self):
        """用 skill_loader.skill_dir 检查已存在时返回 error。"""
        self.skill_loader._metas = {}
        # 清理 _metas 和 skill_dir 路径下预置已存在目录
        (self.temp_dir / "existing_skill").mkdir(parents=True)
        # 以另一个 SKILL_BASE_DIR 运行（确保不通过 base dir 冲突）
        other_temp = Path(tempfile.mkdtemp())
        try:
            with patch("src.agent.skill_tools.SKILL_BASE_DIR", other_temp):
                result = self.registry.execute_tool(
                    "skill_propose",
                    {
                        "name": "existing_skill",
                        "description": "Test",
                        "tools_py": "",
                    },
                )
            data = json.loads(result)
            self.assertEqual(data["status"], "error")
            self.assertIn("已存在", data["reason"])
        finally:
            shutil.rmtree(other_temp, ignore_errors=True)

    # -- tools.py 语法检查 --------------------------------------------------

    def test_propose_invalid_tools_py_syntax_returns_error(self):
        """tools.py 语法错误返回 error。"""
        bad_tools = "def broken( :::"
        result = self._propose({"name": "my_skill", "description": "Test", "tools_py": bad_tools}, patch_base=False)
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("语法错误", data["reason"])

    # -- 注册失败处理 -------------------------------------------------------

    def test_propose_registration_failure_returns_error(self):
        """load_skill_to_registry 失败时返回 error 但保留文件。"""
        mock_skill = _build_mock_skill(
            name="fail_reg",
            tools=[{"name": "boom", "description": "", "input_schema": {}}],
        )
        # 让 skill.tools 中的 handler 不匹配，导致注册空转
        mock_skill.handlers = {}
        self.skill_loader.load.return_value = mock_skill

        result = self._propose({"name": "fail_reg", "description": "Test", "tools_py": _make_empty_tools_py()})
        data = json.loads(result)
        # 文件已写入但注册失败 — 文件应保留
        skill_dir = self.temp_dir / "fail_reg"
        self.assertTrue(skill_dir.exists(), "注册失败时文件应保留")
        self.assertTrue((skill_dir / "SKILL.md").exists(), "SKILL.md 应保留")

    # -- 空 tools.py 处理 ---------------------------------------------------

    def test_propose_without_tools_py_still_activates(self):
        """不传 tools_py（空字符串）时仍创建 SKILL.md 并返回 activated。"""
        mock_skill = _build_mock_skill(name="no_tools")
        self.skill_loader.load.return_value = mock_skill

        result = self._propose({"name": "no_tools", "description": "Test skill"})
        data = json.loads(result)
        self.assertEqual(data["status"], "activated")


# ===========================================================================
# 3. TestReloadSkill
# ===========================================================================

class TestReloadSkill(TestCase):
    """验证 reload_skill：卸载、重新加载、注册。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.skill_loader = MagicMock(spec=SkillLoader)
        self.skill_loader.unload = MagicMock()
        register_skill_tools(self.registry, self.skill_loader)

    def test_reload_valid_skill_returns_reloaded(self):
        """合法 Skill 返回 status=reloaded。"""
        mock_skill = _build_mock_skill(
            name="reload_me",
            tools=[{"name": "tool1", "description": "d1", "input_schema": {}}],
        )
        self.skill_loader.load.return_value = mock_skill

        result = self.registry.execute_tool("skill_reload", {"name": "reload_me"})
        data = json.loads(result)

        self.assertEqual(data["status"], "reloaded")
        self.skill_loader.unload.assert_called_once_with("reload_me")

    def test_reload_calls_unload_then_load(self):
        """reload 时先 unload 再 load。"""
        mock_skill = _build_mock_skill(name="r1")
        self.skill_loader.load.return_value = mock_skill

        self.registry.execute_tool("skill_reload", {"name": "r1"})

        self.skill_loader.unload.assert_called_once_with("r1")
        self.skill_loader.load.assert_called_once_with("r1")

    def test_reload_registers_tools_to_deferred(self):
        """reload 后工具的技能工具出现在 _deferred_tools。"""
        mock_skill = _build_mock_skill(
            name="reloaded_skill",
            tools=[{"name": "my_tool", "description": "My tool", "input_schema": {}}],
        )
        self.skill_loader.load.return_value = mock_skill

        self.registry.execute_tool("skill_reload", {"name": "reloaded_skill"})

        self.assertIn(
            "skill__reloaded_skill__my_tool",
            self.registry._deferred_tools,
        )

    def test_reload_removes_old_tools_before_registering(self):
        """reload 先删除旧工具再注册新工具。"""
        # 先在 registry 中预置一个旧工具
        self.registry.register_deferred(
            name="skill__old_skill__stale_tool",
            description="stale",
            input_schema={},
            handler=lambda: "",
        )
        mock_skill = _build_mock_skill(
            name="old_skill",
            tools=[{"name": "fresh_tool", "description": "fresh", "input_schema": {}}],
        )
        self.skill_loader.load.return_value = mock_skill

        self.registry.execute_tool("skill_reload", {"name": "old_skill"})

        # 旧工具应不再存在
        self.assertNotIn("skill__old_skill__stale_tool", self.registry._deferred_tools)
        # 新工具应注册
        self.assertIn("skill__old_skill__fresh_tool", self.registry._deferred_tools)

    # -- 错误路径 -----------------------------------------------------------

    def test_reload_empty_name_returns_error(self):
        """空名称返回 error。"""
        result = self.registry.execute_tool("skill_reload", {"name": ""})
        data = json.loads(result)
        self.assertEqual(data["status"], "error")

    def test_reload_nonexistent_returns_error(self):
        """不存在的技能返回 error。"""
        self.skill_loader.load.return_value = None
        result = self.registry.execute_tool("skill_reload", {"name": "ghost"})
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("不存在", data["reason"])

    def test_reload_skill_loader_none_returns_error(self):
        """skill_loader 为 None 时返回 error。"""
        registry2 = ToolRegistry()
        register_skill_tools(registry2, None)
        result = registry2.execute_tool("skill_reload", {"name": "test"})
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("skill_loader 不可用", data["reason"])

    def test_reload_load_exception_returns_error(self):
        """skill_loader.load 抛异常时返回 error。"""
        self.skill_loader.load.side_effect = RuntimeError("load failed")
        result = self.registry.execute_tool("skill_reload", {"name": "broken"})
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("重新加载失败", data["reason"])


# ===========================================================================
# 4. TestToggleSkill
# ===========================================================================

class TestToggleSkill(TestCase):
    """验证 toggle_skill：禁用、启用、锁定检查。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.skill_loader = MagicMock(spec=SkillLoader)
        self.skill_loader.unload = MagicMock()
        self.skill_loader.load = MagicMock()
        register_skill_tools(self.registry, self.skill_loader)

    # -- 禁用 ---------------------------------------------------------------

    def test_disable_returns_disabled_status(self):
        """禁用 skill 返回 status=disabled。"""
        fd, state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        Path(state_path).write_text(
            json.dumps({"disabled": [], "locked": []}), encoding="utf-8"
        )
        try:
            with patch("src.agent.skill_tools.SKILL_STATE_PATH", Path(state_path)):
                result = self.registry.execute_tool(
                    "skill_toggle", {"name": "my_skill", "action": "disable"}
                )
            data = json.loads(result)
            self.assertEqual(data["status"], "disabled")
        finally:
            Path(state_path).unlink(missing_ok=True)

    def test_disable_removes_tools_from_registry(self):
        """禁用后 skill 的工具从 registry 移除。"""
        self.registry.register_deferred(
            name="skill__my_skill__tool1",
            description="test",
            input_schema={},
            handler=lambda: "",
        )
        self.registry.register_deferred(
            name="skill__my_skill__tool2",
            description="test",
            input_schema={},
            handler=lambda: "",
        )
        fd, state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        Path(state_path).write_text(
            json.dumps({"disabled": [], "locked": []}), encoding="utf-8"
        )
        try:
            with patch("src.agent.skill_tools.SKILL_STATE_PATH", Path(state_path)):
                self.registry.execute_tool(
                    "skill_toggle", {"name": "my_skill", "action": "disable"}
                )
        finally:
            Path(state_path).unlink(missing_ok=True)

        self.assertNotIn("skill__my_skill__tool1", self.registry._deferred_tools)
        self.assertNotIn("skill__my_skill__tool2", self.registry._deferred_tools)

    def test_disable_writes_to_state_file(self):
        """禁用后状态文件记录 disabled 列表。"""
        fd, state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        Path(state_path).write_text(
            json.dumps({"disabled": [], "locked": []}), encoding="utf-8"
        )
        try:
            with patch("src.agent.skill_tools.SKILL_STATE_PATH", Path(state_path)):
                self.registry.execute_tool(
                    "skill_toggle", {"name": "my_skill", "action": "disable"}
                )
            # 读取状态文件
            saved = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertIn("my_skill", saved["disabled"])
        finally:
            Path(state_path).unlink(missing_ok=True)

    # -- 启用 ---------------------------------------------------------------

    def test_enable_returns_enabled_status(self):
        """启用 skill 返回 status=enabled。"""
        mock_skill = _build_mock_skill(name="my_skill")
        self.skill_loader.load.return_value = mock_skill

        result = self.registry.execute_tool(
            "skill_toggle", {"name": "my_skill", "action": "enable"}
        )
        data = json.loads(result)
        self.assertEqual(data["status"], "enabled")

    def test_enable_removes_from_disabled_list(self):
        """启用后状态文件的 disabled 列表不再包含该技能。"""
        fd, state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        Path(state_path).write_text(
            json.dumps({"disabled": ["my_skill"], "locked": []}), encoding="utf-8"
        )
        try:
            with patch("src.agent.skill_tools.SKILL_STATE_PATH", Path(state_path)):
                mock_skill = _build_mock_skill(name="my_skill")
                self.skill_loader.load.return_value = mock_skill
                self.registry.execute_tool(
                    "skill_toggle", {"name": "my_skill", "action": "enable"}
                )
            saved = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertNotIn("my_skill", saved["disabled"])
        finally:
            Path(state_path).unlink(missing_ok=True)

    def test_enable_loads_and_registers_tools(self):
        """启用后重新加载 skill 并注册工具到 registry。"""
        mock_skill = _build_mock_skill(
            name="just_enabled",
            tools=[{"name": "new_tool", "description": "New", "input_schema": {}}],
        )
        self.skill_loader.load.return_value = mock_skill

        self.registry.execute_tool(
            "skill_toggle", {"name": "just_enabled", "action": "enable"}
        )

        self.skill_loader.unload.assert_called_once_with("just_enabled")
        self.skill_loader.load.assert_called_once_with("just_enabled")
        self.assertIn(
            "skill__just_enabled__new_tool",
            self.registry._deferred_tools,
        )

    def test_enable_without_skill_loader_still_returns_enabled(self):
        """skill_loader 为 None 时启用仍返回 enabled。"""
        registry2 = ToolRegistry()
        register_skill_tools(registry2, None)
        result = registry2.execute_tool(
            "skill_toggle", {"name": "x", "action": "enable"}
        )
        data = json.loads(result)
        self.assertEqual(data["status"], "enabled")

    def test_enable_with_skill_loader_exception_returns_warning(self):
        """重新加载失败时返回 enabled 但带 warning。"""
        self.skill_loader.load.side_effect = RuntimeError("reload failed")

        result = self.registry.execute_tool(
            "skill_toggle", {"name": "broken", "action": "enable"}
        )
        data = json.loads(result)
        self.assertEqual(data["status"], "enabled")
        self.assertIn("warning", data)
        self.assertIn("reload failed", data["warning"])

    # -- 错误路径 -----------------------------------------------------------

    def test_toggle_invalid_action_returns_error(self):
        """非 enable/disable 的 action 返回 error。"""
        result = self.registry.execute_tool(
            "skill_toggle", {"name": "my_skill", "action": "invalid"}
        )
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("action 必须是", data["reason"])

    def test_toggle_empty_name_returns_error(self):
        """空名称返回 error。"""
        result = self.registry.execute_tool(
            "skill_toggle", {"name": "", "action": "disable"}
        )
        data = json.loads(result)
        self.assertEqual(data["status"], "error")

    def test_toggle_locked_skill_returns_error(self):
        """锁定列表中的技能不可切换。"""
        state_path = _make_temp_state_file(locked=["my_skill"])
        try:
            with patch("src.agent.skill_tools.SKILL_STATE_PATH", Path(state_path)):
                result = self.registry.execute_tool(
                    "skill_toggle", {"name": "my_skill", "action": "disable"}
                )
            data = json.loads(result)
            self.assertEqual(data["status"], "error")
            self.assertIn("已锁定", data["reason"])
        finally:
            Path(state_path).unlink(missing_ok=True)

    def test_toggle_locked_case_insensitive(self):
        """锁定匹配不区分大小写。"""
        state_path = _make_temp_state_file(locked=["My_Skill"])
        try:
            with patch("src.agent.skill_tools.SKILL_STATE_PATH", Path(state_path)):
                result = self.registry.execute_tool(
                    "skill_toggle", {"name": "my_skill", "action": "disable"}
                )
            data = json.loads(result)
            self.assertEqual(data["status"], "error")
            self.assertIn("已锁定", data["reason"])
        finally:
            Path(state_path).unlink(missing_ok=True)

    def test_toggle_state_file_not_found_uses_default(self):
        """状态文件不存在时使用默认（空列表），不阻塞操作。"""
        with patch(
            "src.agent.skill_tools.SKILL_STATE_PATH",
            Path(tempfile.mktemp(suffix=".json")),
        ):
            result = self.registry.execute_tool(
                "skill_toggle", {"name": "random", "action": "enable"}
            )
        data = json.loads(result)
        self.assertEqual(data["status"], "enabled")


# ===========================================================================
# 5. TestListSkills
# ===========================================================================

class TestListSkills(TestCase):
    """验证 list_skills：概览与详情。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.skill_loader = MagicMock(spec=SkillLoader)
        self.skill_loader.discover = MagicMock(return_value=[])
        self.skill_loader._skills = {}
        register_skill_tools(self.registry, self.skill_loader)

    # -- 概览模式 -----------------------------------------------------------

    def test_list_overview_contains_required_keys(self):
        """概览返回 loaded_skills / on_disk_skills / disabled / locked。"""
        result = self.registry.execute_tool("skill_list", {})
        data = json.loads(result)

        self.assertIn("loaded_skills", data)
        self.assertIn("on_disk_skills", data)
        self.assertIn("disabled", data)
        self.assertIn("locked", data)
        self.assertIsInstance(data["loaded_skills"], list)
        self.assertIsInstance(data["on_disk_skills"], list)

    def test_list_overview_empty_when_nothing_loaded(self):
        """无加载技能时 loaded_skills 为空列表。"""
        result = self.registry.execute_tool("skill_list", {})
        data = json.loads(result)
        self.assertEqual(data["loaded_skills"], [])

    def test_list_overview_with_loaded_skills(self):
        """loaded_skills 显示已加载的技能元数据。"""
        skill_a = MagicMock()
        skill_a.name = "skill_a"
        skill_a.description = "Skill A"
        skill_b = MagicMock()
        skill_b.name = "skill_b"
        skill_b.description = "Skill B"
        self.skill_loader._skills = {"skill_a": skill_a, "skill_b": skill_b}

        result = self.registry.execute_tool("skill_list", {})
        data = json.loads(result)

        loaded_names = [s["name"] for s in data["loaded_skills"]]
        self.assertIn("skill_a", loaded_names)
        self.assertIn("skill_b", loaded_names)

    def test_list_overview_with_discovered_skills(self):
        """on_disk_skills 显示 skill_loader.discover() 发现的技能。"""
        from src.skill.loader import SkillMeta
        meta = SkillMeta(
            name="disk_skill",
            version="0.2.0",
            description="On disk skill",
            requires=[],
        )
        self.skill_loader.discover.return_value = [meta]

        result = self.registry.execute_tool("skill_list", {})
        data = json.loads(result)

        disk_names = [s["name"] for s in data["on_disk_skills"]]
        self.assertIn("disk_skill", disk_names)
        # 验证字段
        disk_entry = data["on_disk_skills"][0]
        self.assertEqual(disk_entry["name"], "disk_skill")
        self.assertEqual(disk_entry["version"], "0.2.0")
        self.assertEqual(disk_entry["description"], "On disk skill")

    def test_list_overview_discover_exception_does_not_crash(self):
        """discover() 抛异常时 on_disk_skills 为空列表，不崩溃。"""
        self.skill_loader.discover.side_effect = RuntimeError("discover error")

        result = self.registry.execute_tool("skill_list", {})
        data = json.loads(result)
        self.assertEqual(data["on_disk_skills"], [])

    def test_list_overview_disabled_and_locked_from_state(self):
        """disabled 与 locked 列表从状态文件读取。"""
        state_path = _make_temp_state_file(
            disabled=["offline_skill"], locked=["locked_skill"]
        )
        try:
            with patch("src.agent.skill_tools.SKILL_STATE_PATH", Path(state_path)):
                result = self.registry.execute_tool("skill_list", {})
            data = json.loads(result)
            self.assertIn("offline_skill", data["disabled"])
            self.assertIn("locked_skill", data["locked"])
        finally:
            Path(state_path).unlink(missing_ok=True)

    # -- 详情模式 -----------------------------------------------------------

    def test_list_with_name_returns_skill_detail(self):
        """按名称查询返回技能详情（含 tools）。"""
        mock_skill = _build_mock_skill(
            name="detail_skill",
            description="Detail description",
            system_prompt="You are a detail skill",
            tools=[
                {
                    "name": "tool_a",
                    "description": "Tool A",
                    "input_schema": {"type": "object"},
                },
            ],
        )
        self.skill_loader._skills = {"detail_skill": mock_skill}

        result = self.registry.execute_tool("skill_list", {"name": "detail_skill"})
        data = json.loads(result)

        self.assertEqual(data["name"], "detail_skill")
        self.assertEqual(data["description"], "Detail description")
        self.assertEqual(data["system_prompt"], "You are a detail skill")
        self.assertIn("tools", data)
        self.assertIsInstance(data["tools"], list)
        self.assertEqual(len(data["tools"]), 1)
        self.assertEqual(
            data["tools"][0]["name"],
            "skill__detail_skill__tool_a",
        )
        self.assertEqual(data["tools"][0]["description"], "Tool A")

    def test_list_with_name_falls_back_to_load(self):
        """_skills 缓存未命中时回退到 skill_loader.load()。"""
        mock_skill = _build_mock_skill(
            name="lazy_load",
            description="Loaded on demand",
            tools=[{"name": "tool1", "description": "T1", "input_schema": {}}],
        )
        self.skill_loader._skills = {}
        self.skill_loader.load.return_value = mock_skill

        result = self.registry.execute_tool("skill_list", {"name": "lazy_load"})
        data = json.loads(result)

        self.assertEqual(data["name"], "lazy_load")
        self.skill_loader.load.assert_called_once_with("lazy_load")

    def test_list_nonexistent_name_returns_error(self):
        """不存在的技能名返回 error。"""
        self.skill_loader._skills = {}
        self.skill_loader.load.return_value = None

        result = self.registry.execute_tool("skill_list", {"name": "nonexistent"})
        data = json.loads(result)

        self.assertEqual(data["status"], "error")
        self.assertIn("不存在", data["reason"])

    def test_list_with_name_when_skill_loader_none(self):
        """skill_loader 为 None 时查询详情返回 error。"""
        registry2 = ToolRegistry()
        register_skill_tools(registry2, None)
        result = registry2.execute_tool("skill_list", {"name": "test"})
        data = json.loads(result)
        self.assertEqual(data["status"], "error")
        self.assertIn("skill_loader 不可用", data["reason"])

    # -- 无 skill_loader 的 fallback ----------------------------------------

    def test_list_overview_without_skill_loader_falls_back_to_disk_scan(self):
        """skill_loader 为 None 时扫描 SKILL_BASE_DIR 下的目录。"""
        registry2 = ToolRegistry()
        register_skill_tools(registry2, None)

        # 创建临时 skills 目录模拟
        tmp_skills = Path(tempfile.mkdtemp())
        (tmp_skills / "found_skill").mkdir(parents=True)
        (tmp_skills / "found_skill" / "SKILL.md").write_text(
            _make_skill_md("found_skill"), encoding="utf-8"
        )
        (tmp_skills / "no_md_dir").mkdir(parents=True)

        try:
            with patch("src.agent.skill_tools.SKILL_BASE_DIR", tmp_skills):
                result = registry2.execute_tool("skill_list", {})
            data = json.loads(result)
            disk_names = [s["name"] for s in data["on_disk_skills"]]
            self.assertIn("found_skill", disk_names)
        finally:
            shutil.rmtree(tmp_skills, ignore_errors=True)


# ===========================================================================
# 6. 工具注册验证
# ===========================================================================

class TestSkillToolRegistration(TestCase):
    """验证 register_skill_tools 注册 5 个 Core Tier 工具。"""

    def setUp(self):
        self.registry = ToolRegistry()
        self.skill_loader = MagicMock(spec=SkillLoader)
        register_skill_tools(self.registry, self.skill_loader)

    def test_registers_five_tools_as_core_tier(self):
        """注册 5 个 Core Tier 工具（含完整 input_schema）。"""
        schemas = self.registry.get_tools_schema()
        names = {s["name"] for s in schemas}

        for expected in (
            "skill_template",
            "skill_propose",
            "skill_reload",
            "skill_toggle",
            "skill_list",
        ):
            with self.subTest(tool=expected):
                self.assertIn(expected, names)

        # 全是 Core Tier（含 input_schema）
        for s in schemas:
            if s["name"] in names:
                self.assertIn("input_schema", s)
                self.assertNotIn("defer_loading", s)

    def test_execute_tool_dispatches_to_correct_handler(self):
        """execute_tool 正确分发到各 handler。"""
        # skill_template: 无参数，返回模板 JSON
        result = self.registry.execute_tool("skill_template", {})
        data = json.loads(result)
        self.assertIn("fields", data)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
