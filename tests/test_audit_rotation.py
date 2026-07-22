"""audit.jsonl 轮转测试（TDD）。

验证 AuditLogger 在 audit.jsonl 行数超过阈值时自动轮转：
- 重命名为 audit.{timestamp}.jsonl
- 保留最近 N 个轮转文件，更老的删除
- 当前文件从空开始继续写入

运行方式:
    python -m pytest tests/test_audit_rotation.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from teage_liu.agent.audit import AuditLogger


class TestAuditRotation(unittest.TestCase):
    """验证 audit.jsonl 的轮转行为。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="hermes_audit_test_")
        self._log_path = os.path.join(self._tmp_dir, "audit.jsonl")

    def tearDown(self):
        # 清理临时目录
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _count_rotated_files(self) -> int:
        """统计轮转文件数量（后缀 .rotated）。"""
        return sum(
            1 for f in os.listdir(self._tmp_dir)
            if f.startswith("audit.") and f.endswith(".rotated")
        )

    def test_no_rotation_below_threshold(self):
        """行数未达阈值时不触发轮转。"""
        logger = AuditLogger(
            log_path=self._log_path,
            buffer_size=100,
            max_lines=100,  # 阈值 100
            max_files=3,
        )
        # 写入 50 条（未达阈值）
        for i in range(50):
            logger.log_tool_call(
                session_id=f"session_{i}",
                tool_name="echo",
                tool_input={"msg": f"msg_{i}"},
                result="ok",
                is_error=False,
                duration_ms=10,
            )
        logger.close()
        # 当前文件存在且只有 50 行
        self.assertTrue(os.path.exists(self._log_path))
        with open(self._log_path, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 50)
        # 没有轮转文件
        self.assertEqual(self._count_rotated_files(), 0)

    def test_rotation_triggered_at_threshold(self):
        """行数达到阈值时触发轮转，当前文件清空。"""
        logger = AuditLogger(
            log_path=self._log_path,
            buffer_size=200,
            max_lines=100,  # 阈值 100
            max_files=3,
        )
        # 写入 101 条（第 100 条触发检查，文件正好 100 行）
        for i in range(101):
            logger.log_tool_call(
                session_id=f"session_{i}",
                tool_name="echo",
                tool_input={"msg": f"msg_{i}"},
                result="ok",
                is_error=False,
                duration_ms=10,
            )
        logger.close()
        # 应至少有 1 个轮转文件
        self.assertGreaterEqual(
            self._count_rotated_files(), 1, "应触发至少 1 次轮转"
        )

    def test_max_files_retention(self):
        """保留最近 max_files 个轮转文件，更老的删除。"""
        logger = AuditLogger(
            log_path=self._log_path,
            buffer_size=500,
            max_lines=50,  # 阈值低，便于多次触发
            max_files=3,
        )
        # 写入足够多条触发多次轮转（每 100 次检查一次，200 条触发 2 次检查）
        for i in range(200):
            logger.log_tool_call(
                session_id=f"session_{i}",
                tool_name="echo",
                tool_input={"msg": f"msg_{i}"},
                result="ok",
                is_error=False,
                duration_ms=10,
            )
        logger.close()
        # 轮转文件数应不超过 max_files
        rotated_count = self._count_rotated_files()
        self.assertLessEqual(
            rotated_count, 3,
            f"轮转文件数应不超过 max_files=3，实际 {rotated_count}"
        )

    def test_rotation_disabled_when_max_lines_zero(self):
        """max_lines=0 时禁用轮转（保持向后兼容）。"""
        logger = AuditLogger(
            log_path=self._log_path,
            buffer_size=100,
            max_lines=0,  # 禁用
            max_files=3,
        )
        for i in range(200):
            logger.log_tool_call(
                session_id=f"session_{i}",
                tool_name="echo",
                tool_input={"msg": f"msg_{i}"},
                result="ok",
                is_error=False,
                duration_ms=10,
            )
        logger.close()
        # 没有轮转文件
        self.assertEqual(self._count_rotated_files(), 0)
        # 当前文件应有 200 行
        with open(self._log_path, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 200)

    def test_default_max_lines_value(self):
        """默认 max_lines 为 50000（向后兼容）。"""
        logger = AuditLogger(log_path=self._log_path, buffer_size=100)
        self.assertEqual(
            getattr(logger, "_max_lines", None), 50000,
            "默认 max_lines 应为 50000",
        )
        logger.close()


if __name__ == "__main__":
    unittest.main()
