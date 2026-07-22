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

from teage_liu.agent.policy import PolicyEngine, Decision, DEFAULT_RULES  # noqa: E402
from teage_liu.agent.file_registry import FileOperationRegistry  # noqa: E402


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

    def test_profile_update_not_in_default_rules(self):
        """profile_update 不在 DEFAULT_RULES 中（默认放行 allow），不触发 HIL。

        用户画像沉淀依赖 SignalPool 阈值累积 + handler 三层防护，无需 HIL 拦截。
        """
        # 断言 DEFAULT_RULES 中无 profile_update 规则
        rule_tools = [r["tool"] for r in DEFAULT_RULES]
        self.assertNotIn("profile_update", rule_tools)
        # check 返回 allow / low
        engine = PolicyEngine()
        decision = engine.check("profile_update", {"content": "some content"})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.risk_level, "low")

    def test_profile_update_tool_kind_memory(self):
        """_compute_tool_kind 仍将 profile_update 归类为 memory（监控/日志用）。"""
        engine = PolicyEngine()
        self.assertEqual(engine._compute_tool_kind("profile_update"), "memory")


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


class TestPolicyEngineEnabledSetter(unittest.TestCase):
    """验证 enabled property setter 支持热更新翻转。"""

    def test_enabled_property_reads_init_value(self):
        """enabled property 返回 __init__ 设置的值。"""
        engine_on = PolicyEngine(enabled=True)
        engine_off = PolicyEngine(enabled=False)
        self.assertTrue(engine_on.enabled)
        self.assertFalse(engine_off.enabled)

    def test_setter_true_to_false_allows_all(self):
        """enabled=True→False 后 check() 一律放行（即使规则是 confirm/deny）。"""
        engine = PolicyEngine(enabled=True)
        # 翻转前 bash_exec 默认 confirm
        d_before = engine.check("bash_exec", {"command": "del foo"}, session_id="s1")
        self.assertEqual(d_before.action, "confirm")
        # 翻转为 False
        engine.enabled = False
        self.assertFalse(engine.enabled)
        # 翻转后一律 allow
        d_after = engine.check("bash_exec", {"command": "del foo"}, session_id="s1")
        self.assertEqual(d_after.action, "allow")

    def test_setter_false_to_true_restores_decisions(self):
        """enabled=False→True 后 check() 恢复正常决策。"""
        engine = PolicyEngine(enabled=False)
        # 关闭时 allow
        d_off = engine.check("bash_exec", {"command": "del foo"}, session_id="s1")
        self.assertEqual(d_off.action, "allow")
        # 重新开启
        engine.enabled = True
        self.assertTrue(engine.enabled)
        # 恢复 confirm
        d_on = engine.check("bash_exec", {"command": "del foo"}, session_id="s1")
        self.assertEqual(d_on.action, "confirm")

    def test_setter_bool_coercion(self):
        """setter 对非布尔值做 bool() 强制转换。"""
        engine = PolicyEngine(enabled=True)
        engine.enabled = 0  # 0 → False
        self.assertFalse(engine.enabled)
        engine.enabled = "yes"  # 非空字符串 → True
        self.assertTrue(engine.enabled)
        engine.enabled = None  # None → False
        self.assertFalse(engine.enabled)

    def test_setter_idempotent_no_log_when_unchanged(self):
        """setter 设置相同值时不触发变更日志（值未变）。"""
        engine = PolicyEngine(enabled=True)
        # 设置相同值，_enabled 仍为 True
        engine.enabled = True
        self.assertTrue(engine.enabled)
        # 再设置 False 触发变更
        engine.enabled = False
        self.assertFalse(engine.enabled)


class TestPolicyEngineReadPath(unittest.TestCase):
    """P0 止血：读路径黑名单验证。

    验证 file_read/file_listdir/file_glob/file_grep/file_query 在
    deny_first 模式下命中黑名单返回 deny，未命中返回 None（不阻断）。
    """

    def test_file_read_src_denied(self):
        """file_read 读 teage_liu/server.py → deny，reason 含'黑名单'。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_read", {"path": "teage_liu/server.py"})
        self.assertEqual(d.action, "deny")
        self.assertIn("黑名单", d.reason)

    def test_file_read_data_uploads_allowed(self):
        """file_read 读 data/uploads/x.txt → 未命中黑名单，继续走后续规则（最终 allow）。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_read", {"path": "data/uploads/x.txt"})
        # file_read 不在 DEFAULT_RULES 中，未命中黑名单 → allow
        self.assertEqual(d.action, "allow")

    def test_file_listdir_tests_denied(self):
        """file_listdir 列 tests/ 目录 → deny。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_listdir", {"dir": "tests/"})
        self.assertEqual(d.action, "deny")

    def test_file_grep_git_denied(self):
        """file_grep 在 .git/ 下搜索 → deny。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_grep", {"path": ".git/config", "query": "foo"})
        self.assertEqual(d.action, "deny")

    def test_file_read_config_yaml_denied(self):
        """file_read 读 config.yaml → deny。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_read", {"path": "config.yaml"})
        self.assertEqual(d.action, "deny")

    def test_file_read_no_path_not_blocked(self):
        """file_read 无 path 参数 → _check_read_path 返回 None，不阻断。"""
        engine = PolicyEngine(enabled=True)
        # 无 path 参数，read_path 检查返回 None，继续走后续规则
        d = engine.check("file_read", {})
        self.assertEqual(d.action, "allow")

    def test_file_read_env_denied(self):
        """file_read 读 .env → deny。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_read", {"path": ".env"})
        self.assertEqual(d.action, "deny")

    def test_file_read_pyc_denied(self):
        """file_read 读 *.pyc → deny（通配匹配）。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_read", {"path": "src/__pycache__/foo.cpython-310.pyc"})
        self.assertEqual(d.action, "deny")

    def test_file_read_workspace_data_allowed(self):
        """file_read 读 data/ 下任意文件 → allow。"""
        engine = PolicyEngine(enabled=True)
        d = engine.check("file_read", {"path": "data/memory.md"})
        self.assertEqual(d.action, "allow")

    def test_read_paths_config_override(self):
        """read_paths_config 自定义配置覆盖默认黑名单。"""
        engine = PolicyEngine(
            enabled=True,
            read_paths_config={
                "mode": "deny_first",
                "deny": ["secret/"],
                "allow": [],
            },
        )
        # secret/ 在自定义黑名单中 → deny
        d = engine.check("file_read", {"path": "secret/passwords.txt"})
        self.assertEqual(d.action, "deny")
        # teage_liu/ 不在自定义黑名单中 → allow
        d2 = engine.check("file_read", {"path": "teage_liu/server.py"})
        self.assertEqual(d2.action, "allow")


class TestPolicyEngineMcpHil(unittest.TestCase):
    """P1-7: MCP HIL 前缀匹配验证。"""

    def test_mcp_hil_false_allows_directly(self):
        """hil=False 的 MCP server 调用直接 allow。"""
        engine = PolicyEngine(
            enabled=True,
            mcp_hil_config={"filesystem": False},
        )
        d = engine.check("mcp__filesystem__read_file", {"path": "data/test.txt"})
        self.assertEqual(d.action, "allow")
        self.assertIn("可信", d.reason)

    def test_mcp_hil_true_triggers_confirm(self):
        """hil=True 的 MCP server 调用走 confirm。"""
        engine = PolicyEngine(
            enabled=True,
            mcp_hil_config={"custom-tool": True},
        )
        d = engine.check("mcp__custom-tool__do_stuff", {"arg": "value"})
        self.assertEqual(d.action, "confirm")
        self.assertIn("hil=true", d.reason)

    def test_mcp_default_hil_true_when_not_configured(self):
        """未配置的 MCP server 默认 hil=True（安全优先）。"""
        engine = PolicyEngine(enabled=True, mcp_hil_config={})
        d = engine.check("mcp__unknown__tool", {})
        self.assertEqual(d.action, "confirm")

    def test_mcp_set_hil_config_updates(self):
        """set_mcp_hil_config 动态更新配置。"""
        engine = PolicyEngine(enabled=True, mcp_hil_config={})
        # 初始未配置 → confirm
        d1 = engine.check("mcp__fs__read", {})
        self.assertEqual(d1.action, "confirm")
        # 动态设置为可信 → allow
        engine.set_mcp_hil_config({"fs": False})
        d2 = engine.check("mcp__fs__read", {})
        self.assertEqual(d2.action, "allow")

    def test_mcp_disabled_engine_allows_all(self):
        """enabled=False 时 MCP 工具也走 allow 路径。"""
        engine = PolicyEngine(
            enabled=False,
            mcp_hil_config={"custom": True},
        )
        d = engine.check("mcp__custom__tool", {})
        self.assertEqual(d.action, "allow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
