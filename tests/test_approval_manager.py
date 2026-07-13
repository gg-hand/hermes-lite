"""ApprovalManager 单元测试 — 验证审批队列的创建、resolve、wait_for_decision、
超时自动拒绝、幂等、list_pending、get_status 等核心逻辑。

由于 ``ApprovalManager.wait_for_decision`` 是 async 协程，测试方法内通过
``asyncio.run()`` 驱动事件循环。

运行方式:
    python -m unittest tests.test_approval_manager -v
    python tests/test_approval_manager.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.agent.approval import ApprovalManager  # noqa: E402


class TestApprovalManagerCreateAndResolve(unittest.TestCase):
    """验证创建审批请求 + resolve + wait_for_decision 的基础流程。"""

    def test_create_and_approve(self):
        """创建 + resolve(approve) + wait_for_decision 返回 ("approve", None)。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {"cmd": "ls"}, "shell", "high")

        async def _run():
            # 先 resolve 再 wait，确保 event.set() 在 wait 之前
            ok = mgr.resolve(approval_id, "approve")
            self.assertTrue(ok)
            decision, reason = await mgr.wait_for_decision(approval_id, timeout=1.0)
            self.assertEqual(decision, "approve")
            self.assertIsNone(reason)

        asyncio.run(_run())

    def test_create_and_deny(self):
        """创建 + resolve(deny) + wait_for_decision 返回 ("deny", None)。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {"cmd": "rm -rf /"}, "dangerous", "high")

        async def _run():
            ok = mgr.resolve(approval_id, "deny")
            self.assertTrue(ok)
            decision, reason = await mgr.wait_for_decision(approval_id, timeout=1.0)
            self.assertEqual(decision, "deny")
            self.assertIsNone(reason)

        asyncio.run(_run())

    def test_resolve_with_reason(self):
        """resolve 时带 reason，wait 返回该 reason。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")

        async def _run():
            ok = mgr.resolve(approval_id, "deny", "user rejected")
            self.assertTrue(ok)
            decision, reason = await mgr.wait_for_decision(approval_id, timeout=1.0)
            self.assertEqual(decision, "deny")
            self.assertEqual(reason, "user rejected")

        asyncio.run(_run())

    def test_resolve_with_reason_persists_in_status(self):
        """resolve 传 reason 后，get_status 的 decision_reason 等于该值。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")
        ok = mgr.resolve(approval_id, "deny", "操作风险过高")
        self.assertTrue(ok)
        status = mgr.get_status(approval_id)
        self.assertEqual(status["status"], "denied")
        self.assertEqual(status["decision_reason"], "操作风险过高")

    def test_resolve_without_reason_backward_compat(self):
        """不传 reason 时 decision_reason 为 None（向后兼容）。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")
        ok = mgr.resolve(approval_id, "deny")
        self.assertTrue(ok)
        status = mgr.get_status(approval_id)
        self.assertEqual(status["status"], "denied")
        self.assertIsNone(status["decision_reason"])


class TestApprovalManagerTimeout(unittest.TestCase):
    """验证超时自动拒绝逻辑。"""

    def test_timeout_auto_deny(self):
        """wait_for_decision 超时返回 ("deny", "审批超时自动拒绝")。"""
        mgr = ApprovalManager(timeout=0.1)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")

        async def _run():
            decision, reason = await mgr.wait_for_decision(approval_id, timeout=0.1)
            self.assertEqual(decision, "deny")
            self.assertEqual(reason, "审批超时自动拒绝")

        asyncio.run(_run())


class TestApprovalManagerResolveEdgeCases(unittest.TestCase):
    """验证 resolve 的幂等、不存在 ID、非法 decision 等边界情况。"""

    def test_resolve_idempotent(self):
        """已 approved 后再 resolve 返回 False，状态不变。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")
        self.assertTrue(mgr.resolve(approval_id, "approve"))
        self.assertFalse(mgr.resolve(approval_id, "deny"))  # 已 approved
        self.assertFalse(mgr.resolve(approval_id, "approve"))  # 再次 approve 也 False
        status = mgr.get_status(approval_id)
        self.assertEqual(status["status"], "approved")

    def test_resolve_nonexistent_returns_false(self):
        """不存在的 approval_id resolve 返回 False。"""
        mgr = ApprovalManager(timeout=5.0)
        self.assertFalse(mgr.resolve("nonexistent_id", "approve"))

    def test_resolve_invalid_decision_returns_false(self):
        """decision 非 approve/deny 返回 False，状态保持 pending。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")
        self.assertFalse(mgr.resolve(approval_id, "maybe"))
        status = mgr.get_status(approval_id)
        self.assertEqual(status["status"], "pending")  # 状态未变


class TestApprovalManagerWaitEdgeCases(unittest.TestCase):
    """验证 wait_for_decision 的不存在 ID 边界情况。"""

    def test_wait_nonexistent_returns_deny(self):
        """不存在的 approval_id wait 返回 ("deny", "审批请求不存在")。"""
        mgr = ApprovalManager(timeout=1.0)

        async def _run():
            decision, reason = await mgr.wait_for_decision("nonexistent_id", timeout=1.0)
            self.assertEqual(decision, "deny")
            self.assertEqual(reason, "审批请求不存在")

        asyncio.run(_run())


class TestApprovalManagerListPending(unittest.TestCase):
    """验证 list_pending 摘要字段与已 resolved 排除逻辑。"""

    def test_list_pending(self):
        """3 个 pending 审批 list_pending 返回 3 条，摘要字段完整且不含内部字段。"""
        mgr = ApprovalManager(timeout=60.0)
        for i in range(3):
            mgr.create_request(f"tool_{i}", {"arg": i}, f"reason_{i}", "high")
        pending = mgr.list_pending()
        self.assertEqual(len(pending), 3)
        # 验证摘要字段
        for item in pending:
            self.assertIn("approval_id", item)
            self.assertIn("tool_name", item)
            self.assertIn("tool_input", item)
            self.assertIn("reason", item)
            self.assertIn("created_at", item)
            # 摘要不含 status / decision_reason / risk_level
            self.assertNotIn("status", item)
            self.assertNotIn("decision_reason", item)
            self.assertNotIn("risk_level", item)

    def test_list_pending_excludes_resolved(self):
        """resolved 的审批不在 list_pending 中。"""
        mgr = ApprovalManager(timeout=60.0)
        id1 = mgr.create_request("tool_a", {}, "reason", "high")
        id2 = mgr.create_request("tool_b", {}, "reason", "high")
        mgr.resolve(id1, "approve")
        pending = mgr.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["approval_id"], id2)


class TestApprovalManagerGetStatus(unittest.TestCase):
    """验证 get_status 返回的完整摘要字段。"""

    def test_get_status_returns_full_summary(self):
        """get_status 返回含所有字段的完整摘要。"""
        mgr = ApprovalManager(timeout=60.0)
        approval_id = mgr.create_request("bash_exec", {"cmd": "ls"}, "shell", "high")
        status = mgr.get_status(approval_id)
        self.assertEqual(status["approval_id"], approval_id)
        self.assertEqual(status["tool_name"], "bash_exec")
        self.assertEqual(status["tool_input"], {"cmd": "ls"})
        self.assertEqual(status["reason"], "shell")
        self.assertEqual(status["risk_level"], "high")
        self.assertEqual(status["status"], "pending")
        self.assertIsNone(status["decision_reason"])
        self.assertIn("created_at", status)

    def test_get_status_nonexistent_returns_none(self):
        """get_status 不存在返回 None。"""
        mgr = ApprovalManager(timeout=60.0)
        self.assertIsNone(mgr.get_status("nonexistent_id"))


class TestApprovalManagerDefaults(unittest.TestCase):
    """验证默认配置。"""

    def test_default_timeout_from_constructor(self):
        """默认 timeout=300.0。"""
        mgr = ApprovalManager()
        self.assertEqual(mgr.timeout, 300.0)

    def test_default_metrics_is_none(self):
        """默认 metrics=None（向后兼容）。"""
        mgr = ApprovalManager()
        self.assertIsNone(mgr.metrics)


class TestApprovalMetricsReporting(unittest.TestCase):
    """Phase 2 反馈监控：approval 决策计数埋点。"""

    def test_timeout_reports_timeout_decision(self):
        """超时路径上报 observe_approval_decision('timeout')。"""
        from hermes.monitoring.metrics import MetricsCollector

        metrics = MetricsCollector()
        mgr = ApprovalManager(timeout=0.1, metrics=metrics)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")

        async def _run():
            decision, reason = await mgr.wait_for_decision(
                approval_id, timeout=0.1
            )
            self.assertEqual(decision, "deny")
            self.assertEqual(reason, "审批超时自动拒绝")

        asyncio.run(_run())

        snap = metrics.snapshot()
        self.assertEqual(snap["approval_decisions_total"].get("timeout", 0), 1)
        # 用户主动路径不应被误计数
        self.assertNotIn("approve", snap["approval_decisions_total"])
        self.assertNotIn("deny", snap["approval_decisions_total"])

    def test_metrics_none_does_not_crash_on_timeout(self):
        """metrics=None 时超时路径不崩溃（向后兼容）。"""
        mgr = ApprovalManager(timeout=0.1, metrics=None)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")

        async def _run():
            decision, reason = await mgr.wait_for_decision(
                approval_id, timeout=0.1
            )
            self.assertEqual(decision, "deny")

        # 不应抛异常
        asyncio.run(_run())

    def test_user_resolve_does_not_double_count_timeout(self):
        """用户主动 approve 后再 wait 不应触发 timeout 上报。"""
        from hermes.monitoring.metrics import MetricsCollector

        metrics = MetricsCollector()
        mgr = ApprovalManager(timeout=5.0, metrics=metrics)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")
        # 用户立即 approve
        self.assertTrue(mgr.resolve(approval_id, "approve"))

        async def _run():
            decision, _ = await mgr.wait_for_decision(
                approval_id, timeout=5.0
            )
            self.assertEqual(decision, "approve")

        asyncio.run(_run())

        snap = metrics.snapshot()
        # 用户主动 approve 不在 approval.py 内上报（由 server.py 端点上报）
        self.assertNotIn("timeout", snap["approval_decisions_total"])
        self.assertNotIn("approve", snap["approval_decisions_total"])


class TestMetricsApprovalDecisionCounter(unittest.TestCase):
    """Phase 2 反馈监控：MetricsCollector.observe_approval_decision 计数器。"""

    def test_observe_approval_decision_accumulates_by_decision(self):
        """observe_approval_decision 按 decision 分桶累加。"""
        from hermes.monitoring.metrics import MetricsCollector

        collector = MetricsCollector()
        collector.observe_approval_decision("approve")
        collector.observe_approval_decision("approve")
        collector.observe_approval_decision("deny")
        collector.observe_approval_decision("timeout")
        snap = collector.snapshot()
        self.assertEqual(snap["approval_decisions_total"]["approve"], 2)
        self.assertEqual(snap["approval_decisions_total"]["deny"], 1)
        self.assertEqual(snap["approval_decisions_total"]["timeout"], 1)

    def test_reset_clears_approval_decisions(self):
        """reset 清空 approval_decisions_total。"""
        from hermes.monitoring.metrics import MetricsCollector

        collector = MetricsCollector()
        collector.observe_approval_decision("approve")
        collector.reset()
        snap = collector.snapshot()
        self.assertEqual(snap["approval_decisions_total"], {})

    def test_initial_snapshot_has_empty_approval_decisions(self):
        """新 collector 的 approval_decisions_total 为空 dict。"""
        from hermes.monitoring.metrics import MetricsCollector

        collector = MetricsCollector()
        snap = collector.snapshot()
        self.assertEqual(snap["approval_decisions_total"], {})


class TestApprovalManagerResolveAll(unittest.TestCase):
    """验证 resolve_all 批量处理 PENDING 审批。"""

    def test_resolve_all_denies_all_pending(self):
        """resolve_all('deny') 处理所有 PENDING，返回处理数量。"""
        mgr = ApprovalManager(timeout=5.0)
        id1 = mgr.create_request("bash_exec", {"cmd": "ls"}, "shell", "medium")
        id2 = mgr.create_request("file_write", {"path": "a.txt"}, "write", "high")
        id3 = mgr.create_request("memory_delete", {}, "delete", "high")

        n = mgr.resolve_all("deny", "HIL 已关闭")

        self.assertEqual(n, 3)
        self.assertEqual(mgr.get_status(id1)["status"], "denied")
        self.assertEqual(mgr.get_status(id2)["status"], "denied")
        self.assertEqual(mgr.get_status(id3)["status"], "denied")
        self.assertEqual(mgr.get_status(id1)["decision_reason"], "HIL 已关闭")

    def test_resolve_all_skips_non_pending(self):
        """resolve_all 跳过非 PENDING 状态的审批，只处理 PENDING。"""
        mgr = ApprovalManager(timeout=5.0)
        id1 = mgr.create_request("bash_exec", {}, "shell", "medium")
        id2 = mgr.create_request("file_write", {}, "write", "high")
        id3 = mgr.create_request("memory_delete", {}, "delete", "high")
        # 先 resolve id1 为 approved
        mgr.resolve(id1, "approve")

        n = mgr.resolve_all("deny", "HIL 已关闭")

        # 只处理了 id2 和 id3
        self.assertEqual(n, 2)
        self.assertEqual(mgr.get_status(id1)["status"], "approved")
        self.assertEqual(mgr.get_status(id2)["status"], "denied")
        self.assertEqual(mgr.get_status(id3)["status"], "denied")

    def test_resolve_all_wakes_waiters(self):
        """resolve_all 唤醒所有 wait_for_decision 协程，返回 deny。"""
        mgr = ApprovalManager(timeout=5.0)
        approval_id = mgr.create_request("bash_exec", {}, "shell", "high")

        async def _run():
            # 启动 wait 协程，await 期间 resolve_all 应唤醒它
            import asyncio as _asyncio

            task = _asyncio.create_task(
                mgr.wait_for_decision(approval_id, timeout=2.0)
            )
            # 让控制权回到 task，让它开始 await event.wait()
            await _asyncio.sleep(0.05)
            # 此时 task 仍在等待，未完成
            self.assertFalse(task.done())
            # resolve_all 唤醒
            n = mgr.resolve_all("deny", "HIL 已关闭")
            self.assertEqual(n, 1)
            decision, reason = await task
            self.assertEqual(decision, "deny")
            self.assertEqual(reason, "HIL 已关闭")

        asyncio.run(_run())

    def test_resolve_all_invalid_decision_returns_zero(self):
        """resolve_all 收到非法 decision 返回 0，不处理任何审批。"""
        mgr = ApprovalManager(timeout=5.0)
        mgr.create_request("bash_exec", {}, "shell", "medium")

        n = mgr.resolve_all("invalid", "bad decision")
        self.assertEqual(n, 0)
        # 所有审批仍为 PENDING
        self.assertEqual(len(mgr.list_pending()), 1)

    def test_resolve_all_empty_returns_zero(self):
        """无 PENDING 审批时 resolve_all 返回 0。"""
        mgr = ApprovalManager(timeout=5.0)
        n = mgr.resolve_all("deny", "HIL 已关闭")
        self.assertEqual(n, 0)

    def test_resolve_all_approve_all(self):
        """resolve_all('approve') 也支持，处理所有 PENDING 为 approved。"""
        mgr = ApprovalManager(timeout=5.0)
        id1 = mgr.create_request("bash_exec", {}, "shell", "medium")
        id2 = mgr.create_request("file_write", {}, "write", "high")

        n = mgr.resolve_all("approve", "批量通过")

        self.assertEqual(n, 2)
        self.assertEqual(mgr.get_status(id1)["status"], "approved")
        self.assertEqual(mgr.get_status(id2)["status"], "approved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
