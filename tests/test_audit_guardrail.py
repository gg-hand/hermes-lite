"""Phase 9 Task 5: AuditLogger 护栏决策审计测试套件。

覆盖 ``log_guardrail_decision`` 方法的写入、内存缓冲、JSONL 持久化、
向后兼容与查询过滤：

- ``log_guardrail_decision`` 写入测试：验证 JSONL 文件含
  ``entry_type="guardrail"``，且字段完整（layer / action / reason /
  matched_patterns / risk_level）。
- 旧记录兼容性测试：无 ``entry_type`` 时 ``get_recent`` 默认
  ``"tool_call"``（向后兼容 Phase 8 工具调用记录）。
- 查询过滤测试：``get_recent(entry_type=...)`` 可按记录类型过滤。
- 内存环形缓冲包含护栏记录：护栏记录与工具调用记录共用同一缓冲。
- 多条护栏记录写入：连续写入多条护栏记录，均能正确读取。
- ``matched_patterns`` 参数测试：``None`` 与列表两种入参均能持久化。

设计要点：
- 使用 ``tempfile.TemporaryDirectory`` 隔离 JSONL 文件，避免测试间污染。
- 通过直接操作 ``_buffer`` 模拟旧记录（绕过 ``log_tool_call`` 字段填充），
  验证 ``_normalize_entry`` 的向后兼容行为。
- 护栏记录与工具调用记录共用同一缓冲与 JSONL 文件，通过 ``entry_type``
  字段区分。

运行方式:
    python -m pytest tests/test_audit_guardrail.py -v
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


class TestLogGuardrailDecision(unittest.TestCase):
    """``log_guardrail_decision`` 写入测试。"""

    def test_writes_entry_to_jsonl_with_entry_type(self):
        """log_guardrail_decision 写入 JSONL 文件，含 entry_type="guardrail"。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            logger.log_guardrail_decision(
                layer="input_scan",
                action="deny",
                reason="检测到注入攻击模式",
                session_id="sess-1",
                matched_patterns=["sql_injection"],
                risk_level="high",
            )
            logger.close()

            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["entry_type"], "guardrail")
            self.assertEqual(entry["layer"], "input_scan")
            self.assertEqual(entry["action"], "deny")
            self.assertEqual(entry["reason"], "检测到注入攻击模式")
            self.assertEqual(entry["session_id"], "sess-1")
            self.assertEqual(entry["matched_patterns"], ["sql_injection"])
            self.assertEqual(entry["risk_level"], "high")
            self.assertIn("timestamp", entry)

    def test_buffer_contains_guardrail_record(self):
        """护栏记录加入内存环形缓冲，get_recent 可读取。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            logger.log_guardrail_decision(
                layer="output_filter",
                action="warn",
                reason="输出包含敏感关键词",
                session_id="sess-2",
            )
            recent = logger.get_recent(1)
            self.assertEqual(len(recent), 1)
            entry = recent[0]
            self.assertEqual(entry["entry_type"], "guardrail")
            self.assertEqual(entry["layer"], "output_filter")
            self.assertEqual(entry["action"], "warn")
            self.assertEqual(entry["reason"], "输出包含敏感关键词")
            self.assertEqual(entry["session_id"], "sess-2")
            # 不传 matched_patterns 时默认 None
            self.assertIsNone(entry["matched_patterns"])
            # 不传 risk_level 时默认 "medium"
            self.assertEqual(entry["risk_level"], "medium")
            logger.close()

    def test_default_risk_level_is_medium(self):
        """不传 risk_level 时默认 "medium"。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            logger.log_guardrail_decision(
                layer="tool_result_sanitize",
                action="allow",
                reason="工具结果无敏感内容",
                session_id="sess-3",
            )
            recent = logger.get_recent(1)
            self.assertEqual(recent[0]["risk_level"], "medium")
            logger.close()

    def test_default_matched_patterns_is_none(self):
        """不传 matched_patterns 时默认 None，且能正确持久化到 JSONL。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            logger.log_guardrail_decision(
                layer="tool_result_sanitize",
                action="allow",
                reason="clean",
                session_id="sess-4",
            )
            logger.close()
            with open(log_path, "r", encoding="utf-8") as f:
                entry = json.loads(f.readline())
            # JSON 中应显式包含 "matched_patterns": null
            self.assertIn("matched_patterns", entry)
            self.assertIsNone(entry["matched_patterns"])


class TestMatchedPatternsParameter(unittest.TestCase):
    """``matched_patterns`` 参数测试。"""

    def test_matched_patterns_list_persisted(self):
        """matched_patterns 列表正确持久化到 JSONL 与内存缓冲。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            patterns = ["sql_injection", "xss", "path_traversal"]
            logger.log_guardrail_decision(
                layer="input_scan",
                action="deny",
                reason="命中多个注入模式",
                session_id="sess-5",
                matched_patterns=patterns,
                risk_level="high",
            )
            logger.close()

            # 验证 JSONL 文件
            with open(log_path, "r", encoding="utf-8") as f:
                entry = json.loads(f.readline())
            self.assertEqual(entry["matched_patterns"], patterns)

    def test_matched_patterns_empty_list(self):
        """matched_patterns 传空列表时持久化为空列表（区别于 None）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            logger.log_guardrail_decision(
                layer="input_scan",
                action="allow",
                reason="无命中",
                session_id="sess-6",
                matched_patterns=[],
            )
            recent = logger.get_recent(1)
            self.assertEqual(recent[0]["matched_patterns"], [])
            logger.close()


class TestMultipleGuardrailRecords(unittest.TestCase):
    """多条护栏记录写入测试。"""

    def test_multiple_guardrail_records_in_order(self):
        """连续写入多条护栏记录，get_recent 返回所有（倒序，最新在前）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            # 写入 3 条护栏记录，覆盖不同 layer / action
            logger.log_guardrail_decision(
                layer="input_scan",
                action="deny",
                reason="r1",
                session_id="sess-a",
                risk_level="high",
            )
            logger.log_guardrail_decision(
                layer="tool_result_sanitize",
                action="warn",
                reason="r2",
                session_id="sess-b",
                risk_level="medium",
            )
            logger.log_guardrail_decision(
                layer="output_filter",
                action="allow",
                reason="r3",
                session_id="sess-c",
                risk_level="low",
            )
            recent = logger.get_recent(10)
            self.assertEqual(len(recent), 3)
            # 倒序：最新（r3）在前
            self.assertEqual(recent[0]["reason"], "r3")
            self.assertEqual(recent[0]["layer"], "output_filter")
            self.assertEqual(recent[1]["reason"], "r2")
            self.assertEqual(recent[1]["layer"], "tool_result_sanitize")
            self.assertEqual(recent[2]["reason"], "r1")
            self.assertEqual(recent[2]["layer"], "input_scan")
            # 全部为护栏记录
            for entry in recent:
                self.assertEqual(entry["entry_type"], "guardrail")
            logger.close()

    def test_multiple_guardrail_records_jsonl(self):
        """多条护栏记录持久化到 JSONL，每行可 json.loads。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            for i in range(5):
                logger.log_guardrail_decision(
                    layer="input_scan",
                    action="allow" if i % 2 == 0 else "deny",
                    reason=f"reason-{i}",
                    session_id=f"sess-{i}",
                    matched_patterns=[f"pattern-{i}"] if i % 2 else None,
                    risk_level="low" if i % 2 == 0 else "high",
                )
            logger.close()

            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 5)
            for idx, line in enumerate(lines):
                entry = json.loads(line)
                self.assertEqual(entry["entry_type"], "guardrail")
                self.assertEqual(entry["layer"], "input_scan")
                self.assertEqual(entry["reason"], f"reason-{idx}")
                self.assertEqual(entry["session_id"], f"sess-{idx}")
                if idx % 2 == 0:
                    self.assertEqual(entry["action"], "allow")
                    self.assertIsNone(entry["matched_patterns"])
                    self.assertEqual(entry["risk_level"], "low")
                else:
                    self.assertEqual(entry["action"], "deny")
                    self.assertEqual(entry["matched_patterns"], [f"pattern-{idx}"])
                    self.assertEqual(entry["risk_level"], "high")


class TestBackwardCompatOldRecords(unittest.TestCase):
    """旧记录兼容性测试：无 ``entry_type`` 时默认 ``"tool_call"``。"""

    def test_old_tool_call_record_defaults_to_tool_call(self):
        """旧工具调用记录（无 entry_type）经 _normalize_entry 后默认 "tool_call"。

        通过直接往 buffer 注入旧格式记录（绕过 log_tool_call 的字段填充），
        模拟 Phase 8 时代的旧记录。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
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
            self.assertEqual(recent[0]["entry_type"], "tool_call")
            logger.close()

    def test_normalize_entry_fills_entry_type_default(self):
        """_normalize_entry 为旧记录填充 entry_type="tool_call"。"""
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
        self.assertEqual(normalized["entry_type"], "tool_call")
        # 原有字段保留
        self.assertEqual(normalized["tool_name"], "file_read")
        # 旧 Task 4.4 字段仍填充默认值
        self.assertEqual(normalized["decision_source"], "default_rule")
        self.assertIsNone(normalized["schedule_id"])
        self.assertIsNone(normalized["run_id"])

    def test_normalize_entry_preserves_guardrail_entry_type(self):
        """_normalize_entry 不覆盖已有的 entry_type="guardrail"。"""
        guardrail_entry = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "session_id": "sess-1",
            "layer": "input_scan",
            "action": "deny",
            "reason": "test",
            "matched_patterns": ["x"],
            "risk_level": "high",
            "entry_type": "guardrail",
        }
        normalized = AuditLogger._normalize_entry(guardrail_entry)
        self.assertEqual(normalized["entry_type"], "guardrail")
        # 护栏字段保留
        self.assertEqual(normalized["layer"], "input_scan")
        self.assertEqual(normalized["action"], "deny")

    def test_entry_type_of_helper_defaults(self):
        """_entry_type_of 对无 entry_type 的记录返回 "tool_call"。"""
        old_entry = {"tool_name": "x"}
        self.assertEqual(AuditLogger._entry_type_of(old_entry), "tool_call")
        new_guardrail = {"entry_type": "guardrail"}
        self.assertEqual(AuditLogger._entry_type_of(new_guardrail), "guardrail")
        new_tool = {"entry_type": "tool_call"}
        self.assertEqual(AuditLogger._entry_type_of(new_tool), "tool_call")


class TestGetRecentEntryTypeFilter(unittest.TestCase):
    """``get_recent(entry_type=...)`` 查询过滤测试。"""

    def setUp(self):
        """构造含工具调用与护栏决策混合记录的 AuditLogger。"""
        self.tmpdir = tempfile.mkdtemp(prefix="audit_guardrail_test_")
        log_path = os.path.join(self.tmpdir, "audit.jsonl")
        self.logger = AuditLogger(log_path=log_path, buffer_size=100)
        # 写入 2 条工具调用 + 3 条护栏决策（交替写入，便于验证过滤）
        # 1. 工具调用
        self.logger.log_tool_call(
            session_id="sess-1",
            tool_name="file_read",
            tool_input={"path": "/a"},
            result="r1",
            is_error=False,
            duration_ms=1.0,
        )
        # 2. 护栏决策（deny）
        self.logger.log_guardrail_decision(
            layer="input_scan",
            action="deny",
            reason="sql injection",
            session_id="sess-1",
            matched_patterns=["sql_injection"],
            risk_level="high",
        )
        # 3. 工具调用
        self.logger.log_tool_call(
            session_id="sess-2",
            tool_name="file_write",
            tool_input={"path": "/b"},
            result="r2",
            is_error=False,
            duration_ms=2.0,
        )
        # 4. 护栏决策（warn）
        self.logger.log_guardrail_decision(
            layer="output_filter",
            action="warn",
            reason="sensitive keyword",
            session_id="sess-2",
            risk_level="medium",
        )
        # 5. 护栏决策（allow）
        self.logger.log_guardrail_decision(
            layer="tool_result_sanitize",
            action="allow",
            reason="clean result",
            session_id="sess-2",
            risk_level="low",
        )

    def tearDown(self):
        self.logger.close()

    def test_no_filter_returns_all(self):
        """不传 entry_type 时返回全部 5 条记录（与旧行为一致）。"""
        recent = self.logger.get_recent(50)
        self.assertEqual(len(recent), 5)

    def test_filter_tool_call_only(self):
        """entry_type="tool_call" 仅返回 2 条工具调用记录。"""
        recent = self.logger.get_recent(50, entry_type="tool_call")
        self.assertEqual(len(recent), 2)
        for entry in recent:
            self.assertEqual(entry["entry_type"], "tool_call")
        # 倒序：最新（r2）在前
        self.assertEqual(recent[0]["result"], "r2")
        self.assertEqual(recent[1]["result"], "r1")

    def test_filter_guardrail_only(self):
        """entry_type="guardrail" 仅返回 3 条护栏决策记录。"""
        recent = self.logger.get_recent(50, entry_type="guardrail")
        self.assertEqual(len(recent), 3)
        for entry in recent:
            self.assertEqual(entry["entry_type"], "guardrail")
        # 倒序：最新（allow）在前
        self.assertEqual(recent[0]["action"], "allow")
        self.assertEqual(recent[0]["layer"], "tool_result_sanitize")
        self.assertEqual(recent[1]["action"], "warn")
        self.assertEqual(recent[1]["layer"], "output_filter")
        self.assertEqual(recent[2]["action"], "deny")
        self.assertEqual(recent[2]["layer"], "input_scan")

    def test_filter_with_limit(self):
        """entry_type="guardrail" + limit=2 仅返回最近 2 条护栏记录。"""
        recent = self.logger.get_recent(2, entry_type="guardrail")
        self.assertEqual(len(recent), 2)
        # 倒序：最新两条是 allow 与 warn
        self.assertEqual(recent[0]["action"], "allow")
        self.assertEqual(recent[1]["action"], "warn")

    def test_filter_nonexistent_entry_type_returns_empty(self):
        """不存在的 entry_type 返回空列表。"""
        recent = self.logger.get_recent(50, entry_type="nonexistent")
        self.assertEqual(recent, [])

    def test_filter_returns_deep_copy(self):
        """过滤查询返回深拷贝，修改不影响内部缓冲。"""
        recent = self.logger.get_recent(50, entry_type="guardrail")
        original = recent[0]["reason"]
        recent[0]["reason"] = "modified"
        # 再次查询，内部缓冲未被污染
        recent2 = self.logger.get_recent(50, entry_type="guardrail")
        self.assertEqual(recent2[0]["reason"], original)


class TestMixedRecordsRingBuffer(unittest.TestCase):
    """护栏记录与工具调用记录共用同一内存环形缓冲。"""

    def test_ring_buffer_contains_both_types(self):
        """buffer 同时含工具调用与护栏记录，溢出时按时间丢弃最旧。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            # buffer_size=3，写入 5 条混合记录，仅保留最近 3 条
            logger = AuditLogger(log_path=log_path, buffer_size=3)
            logger.log_tool_call(
                session_id="s1", tool_name="t1", tool_input={},
                result="r1", is_error=False, duration_ms=1.0,
            )
            logger.log_guardrail_decision(
                layer="input_scan", action="deny", reason="g1",
                session_id="s1",
            )
            logger.log_tool_call(
                session_id="s2", tool_name="t2", tool_input={},
                result="r2", is_error=False, duration_ms=2.0,
            )
            logger.log_guardrail_decision(
                layer="output_filter", action="warn", reason="g2",
                session_id="s2",
            )
            logger.log_tool_call(
                session_id="s3", tool_name="t3", tool_input={},
                result="r3", is_error=False, duration_ms=3.0,
            )
            # 倒序：最新（t3）在前，应仅保留 t3, g2, t2
            recent = logger.get_recent(10)
            self.assertEqual(len(recent), 3)
            self.assertEqual(recent[0]["result"], "r3")  # t3
            self.assertEqual(recent[0]["entry_type"], "tool_call")
            self.assertEqual(recent[1]["reason"], "g2")  # g2
            self.assertEqual(recent[1]["entry_type"], "guardrail")
            self.assertEqual(recent[2]["result"], "r2")  # t2
            self.assertEqual(recent[2]["entry_type"], "tool_call")
            logger.close()

    def test_ring_buffer_overflow_with_filter(self):
        """buffer 溢出后按 entry_type 过滤仍正确。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            # buffer_size=2，写入 2 工具调用 + 2 护栏（共 4 条）
            # 仅保留最近 2 条（最后 2 条护栏会被丢弃前 2 条工具调用后剩下...）
            # 实际：写入顺序 t1, g1, t2, g2 → 保留 [t2, g2]？不，deque maxlen=2
            # 写入 t1 → [t1]
            # 写入 g1 → [t1, g1]
            # 写入 t2 → [g1, t2]（t1 被挤出）
            # 写入 g2 → [t2, g2]（g1 被挤出）
            logger = AuditLogger(log_path=log_path, buffer_size=2)
            logger.log_tool_call(
                session_id="s1", tool_name="t1", tool_input={},
                result="r1", is_error=False, duration_ms=1.0,
            )
            logger.log_guardrail_decision(
                layer="input_scan", action="deny", reason="g1",
                session_id="s1",
            )
            logger.log_tool_call(
                session_id="s2", tool_name="t2", tool_input={},
                result="r2", is_error=False, duration_ms=2.0,
            )
            logger.log_guardrail_decision(
                layer="output_filter", action="warn", reason="g2",
                session_id="s2",
            )
            # 全部记录
            all_recent = logger.get_recent(10)
            self.assertEqual(len(all_recent), 2)
            # 仅工具调用：t2 还在缓冲中
            tool_calls = logger.get_recent(10, entry_type="tool_call")
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0]["result"], "r2")
            # 仅护栏：g2 还在缓冲中
            guardrails = logger.get_recent(10, entry_type="guardrail")
            self.assertEqual(len(guardrails), 1)
            self.assertEqual(guardrails[0]["reason"], "g2")
            logger.close()


class TestFileWriteFailureDegradation(unittest.TestCase):
    """护栏记录文件写入失败时的降级行为（与 log_tool_call 一致）。"""

    def test_log_guardrail_after_close_does_not_raise(self):
        """close 后再调用 log_guardrail_decision 不抛异常（仅 warning）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=10)
            logger.close()
            # close 后 self._file 为 None，写入会触发 AttributeError 被 try/except 捕获
            try:
                logger.log_guardrail_decision(
                    layer="input_scan",
                    action="deny",
                    reason="test",
                    session_id="sess-degrade",
                )
            except Exception as e:
                self.fail(
                    f"log_guardrail_decision should not raise after close, "
                    f"but got: {e!r}"
                )
            # 内存缓冲仍应记录该条（写入文件失败不影响 deque 追加）
            recent = logger.get_recent(1, entry_type="guardrail")
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["session_id"], "sess-degrade")


class TestJsonlFileSharedWithToolCall(unittest.TestCase):
    """护栏记录与工具调用记录写入同一 JSONL 文件。"""

    def test_mixed_records_in_same_jsonl_file(self):
        """工具调用与护栏记录交替写入同一 JSONL 文件，按写入顺序排列。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "audit.jsonl")
            logger = AuditLogger(log_path=log_path, buffer_size=100)
            # 1. 工具调用
            logger.log_tool_call(
                session_id="sess-1",
                tool_name="file_read",
                tool_input={"path": "/a"},
                result="r1",
                is_error=False,
                duration_ms=1.0,
            )
            # 2. 护栏决策
            logger.log_guardrail_decision(
                layer="input_scan",
                action="deny",
                reason="injection",
                session_id="sess-1",
                matched_patterns=["sql_injection"],
                risk_level="high",
            )
            # 3. 工具调用
            logger.log_tool_call(
                session_id="sess-2",
                tool_name="file_write",
                tool_input={"path": "/b"},
                result="r2",
                is_error=False,
                duration_ms=2.0,
            )
            logger.close()

            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 3)

            # 第 1 行：工具调用（无 entry_type 字段，向后兼容）
            entry1 = json.loads(lines[0])
            self.assertNotIn("entry_type", entry1)
            self.assertEqual(entry1["tool_name"], "file_read")

            # 第 2 行：护栏决策（含 entry_type="guardrail"）
            entry2 = json.loads(lines[1])
            self.assertEqual(entry2["entry_type"], "guardrail")
            self.assertEqual(entry2["layer"], "input_scan")
            self.assertEqual(entry2["action"], "deny")

            # 第 3 行：工具调用（无 entry_type 字段）
            entry3 = json.loads(lines[2])
            self.assertNotIn("entry_type", entry3)
            self.assertEqual(entry3["tool_name"], "file_write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
