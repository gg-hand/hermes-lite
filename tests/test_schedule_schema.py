"""Phase 8 Task 1.6: Schedule config schema 扩展测试。

验证 Schedule dataclass 新增字段：
- ``cron_id`` / ``granted_tools`` / ``active_tools_snapshot`` /
  ``workflow`` / ``generate_llm_summary``
- ``add_schedule`` / ``update_schedule`` / ``load_from_config`` 支持新字段
- ``_load_persisted`` 从 YAML 加载新字段（向后兼容旧 YAML）
- 持久化到 ``schedules.yaml`` 含新字段
- ``get_cron_id()`` 在 cron_id 为 None 时回退到 id

运行方式:
    python -m unittest tests.test_schedule_schema -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from teage_liu.tasks.scheduler import CronScheduler, Schedule  # noqa: E402


class TestScheduleDataclassNewFields(unittest.TestCase):
    """验证 Schedule dataclass 新增字段默认值。"""

    def test_default_new_fields_are_none_or_false(self):
        """新字段默认值：cron_id/granted_tools/snapshot/workflow=None, summary=False。"""
        sched = Schedule(id="s1", name="test", cron="0 * * * *", task="hi")
        self.assertIsNone(sched.cron_id)
        self.assertIsNone(sched.granted_tools)
        self.assertIsNone(sched.active_tools_snapshot)
        self.assertIsNone(sched.workflow)
        self.assertFalse(sched.generate_llm_summary)

    def test_get_cron_id_falls_back_to_id(self):
        """cron_id 为 None 时 get_cron_id() 回退到 id。"""
        sched = Schedule(id="s1", name="test", cron="0 * * * *", task="hi")
        self.assertEqual(sched.get_cron_id(), "s1")

    def test_get_cron_id_uses_explicit_value(self):
        """cron_id 显式设置时 get_cron_id() 返回该值。"""
        sched = Schedule(
            id="s1", name="test", cron="0 * * * *", task="hi", cron_id="custom_cron"
        )
        self.assertEqual(sched.get_cron_id(), "custom_cron")

    def test_all_new_fields_settable(self):
        """所有新字段可在构造时设置。"""
        granted = [{"tool": "file_read", "scope": "all", "allowed_paths": []}]
        snapshot = ["file_read", "memory_search"]
        workflow = {"template": "directory_watch", "watch_path": "/data"}
        sched = Schedule(
            id="s1",
            name="test",
            cron="0 * * * *",
            task="hi",
            cron_id="cron_s1",
            granted_tools=granted,
            active_tools_snapshot=snapshot,
            workflow=workflow,
            generate_llm_summary=True,
        )
        self.assertEqual(sched.cron_id, "cron_s1")
        self.assertEqual(sched.granted_tools, granted)
        self.assertEqual(sched.active_tools_snapshot, snapshot)
        self.assertEqual(sched.workflow, workflow)
        self.assertTrue(sched.generate_llm_summary)


class TestAddScheduleNewFields(unittest.TestCase):
    """验证 add_schedule 支持新字段。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="sched_add_")
        self._sched_file = os.path.join(self._tmpdir, "schedules.yaml")
        self.scheduler = CronScheduler(schedules_file=self._sched_file)

    def test_add_schedule_with_new_fields(self):
        """add_schedule 含新字段时正确写入。"""
        granted = [{"tool": "file_read", "scope": "path_prefix", "allowed_paths": ["/data"]}]
        snapshot = ["file_read", "memory_search"]
        workflow = {"template": "summary", "session_id": "sess_123"}
        sid = self.scheduler.add_schedule(
            {
                "name": "test",
                "cron": "0 * * * *",
                "task": "summarize",
                "cron_id": "cron_x",
                "granted_tools": granted,
                "active_tools_snapshot": snapshot,
                "workflow": workflow,
                "generate_llm_summary": True,
            }
        )
        sched = self.scheduler.get_schedule(sid)
        self.assertEqual(sched["cron_id"], "cron_x")
        self.assertEqual(sched["granted_tools"], granted)
        self.assertEqual(sched["active_tools_snapshot"], snapshot)
        self.assertEqual(sched["workflow"], workflow)
        self.assertTrue(sched["generate_llm_summary"])

    def test_add_schedule_without_new_fields_uses_defaults(self):
        """add_schedule 不含新字段时使用默认值（向后兼容）。"""
        sid = self.scheduler.add_schedule(
            {"name": "test", "cron": "0 * * * *", "task": "hi"}
        )
        sched = self.scheduler.get_schedule(sid)
        self.assertIsNone(sched["cron_id"])
        self.assertIsNone(sched["granted_tools"])
        self.assertIsNone(sched["active_tools_snapshot"])
        self.assertIsNone(sched["workflow"])
        self.assertFalse(sched["generate_llm_summary"])


class TestUpdateScheduleNewFields(unittest.TestCase):
    """验证 update_schedule 支持新字段。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="sched_upd_")
        self._sched_file = os.path.join(self._tmpdir, "schedules.yaml")
        self.scheduler = CronScheduler(schedules_file=self._sched_file)
        self.sid = self.scheduler.add_schedule(
            {"name": "test", "cron": "0 * * * *", "task": "hi"}
        )

    def test_update_granted_tools(self):
        """update_schedule 可更新 granted_tools。"""
        granted = [{"tool": "file_write", "scope": "path_prefix", "allowed_paths": ["/tmp"]}]
        ok = self.scheduler.update_schedule(self.sid, {"granted_tools": granted})
        self.assertTrue(ok)
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["granted_tools"], granted)

    def test_update_active_tools_snapshot(self):
        """update_schedule 可更新 active_tools_snapshot。"""
        snapshot = ["file_read", "file_write"]
        ok = self.scheduler.update_schedule(
            self.sid, {"active_tools_snapshot": snapshot}
        )
        self.assertTrue(ok)
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["active_tools_snapshot"], snapshot)

    def test_update_workflow(self):
        """update_schedule 可更新 workflow。"""
        workflow = {"template": "email_notify", "to": "user@example.com"}
        ok = self.scheduler.update_schedule(self.sid, {"workflow": workflow})
        self.assertTrue(ok)
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["workflow"], workflow)

    def test_update_generate_llm_summary(self):
        """update_schedule 可更新 generate_llm_summary。"""
        ok = self.scheduler.update_schedule(
            self.sid, {"generate_llm_summary": True}
        )
        self.assertTrue(ok)
        sched = self.scheduler.get_schedule(self.sid)
        self.assertTrue(sched["generate_llm_summary"])

    def test_update_cron_id(self):
        """update_schedule 可更新 cron_id。"""
        ok = self.scheduler.update_schedule(self.sid, {"cron_id": "new_cron"})
        self.assertTrue(ok)
        sched = self.scheduler.get_schedule(self.sid)
        self.assertEqual(sched["cron_id"], "new_cron")


class TestPersistAndLoadNewFields(unittest.TestCase):
    """验证新字段持久化到 YAML 并能正确加载。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="sched_persist_")
        self._sched_file = os.path.join(self._tmpdir, "schedules.yaml")

    def test_persist_and_reload_with_new_fields(self):
        """含新字段的调度项持久化后重新加载，字段值保持一致。"""
        scheduler1 = CronScheduler(schedules_file=self._sched_file)
        granted = [{"tool": "file_read", "scope": "all", "allowed_paths": []}]
        snapshot = ["file_read", "memory_search"]
        workflow = {"template": "directory_watch", "watch_path": "/data"}
        sid = scheduler1.add_schedule(
            {
                "name": "test",
                "cron": "0 * * * *",
                "task": "scan",
                "cron_id": "cron_persist",
                "granted_tools": granted,
                "active_tools_snapshot": snapshot,
                "workflow": workflow,
                "generate_llm_summary": True,
            }
        )

        # 重新加载
        scheduler2 = CronScheduler(schedules_file=self._sched_file)
        sched = scheduler2.get_schedule(sid)
        self.assertIsNotNone(sched)
        self.assertEqual(sched["cron_id"], "cron_persist")
        self.assertEqual(sched["granted_tools"], granted)
        self.assertEqual(sched["active_tools_snapshot"], snapshot)
        self.assertEqual(sched["workflow"], workflow)
        self.assertTrue(sched["generate_llm_summary"])

    def test_load_old_yaml_without_new_fields(self):
        """旧 YAML（无新字段）能正确加载，新字段使用默认值（向后兼容）。"""
        # 手动写入旧格式 YAML（无新字段）
        import yaml

        old_data = [
            {
                "id": "old_sched",
                "name": "old",
                "cron": "0 * * * *",
                "task": "legacy",
                "enabled": True,
                "last_run": None,
                "next_run": None,
            }
        ]
        with open(self._sched_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(old_data, f, allow_unicode=True, sort_keys=False)

        # 加载
        scheduler = CronScheduler(schedules_file=self._sched_file)
        sched = scheduler.get_schedule("old_sched")
        self.assertIsNotNone(sched)
        self.assertEqual(sched["id"], "old_sched")
        self.assertEqual(sched["task"], "legacy")
        # 新字段使用默认值
        self.assertIsNone(sched["cron_id"])
        self.assertIsNone(sched["granted_tools"])
        self.assertIsNone(sched["active_tools_snapshot"])
        self.assertIsNone(sched["workflow"])
        self.assertFalse(sched["generate_llm_summary"])

    def test_load_from_config_with_new_fields(self):
        """load_from_config 支持新字段。"""
        scheduler = CronScheduler(schedules_file=self._sched_file)
        cfg = [
            {
                "name": "cfg_test",
                "cron": "0 * * * *",
                "task": "scan",
                "cron_id": "cfg_cron",
                "granted_tools": [{"tool": "file_read", "scope": "all"}],
                "active_tools_snapshot": ["file_read"],
                "workflow": {"template": "summary"},
                "generate_llm_summary": True,
            }
        ]
        scheduler.load_from_config(cfg)
        scheds = scheduler.list_schedules()
        self.assertEqual(len(scheds), 1)
        sched = scheds[0]
        self.assertEqual(sched["cron_id"], "cfg_cron")
        self.assertEqual(len(sched["granted_tools"]), 1)
        self.assertEqual(sched["active_tools_snapshot"], ["file_read"])
        self.assertEqual(sched["workflow"], {"template": "summary"})
        self.assertTrue(sched["generate_llm_summary"])

    def test_load_from_config_without_new_fields(self):
        """load_from_config 不含新字段时使用默认值（向后兼容）。"""
        scheduler = CronScheduler(schedules_file=self._sched_file)
        cfg = [
            {
                "name": "legacy",
                "cron": "0 * * * *",
                "task": "hi",
            }
        ]
        scheduler.load_from_config(cfg)
        sched = scheduler.list_schedules()[0]
        self.assertIsNone(sched["cron_id"])
        self.assertIsNone(sched["granted_tools"])
        self.assertIsNone(sched["active_tools_snapshot"])
        self.assertIsNone(sched["workflow"])
        self.assertFalse(sched["generate_llm_summary"])


class TestGetCronIdMethod(unittest.TestCase):
    """验证 Schedule.get_cron_id() 方法。"""

    def test_get_cron_id_with_none_uses_id(self):
        """cron_id=None 时 get_cron_id 返回 id。"""
        sched = Schedule(id="abc", name="t", cron="0 * * * *", task="x")
        self.assertEqual(sched.get_cron_id(), "abc")

    def test_get_cron_id_with_explicit_value(self):
        """cron_id 显式设置时 get_cron_id 返回该值。"""
        sched = Schedule(
            id="abc", name="t", cron="0 * * * *", task="x", cron_id="xyz"
        )
        self.assertEqual(sched.get_cron_id(), "xyz")

    def test_get_cron_id_after_update(self):
        """update_schedule 更新 cron_id 后 get_cron_id 返回新值。"""
        scheduler = CronScheduler(
            schedules_file=os.path.join(
                tempfile.mkdtemp(prefix="sched_get_"), "s.yaml"
            )
        )
        sid = scheduler.add_schedule(
            {"name": "t", "cron": "0 * * * *", "task": "x"}
        )
        sched_obj = scheduler._find_schedule(sid)
        self.assertEqual(sched_obj.get_cron_id(), sid)
        scheduler.update_schedule(sid, {"cron_id": "custom"})
        self.assertEqual(sched_obj.get_cron_id(), "custom")


if __name__ == "__main__":
    unittest.main()
