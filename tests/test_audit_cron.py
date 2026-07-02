"""Phase 8 Task 4: AuditLogger cron 审计增强测试套件。

覆盖 SubTask 4.4 / 4.5 / 4.8 的审计层实现：
- SubTask 4.4: ``log_tool_call`` 新增 ``decision_source`` / ``schedule_id`` /
  ``run_id`` 字段；旧记录向后兼容（``_normalize_entry`` 填充默认值）。
- SubTask 4.5: ``get_by_schedule`` 与 ``get_by_run_id`` 查询方法。
- SubTask 4.8: 回归测试。

设计要点：
- 使用 ``tempfile.TemporaryDirectory`` 隔离 JSONL 文件，避免测试间污染
- 旧记录（缺少 Task 4.4 新字段）通过 ``_normalize_entry`` 填充默认值
- ``get_by_schedule`` 优先用 ``schedule_id`` 冗余字段匹配，回退到
  ``session_id`` 前缀匹配（兼容旧记录未填充 ``schedule_id`` 的场景）
- ``get_by_run_id`` 按 ``schedule_id`` + ``run_id`` 双重过滤

运行方式:
    python -m unittest tests.test_audit_cron -v
"""

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

from src.agent.audit import AuditLogger  # noqa: E402


class TestAuditLoggerNewFields(unittest.TestCase):
    """SubTask 4.4: log_tool_call 新增字段写入与读取。"""

    def test_log_with_new_fields(self):
        """log_tool_call 传入 decision_source / schedule_id / run_id 后正确写入。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            logger.log_tool_call(
                session_id="cron:sched-1",
                tool_name="file_read",
                tool_input={"path": "/x"},
                result="ok",
                is_error=False,
                duration_ms=5.0,
                decision_source="schedule_grant",
                schedule_id="sched-1",
                run_id="run-001",
            )
            recent = logger.get_recent(1)
            self.assertEqual(len(recent), 1)
            entry = recent[0]
            self.assertEqual(entry["decision_source"], "schedule_grant")
            self.assertEqual(entry["schedule_id"], "sched-1")
            self.assertEqual(entry["run_id"], "run-001")
            logger.close()

    def test_log_default_decision_source(self):
        """不传 decision_source 时默认为 default_rule。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            logger.log_tool_call(
                session_id="user-session",
                tool_name="file_read",
                tool_input={},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
            recent = logger.get_recent(1)
            self.assertEqual(recent[0]["decision_source"], "default_rule")
            self.assertIsNone(recent[0]["schedule_id"])
            self.assertIsNone(recent[0]["run_id"])
            logger.close()

    def test_jsonl_persistence_with_new_fields(self):
        """新字段持久化到 JSONL 文件，每行可 json.loads 且字段完整。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            logger.log_tool_call(
                session_id="cron:sched-1",
                tool_name="file_write",
                tool_input={"path": "/data/x"},
                result="written",
                is_error=False,
                duration_ms=10.0,
                decision_source="schedule_grant",
                schedule_id="sched-1",
                run_id="run-001",
            )
            logger.close()

            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["decision_source"], "schedule_grant")
            self.assertEqual(entry["schedule_id"], "sched-1")
            self.assertEqual(entry["run_id"], "run-001")


class TestNormalizeEntryBackwardCompat(unittest.TestCase):
    """SubTask 4.4: _normalize_entry 为旧记录填充默认值。"""

    def test_normalize_fills_missing_fields(self):
        """旧记录缺少新字段时填充默认值。"""
        old_entry = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "session_id": "old-session",
            "tool_name": "file_read",
            "tool_input": {},
            "result": "ok",
            "is_error": False,
            "duration_ms": 1.0,
        }
        normalized = AuditLogger._normalize_entry(old_entry)
        self.assertEqual(normalized["decision_source"], "default_rule")
        self.assertIsNone(normalized["schedule_id"])
        self.assertIsNone(normalized["run_id"])
        # 原有字段保留
        self.assertEqual(normalized["tool_name"], "file_read")
        self.assertEqual(normalized["session_id"], "old-session")

    def test_normalize_preserves_existing_fields(self):
        """新记录已有新字段时保留原值。"""
        new_entry = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "session_id": "cron:sched-1",
            "tool_name": "file_write",
            "tool_input": {},
            "result": "ok",
            "is_error": False,
            "duration_ms": 1.0,
            "decision_source": "schedule_grant",
            "schedule_id": "sched-1",
            "run_id": "run-001",
        }
        normalized = AuditLogger._normalize_entry(new_entry)
        self.assertEqual(normalized["decision_source"], "schedule_grant")
        self.assertEqual(normalized["schedule_id"], "sched-1")
        self.assertEqual(normalized["run_id"], "run-001")

    def test_normalize_returns_deep_copy(self):
        """_normalize_entry 返回深拷贝，修改不影响原 dict。"""
        entry = {"tool_name": "x", "decision_source": "default_rule"}
        normalized = AuditLogger._normalize_entry(entry)
        normalized["tool_name"] = "y"
        # 原 dict 不变
        self.assertEqual(entry["tool_name"], "x")

    def test_get_recent_normalizes_old_buffer_entries(self):
        """get_recent 对内存缓冲中的旧记录（无新字段）填充默认值。

        通过直接操作 _buffer 模拟旧记录（绕过 log_tool_call 的字段填充）。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            # 直接往 buffer 注入旧格式记录（无 decision_source 等字段）
            old_entry = {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "session_id": "old-session",
                "tool_name": "file_read",
                "tool_input": {},
                "result": "ok",
                "is_error": False,
                "duration_ms": 1.0,
            }
            logger._buffer.append(old_entry)
            recent = logger.get_recent(1)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["decision_source"], "default_rule")
            self.assertIsNone(recent[0]["schedule_id"])
            self.assertIsNone(recent[0]["run_id"])
            logger.close()


class TestGetBySchedule(unittest.TestCase):
    """SubTask 4.5: get_by_schedule 按调度项 ID 查询审计记录。"""

    def setUp(self):
        """构造带多条记录的 AuditLogger（含 cron 与非 cron 会话）。"""
        self.tmpdir = tempfile.mkdtemp(prefix="audit_cron_test_")
        log_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.logger = AuditLogger(log_path=log_path, buffer_size=100)
        # 写入 5 条记录：
        # - 3 条属于 cron:sched-1（含 schedule_id 冗余字段）
        # - 1 条属于 cron:sched-2
        # - 1 条属于普通用户会话
        self.logger.log_tool_call(
            session_id="cron:sched-1",
            tool_name="file_read",
            tool_input={"path": "/a"},
            result="r1",
            is_error=False,
            duration_ms=1.0,
            decision_source="schedule_grant",
            schedule_id="sched-1",
            run_id="run-001",
        )
        self.logger.log_tool_call(
            session_id="cron:sched-1",
            tool_name="file_write",
            tool_input={"path": "/b"},
            result="r2",
            is_error=False,
            duration_ms=2.0,
            decision_source="schedule_grant",
            schedule_id="sched-1",
            run_id="run-001",
        )
        self.logger.log_tool_call(
            session_id="cron:sched-1",
            tool_name="file_read",
            tool_input={"path": "/c"},
            result="r3",
            is_error=False,
            duration_ms=3.0,
            decision_source="schedule_grant",
            schedule_id="sched-1",
            run_id="run-002",
        )
        self.logger.log_tool_call(
            session_id="cron:sched-2",
            tool_name="file_read",
            tool_input={"path": "/d"},
            result="r4",
            is_error=False,
            duration_ms=4.0,
            decision_source="schedule_grant",
            schedule_id="sched-2",
            run_id="run-003",
        )
        self.logger.log_tool_call(
            session_id="user-session-1",
            tool_name="file_read",
            tool_input={"path": "/e"},
            result="r5",
            is_error=False,
            duration_ms=5.0,
            decision_source="default_rule",
            schedule_id=None,
            run_id=None,
        )

    def tearDown(self):
        self.logger.close()

    def test_filter_by_schedule_id(self):
        """按 sched-1 过滤返回 3 条记录（倒序，最新在前）。"""
        logs = self.logger.get_by_schedule("sched-1", limit=50)
        self.assertEqual(len(logs), 3)
        # 倒序：最新（r3）在前
        self.assertEqual(logs[0]["result"], "r3")
        self.assertEqual(logs[1]["result"], "r2")
        self.assertEqual(logs[2]["result"], "r1")
        # 所有记录的 schedule_id 都为 sched-1
        for log in logs:
            self.assertEqual(log["schedule_id"], "sched-1")

    def test_filter_by_different_schedule(self):
        """按 sched-2 过滤返回 1 条记录。"""
        logs = self.logger.get_by_schedule("sched-2", limit=50)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result"], "r4")

    def test_filter_nonexistent_schedule(self):
        """不存在的 schedule_id 返回空列表。"""
        logs = self.logger.get_by_schedule("nonexistent", limit=50)
        self.assertEqual(len(logs), 0)

    def test_limit_truncation(self):
        """limit=2 时仅返回最近 2 条（倒序）。"""
        logs = self.logger.get_by_schedule("sched-1", limit=2)
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["result"], "r3")
        self.assertEqual(logs[1]["result"], "r2")

    def test_filter_by_run_id(self):
        """按 schedule_id + run_id 双重过滤。"""
        logs = self.logger.get_by_schedule("sched-1", limit=50, run_id="run-001")
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertEqual(log["run_id"], "run-001")
        # 倒序：r2 在前，r1 在后
        self.assertEqual(logs[0]["result"], "r2")
        self.assertEqual(logs[1]["result"], "r1")

    def test_filter_by_nonexistent_run_id(self):
        """run_id 不存在时返回空列表。"""
        logs = self.logger.get_by_schedule("sched-1", limit=50, run_id="nonexistent")
        self.assertEqual(len(logs), 0)

    def test_user_session_not_matched(self):
        """非 cron 会话（无 schedule_id）不被 sched-1 匹配。"""
        logs = self.logger.get_by_schedule("sched-1", limit=50)
        for log in logs:
            self.assertNotEqual(log["session_id"], "user-session-1")

    def test_fallback_to_session_id_prefix(self):
        """旧记录（schedule_id=None）通过 session_id 前缀匹配。"""
        # 直接往 buffer 注入旧格式记录（无 schedule_id 字段）
        old_entry = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "session_id": "cron:sched-old",
            "tool_name": "file_read",
            "tool_input": {},
            "result": "old-record",
            "is_error": False,
            "duration_ms": 1.0,
        }
        self.logger._buffer.append(old_entry)
        # 通过 session_id 前缀匹配
        logs = self.logger.get_by_schedule("sched-old", limit=50)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result"], "old-record")

    def test_returns_deep_copy(self):
        """get_by_schedule 返回深拷贝，修改不影响内部缓冲。"""
        logs = self.logger.get_by_schedule("sched-1", limit=50)
        original_result = logs[0]["result"]
        logs[0]["result"] = "modified"
        # 再次查询，内部缓冲未被污染
        logs2 = self.logger.get_by_schedule("sched-1", limit=50)
        self.assertEqual(logs2[0]["result"], original_result)


class TestGetByRunId(unittest.TestCase):
    """SubTask 4.5: get_by_run_id 按调度项 ID + run_id 查询审计记录。"""

    def setUp(self):
        """构造带多条记录（含不同 run_id）的 AuditLogger。"""
        self.tmpdir = tempfile.mkdtemp(prefix="audit_runid_test_")
        log_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.logger = AuditLogger(log_path=log_path, buffer_size=100)
        # run-001: 3 条
        for i in range(3):
            self.logger.log_tool_call(
                session_id="cron:sched-1",
                tool_name=f"t{i}",
                tool_input={},
                result=f"r{i}",
                is_error=False,
                duration_ms=float(i),
                decision_source="schedule_grant",
                schedule_id="sched-1",
                run_id="run-001",
            )
        # run-002: 2 条
        for i in range(2):
            self.logger.log_tool_call(
                session_id="cron:sched-1",
                tool_name=f"t2_{i}",
                tool_input={},
                result=f"r2_{i}",
                is_error=False,
                duration_ms=float(i),
                decision_source="schedule_grant",
                schedule_id="sched-1",
                run_id="run-002",
            )

    def tearDown(self):
        self.logger.close()

    def test_filter_by_run_id(self):
        """按 sched-1 + run-001 过滤返回 3 条（正序，最早在前）。"""
        logs = self.logger.get_by_run_id("sched-1", "run-001")
        self.assertEqual(len(logs), 3)
        # 正序：最早（r0）在前
        self.assertEqual(logs[0]["result"], "r0")
        self.assertEqual(logs[1]["result"], "r1")
        self.assertEqual(logs[2]["result"], "r2")

    def test_filter_by_different_run_id(self):
        """按 run-002 过滤返回 2 条。"""
        logs = self.logger.get_by_run_id("sched-1", "run-002")
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertEqual(log["run_id"], "run-002")

    def test_nonexistent_run_id_returns_empty(self):
        """不存在的 run_id 返回空列表。"""
        logs = self.logger.get_by_run_id("sched-1", "nonexistent")
        self.assertEqual(len(logs), 0)

    def test_nonexistent_schedule_returns_empty(self):
        """不存在的 schedule_id 返回空列表（即使 run_id 存在）。"""
        logs = self.logger.get_by_run_id("nonexistent", "run-001")
        self.assertEqual(len(logs), 0)

    def test_run_id_isolation_between_schedules(self):
        """同一 run_id 在不同 schedule 间隔离。"""
        # 在 sched-2 下也写入 run-001
        self.logger.log_tool_call(
            session_id="cron:sched-2",
            tool_name="x",
            tool_input={},
            result="sched-2-record",
            is_error=False,
            duration_ms=1.0,
            decision_source="schedule_grant",
            schedule_id="sched-2",
            run_id="run-001",
        )
        # 查 sched-1 + run-001 不应包含 sched-2 的记录
        logs = self.logger.get_by_run_id("sched-1", "run-001")
        self.assertEqual(len(logs), 3)
        for log in logs:
            self.assertEqual(log["schedule_id"], "sched-1")

    def test_returns_deep_copy(self):
        """get_by_run_id 返回深拷贝。"""
        logs = self.logger.get_by_run_id("sched-1", "run-001")
        original = logs[0]["result"]
        logs[0]["result"] = "modified"
        logs2 = self.logger.get_by_run_id("sched-1", "run-001")
        self.assertEqual(logs2[0]["result"], original)


class TestGetByScheduleEmptyAllowedPaths(unittest.TestCase):
    """边界场景：空 buffer 与空 schedule_id。"""

    def test_empty_buffer_returns_empty(self):
        """空 buffer 时 get_by_schedule 返回空列表。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            self.assertEqual(logger.get_by_schedule("any"), [])
            self.assertEqual(logger.get_by_run_id("any", "any"), [])
            logger.close()

    def test_empty_schedule_id_returns_empty(self):
        """空 schedule_id 字符串不匹配任何记录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            logger.log_tool_call(
                session_id="cron:sched-1",
                tool_name="t",
                tool_input={},
                result="r",
                is_error=False,
                duration_ms=1.0,
                schedule_id="sched-1",
            )
            # 空字符串不匹配
            self.assertEqual(logger.get_by_schedule(""), [])
            logger.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
