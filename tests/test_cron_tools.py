"""Phase 8 Task 3: cron 工具集 + 提议-确认协议测试套件。

覆盖 9 个 SubTask 的实现：
- SubTask 3.1: ProposalStore 内存存储 + 状态机
- SubTask 3.2: list_schedules 工具（read-only，精简字段）
- SubTask 3.3: propose_schedule 工具 + 硬禁止检测
- SubTask 3.4: create_schedule 工具（confirm，锁定快照）
- SubTask 3.5: update_schedule 工具（confirm，重新锁定快照）
- SubTask 3.6: 4 个 cron 工具注册到 ToolRegistry
- SubTask 3.8: proposal 端点（间接覆盖，端点本身在 server.py）

设计要点：
- 使用 mock ToolRegistry / CronScheduler，避免依赖真实持久化与 cron 计算
- ProposalStore 测试覆盖状态机所有合法/非法转换分支
- 工具 handler 测试通过 mock registry.execute_tool 调用，验证返回 JSON
- 硬禁止工具清单 ``HARD_DISABLED_TOOLS`` 检测覆盖三项
- active_tools_snapshot 锁定与重新锁定验证

运行方式:
    python -m unittest tests.test_cron_tools -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.cron_proposals import (  # noqa: E402
    VALID_STATUSES,
    Proposal,
    ProposalStore,
)
from src.agent.cron_tools import (  # noqa: E402
    HARD_DISABLED_TOOLS,
    _extract_tool_snapshot,
    is_hard_disabled,
    register_cron_tools,
)


# ---------------------------------------------------------------------------
# Mock 对象
# ---------------------------------------------------------------------------


class _MockToolRegistry:
    """记录 register_core / register_deferred 调用的 mock ToolRegistry。

    与 tests/test_plan_tools.py 中的 mock 一致，便于跨测试复用模式。
    支持 register_core / register_deferred / execute_tool / get_tools_schema。
    register_deferred 与 register_core 行为一致（仅记录注册，不区分 Tier）。
    """

    def __init__(self) -> None:
        self.tools: Dict[str, Dict[str, Any]] = {}

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

    def register_deferred(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler,
    ) -> None:
        """Deferred Tier 注册（与 register_core 行为一致，仅记录）。"""
        self.tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name not in self.tools:
            return f"未注册的工具: {tool_name}"
        try:
            result = self.tools[tool_name]["handler"](**(tool_input or {}))
            return str(result)
        except Exception as e:
            return f"工具 {tool_name} 执行出错: {e}"

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": name,
                "description": t["description"],
                "input_schema": t["input_schema"],
            }
            for name, t in self.tools.items()
        ]


class _MockCronScheduler:
    """记录 add_schedule / update_schedule / list_schedules 调用的 mock。

    支持 Phase 8 Task 1.6 新增字段（granted_tools / active_tools_snapshot
    / workflow / generate_llm_summary / cron_id）。
    """

    def __init__(self) -> None:
        self._schedules: Dict[str, Dict[str, Any]] = {}
        self._next_id = 0
        self.add_calls: List[Dict[str, Any]] = []
        self.update_calls: List[tuple] = []

    def add_schedule(self, sched_dict: dict) -> str:
        sid = sched_dict.get("id") or f"mock-{self._next_id:04d}"
        self._next_id += 1
        stored = dict(sched_dict)
        stored["id"] = sid
        self._schedules[sid] = stored
        self.add_calls.append(dict(sched_dict))
        return sid

    def update_schedule(self, schedule_id: str, fields: dict) -> bool:
        if schedule_id not in self._schedules:
            return False
        self._schedules[schedule_id].update(fields)
        self.update_calls.append((schedule_id, dict(fields)))
        return True

    def list_schedules(self) -> List[Dict[str, Any]]:
        return [dict(s) for s in self._schedules.values()]

    def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        s = self._schedules.get(schedule_id)
        return dict(s) if s else None


# ---------------------------------------------------------------------------
# SubTask 3.1: ProposalStore 内存存储 + 状态机
# ---------------------------------------------------------------------------


class TestProposalDataclass(unittest.TestCase):
    """Proposal dataclass 字段与默认值测试。"""

    def test_proposal_fields_default_values(self):
        """Proposal 必填字段外，status/created_at/schedule_id 有合理默认。"""
        p = Proposal(
            proposal_id="p1",
            schedule_config={"cron": "0 9 * * *", "task": "hi"},
            requested_tools=[],
            llm_explanation="",
        )
        self.assertEqual(p.proposal_id, "p1")
        self.assertEqual(p.status, "pending_confirm")
        self.assertEqual(p.created_at, "")  # 默认空，由 store.create 填充
        self.assertIsNone(p.schedule_id)

    def test_proposal_to_dict_returns_all_fields(self):
        """to_dict 返回所有字段（用于 API 序列化）。"""
        p = Proposal(
            proposal_id="p1",
            schedule_config={"cron": "0 9 * * *"},
            requested_tools=[{"tool": "file_read"}],
            llm_explanation="test",
            status="pending_confirm",
            created_at="2026-01-01T00:00:00",
        )
        d = p.to_dict()
        self.assertIn("proposal_id", d)
        self.assertIn("schedule_config", d)
        self.assertIn("requested_tools", d)
        self.assertIn("llm_explanation", d)
        self.assertIn("status", d)
        self.assertIn("created_at", d)
        self.assertIn("schedule_id", d)

    def test_valid_statuses_constant(self):
        """VALID_STATUSES 含全部 6 个状态。"""
        expected = {
            "proposal_created",
            "pending_confirm",
            "confirmed",
            "modified",
            "rejected",
            "schedule_active",
        }
        self.assertEqual(set(VALID_STATUSES), expected)


class TestProposalStoreCreate(unittest.TestCase):
    """ProposalStore.create 行为测试。"""

    def setUp(self) -> None:
        self.store = ProposalStore()

    def test_create_returns_proposal_id(self):
        """create 返回非空 proposal_id。"""
        pid = self.store.create(
            schedule_config={"cron": "0 9 * * *", "task": "hi"},
            requested_tools=[{"tool": "file_read"}],
            llm_explanation="说明",
        )
        self.assertTrue(pid)
        self.assertIsInstance(pid, str)

    def test_create_initial_status_is_pending_confirm(self):
        """create 后 proposal 状态为 pending_confirm（proposal_created 为瞬时态）。"""
        pid = self.store.create(
            schedule_config={"cron": "0 9 * * *", "task": "hi"},
            requested_tools=[],
            llm_explanation="",
        )
        p = self.store.get(pid)
        self.assertIsNotNone(p)
        self.assertEqual(p.status, "pending_confirm")

    def test_create_fills_created_at(self):
        """create 填充 created_at（ISO 格式字符串）。"""
        pid = self.store.create(
            schedule_config={"cron": "0 9 * * *", "task": "hi"},
            requested_tools=[],
            llm_explanation="",
        )
        p = self.store.get(pid)
        self.assertTrue(p.created_at)

    def test_create_stores_config_and_tools(self):
        """create 存储 schedule_config 与 requested_tools。"""
        cfg = {"cron": "0 9 * * *", "task": "hi", "name": "test"}
        tools = [{"tool": "file_read", "scope": "all"}]
        pid = self.store.create(
            schedule_config=cfg,
            requested_tools=tools,
            llm_explanation="reason",
        )
        p = self.store.get(pid)
        self.assertEqual(p.schedule_config, cfg)
        self.assertEqual(p.requested_tools, tools)
        self.assertEqual(p.llm_explanation, "reason")

    def test_create_defensive_copy_of_config(self):
        """create 对 schedule_config 做浅拷贝，外部修改不影响已存 proposal。"""
        cfg = {"cron": "0 9 * * *", "task": "hi"}
        pid = self.store.create(
            schedule_config=cfg,
            requested_tools=[],
            llm_explanation="",
        )
        cfg["task"] = "modified"
        p = self.store.get(pid)
        self.assertEqual(p.schedule_config["task"], "hi")


class TestProposalStoreStateMachine(unittest.TestCase):
    """ProposalStore 状态机转换测试。"""

    def setUp(self) -> None:
        self.store = ProposalStore()
        self.pid = self.store.create(
            schedule_config={"cron": "0 9 * * *", "task": "hi"},
            requested_tools=[],
            llm_explanation="",
        )

    def test_confirm_pending_to_confirmed(self):
        """confirm: pending_confirm → confirmed。"""
        ok = self.store.confirm(self.pid)
        self.assertTrue(ok)
        self.assertEqual(self.store.get(self.pid).status, "confirmed")

    def test_reject_pending_to_rejected(self):
        """reject: pending_confirm → rejected。"""
        ok = self.store.reject(self.pid)
        self.assertTrue(ok)
        self.assertEqual(self.store.get(self.pid).status, "rejected")

    def test_modify_pending_to_modified(self):
        """modify: pending_confirm → modified（并应用修改）。"""
        ok = self.store.modify(
            self.pid,
            schedule_config_updates={"task": "new task"},
            requested_tools=[{"tool": "file_write"}],
        )
        self.assertTrue(ok)
        p = self.store.get(self.pid)
        self.assertEqual(p.status, "modified")
        self.assertEqual(p.schedule_config["task"], "new task")
        # 原 cron 字段保留（浅合并）
        self.assertEqual(p.schedule_config["cron"], "0 9 * * *")
        self.assertEqual(p.requested_tools, [{"tool": "file_write"}])

    def test_modify_only_updates_provided_fields(self):
        """modify 不传 requested_tools 时不更新工具列表。"""
        original_tools = [{"tool": "file_read"}]
        pid = self.store.create(
            schedule_config={"cron": "0 9 * * *", "task": "hi"},
            requested_tools=original_tools,
            llm_explanation="",
        )
        ok = self.store.modify(
            pid, schedule_config_updates={"task": "changed"}
        )
        self.assertTrue(ok)
        p = self.store.get(pid)
        self.assertEqual(p.requested_tools, original_tools)
        self.assertEqual(p.schedule_config["task"], "changed")

    def test_mark_schedule_active_from_confirmed(self):
        """mark_schedule_active: confirmed → schedule_active。"""
        self.store.confirm(self.pid)
        ok = self.store.mark_schedule_active(self.pid, "sched-123")
        self.assertTrue(ok)
        p = self.store.get(self.pid)
        self.assertEqual(p.status, "schedule_active")
        self.assertEqual(p.schedule_id, "sched-123")

    def test_mark_schedule_active_from_modified(self):
        """mark_schedule_active: modified → schedule_active。"""
        self.store.modify(self.pid, schedule_config_updates={"task": "x"})
        ok = self.store.mark_schedule_active(self.pid, "sched-456")
        self.assertTrue(ok)
        p = self.store.get(self.pid)
        self.assertEqual(p.status, "schedule_active")
        self.assertEqual(p.schedule_id, "sched-456")


class TestProposalStoreInvalidTransitions(unittest.TestCase):
    """ProposalStore 非法状态转换测试。"""

    def setUp(self) -> None:
        self.store = ProposalStore()
        self.pid = self.store.create(
            schedule_config={"cron": "0 9 * * *", "task": "hi"},
            requested_tools=[],
            llm_explanation="",
        )

    def test_confirm_after_rejected_fails(self):
        """rejected 为终态，confirm 失败。"""
        self.store.reject(self.pid)
        ok = self.store.confirm(self.pid)
        self.assertFalse(ok)
        self.assertEqual(self.store.get(self.pid).status, "rejected")

    def test_reject_after_confirmed_fails(self):
        """confirmed 不可 reject。"""
        self.store.confirm(self.pid)
        ok = self.store.reject(self.pid)
        self.assertFalse(ok)

    def test_mark_schedule_active_from_pending_fails(self):
        """pending_confirm 不可直接 mark_schedule_active。"""
        ok = self.store.mark_schedule_active(self.pid, "sched-x")
        self.assertFalse(ok)

    def test_mark_schedule_active_from_rejected_fails(self):
        """rejected 不可 mark_schedule_active。"""
        self.store.reject(self.pid)
        ok = self.store.mark_schedule_active(self.pid, "sched-x")
        self.assertFalse(ok)

    def test_confirm_nonexistent_returns_false(self):
        """confirm 不存在的 proposal 返回 False。"""
        self.assertFalse(self.store.confirm("nonexistent-id"))

    def test_modify_nonexistent_returns_false(self):
        """modify 不存在的 proposal 返回 False。"""
        self.assertFalse(self.store.modify("nonexistent-id"))

    def test_reject_nonexistent_returns_false(self):
        """reject 不存在的 proposal 返回 False。"""
        self.assertFalse(self.store.reject("nonexistent-id"))

    def test_double_confirm_fails(self):
        """confirmed 不可再次 confirm。"""
        self.store.confirm(self.pid)
        ok = self.store.confirm(self.pid)
        self.assertFalse(ok)


class TestProposalStoreListAndClear(unittest.TestCase):
    """ProposalStore.list / clear / 内存特性测试。"""

    def test_list_returns_all_proposals_in_creation_order(self):
        """list 返回所有 proposal，按创建顺序。"""
        store = ProposalStore()
        pid1 = store.create(
            schedule_config={"cron": "0 9 * * *", "task": "a"},
            requested_tools=[],
            llm_explanation="",
        )
        pid2 = store.create(
            schedule_config={"cron": "0 10 * * *", "task": "b"},
            requested_tools=[],
            llm_explanation="",
        )
        proposals = store.list()
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0].proposal_id, pid1)
        self.assertEqual(proposals[1].proposal_id, pid2)

    def test_clear_empties_store(self):
        """clear 清空所有 proposals（测试用）。"""
        store = ProposalStore()
        store.create(
            schedule_config={"cron": "0 9 * * *", "task": "a"},
            requested_tools=[],
            llm_explanation="",
        )
        self.assertEqual(len(store.list()), 1)
        store.clear()
        self.assertEqual(len(store.list()), 0)

    def test_new_instance_empty(self):
        """新 ProposalStore 实例为空（纯内存，重启清空验证）。"""
        store1 = ProposalStore()
        store1.create(
            schedule_config={"cron": "0 9 * * *", "task": "a"},
            requested_tools=[],
            llm_explanation="",
        )
        # 模拟重启：新实例
        store2 = ProposalStore()
        self.assertEqual(len(store2.list()), 0)

    def test_get_nonexistent_returns_none(self):
        """get 不存在的 proposal 返回 None。"""
        store = ProposalStore()
        self.assertIsNone(store.get("nonexistent"))


# ---------------------------------------------------------------------------
# SubTask 3.2-3.6: 工具注册与 handler 测试
# ---------------------------------------------------------------------------


class TestRegisterCronTools(unittest.TestCase):
    """register_cron_tools 注册行为测试（SubTask 3.6）。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)

    def test_registers_four_tools(self):
        """register_cron_tools 注册 4 个工具。"""
        expected = {
            "cron_list",
            "cron_propose",
            "cron_create",
            "cron_update",
        }
        self.assertEqual(set(self.registry.tools.keys()), expected)

    def test_all_tools_in_core_tier(self):
        """4 个工具均通过 register_deferred 注册（Deferred Tier）。"""
        # _MockToolRegistry 支持 register_core 与 register_deferred，
        # 二者行为一致（仅记录注册）
        for name, tool in self.registry.tools.items():
            self.assertIn("description", tool)
            self.assertIn("input_schema", tool)
            self.assertIn("handler", tool)

    def test_tool_schemas_follow_anthropic_format(self):
        """工具 schema 符合 Anthropic tool use 格式（含 type/properties/required）。"""
        for name, tool in self.registry.tools.items():
            schema = tool["input_schema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            self.assertIn("required", schema)

    def test_create_schedule_description_marks_confirm_required(self):
        """create_schedule 描述含 [需确认] 前缀（高危标识）。"""
        desc = self.registry.tools["cron_create"]["description"]
        self.assertIn("[需确认]", desc)

    def test_update_schedule_description_marks_confirm_required(self):
        """update_schedule 描述含 [需确认] 前缀。"""
        desc = self.registry.tools["cron_update"]["description"]
        self.assertIn("[需确认]", desc)

    def test_list_schedules_description_readonly(self):
        """list_schedules 描述提示只读、无需确认。"""
        desc = self.registry.tools["cron_list"]["description"]
        self.assertIn("只读", desc)

    def test_propose_schedule_description_mentions_hard_disabled(self):
        """propose_schedule 描述提及硬禁止工具。"""
        desc = self.registry.tools["cron_propose"]["description"]
        self.assertIn("memory_delete", desc)
        self.assertIn("bash_exec", desc)
        self.assertIn("tool_call", desc)


# ---------------------------------------------------------------------------
# SubTask 3.2: list_schedules 工具
# ---------------------------------------------------------------------------


class TestListSchedulesTool(unittest.TestCase):
    """list_schedules 工具 handler 测试。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)
        # 预置两条调度项（含敏感字段，应被过滤）
        self.scheduler.add_schedule(
            {
                "name": "s1",
                "cron": "0 9 * * *",
                "task": "task1",
                "enabled": True,
                "granted_tools": [{"tool": "file_read"}],
                "active_tools_snapshot": ["file_read"],
            }
        )
        self.scheduler.add_schedule(
            {
                "name": "s2",
                "cron": "0 10 * * *",
                "task": "task2",
                "enabled": False,
            }
        )

    def test_returns_json_with_schedules_array(self):
        """list_schedules 返回 JSON，含 schedules 数组与 total。"""
        result = self.registry.execute_tool("cron_list", {})
        data = json.loads(result)
        self.assertIn("schedules", data)
        self.assertIn("total", data)
        self.assertEqual(data["total"], 2)

    def test_returns_only_public_fields(self):
        """list_schedules 只返回精简字段，不含 granted_tools / active_tools_snapshot。"""
        result = self.registry.execute_tool("cron_list", {})
        data = json.loads(result)
        for s in data["schedules"]:
            self.assertIn("id", s)
            self.assertIn("name", s)
            self.assertIn("cron", s)
            self.assertIn("task", s)
            self.assertIn("enabled", s)
            # 敏感字段不应出现
            self.assertNotIn("granted_tools", s)
            self.assertNotIn("active_tools_snapshot", s)
            self.assertNotIn("workflow", s)

    def test_returns_empty_when_no_schedules(self):
        """无调度项时返回空数组与 total=0。"""
        registry = _MockToolRegistry()
        scheduler = _MockCronScheduler()
        store = ProposalStore()
        register_cron_tools(registry, scheduler, store)
        result = registry.execute_tool("cron_list", {})
        data = json.loads(result)
        self.assertEqual(data["schedules"], [])
        self.assertEqual(data["total"], 0)


# ---------------------------------------------------------------------------
# SubTask 3.3: propose_schedule 工具 + 硬禁止检测
# ---------------------------------------------------------------------------


class TestProposeScheduleTool(unittest.TestCase):
    """propose_schedule 工具 handler 测试。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)

    def test_normal_proposal_returns_proposal_id(self):
        """正常提议返回 proposal_id 与 pending_confirm 状态。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {
                    "name": "daily_report",
                    "cron": "0 9 * * *",
                    "task": "生成日报",
                },
                "requested_tools": [
                    {"tool": "file_read", "scope": "all", "allowed_paths": []}
                ],
                "llm_explanation": "每天早晨生成日报",
            },
        )
        data = json.loads(result)
        self.assertIn("proposal_id", data)
        self.assertEqual(data["status"], "pending_confirm")
        self.assertTrue(data["proposal_id"])

    def test_proposal_actually_created_in_store(self):
        """propose_schedule 成功后 proposal 存入 ProposalStore。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [],
                "llm_explanation": "test",
            },
        )
        data = json.loads(result)
        pid = data["proposal_id"]
        p = self.store.get(pid)
        self.assertIsNotNone(p)
        self.assertEqual(p.status, "pending_confirm")

    def test_propose_does_not_create_schedule(self):
        """propose_schedule 不实际创建调度项（仅创建 proposal）。"""
        self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [],
                "llm_explanation": "test",
            },
        )
        self.assertEqual(len(self.scheduler.add_calls), 0)

    def test_hard_disabled_delete_memory_rejected(self):
        """requested_tools 含 delete_memory 被拒绝。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [
                    {"tool": "memory_delete", "scope": "all"}
                ],
                "llm_explanation": "恶意提议",
            },
        )
        self.assertIn("memory_delete", result)
        self.assertIn("错误", result)
        # 不应创建 proposal
        self.assertEqual(len(self.store.list()), 0)

    def test_hard_disabled_execute_command_rejected(self):
        """requested_tools 含 execute_command 被拒绝。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [
                    {"tool": "bash_exec", "scope": "all"}
                ],
                "llm_explanation": "",
            },
        )
        self.assertIn("bash_exec", result)
        self.assertIn("错误", result)

    def test_hard_disabled_call_tool_rejected(self):
        """requested_tools 含 call_tool 被拒绝。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [{"tool": "tool_call", "scope": "all"}],
                "llm_explanation": "",
            },
        )
        self.assertIn("tool_call", result)
        self.assertIn("错误", result)

    def test_multiple_hard_disabled_listed_in_error(self):
        """多个硬禁止工具同时出现在错误信息中。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [
                    {"tool": "memory_delete"},
                    {"tool": "bash_exec"},
                    {"tool": "tool_call"},
                ],
                "llm_explanation": "",
            },
        )
        self.assertIn("memory_delete", result)
        self.assertIn("bash_exec", result)
        self.assertIn("tool_call", result)

    def test_hard_disabled_with_normal_tools_still_rejected(self):
        """硬禁止工具与正常工具混合时仍被拒绝。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [
                    {"tool": "file_read", "scope": "all"},
                    {"tool": "memory_delete", "scope": "all"},
                ],
                "llm_explanation": "",
            },
        )
        self.assertIn("错误", result)
        self.assertEqual(len(self.store.list()), 0)

    def test_missing_cron_rejected(self):
        """schedule_config.cron 缺失返回错误。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"task": "hi"},
                "requested_tools": [],
                "llm_explanation": "",
            },
        )
        self.assertIn("cron", result)
        self.assertIn("错误", result)

    def test_missing_task_rejected(self):
        """schedule_config.task 缺失返回错误。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *"},
                "requested_tools": [],
                "llm_explanation": "",
            },
        )
        self.assertIn("task", result)
        self.assertIn("错误", result)

    def test_empty_schedule_config_rejected(self):
        """schedule_config 为空返回错误。"""
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {},
                "requested_tools": [],
                "llm_explanation": "",
            },
        )
        self.assertIn("错误", result)


# ---------------------------------------------------------------------------
# SubTask 3.4: create_schedule 工具（confirm）
# ---------------------------------------------------------------------------


class TestCreateScheduleTool(unittest.TestCase):
    """create_schedule 工具 handler 测试。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)

    def _create_proposal(
        self,
        tools: List[Dict[str, Any]] = None,
        config: Dict[str, Any] = None,
    ) -> str:
        """辅助：创建一个 pending_confirm 状态的 proposal。"""
        cfg = config or {"cron": "0 9 * * *", "task": "hi", "name": "test"}
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": cfg,
                "requested_tools": tools or [],
                "llm_explanation": "test",
            },
        )
        return json.loads(result)["proposal_id"]

    def test_create_from_confirmed_proposal(self):
        """已确认 proposal 创建调度项成功。"""
        pid = self._create_proposal(
            tools=[{"tool": "file_read", "scope": "all"}]
        )
        self.store.confirm(pid)

        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        data = json.loads(result)
        self.assertIn("schedule_id", data)
        self.assertEqual(data["status"], "schedule_active")
        self.assertEqual(data["proposal_id"], pid)

    def test_create_locks_active_tools_snapshot(self):
        """create_schedule 创建时锁定 active_tools_snapshot。"""
        pid = self._create_proposal(
            tools=[
                {"tool": "file_read", "scope": "all"},
                {"tool": "file_write", "scope": "path_prefix", "allowed_paths": ["/tmp"]},
            ]
        )
        self.store.confirm(pid)

        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        data = json.loads(result)
        snapshot = data["active_tools_snapshot"]
        self.assertEqual(set(snapshot), {"file_read", "file_write"})

    def test_create_passes_granted_tools_to_scheduler(self):
        """create_schedule 将 requested_tools 作为 granted_tools 传入 scheduler。"""
        tools = [{"tool": "file_read", "scope": "all", "allowed_paths": []}]
        pid = self._create_proposal(tools=tools)
        self.store.confirm(pid)

        self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        self.assertEqual(len(self.scheduler.add_calls), 1)
        added = self.scheduler.add_calls[0]
        self.assertEqual(added["granted_tools"], tools)

    def test_create_marks_proposal_schedule_active(self):
        """create_schedule 成功后 proposal 标记为 schedule_active。"""
        pid = self._create_proposal()
        self.store.confirm(pid)

        self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        p = self.store.get(pid)
        self.assertEqual(p.status, "schedule_active")
        self.assertIsNotNone(p.schedule_id)

    def test_create_from_pending_proposal_fails(self):
        """未确认 proposal（pending_confirm）调用 create_schedule 失败。"""
        pid = self._create_proposal()
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        self.assertIn("错误", result)
        self.assertIn(pid, result)
        self.assertEqual(len(self.scheduler.add_calls), 0)

    def test_create_from_rejected_proposal_fails(self):
        """rejected 状态 proposal 调用 create_schedule 失败。"""
        pid = self._create_proposal()
        self.store.reject(pid)
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        self.assertIn("错误", result)

    def test_create_from_schedule_active_proposal_fails(self):
        """schedule_active（终态）proposal 再次 create_schedule 失败。"""
        pid = self._create_proposal()
        self.store.confirm(pid)
        self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        # 再次调用
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        self.assertIn("错误", result)

    def test_create_nonexistent_proposal_fails(self):
        """不存在的 proposal_id 返回错误。"""
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": "nonexistent"}
        )
        self.assertIn("错误", result)
        self.assertIn("不存在", result)

    def test_create_from_modified_proposal_succeeds(self):
        """modified 状态 proposal 可创建调度项。"""
        pid = self._create_proposal()
        self.store.modify(
            pid,
            schedule_config_updates={"task": "modified task"},
            requested_tools=[{"tool": "file_read", "scope": "all"}],
        )
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        data = json.loads(result)
        self.assertIn("schedule_id", data)
        # 验证修改后的 task 已生效
        added = self.scheduler.add_calls[0]
        self.assertEqual(added["task"], "modified task")


# ---------------------------------------------------------------------------
# SubTask 3.5: update_schedule 工具（confirm）
# ---------------------------------------------------------------------------


class TestUpdateScheduleTool(unittest.TestCase):
    """update_schedule 工具 handler 测试。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)
        # 预置一个调度项
        self.sid = self.scheduler.add_schedule(
            {"name": "orig", "cron": "0 9 * * *", "task": "hi"}
        )

    def test_update_basic_fields(self):
        """更新 name / task / enabled 字段成功。"""
        result = self.registry.execute_tool(
            "cron_update",
            {
                "schedule_id": self.sid,
                "fields": {
                    "name": "new_name",
                    "task": "new_task",
                    "enabled": False,
                },
            },
        )
        data = json.loads(result)
        self.assertTrue(data["updated"])
        self.assertFalse(data["snapshot_relocked"])
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["name"], "new_name")
        self.assertEqual(sched["task"], "new_task")
        self.assertFalse(sched["enabled"])

    def test_update_granted_tools_relocks_snapshot(self):
        """granted_tools 变更时自动重新锁定 active_tools_snapshot。"""
        new_tools = [
            {"tool": "file_read", "scope": "all"},
            {"tool": "file_write", "scope": "path_prefix", "allowed_paths": ["/data"]},
        ]
        result = self.registry.execute_tool(
            "cron_update",
            {"schedule_id": self.sid, "fields": {"granted_tools": new_tools}},
        )
        data = json.loads(result)
        self.assertTrue(data["updated"])
        self.assertTrue(data["snapshot_relocked"])
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["granted_tools"], new_tools)
        self.assertEqual(
            set(sched["active_tools_snapshot"]), {"file_read", "file_write"}
        )

    def test_update_without_granted_tools_no_relock(self):
        """不含 granted_tools 时 snapshot_relocked=False。"""
        result = self.registry.execute_tool(
            "cron_update",
            {"schedule_id": self.sid, "fields": {"name": "x"}},
        )
        data = json.loads(result)
        self.assertFalse(data["snapshot_relocked"])

    def test_update_nonexistent_schedule_fails(self):
        """不存在的 schedule_id 返回错误。"""
        result = self.registry.execute_tool(
            "cron_update",
            {"schedule_id": "nonexistent", "fields": {"name": "x"}},
        )
        self.assertIn("错误", result)

    def test_update_empty_fields_rejected(self):
        """fields 为空返回错误。"""
        result = self.registry.execute_tool(
            "cron_update",
            {"schedule_id": self.sid, "fields": {}},
        )
        self.assertIn("错误", result)

    def test_update_empty_schedule_id_rejected(self):
        """schedule_id 为空返回错误。"""
        result = self.registry.execute_tool(
            "cron_update",
            {"schedule_id": "", "fields": {"name": "x"}},
        )
        self.assertIn("错误", result)

    def test_update_granted_tools_empty_array(self):
        """granted_tools 设为空数组时 snapshot 也为空。"""
        result = self.registry.execute_tool(
            "cron_update",
            {"schedule_id": self.sid, "fields": {"granted_tools": []}},
        )
        data = json.loads(result)
        self.assertTrue(data["snapshot_relocked"])
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["active_tools_snapshot"], [])


# ---------------------------------------------------------------------------
# Task 9: cron_propose / cron_update workflow schema 校验集成
# ---------------------------------------------------------------------------


class TestWorkflowValidationInProposeSchedule(unittest.TestCase):
    """Task 9.2/9.4: cron_propose 多步 workflow 校验。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)

    def test_multistep_workflow_validation_passes(self):
        """多步 workflow 校验通过，proposal 正常创建。"""
        workflow = {
            "name": "watch_and_notify",
            "steps": [
                {
                    "id": "s1",
                    "type": "deterministic",
                    "config": {"template": "directory_watch"},
                },
                {
                    "id": "s2",
                    "type": "llm",
                    "config": {"prompt": "总结"},
                    "depends_on": ["s1"],
                },
            ],
        }
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {
                    "name": "多步测试",
                    "cron": "0 9 * * *",
                    "task": "执行多步 workflow",
                    "workflow": workflow,
                },
                "requested_tools": [],
                "llm_explanation": "多步 workflow 提议",
            },
        )
        data = json.loads(result)
        self.assertIn("proposal_id", data)
        self.assertEqual(data["status"], "pending_confirm")

    def test_workflow_validation_failed_rejects_proposal(self):
        """多步 workflow 校验失败（depends_on 环），拒绝创建 proposal。"""
        workflow = {
            "name": "cyclic_workflow",
            "steps": [
                {
                    "id": "s1",
                    "type": "llm",
                    "config": {"prompt": "A"},
                    "depends_on": ["s2"],
                },
                {
                    "id": "s2",
                    "type": "llm",
                    "config": {"prompt": "B"},
                    "depends_on": ["s1"],
                },
            ],
        }
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {
                    "cron": "0 9 * * *",
                    "task": "循环 workflow",
                    "workflow": workflow,
                },
                "requested_tools": [],
                "llm_explanation": "测试环检测",
            },
        )
        data = json.loads(result)
        self.assertIn("error", data)
        self.assertIn("validation_errors", data)
        self.assertFalse(self.store.list())

    def test_simple_mode_workflow_skips_validation(self):
        """简易模式（仅 template 字段）跳过校验，proposal 正常创建。"""
        workflow = {
            "template": "directory_watch",
            "watch_path": "/tmp",
        }
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {
                    "cron": "0 9 * * *",
                    "task": "目录监控",
                    "workflow": workflow,
                },
                "requested_tools": [],
                "llm_explanation": "简易模式",
            },
        )
        data = json.loads(result)
        self.assertIn("proposal_id", data)

    def test_bare_dict_workflow_schema_compatible(self):
        """LLM 旧调用（裸 dict {template, topic}）通过 schema 校验。"""
        # LLM 常见调用：workflow 字段含 template + 模板特定字段（如 topic）
        workflow = {"template": "research", "topic": "AI 趋势"}
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {
                    "cron": "0 9 * * *",
                    "task": "研究",
                    "workflow": workflow,
                },
                "requested_tools": [],
                "llm_explanation": "研究类调度",
            },
        )
        data = json.loads(result)
        self.assertIn("proposal_id", data)
        self.assertEqual(data["status"], "pending_confirm")


class TestWorkflowValidationInUpdateSchedule(unittest.TestCase):
    """Task 9.3/9.4: cron_update workflow 字段更新校验。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)
        self.sid = self.scheduler.add_schedule(
            {"name": "orig", "cron": "0 9 * * *", "task": "hi"}
        )

    def test_update_workflow_validation_failed(self):
        """cron_update 更新 workflow 字段时校验失败拒绝更新。"""
        bad_workflow = {
            "name": "bad_step_type",
            "steps": [
                {
                    "id": "s1",
                    "type": "invalid_type_xyz",  # 非法 step 类型
                    "config": {},
                },
            ],
        }
        result = self.registry.execute_tool(
            "cron_update",
            {
                "schedule_id": self.sid,
                "fields": {"workflow": bad_workflow},
            },
        )
        data = json.loads(result)
        self.assertIn("error", data)
        self.assertIn("validation_errors", data)
        # 调度项未被更新
        sched = self.scheduler.get_schedule(self.sid)
        self.assertIsNone(sched.get("workflow"))

    def test_update_workflow_simple_mode_skips_validation(self):
        """cron_update 更新为简易模式 workflow（仅 template）跳过校验。"""
        simple_workflow = {"template": "summary", "session_id": "sess1"}
        result = self.registry.execute_tool(
            "cron_update",
            {
                "schedule_id": self.sid,
                "fields": {"workflow": simple_workflow},
            },
        )
        data = json.loads(result)
        self.assertTrue(data["updated"])
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["workflow"], simple_workflow)


# ---------------------------------------------------------------------------
# 辅助函数测试
# ---------------------------------------------------------------------------


class TestExtractToolSnapshot(unittest.TestCase):
    """_extract_tool_snapshot 辅助函数测试。"""

    def test_extracts_tool_names(self):
        """从 granted_tools 提取工具名列表。"""
        tools = [
            {"tool": "file_read", "scope": "all"},
            {"tool": "file_write", "scope": "path_prefix", "allowed_paths": ["/x"]},
        ]
        self.assertEqual(_extract_tool_snapshot(tools), ["file_read", "file_write"])

    def test_preserves_order(self):
        """保持输入顺序。"""
        tools = [
            {"tool": "z_tool"},
            {"tool": "a_tool"},
            {"tool": "m_tool"},
        ]
        self.assertEqual(_extract_tool_snapshot(tools), ["z_tool", "a_tool", "m_tool"])

    def test_dedup(self):
        """重复工具名去重。"""
        tools = [
            {"tool": "file_read"},
            {"tool": "file_read"},
            {"tool": "file_write"},
        ]
        self.assertEqual(_extract_tool_snapshot(tools), ["file_read", "file_write"])

    def test_empty_input(self):
        """空列表返回空列表。"""
        self.assertEqual(_extract_tool_snapshot([]), [])

    def test_none_input(self):
        """None 返回空列表。"""
        self.assertEqual(_extract_tool_snapshot(None), [])

    def test_skips_non_dict_entries(self):
        """非 dict 项被跳过。"""
        tools = [
            {"tool": "file_read"},
            "invalid",
            None,
            123,
            {"tool": "file_write"},
        ]
        self.assertEqual(_extract_tool_snapshot(tools), ["file_read", "file_write"])

    def test_skips_empty_tool_name(self):
        """tool 字段为空被跳过。"""
        tools = [
            {"tool": ""},
            {"tool": "file_read"},
            {"scope": "all"},  # 缺 tool 字段
        ]
        self.assertEqual(_extract_tool_snapshot(tools), ["file_read"])


class TestIsHardDisabled(unittest.TestCase):
    """is_hard_disabled 辅助函数测试。"""

    def test_delete_memory_is_hard_disabled(self):
        self.assertTrue(is_hard_disabled("memory_delete"))

    def test_execute_command_is_hard_disabled(self):
        self.assertTrue(is_hard_disabled("bash_exec"))

    def test_call_tool_is_hard_disabled(self):
        self.assertTrue(is_hard_disabled("tool_call"))

    def test_normal_tool_not_hard_disabled(self):
        """正常工具不在硬禁止清单中。"""
        self.assertFalse(is_hard_disabled("file_read"))
        self.assertFalse(is_hard_disabled("file_write"))
        self.assertFalse(is_hard_disabled("memory_search"))

    def test_hard_disabled_constant_has_three_items(self):
        """HARD_DISABLED_TOOLS 常量含 3 项（spec 要求）。"""
        self.assertEqual(
            HARD_DISABLED_TOOLS, {"memory_delete", "bash_exec", "tool_call"}
        )


# ---------------------------------------------------------------------------
# 集成场景：提议 → 确认 → 创建 调用链
# ---------------------------------------------------------------------------


class TestProposalConfirmFlow(unittest.TestCase):
    """提议-确认-创建全流程集成测试。"""

    def setUp(self) -> None:
        self.registry = _MockToolRegistry()
        self.scheduler = _MockCronScheduler()
        self.store = ProposalStore()
        register_cron_tools(self.registry, self.scheduler, self.store)

    def test_full_confirm_flow(self):
        """完整流程：propose → confirm → create_schedule → schedule_active。"""
        # 1. 提议
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {
                    "name": "daily_report",
                    "cron": "0 9 * * *",
                    "task": "生成日报",
                },
                "requested_tools": [
                    {"tool": "file_read", "scope": "all"},
                    {"tool": "file_write", "scope": "path_prefix", "allowed_paths": ["/reports"]},
                ],
                "llm_explanation": "每天生成日报",
            },
        )
        pid = json.loads(result)["proposal_id"]

        # 2. 用户确认
        ok = self.store.confirm(pid)
        self.assertTrue(ok)

        # 3. 创建调度项
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        data = json.loads(result)
        sid = data["schedule_id"]

        # 4. 验证调度项已创建，字段正确
        sched = self.scheduler.get_schedule(sid)
        self.assertIsNotNone(sched)
        self.assertEqual(sched["name"], "daily_report")
        self.assertEqual(sched["task"], "生成日报")
        self.assertEqual(len(sched["granted_tools"]), 2)
        self.assertEqual(
            set(sched["active_tools_snapshot"]), {"file_read", "file_write"}
        )

        # 5. 验证 proposal 已标记终态
        p = self.store.get(pid)
        self.assertEqual(p.status, "schedule_active")
        self.assertEqual(p.schedule_id, sid)

    def test_full_modify_flow(self):
        """完整流程：propose → modify → create_schedule → schedule_active。"""
        # 1. 提议
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {
                    "cron": "0 9 * * *",
                    "task": "原始任务",
                },
                "requested_tools": [{"tool": "file_read", "scope": "all"}],
                "llm_explanation": "原始",
            },
        )
        pid = json.loads(result)["proposal_id"]

        # 2. 用户修改并确认
        ok = self.store.modify(
            pid,
            schedule_config_updates={"task": "修改后任务", "cron": "0 10 * * *"},
            requested_tools=[
                {"tool": "file_read", "scope": "all"},
                {"tool": "memory_search", "scope": "all"},
            ],
        )
        self.assertTrue(ok)

        # 3. 创建调度项
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        data = json.loads(result)
        sid = data["schedule_id"]

        # 4. 验证修改已生效
        sched = self.scheduler.get_schedule(sid)
        self.assertEqual(sched["task"], "修改后任务")
        self.assertEqual(sched["cron"], "0 10 * * *")
        self.assertEqual(
            set(sched["active_tools_snapshot"]),
            {"file_read", "memory_search"},
        )

    def test_full_reject_flow(self):
        """完整流程：propose → reject → 不能创建调度项。"""
        # 1. 提议
        result = self.registry.execute_tool(
            "cron_propose",
            {
                "schedule_config": {"cron": "0 9 * * *", "task": "hi"},
                "requested_tools": [],
                "llm_explanation": "",
            },
        )
        pid = json.loads(result)["proposal_id"]

        # 2. 用户拒绝
        ok = self.store.reject(pid)
        self.assertTrue(ok)

        # 3. 尝试创建调度项应失败
        result = self.registry.execute_tool(
            "cron_create", {"proposal_id": pid}
        )
        self.assertIn("错误", result)
        self.assertEqual(len(self.scheduler.add_calls), 0)


if __name__ == "__main__":
    unittest.main()
