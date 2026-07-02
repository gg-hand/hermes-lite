"""CronExpr 与 CronScheduler 单元测试（Phase 6 Task 9）。

验证：
- ``src/tasks/cron_expr.py`` 的 CronExpr 类：通配符/具体值/步长/范围/列表/
  非法字段/next_run/dom 与 dow 的 OR 语义。
- ``src/tasks/scheduler.py`` 的 CronScheduler 类：配置加载/非法 cron 标记禁用/
  立即触发/同分钟去重/CRUD/YAML 持久化。

CronScheduler 测试使用 mock orchestrator（真实可调用对象，不调用 LLM），
每个测试用例使用独立的临时 schedules.yaml 文件。

运行方式:
    python -m unittest tests.test_scheduler -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.tasks.cron_expr import CronExpr  # noqa: E402
from src.tasks.scheduler import CronScheduler  # noqa: E402


class MockOrchestrator:
    """真实可调用的 mock orchestrator，记录 chat 调用。

    不使用 unittest.mock.MagicMock，因为 asyncio.to_thread 需要真实可调用对象。
    """

    def __init__(self):
        self.calls = []

    def chat(self, session_id, user_input):
        self.calls.append((session_id, user_input))
        return "mock response"


# ===========================================================================
# CronExpr 测试
# ===========================================================================

class TestCronExpr(unittest.TestCase):
    """CronExpr 解析与匹配逻辑测试。"""

    def test_cron_expr_wildcard_match(self):
        """`* * * * *` 任意时间命中。"""
        expr = CronExpr("* * * * *")
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 30)))
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 0, 0)))
        self.assertTrue(expr.matches(datetime(2026, 12, 31, 23, 59)))

    def test_cron_expr_specific_minute(self):
        """`30 * * * *` 仅 30 分命中。"""
        expr = CronExpr("30 * * * *")
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 30)))
        self.assertFalse(expr.matches(datetime(2026, 6, 28, 9, 31)))

    def test_cron_expr_step(self):
        """`*/15 * * * *` 0/15/30/45 命中。"""
        expr = CronExpr("*/15 * * * *")
        for m in (0, 15, 30, 45):
            self.assertTrue(
                expr.matches(datetime(2026, 6, 28, 9, m)),
                f"分钟 {m} 应命中",
            )
        for m in (1, 14, 16, 29, 31, 44, 46):
            self.assertFalse(
                expr.matches(datetime(2026, 6, 28, 9, m)),
                f"分钟 {m} 不应命中",
            )

    def test_cron_expr_range(self):
        """`0 9 * * 1-5` 工作日 9 点命中。"""
        expr = CronExpr("0 9 * * 1-5")
        # 2026-06-29 是周一（cron dow=1），9:00 命中
        self.assertTrue(expr.matches(datetime(2026, 6, 29, 9, 0)))
        # 2026-06-28 是周日（cron dow=0），不命中
        self.assertFalse(expr.matches(datetime(2026, 6, 28, 9, 0)))
        # 周一 10:00 不命中（小时不符）
        self.assertFalse(expr.matches(datetime(2026, 6, 29, 10, 0)))

    def test_cron_expr_list(self):
        """`0,30 * * * *` 0 与 30 分命中。"""
        expr = CronExpr("0,30 * * * *")
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 0)))
        self.assertTrue(expr.matches(datetime(2026, 6, 28, 9, 30)))
        self.assertFalse(expr.matches(datetime(2026, 6, 28, 9, 15)))

    def test_cron_expr_invalid_field_raises(self):
        """`99 * * * *` 抛 ValueError。"""
        with self.assertRaises(ValueError):
            CronExpr("99 * * * *")

    def test_cron_expr_next_run(self):
        """next_run 返回正确未来时间。"""
        expr = CronExpr("30 * * * *")
        # 9:00 之后下一次命中是 9:30
        nxt = expr.next_run(datetime(2026, 6, 28, 9, 0, 0))
        self.assertEqual(nxt, datetime(2026, 6, 28, 9, 30, 0))
        # 恰好命中时返回严格大于 after 的下一次（10:30）
        nxt2 = expr.next_run(datetime(2026, 6, 28, 9, 30, 0))
        self.assertEqual(nxt2, datetime(2026, 6, 28, 10, 30, 0))

    def test_cron_expr_dom_and_dow_or_semantics(self):
        """日均非 * 时 OR 关系：任一字段命中即匹配。"""
        # `0 0 15 * 1`：每月 15 号 OR 每周一（cron dow=1）的 0:00
        expr = CronExpr("0 0 15 * 1")
        # 2026-07-15 是周三，day=15 命中 dom 分支
        self.assertTrue(expr.matches(datetime(2026, 7, 15, 0, 0)))
        # 2026-06-22 是周一，day=22 不命中 dom 但 cron dow=1 命中 dow
        self.assertTrue(expr.matches(datetime(2026, 6, 22, 0, 0)))
        # 2026-06-16 是周二，day=16 不命中 dom，dow=2 不命中 dow → 不匹配
        self.assertFalse(expr.matches(datetime(2026, 6, 16, 0, 0)))


# ===========================================================================
# CronScheduler 测试
# ===========================================================================

class TestCronScheduler(unittest.TestCase):
    """CronScheduler 调度器测试，每例使用独立临时 schedules.yaml。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sched_file = os.path.join(self.tmpdir, "schedules.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_scheduler(self) -> CronScheduler:
        return CronScheduler(schedules_file=self.sched_file)

    def test_scheduler_load_from_config(self):
        """解析 config 段为 Schedule 列表。"""
        scheduler = self._new_scheduler()
        scheduler.load_from_config([
            {"id": "s1", "name": "每分钟", "cron": "* * * * *", "task": "hello"},
            {"id": "s2", "name": "九点", "cron": "0 9 * * *", "task": "morning", "enabled": False},
        ])
        schedules = scheduler.list_schedules()
        self.assertEqual(len(schedules), 2)
        self.assertEqual(schedules[0]["id"], "s1")
        self.assertTrue(schedules[0]["enabled"])
        self.assertEqual(schedules[1]["id"], "s2")
        self.assertFalse(schedules[1]["enabled"])

    def test_scheduler_load_invalid_cron_marks_disabled(self):
        """非法 cron 项 enabled=False。"""
        scheduler = self._new_scheduler()
        scheduler.load_from_config([
            {"id": "bad", "name": "非法", "cron": "99 * * * *", "task": "x"},
        ])
        schedules = scheduler.list_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertFalse(schedules[0]["enabled"])

    def test_scheduler_trigger_now(self):
        """立即触发调用 orchestrator.chat（验证调用次数与参数）。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "测试", "cron": "* * * * *", "task": "执行任务", "enabled": True,
        })
        orch = MockOrchestrator()
        asyncio.run(scheduler.trigger_now(orch, sched_id))
        self.assertEqual(len(orch.calls), 1)
        session_id, user_input = orch.calls[0]
        self.assertEqual(session_id, f"cron:{sched_id}")
        self.assertEqual(user_input, "执行任务")
        # 触发后 last_run 已更新
        sched = scheduler.get_schedule(sched_id)
        self.assertIsNotNone(sched["last_run"])

    def test_scheduler_dedup_same_minute(self):
        """同一分钟内仅触发一次（运行真实 run_loop，patch wait_for 加速）。"""
        scheduler = self._new_scheduler()
        scheduler.add_schedule({
            "name": "每分钟", "cron": "* * * * *", "task": "hello", "enabled": True,
        })
        orch = MockOrchestrator()
        iteration = {"count": 0}

        async def fake_wait_for(awaitable, timeout):
            iteration["count"] += 1
            # 关闭未 await 的 wait() 协程，避免 ResourceWarning
            if hasattr(awaitable, "close"):
                try:
                    awaitable.close()
                except Exception:
                    pass
            if iteration["count"] >= 2:
                scheduler.stop()
                return
            raise asyncio.TimeoutError()

        async def run():
            with patch("asyncio.wait_for", fake_wait_for):
                await scheduler.run_loop(orch)

        asyncio.run(run())
        # 两次迭代均在同一分钟内，第二次被去重，仅触发一次
        self.assertEqual(len(orch.calls), 1)

    def test_scheduler_crud(self):
        """add/update/delete/list 持久化。"""
        scheduler = self._new_scheduler()
        # add
        sched_id = scheduler.add_schedule({
            "name": "原", "cron": "* * * * *", "task": "t1", "enabled": True,
        })
        self.assertEqual(len(scheduler.list_schedules()), 1)
        # update
        ok = scheduler.update_schedule(sched_id, {"name": "新", "enabled": False})
        self.assertTrue(ok)
        sched = scheduler.get_schedule(sched_id)
        self.assertEqual(sched["name"], "新")
        self.assertFalse(sched["enabled"])
        # update 不存在
        self.assertFalse(scheduler.update_schedule("nope", {"enabled": True}))
        # delete
        self.assertTrue(scheduler.delete_schedule(sched_id))
        self.assertEqual(scheduler.list_schedules(), [])
        # delete 不存在
        self.assertFalse(scheduler.delete_schedule(sched_id))

    def test_scheduler_persistence_yaml(self):
        """YAML 读写 roundtrip。"""
        scheduler = self._new_scheduler()
        sched_id = scheduler.add_schedule({
            "name": "持久化", "cron": "0 9 * * *", "task": "morning", "enabled": True,
        })
        # 新实例同路径重新加载
        scheduler2 = CronScheduler(schedules_file=self.sched_file)
        schedules = scheduler2.list_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["id"], sched_id)
        self.assertEqual(schedules[0]["cron"], "0 9 * * *")
        self.assertEqual(schedules[0]["task"], "morning")


if __name__ == "__main__":
    unittest.main(verbosity=2)
