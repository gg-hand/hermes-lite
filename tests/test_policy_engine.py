"""PolicyEngine 单元测试 — 验证策略评估器的默认规则、配置覆盖、
元工具内省、正则匹配与规则校验逻辑。

运行方式:
    python -m unittest tests.test_policy_engine -v
    python tests/test_policy_engine.py
"""

from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.policy import PolicyEngine, Decision, DEFAULT_RULES  # noqa: E402
from src.agent.file_registry import FileOperationRegistry  # noqa: E402


class TestPolicyEngineDefaults(unittest.TestCase):
    """验证 DEFAULT_RULES 默认规则下的策略评估结果。"""

    def test_default_rules_allow_read_file(self):
        """未配置时 read_file 未在 DEFAULT_RULES 中，返回 allow / low。"""
        engine = PolicyEngine()
        decision = engine.check("file_read", {"path": "a.txt"})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.risk_level, "low")

    def test_default_rules_confirm_execute_command(self):
        """未配置时 execute_command 走命令分类器：other 类默认放行 allow / low。

        注：黑名单为主策略改造后，``mkdir test_dir``（非读取白名单/删除黑名单）
        归 other → 默认放行 allow，reason 标注"未知命令默认放行"以便审计追踪。
        原 confirm 行为已迁移到黑名单命中路径（如 ``git push --force``）。
        """
        engine = PolicyEngine()
        decision = engine.check("bash_exec", {"command": "mkdir test_dir"})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.risk_level, "low")
        self.assertEqual(decision.reason, "未知命令默认放行")

    def test_default_rules_confirm_blacklist_command(self):
        """黑名单命令（``git push --force``）→ confirm / high（黑名单拦截）。

        黑名单为主策略下，高危命令（强制推送/硬重置/删除/重定向）仍走 confirm，
        与未知命令默认放行形成对照。
        """
        engine = PolicyEngine()
        decision = engine.check(
            "bash_exec", {"command": "git push --force origin main"}
        )
        self.assertEqual(decision.action, "confirm")
        self.assertEqual(decision.risk_level, "high")
        self.assertEqual(decision.reason, "删除类命令")

    def test_default_rules_confirm_git_reset_hard(self):
        """``git reset --hard`` → confirm / high（黑名单拦截）。"""
        engine = PolicyEngine()
        decision = engine.check(
            "bash_exec", {"command": "git reset --hard HEAD~1"}
        )
        self.assertEqual(decision.action, "confirm")
        self.assertEqual(decision.risk_level, "high")

    def test_default_rules_allow_read_command(self):
        """读取类命令（``ls``）→ allow / low（白名单加速）。"""
        engine = PolicyEngine()
        decision = engine.check("bash_exec", {"command": "ls"})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.risk_level, "low")
        self.assertEqual(decision.reason, "读取类命令")

    def test_default_rules_confirm_write_file(self):
        """未配置时 write_file 命中 DEFAULT_RULES，返回 confirm / high。"""
        engine = PolicyEngine()
        decision = engine.check("file_write", {"path": "a.txt"})
        self.assertEqual(decision.action, "confirm")
        self.assertEqual(decision.risk_level, "high")

    def test_default_rules_allow_http_request(self):
        """未配置时 http_request 未在 DEFAULT_RULES 中（访问类放行），返回 allow / low。"""
        engine = PolicyEngine()
        decision = engine.check("web_fetch", {"url": "http://example.com"})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.risk_level, "low")

    def test_default_rules_confirm_call_tool(self):
        """未配置时 call_tool 命中 DEFAULT_RULES，返回 confirm / high。"""
        engine = PolicyEngine()
        decision = engine.check("tool_call", {"name": "some_tool"})
        self.assertEqual(decision.action, "confirm")
        self.assertEqual(decision.risk_level, "high")


class TestPolicyEngineConfigOverride(unittest.TestCase):
    """验证配置 rules 覆盖默认规则与 enabled=False 行为。"""

    def test_config_rules_override_default(self):
        """配置 rules 后使用配置规则（覆盖 DEFAULT_RULES）。"""
        engine = PolicyEngine(rules=[{"tool": "file_read", "risk": "deny", "reason": "test"}])
        decision = engine.check("file_read", {})
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "test")

    def test_disabled_always_allow(self):
        """enabled=False 时一律放行，忽略规则中的 deny。"""
        engine = PolicyEngine(enabled=False, rules=[{"tool": "bash_exec", "risk": "deny"}])
        decision = engine.check("bash_exec", {})
        self.assertEqual(decision.action, "allow")

    def test_from_config_uses_default_when_rules_empty(self):
        """from_config 空 rules 列表时回退到 DEFAULT_RULES。"""
        engine = PolicyEngine.from_config({"enabled": True, "rules": []})
        decision = engine.check("bash_exec", {})
        self.assertEqual(decision.action, "confirm")

    def test_from_config_uses_default_when_rules_missing(self):
        """from_config 缺失 rules 字段时回退到 DEFAULT_RULES。"""
        engine = PolicyEngine.from_config({})
        decision = engine.check("file_write", {})
        self.assertEqual(decision.action, "confirm")


class TestPolicyEngineCallToolIntrospection(unittest.TestCase):
    """验证 call_tool 元工具内省：先按内层工具名查规则，未命中回退自身。"""

    def test_call_tool_introspection_inner_match(self):
        """call_tool 内省内层工具名命中规则时返回内层规则决策。"""
        engine = PolicyEngine(rules=[{"tool": "dangerous_tool", "risk": "deny", "reason": "dangerous"}])
        decision = engine.check("tool_call", {"name": "dangerous_tool"})
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "dangerous")

    def test_call_tool_fallback_to_self(self):
        """内层工具名未命中规则时回退到 call_tool 自身规则。"""
        engine = PolicyEngine(rules=[{"tool": "tool_call", "risk": "confirm", "reason": "default call_tool confirm"}])
        decision = engine.check("tool_call", {"name": "some_unknown_tool"})
        self.assertEqual(decision.action, "confirm")
        self.assertEqual(decision.reason, "default call_tool confirm")

    def test_call_tool_no_inner_name_fallback_to_self(self):
        """call_tool 无 name 字段时回退到 call_tool 自身规则。"""
        engine = PolicyEngine(rules=[{"tool": "tool_call", "risk": "confirm", "reason": "tool_call"}])
        decision = engine.check("tool_call", {})
        self.assertEqual(decision.action, "confirm")


class TestPolicyEngineToolPattern(unittest.TestCase):
    """验证 tool_pattern 正则匹配与精确匹配优先级。"""

    def test_tool_pattern_regex_match(self):
        """tool_pattern 正则匹配工具名时返回对应决策。"""
        engine = PolicyEngine(rules=[{"tool_pattern": "skill__.*__dangerous_.*", "risk": "deny", "reason": "dangerous skill"}])
        decision = engine.check("skill__foo__dangerous_action", {})
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "dangerous skill")

    def test_tool_pattern_no_match_returns_allow(self):
        """tool_pattern 不匹配时返回 allow。"""
        engine = PolicyEngine(rules=[{"tool_pattern": "skill__.*__dangerous_.*", "risk": "deny"}])
        decision = engine.check("file_read", {})
        self.assertEqual(decision.action, "allow")

    def test_exact_match_priority_over_pattern(self):
        """精确匹配优先于正则匹配（即使正则能匹配该工具名）。"""
        engine = PolicyEngine(rules=[
            {"tool_pattern": ".*", "risk": "deny", "reason": "pattern"},
            {"tool": "file_read", "risk": "allow", "reason": "exact"},
        ])
        decision = engine.check("file_read", {})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.reason, "exact")


class TestPolicyEngineRuleValidation(unittest.TestCase):
    """验证规则校验：非法 risk / 缺失 tool 与 tool_pattern / 无匹配。"""

    def test_invalid_risk_skipped(self):
        """risk 字段非法时跳过该规则，后续规则仍可生效。"""
        engine = PolicyEngine(rules=[
            {"tool": "x", "risk": "invalid_risk"},
            {"tool": "bash_exec", "risk": "confirm", "reason": "ok"},
        ])
        # 第二条规则生效
        decision = engine.check("bash_exec", {})
        self.assertEqual(decision.action, "confirm")
        # 第一条被跳过，无匹配返回 allow
        decision = engine.check("x", {})
        self.assertEqual(decision.action, "allow")

    def test_missing_tool_and_pattern_skipped(self):
        """tool 与 tool_pattern 都缺失时跳过该规则。"""
        engine = PolicyEngine(rules=[
            {"risk": "deny"},
            {"tool": "bash_exec", "risk": "confirm"},
        ])
        decision = engine.check("bash_exec", {})
        self.assertEqual(decision.action, "confirm")

    def test_no_match_returns_allow(self):
        """未匹配任何规则时返回 allow。"""
        engine = PolicyEngine(rules=[{"tool": "x", "risk": "deny"}])
        decision = engine.check("unknown_tool", {})
        self.assertEqual(decision.action, "allow")


class TestPolicyEngineV2ParamAware(unittest.TestCase):
    """v2 参数感知：注入 file_registry 后 write_file / delete_file 按文件状态决策。"""

    def setUp(self):
        """每个测试创建独立的 file_registry 与 session_id。"""
        import tempfile
        self.registry = FileOperationRegistry()
        self.session_id = "test-session-v2"
        # 临时目录用于创建真实文件
        self.tmpdir = tempfile.mkdtemp(prefix="policy_v2_test_")

    def _path(self, name: str) -> str:
        """在临时目录下拼接路径名，返回 str。"""
        import os
        return os.path.join(self.tmpdir, name)

    def _make_file(self, name: str, content: str = "x") -> str:
        """在临时目录下创建一个真实文件并返回其路径。"""
        p = self._path(name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    # ------------------------------------------------------------------
    # write_file 决策表
    # ------------------------------------------------------------------
    def test_write_file_new_file_allow(self):
        """新建文件（路径不存在）→ allow / low。"""
        engine = PolicyEngine(file_registry=self.registry)
        path = self._path("not_exist.py")
        d = engine.check("file_write", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "新建文件")
        self.assertEqual(d.risk_level, "low")

    def test_write_file_created_in_session_allow(self):
        """会话内创建的文件 → allow / low（修改自己创建的文件）。"""
        path = self._make_file("scratch.py")
        self.registry.record_write(self.session_id, path, is_new=True)
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_write", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "修改会话内创建的文件")
        self.assertEqual(d.risk_level, "low")

    def test_write_file_modified_in_session_allow(self):
        """已确认过一次修改的文件 → allow / low（不再重复 confirm）。"""
        path = self._make_file("config.yaml")
        self.registry.record_write(self.session_id, path, is_new=False)
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_write", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "已确认过的修改")
        self.assertEqual(d.risk_level, "low")

    def test_write_file_user_file_first_modify_confirm(self):
        """用户已有文件首次修改 → confirm / high。"""
        path = self._make_file("user_existing.txt")
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_write", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "首次修改用户文件")
        self.assertEqual(d.risk_level, "high")

    def test_write_file_no_session_id_fallback_v1(self):
        """无 session_id 时退化到 v1 工具名匹配（命中 DEFAULT_RULES → confirm）。"""
        path = self._make_file("any.txt")
        engine = PolicyEngine(file_registry=self.registry)
        # 不传 session_id
        d = engine.check("file_write", {"path": path})
        self.assertEqual(d.action, "confirm")
        # reason 为 DEFAULT_RULES 中的 reason
        self.assertEqual(d.reason, "写入文件会修改或覆盖磁盘内容")

    def test_write_file_no_file_registry_fallback_v1(self):
        """未注入 file_registry 时退化到 v1 工具名匹配。"""
        path = self._make_file("any.txt")
        engine = PolicyEngine()  # 无 file_registry
        d = engine.check("file_write", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "confirm")

    def test_write_file_no_path_field_fallback(self):
        """tool_input 无 path 字段时降级到工具名匹配（防御）。"""
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_write", {}, session_id=self.session_id)
        # 命中 DEFAULT_RULES
        self.assertEqual(d.action, "confirm")

    # ------------------------------------------------------------------
    # delete_file 决策表
    # ------------------------------------------------------------------
    def test_delete_file_created_in_session_allow(self):
        """删除会话内创建的文件 → allow / low（用户要求：会话内 create_file 享有删除权限）。"""
        path = self._make_file("scratch_to_delete.py")
        self.registry.record_write(self.session_id, path, is_new=True)
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_delete", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "删除会话内创建的文件")
        self.assertEqual(d.risk_level, "low")

    def test_delete_file_modified_in_session_confirm(self):
        """删除会话内修改过的文件 → confirm / high。"""
        path = self._make_file("user_modified.txt")
        self.registry.record_write(self.session_id, path, is_new=False)
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_delete", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除会话内修改过的文件")
        self.assertEqual(d.risk_level, "high")

    def test_delete_file_user_file_confirm(self):
        """删除用户已有文件 → confirm / high。"""
        path = self._make_file("user_proprietary.txt")
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_delete", {"path": path}, session_id=self.session_id)
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除用户文件")
        self.assertEqual(d.risk_level, "high")

    def test_delete_file_no_session_id_allow_fallback(self):
        """delete_file 未在 DEFAULT_RULES，无 session_id 时返回 allow（向后兼容）。"""
        path = self._make_file("any.txt")
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_delete", {"path": path})
        self.assertEqual(d.action, "allow")

    def test_delete_file_no_path_field_allow(self):
        """delete_file tool_input 无 path 字段 → allow（防御）。"""
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_delete", {}, session_id=self.session_id)
        self.assertEqual(d.action, "allow")

    # ------------------------------------------------------------------
    # 会话隔离
    # ------------------------------------------------------------------
    def test_write_file_session_isolation(self):
        """会话 A 创建的文件不会让会话 B 的 write_file 走会话内创建豁免。"""
        path = self._make_file("session_a.py")
        # 会话 A 记录创建
        self.registry.record_write("session-A", path, is_new=True)
        engine = PolicyEngine(file_registry=self.registry)
        # 会话 B 查询此文件 → 应视为用户文件首次修改（confirm）
        d = engine.check("file_write", {"path": path}, session_id="session-B")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "首次修改用户文件")

    def test_delete_file_session_isolation(self):
        """会话 A 创建的文件，会话 B 删除时不享受豁免。"""
        path = self._make_file("session_a_del.py")
        self.registry.record_write("session-A", path, is_new=True)
        engine = PolicyEngine(file_registry=self.registry)
        # 会话 B 删除 → confirm
        d = engine.check("file_delete", {"path": path}, session_id="session-B")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除用户文件")

    # ------------------------------------------------------------------
    # 其他工具不受影响
    # ------------------------------------------------------------------
    def test_other_tools_unaffected_by_v2(self):
        """其他工具（如 read_file）不受 v2 参数感知影响，按 v1 规则匹配。"""
        engine = PolicyEngine(file_registry=self.registry)
        d = engine.check("file_read", {"path": "any.txt"}, session_id=self.session_id)
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.risk_level, "low")

    def test_call_tool_still_introspection_with_v2(self):
        """注入 file_registry 后 call_tool 内省仍正常工作。"""
        engine = PolicyEngine(
            rules=[{"tool": "dangerous_tool", "risk": "deny", "reason": "dangerous"}],
            file_registry=self.registry,
        )
        d = engine.check(
            "tool_call", {"name": "dangerous_tool"}, session_id=self.session_id
        )
        self.assertEqual(d.action, "deny")
        self.assertEqual(d.reason, "dangerous")


class TestPolicyEngineV2FromConfig(unittest.TestCase):
    """验证 from_config 透传 file_registry。"""

    def test_from_config_passes_file_registry(self):
        """from_config 接受 file_registry 参数并透传给 PolicyEngine。"""
        registry = FileOperationRegistry()
        engine = PolicyEngine.from_config({}, file_registry=registry)
        self.assertIs(engine._file_registry, registry)

    def test_from_config_without_file_registry(self):
        """from_config 不传 file_registry 时为 None（向后兼容）。"""
        engine = PolicyEngine.from_config({})
        self.assertIsNone(engine._file_registry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
