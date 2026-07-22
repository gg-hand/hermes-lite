"""AuditLogger 单元测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.agent.audit import AuditLogger  # noqa: E402


class TestAuditLogger(unittest.TestCase):
    """验证 AuditLogger 的核心行为：写入、截断、环形缓冲、持久化、降级。"""

    def test_log_tool_call_basic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            logger.log_tool_call(
                session_id="sess-1",
                tool_name="file_read",
                tool_input={"path": "test.txt"},
                result="file content",
                is_error=False,
                duration_ms=5.0,
            )
            recent = logger.get_recent(1)
            self.assertEqual(len(recent), 1)
            entry = recent[0]
            self.assertEqual(entry["session_id"], "sess-1")
            self.assertEqual(entry["tool_name"], "file_read")
            self.assertEqual(entry["tool_input"], {"path": "test.txt"})
            self.assertEqual(entry["result"], "file content")
            self.assertFalse(entry["is_error"])
            self.assertEqual(entry["duration_ms"], 5.0)
            self.assertIn("timestamp", entry)
            logger.close()

    def test_result_truncation(self):
        """result 超过 _MAX_RESULT_LENGTH 时被截断为前 2000 字符 + '...[truncated]'。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            long_result = "x" * 2500
            logger.log_tool_call(
                session_id="sess-trunc",
                tool_name="big_tool",
                tool_input={},
                result=long_result,
                is_error=False,
                duration_ms=10.0,
            )
            recent = logger.get_recent(1)
            self.assertEqual(len(recent), 1)
            truncated = recent[0]["result"]
            expected_length = 2000 + len("...[truncated]")
            self.assertEqual(len(truncated), expected_length)
            self.assertTrue(truncated.endswith("...[truncated]"))
            self.assertEqual(truncated[:2000], "x" * 2000)
            logger.close()

    def test_ring_buffer_overflow(self):
        """buffer_size=3 时 log 5 条，仅保留最近 3 条（倒序最新在前）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=3)
            for i in range(1, 6):
                logger.log_tool_call(
                    session_id="sess",
                    tool_name=f"t{i}",
                    tool_input={},
                    result=f"r{i}",
                    is_error=False,
                    duration_ms=float(i),
                )
            recent = logger.get_recent(10)
            self.assertEqual(len(recent), 3)
            # 倒序：最新（t5）在前
            self.assertEqual(recent[0]["tool_name"], "t5")
            self.assertEqual(recent[1]["tool_name"], "t4")
            self.assertEqual(recent[2]["tool_name"], "t3")
            logger.close()

    def test_get_recent_limit(self):
        """log 10 条后 get_recent(5) 返回最近 5 条（倒序，最新在前）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            for i in range(1, 11):
                logger.log_tool_call(
                    session_id="sess",
                    tool_name=f"t{i}",
                    tool_input={},
                    result=f"r{i}",
                    is_error=False,
                    duration_ms=float(i),
                )
            recent = logger.get_recent(5)
            self.assertEqual(len(recent), 5)
            # 第 1 条是最后 log 的（t10），最新在前
            self.assertEqual(recent[0]["tool_name"], "t10")
            self.assertEqual(recent[1]["tool_name"], "t9")
            self.assertEqual(recent[2]["tool_name"], "t8")
            self.assertEqual(recent[3]["tool_name"], "t7")
            self.assertEqual(recent[4]["tool_name"], "t6")
            logger.close()

    def test_jsonl_persistence(self):
        """log 3 条后 close，JSONL 文件应有 3 行且每行可 json.loads，字段完整。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            for i in range(1, 4):
                logger.log_tool_call(
                    session_id=f"sess-{i}",
                    tool_name=f"t{i}",
                    tool_input={"idx": i},
                    result=f"r{i}",
                    is_error=(i == 3),
                    duration_ms=float(i),
                )
            logger.close()

            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 3)
            for idx, line in enumerate(lines, start=1):
                entry = json.loads(line)
                self.assertEqual(entry["session_id"], f"sess-{idx}")
                self.assertEqual(entry["tool_name"], f"t{idx}")
                self.assertEqual(entry["tool_input"], {"idx": idx})
                self.assertEqual(entry["result"], f"r{idx}")
                self.assertEqual(entry["is_error"], idx == 3)
                self.assertEqual(entry["duration_ms"], float(idx))
                self.assertIn("timestamp", entry)

    def test_close_idempotent(self):
        """close 两次不抛异常。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            logger.close()
            # 再次 close 应幂等，不抛异常
            logger.close()

    def test_file_write_failure_degradation(self):
        """close 后 _file 为 None，再调用 log_tool_call 不抛异常（仅 warning）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            logger.close()
            # close 后 self._file 为 None，写入会触发 AttributeError 被 try/except 捕获
            try:
                logger.log_tool_call(
                    session_id="sess-degrade",
                    tool_name="t",
                    tool_input={},
                    result="r",
                    is_error=False,
                    duration_ms=1.0,
                )
            except Exception as e:
                self.fail(f"log_tool_call should not raise after close, but got: {e!r}")
            # 内存缓冲仍应记录该条（写入文件失败不影响 deque 追加）
            recent = logger.get_recent(1)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["session_id"], "sess-degrade")


if __name__ == "__main__":
    unittest.main(verbosity=2)
