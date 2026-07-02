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

from src.agent.approval import ApprovalManager  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
