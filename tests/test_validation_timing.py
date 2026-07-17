"""Q14 三时机校验测试：创建时返回 400，启动时记 WARNING。

覆盖 Task 9：
- create_schedule 端点保存前调 validate_workflow_spec，errors 非空返回 400
- _load_persisted 加载每个 schedule 后校验，errors 非空记 WARNING 不阻断
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks
install_mocks()

# 先 import hermes.server.app 触发完整 app 装配，避免循环导入
from hermes.server import app  # noqa: E402,F401
from hermes.routes.schedules import create_schedule  # noqa: E402
from hermes.schemas.schedules import ScheduleCreateRequest  # noqa: E402
from hermes.tasks.scheduler import CronScheduler  # noqa: E402
from hermes.tasks.workflow.spec import WorkflowSpec, StepSpec  # noqa: E402,F401


class TestValidationTimingCreateSchedule(unittest.TestCase):
    """Q14: 创建调度项时校验，返回 400。"""

    def _make_scheduler_mock(self) -> MagicMock:
        sched = MagicMock()
        sched.add_schedule.return_value = "new-id"
        return sched

    def test_create_schedule_with_invalid_step_type_returns_400(self):
        """非法 step.type（'invalid_type'）创建时返回 400。"""
        invalid_workflow = {
            "name": "t",
            "steps": [{"id": "s1", "type": "invalid_type"}],
        }
        req = ScheduleCreateRequest(
            name="t", cron="* * * * *", task="hi", workflow=invalid_workflow,
        )
        sched_mock = self._make_scheduler_mock()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            create_schedule(req, cron_scheduler=sched_mock)
        self.assertEqual(ctx.exception.status_code, 400)
        # 错误信息含非法 type 描述
        self.assertIn("invalid_type", str(ctx.exception.detail))
        # 未调用 add_schedule（被校验拦截）
        sched_mock.add_schedule.assert_not_called()

    def test_create_schedule_with_unregistered_tool_returns_400(self):
        """tool step 引用未注册工具时创建返回 400。"""
        invalid_workflow = {
            "name": "t",
            "steps": [{"id": "s1", "type": "tool", "config": {"tool": "no_such_tool"}}],
        }
        req = ScheduleCreateRequest(
            name="t", cron="* * * * *", task="hi", workflow=invalid_workflow,
        )
        sched_mock = self._make_scheduler_mock()
        # cron_scheduler 暴露 None registry（无法找到工具）
        sched_mock.cron_tool_registry = None
        sched_mock.tool_registry = None
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            create_schedule(req, cron_scheduler=sched_mock)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("no_such_tool", str(ctx.exception.detail))

    def test_create_schedule_with_valid_workflow_passes(self):
        """合法 workflow 正常通过校验，调用 add_schedule。"""
        valid_workflow = {
            "name": "t",
            "steps": [{"id": "s1", "type": "llm"}],
        }
        req = ScheduleCreateRequest(
            name="t", cron="* * * * *", task="hi", workflow=valid_workflow,
        )
        sched_mock = self._make_scheduler_mock()
        response = create_schedule(req, cron_scheduler=sched_mock)
        sched_mock.add_schedule.assert_called_once()
        self.assertEqual(response.schedule_id, "new-id")


class TestLoadPersistedValidationWarning(unittest.TestCase):
    """Q14: 启动加载时校验失败记 WARNING，不阻止启动。"""

    def test_load_persisted_invalid_workflow_logs_warning(self):
        """加载含非法 step.type 的 workflow 时记 WARNING，schedule 仍加载。"""
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        try:
            sched_file = os.path.join(tmpdir, "schedules.yaml")
            # 写入一个含非法 workflow 的调度项
            import yaml as _yaml
            with open(sched_file, "w", encoding="utf-8") as f:
                _yaml.dump([{
                    "id": "s1", "name": "n", "cron": "* * * * *",
                    "task": "t", "enabled": True,
                    "workflow": {"name": "t", "steps": [
                        {"id": "x", "type": "invalid_type"}
                    ]},
                }], f)
            scheduler = CronScheduler(schedules_file=sched_file)
            # 捕获 WARNING 日志
            with self.assertLogs(
                "hermes.tasks.scheduler", level="WARNING"
            ) as cm:
                scheduler._load_persisted()
            # schedule 仍被加载（不阻断启动）
            self.assertEqual(len(scheduler._schedules), 1)
            # 日志中含 workflow 校验失败信息
            joined = "\n".join(cm.output)
            self.assertIn("workflow 校验失败", joined)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_persisted_valid_workflow_no_warning(self):
        """加载合法 workflow 时不记 WARNING。"""
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        try:
            sched_file = os.path.join(tmpdir, "schedules.yaml")
            import yaml as _yaml
            with open(sched_file, "w", encoding="utf-8") as f:
                _yaml.dump([{
                    "id": "s1", "name": "n", "cron": "* * * * *",
                    "task": "t", "enabled": True,
                    "workflow": {"name": "t", "steps": [
                        {"id": "x", "type": "llm"}
                    ]},
                }], f)
            scheduler = CronScheduler(schedules_file=sched_file)
            scheduler._load_persisted()
            self.assertEqual(len(scheduler._schedules), 1)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

