"""Phase 8 Task 4: PolicyEngine cron 预授权路径 + granted_tools 校验测试套件。

覆盖 SubTask 4.1 / 4.2 / 4.3 / 4.7 / 4.8 的策略层实现：
- SubTask 4.1: ``validate_granted_tools`` 严格/非严格校验、路径约束硬约束
- SubTask 4.2: PolicyEngine cron 路径三层检查（工具预授权 + 路径前缀匹配 +
  decision_source="schedule_grant"）
- SubTask 4.3: 硬禁止工具（``HARD_DISABLED_TOOLS``）绝不预授权（defense in depth）
- SubTask 4.7: 预授权端到端场景（预授权正常 / 路径越界 / 硬禁止工具）
- SubTask 4.8: 回归测试

设计要点：
- 使用 ``_MockCronScheduler`` 与 ``_MockSchedule``，避免依赖真实持久化与 cron 计算
- 三层检查通过 → allow + decision_source="schedule_grant"
- 未通过任一层 → 回退默认规则（不直接 deny，保持向后兼容与默认安全策略）
- 路径前缀匹配测试覆盖边界条件（精确匹配 / 子路径匹配 / 路径越界 / 无路径字段）

运行方式:
    python -m unittest tests.test_policy_cron -v
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.policy import PolicyEngine, Decision  # noqa: E402
from src.agent.cron_tools import HARD_DISABLED_TOOLS  # noqa: E402
from src.tasks.scheduler import (  # noqa: E402
    Schedule,
    validate_granted_tools,
    _VALID_GRANT_SCOPES,
    _PATH_CONSTRAINED_TOOLS,
)


# ---------------------------------------------------------------------------
# Mock 对象
# ---------------------------------------------------------------------------


class _MockSchedule:
    """轻量 mock Schedule，仅暴露 granted_tools 与 _find_schedule 所需字段。"""

    def __init__(self, schedule_id: str, granted_tools: Optional[List[Dict[str, Any]]] = None):
        self.id = schedule_id
        self.granted_tools = granted_tools


class _MockCronScheduler:
    """mock CronScheduler，``_find_schedule`` 返回预设的调度项。"""

    def __init__(self, schedules: Optional[Dict[str, _MockSchedule]] = None):
        self._schedules = schedules or {}

    def _find_schedule(self, schedule_id: str) -> Optional[_MockSchedule]:
        return self._schedules.get(schedule_id)


# ---------------------------------------------------------------------------
# SubTask 4.1: validate_granted_tools 校验测试
# ---------------------------------------------------------------------------


class TestValidateGrantedToolsStrict(unittest.TestCase):
    """strict=True 模式下的 granted_tools 校验。"""

    def test_none_returns_empty(self):
        """granted_tools=None 返回空列表（合法，表示无预授权）。"""
        self.assertEqual(validate_granted_tools(None, strict=True), [])

    def test_empty_list_returns_empty(self):
        """空列表返回空列表。"""
        self.assertEqual(validate_granted_tools([], strict=True), [])

    def test_non_list_raises(self):
        """非 list 类型在 strict 模式抛 ValueError。"""
        for invalid in ("not_a_list", {"k": "v"}, 42, None):
            # None 已在 test_none_returns_empty 覆盖，跳过
            if invalid is None:
                continue
            with self.assertRaises(ValueError, msg=f"应抛 ValueError: {invalid!r}"):
                validate_granted_tools(invalid, strict=True)

    def test_non_dict_entry_raises(self):
        """非 dict 项在 strict 模式抛 ValueError。"""
        with self.assertRaises(ValueError):
            validate_granted_tools(["not_a_dict"], strict=True)

    def test_missing_tool_raises(self):
        """缺少非空 tool 字段在 strict 模式抛 ValueError。"""
        with self.assertRaises(ValueError):
            validate_granted_tools([{"scope": "all"}], strict=True)
        with self.assertRaises(ValueError):
            validate_granted_tools([{"tool": "", "scope": "all"}], strict=True)
        with self.assertRaises(ValueError):
            validate_granted_tools([{"tool": 123, "scope": "all"}], strict=True)

    def test_invalid_scope_raises(self):
        """scope 非 all/path_prefix 在 strict 模式抛 ValueError。"""
        with self.assertRaises(ValueError):
            validate_granted_tools(
                [{"tool": "file_read", "scope": "invalid_scope"}], strict=True
            )

    def test_non_list_allowed_paths_raises(self):
        """allowed_paths 非 list 在 strict 模式抛 ValueError。"""
        with self.assertRaises(ValueError):
            validate_granted_tools(
                [{"tool": "file_read", "scope": "all", "allowed_paths": "not_a_list"}],
                strict=True,
            )

    def test_valid_all_scope(self):
        """scope=all 的合法项通过校验。"""
        result = validate_granted_tools(
            [{"tool": "file_read", "scope": "all"}], strict=True
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "file_read")
        self.assertEqual(result[0]["scope"], "all")
        # allowed_paths 缺失时填充默认空列表
        self.assertEqual(result[0]["allowed_paths"], [])

    def test_valid_path_prefix_scope(self):
        """scope=path_prefix 的合法项通过校验。"""
        result = validate_granted_tools(
            [
                {
                    "tool": "file_write",
                    "scope": "path_prefix",
                    "allowed_paths": ["/data/cron"],
                }
            ],
            strict=True,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["allowed_paths"], ["/data/cron"])

    def test_path_constrained_tool_requires_path_prefix(self):
        """write_file / delete_file 必须 scope=path_prefix（scope=all 抛错）。"""
        for tool_name in _PATH_CONSTRAINED_TOOLS:
            with self.subTest(tool=tool_name):
                with self.assertRaises(ValueError):
                    validate_granted_tools(
                        [{"tool": tool_name, "scope": "all"}], strict=True
                    )

    def test_path_constrained_tool_requires_non_empty_allowed_paths(self):
        """write_file / delete_file 的 allowed_paths 不能为空。"""
        for tool_name in _PATH_CONSTRAINED_TOOLS:
            with self.subTest(tool=tool_name):
                with self.assertRaises(ValueError):
                    validate_granted_tools(
                        [{"tool": tool_name, "scope": "path_prefix", "allowed_paths": []}],
                        strict=True,
                    )

    def test_deep_copy_returns_independent_list(self):
        """返回的列表是深拷贝，修改不影响原输入。"""
        original = [{"tool": "file_read", "scope": "all", "allowed_paths": ["/a"]}]
        result = validate_granted_tools(original, strict=True)
        result[0]["allowed_paths"].append("/b")
        # 原输入不变
        self.assertEqual(original[0]["allowed_paths"], ["/a"])


class TestValidateGrantedToolsNonStrict(unittest.TestCase):
    """strict=False 模式：非法项跳过并 warning，返回合法项子集。"""

    def test_non_list_returns_empty(self):
        """非 list 在非 strict 模式返回空列表（不抛异常）。"""
        self.assertEqual(validate_granted_tools("not_a_list", strict=False), [])
        self.assertEqual(validate_granted_tools({"k": "v"}, strict=False), [])

    def test_non_dict_entry_skipped(self):
        """非 dict 项被跳过，合法项仍返回。"""
        result = validate_granted_tools(
            ["not_a_dict", {"tool": "file_read", "scope": "all"}], strict=False
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "file_read")

    def test_missing_tool_skipped(self):
        """缺少 tool 字段的项被跳过。"""
        result = validate_granted_tools(
            [{"scope": "all"}, {"tool": "file_read", "scope": "all"}], strict=False
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "file_read")

    def test_invalid_scope_skipped(self):
        """非法 scope 项被跳过。"""
        result = validate_granted_tools(
            [
                {"tool": "x", "scope": "invalid"},
                {"tool": "file_read", "scope": "all"},
            ],
            strict=False,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "file_read")

    def test_path_constrained_violation_skipped(self):
        """write_file scope=all 在非 strict 模式被跳过。"""
        result = validate_granted_tools(
            [
                {"tool": "file_write", "scope": "all"},  # 非法，跳过
                {"tool": "file_read", "scope": "all"},  # 合法
            ],
            strict=False,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "file_read")


class TestValidateGrantedToolsConstants(unittest.TestCase):
    """SubTask 4.3: 硬约束常量正确性。"""

    def test_valid_grant_scopes(self):
        """_VALID_GRANT_SCOPES 含 all 与 path_prefix。"""
        self.assertIn("all", _VALID_GRANT_SCOPES)
        self.assertIn("path_prefix", _VALID_GRANT_SCOPES)

    def test_path_constrained_tools_contains_write_and_delete(self):
        """_PATH_CONSTRAINED_TOOLS 含 write_file 与 delete_file。"""
        self.assertIn("file_write", _PATH_CONSTRAINED_TOOLS)
        self.assertIn("file_delete", _PATH_CONSTRAINED_TOOLS)

    def test_hard_disabled_tools_constant(self):
        """HARD_DISABLED_TOOLS 含三项硬禁止工具。"""
        self.assertEqual(
            HARD_DISABLED_TOOLS,
            {"memory_delete", "bash_exec", "tool_call"},
        )


# ---------------------------------------------------------------------------
# SubTask 4.2 + 4.7: PolicyEngine cron 预授权三层检查
# ---------------------------------------------------------------------------


class TestPolicyCronGrantBasic(unittest.TestCase):
    """cron 预授权基础场景：三层检查通过 → allow + schedule_grant。"""

    def setUp(self):
        """构造带 granted_tools 的 mock 调度项与 PolicyEngine。"""
        self.schedule_id = "sched-grant"
        self.schedule = _MockSchedule(
            schedule_id=self.schedule_id,
            granted_tools=[
                {"tool": "file_read", "scope": "all"},
                {
                    "tool": "file_write",
                    "scope": "path_prefix",
                    "allowed_paths": ["/data/cron"],
                },
            ],
        )
        self.cron_scheduler = _MockCronScheduler({self.schedule_id: self.schedule})
        self.engine = PolicyEngine(cron_scheduler=self.cron_scheduler)

    def _cron_session(self):
        """返回 cron: 前缀的 session_id。"""
        return f"cron:{self.schedule_id}"

    def test_grant_all_scope_allows(self):
        """scope=all 的预授权工具 → allow + schedule_grant。"""
        d = self.engine.check(
            "file_read",
            {"path": "/anywhere/file.txt"},
            session_id=self._cron_session(),
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.risk_level, "low")
        self.assertEqual(d.decision_source, "schedule_grant")
        self.assertEqual(d.reason, "schedule_grant")

    def test_grant_path_prefix_within_allowed(self):
        """scope=path_prefix 且路径在 allowed_paths 内 → allow + schedule_grant。"""
        # 精确匹配
        d1 = self.engine.check(
            "file_write",
            {"path": "/data/cron"},
            session_id=self._cron_session(),
        )
        self.assertEqual(d1.action, "allow")
        self.assertEqual(d1.decision_source, "schedule_grant")
        # 子路径匹配
        d2 = self.engine.check(
            "file_write",
            {"path": "/data/cron/sub/file.txt"},
            session_id=self._cron_session(),
        )
        self.assertEqual(d2.action, "allow")
        self.assertEqual(d2.decision_source, "schedule_grant")

    def test_grant_fallback_to_default_when_path_violation(self):
        """路径越界 → 回退默认规则（write_file 命中 DEFAULT_RULES → confirm）。"""
        d = self.engine.check(
            "file_write",
            {"path": "/etc/passwd"},  # 不在 /data/cron 前缀内
            session_id=self._cron_session(),
        )
        # 回退默认规则：write_file 命中 DEFAULT_RULES → confirm
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.risk_level, "high")
        # decision_source 为默认值（非 schedule_grant）
        self.assertEqual(d.decision_source, "default_rule")

    def test_tool_not_in_granted_tools_fallback(self):
        """工具不在 granted_tools 列表 → 回退默认规则。"""
        # list_directory 不在 granted_tools，未在 DEFAULT_RULES → allow
        d = self.engine.check(
            "file_listdir",
            {"path": "/data"},
            session_id=self._cron_session(),
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")


class TestPolicyCronGrantHardDisabled(unittest.TestCase):
    """SubTask 4.3 + 4.7: 硬禁止工具绝不预授权（defense in depth）。"""

    def test_hard_disabled_tools_not_pre_authorized(self):
        """硬禁止工具即使在 granted_tools 中也回退默认规则。"""
        schedule_id = "sched-hard"
        # 将三项硬禁止工具都放进 granted_tools（错误配置，应被防御）
        schedule = _MockSchedule(
            schedule_id=schedule_id,
            granted_tools=[
                {"tool": "memory_delete", "scope": "all"},
                {"tool": "bash_exec", "scope": "all"},
                {"tool": "tool_call", "scope": "all"},
            ],
        )
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine(cron_scheduler=cron_scheduler)
        session_id = f"cron:{schedule_id}"

        # delete_memory 命中 DEFAULT_RULES → confirm
        d1 = engine.check("memory_delete", {}, session_id=session_id)
        self.assertEqual(d1.action, "confirm")
        self.assertNotEqual(d1.decision_source, "schedule_grant")

        # execute_command（无 command 字段）→ 命中 DEFAULT_RULES confirm
        # 注意：execute_command 走命令分类器，无 command 字段时降级到 DEFAULT_RULES
        d2 = engine.check("bash_exec", {}, session_id=session_id)
        self.assertNotEqual(d2.decision_source, "schedule_grant")

        # call_tool 命中 DEFAULT_RULES → confirm
        d3 = engine.check("tool_call", {"name": "some_tool"}, session_id=session_id)
        self.assertEqual(d3.action, "confirm")
        self.assertNotEqual(d3.decision_source, "schedule_grant")


class TestPolicyCronGrantEdgeCases(unittest.TestCase):
    """cron 预授权边界场景：非 cron 会话、未注入 scheduler、调度项不存在等。"""

    def test_non_cron_session_skips_grant(self):
        """非 cron: 前缀的 session_id 不走预授权路径。"""
        schedule_id = "sched-x"
        schedule = _MockSchedule(
            schedule_id=schedule_id,
            granted_tools=[{"tool": "file_read", "scope": "all"}],
        )
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine(cron_scheduler=cron_scheduler)
        # 普通用户会话
        d = engine.check(
            "file_read",
            {"path": "/x"},
            session_id="user-session-1",
        )
        # read_file 不在 DEFAULT_RULES → allow（默认规则）
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")

    def test_no_cron_scheduler_fallback(self):
        """未注入 cron_scheduler 时 cron 会话也回退默认规则。"""
        engine = PolicyEngine()  # 无 cron_scheduler
        d = engine.check(
            "file_read",
            {"path": "/x"},
            session_id="cron:sched-1",
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")

    def test_schedule_not_found_fallback(self):
        """调度项不存在（已删除）→ 回退默认规则。"""
        cron_scheduler = _MockCronScheduler({})  # 空调度项集合
        engine = PolicyEngine(cron_scheduler=cron_scheduler)
        d = engine.check(
            "file_read",
            {"path": "/x"},
            session_id="cron:nonexistent",
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")

    def test_no_granted_tools_fallback(self):
        """调度项无 granted_tools（None）→ 回退默认规则。"""
        schedule_id = "sched-no-grant"
        schedule = _MockSchedule(schedule_id=schedule_id, granted_tools=None)
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine(cron_scheduler=cron_scheduler)
        d = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")

    def test_empty_granted_tools_fallback(self):
        """调度项 granted_tools 为空列表 → 回退默认规则。"""
        schedule_id = "sched-empty-grant"
        schedule = _MockSchedule(schedule_id=schedule_id, granted_tools=[])
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine(cron_scheduler=cron_scheduler)
        d = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")

    def test_disabled_engine_always_allow(self):
        """enabled=False 时一律放行，不走 cron 预授权路径。"""
        schedule_id = "sched-disabled"
        schedule = _MockSchedule(
            schedule_id=schedule_id,
            granted_tools=[{"tool": "file_read", "scope": "all"}],
        )
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine(enabled=False, cron_scheduler=cron_scheduler)
        d = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d.action, "allow")
        # enabled=False 时返回 Decision("allow", "", "low")，decision_source 为默认值
        self.assertEqual(d.decision_source, "default_rule")
        self.assertEqual(d.reason, "")


class TestSetCronSchedulerInjection(unittest.TestCase):
    """SubTask 4.2: set_cron_scheduler 后构造注入。"""

    def test_set_cron_scheduler_enables_grant(self):
        """构造时无 cron_scheduler，set_cron_scheduler 后启用预授权路径。"""
        schedule_id = "sched-inject"
        schedule = _MockSchedule(
            schedule_id=schedule_id,
            granted_tools=[{"tool": "file_read", "scope": "all"}],
        )
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine()  # 初始无 cron_scheduler
        # 注入前：回退默认规则
        d1 = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d1.decision_source, "default_rule")
        # 注入
        engine.set_cron_scheduler(cron_scheduler)
        # 注入后：走预授权路径
        d2 = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d2.action, "allow")
        self.assertEqual(d2.decision_source, "schedule_grant")

    def test_set_cron_scheduler_none_disables_grant(self):
        """set_cron_scheduler(None) 禁用预授权路径。"""
        schedule_id = "sched-disable"
        schedule = _MockSchedule(
            schedule_id=schedule_id,
            granted_tools=[{"tool": "file_read", "scope": "all"}],
        )
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine(cron_scheduler=cron_scheduler)
        # 启用状态
        d1 = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d1.decision_source, "schedule_grant")
        # 禁用
        engine.set_cron_scheduler(None)
        d2 = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d2.decision_source, "default_rule")


class TestIsPathAllowed(unittest.TestCase):
    """_is_path_allowed 静态方法：路径前缀匹配边界条件。"""

    def test_empty_allowed_paths_returns_false(self):
        """allowed_paths 为空列表 → False。"""
        self.assertFalse(PolicyEngine._is_path_allowed({"path": "/x"}, []))

    def test_exact_match(self):
        """路径等于前缀 → True。"""
        self.assertTrue(
            PolicyEngine._is_path_allowed({"path": "/data/cron"}, ["/data/cron"])
        )

    def test_subpath_match(self):
        """路径是前缀的子路径 → True。"""
        self.assertTrue(
            PolicyEngine._is_path_allowed(
                {"path": "/data/cron/sub/file.txt"}, ["/data/cron"]
            )
        )

    def test_no_boundary_match_returns_false(self):
        """前缀匹配要求分隔符边界：/data 不匹配 /datax。"""
        self.assertFalse(
            PolicyEngine._is_path_allowed({"path": "/datax"}, ["/data"])
        )

    def test_different_prefix_returns_false(self):
        """路径不在任一前缀内 → False。"""
        self.assertFalse(
            PolicyEngine._is_path_allowed({"path": "/etc/passwd"}, ["/data/cron"])
        )

    def test_multiple_allowed_paths(self):
        """多前缀列表中任一匹配 → True。"""
        self.assertTrue(
            PolicyEngine._is_path_allowed(
                {"path": "/var/log/x"}, ["/data/cron", "/var/log"]
            )
        )

    def test_no_path_field_returns_false(self):
        """tool_input 无路径字段 → False（保守拒绝）。"""
        self.assertFalse(PolicyEngine._is_path_allowed({}, ["/data"]))

    def test_alternative_path_field_names(self):
        """兼容不同路径字段名：file_path / target_path / destination。"""
        for key in ("file_path", "target_path", "destination"):
            with self.subTest(key=key):
                self.assertTrue(
                    PolicyEngine._is_path_allowed(
                        {key: "/data/cron/file"}, ["/data/cron"]
                    )
                )

    def test_normpath_normalization(self):
        """路径归一化：/data/./x 等价于 /data/x。"""
        # 注意：/data/cron/../other 不在 /data/cron 前缀内（归一化后为 /data/other）
        self.assertFalse(
            PolicyEngine._is_path_allowed(
                {"path": "/data/cron/../other"}, ["/data/cron"]
            )
        )
        # /data/cron/./file 归一化后为 /data/cron/file，在前缀内
        self.assertTrue(
            PolicyEngine._is_path_allowed(
                {"path": "/data/cron/./file"}, ["/data/cron"]
            )
        )


class TestFromConfigWithCronScheduler(unittest.TestCase):
    """from_config 支持 cron_scheduler 参数注入。"""

    def test_from_config_accepts_cron_scheduler(self):
        """from_config 透传 cron_scheduler 参数。"""
        schedule_id = "sched-cfg"
        schedule = _MockSchedule(
            schedule_id=schedule_id,
            granted_tools=[{"tool": "file_read", "scope": "all"}],
        )
        cron_scheduler = _MockCronScheduler({schedule_id: schedule})
        engine = PolicyEngine.from_config(
            {"enabled": True}, cron_scheduler=cron_scheduler
        )
        d = engine.check(
            "file_read",
            {"path": "/x"},
            session_id=f"cron:{schedule_id}",
        )
        self.assertEqual(d.decision_source, "schedule_grant")

    def test_from_config_default_no_cron_scheduler(self):
        """from_config 不传 cron_scheduler 时为 None（向后兼容）。"""
        engine = PolicyEngine.from_config({"enabled": True})
        self.assertIsNone(engine._cron_scheduler)


# ---------------------------------------------------------------------------
# SubTask 4.7: 预授权端到端场景
# ---------------------------------------------------------------------------


class TestPreAuthEndToEndScenarios(unittest.TestCase):
    """SubTask 4.7: 预授权端到端场景（正常 / 路径越界 / 硬禁止）。"""

    def setUp(self):
        """构造典型调度项：read_file 全放行 + write_file 限 /data/cron 路径。"""
        self.schedule_id = "e2e-sched"
        self.schedule = _MockSchedule(
            schedule_id=self.schedule_id,
            granted_tools=[
                {"tool": "file_read", "scope": "all"},
                {
                    "tool": "file_write",
                    "scope": "path_prefix",
                    "allowed_paths": ["/data/cron"],
                },
                {"tool": "file_listdir", "scope": "all"},
            ],
        )
        self.cron_scheduler = _MockCronScheduler({self.schedule_id: self.schedule})
        self.engine = PolicyEngine(cron_scheduler=self.cron_scheduler)
        self.session_id = f"cron:{self.schedule_id}"

    def test_e2e_pre_authorized_read_allowed(self):
        """端到端：预授权的 read_file 任意路径放行。"""
        d = self.engine.check(
            "file_read",
            {"path": "/anywhere/anyfile.txt"},
            session_id=self.session_id,
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "schedule_grant")

    def test_e2e_pre_authorized_write_within_path_allowed(self):
        """端到端：预授权的 write_file 在 /data/cron 路径内放行。"""
        d = self.engine.check(
            "file_write",
            {"path": "/data/cron/output.log"},
            session_id=self.session_id,
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "schedule_grant")

    def test_e2e_path_violation_falls_back_to_confirm(self):
        """端到端：write_file 路径越界回退默认规则（confirm）。"""
        d = self.engine.check(
            "file_write",
            {"path": "/etc/system.conf"},  # 越界
            session_id=self.session_id,
        )
        # 回退 DEFAULT_RULES：write_file → confirm
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.decision_source, "default_rule")

    def test_e2e_hard_disabled_tool_falls_back(self):
        """端到端：硬禁止工具（execute_command）回退默认规则。

        注：execute_command 走命令分类器，``ls`` 为读取类 → allow，
        但 decision_source 为 default_rule（非 schedule_grant）。
        """
        d = self.engine.check(
            "bash_exec",
            {"command": "ls /tmp"},
            session_id=self.session_id,
        )
        # execute_command 不在 granted_tools（且为硬禁止工具）
        # 走命令分类器：ls 为读取类 → allow，decision_source=default_rule
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")

    def test_e2e_non_granted_tool_falls_back(self):
        """端到端：未预授权工具（search_memory）回退默认规则。"""
        d = self.engine.check(
            "memory_search",
            {"query": "x"},
            session_id=self.session_id,
        )
        # search_memory 不在 DEFAULT_RULES → allow（默认规则放行读取类）
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.decision_source, "default_rule")


if __name__ == "__main__":
    unittest.main(verbosity=2)
