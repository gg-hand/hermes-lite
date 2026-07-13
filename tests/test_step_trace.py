"""Task 3.4: step_trace.py 数据模型测试。

覆盖：
- 序列化 to_dict / from_dict roundtrip
- from_dict 对缺失字段容错
- 默认值（status=pending / attempts=1）
- tool_calls 与 files 列表
"""

from __future__ import annotations

import unittest
from datetime import datetime

from hermes.tasks.workflow.step_trace import StepTrace, ALLOWED_STEP_STATUSES


class TestStepTraceSerialization(unittest.TestCase):
    def test_to_dict_from_dict_roundtrip(self):
        t = StepTrace(
            step_id="s1",
            step_name="测试 step",
            step_type="llm",
            started_at="2026-07-05T10:00:00",
            finished_at="2026-07-05T10:00:05",
            duration_ms=5000,
            attempts=2,
            status="success",
            outputs={"summary": "ok"},
            tool_calls=[{"name": "file_read", "input": {}, "result": "data", "is_error": False}],
            files=[{"path": "/tmp/report.md", "type": "report"}],
        )
        d = t.to_dict()
        t2 = StepTrace.from_dict(d)
        self.assertEqual(t2.step_id, "s1")
        self.assertEqual(t2.step_name, "测试 step")
        self.assertEqual(t2.step_type, "llm")
        self.assertEqual(t2.duration_ms, 5000)
        self.assertEqual(t2.attempts, 2)
        self.assertEqual(t2.status, "success")
        self.assertEqual(t2.outputs, {"summary": "ok"})
        self.assertEqual(len(t2.tool_calls), 1)
        self.assertEqual(t2.tool_calls[0]["name"], "file_read")
        self.assertEqual(len(t2.files), 1)
        self.assertEqual(t2.files[0]["path"], "/tmp/report.md")


class TestStepTraceFromDictTolerance(unittest.TestCase):
    def test_from_dict_missing_fields_uses_defaults(self):
        """旧记录缺字段时返回默认值。"""
        t = StepTrace.from_dict({"step_id": "old_step"})
        self.assertEqual(t.step_id, "old_step")
        self.assertEqual(t.step_name, "")
        self.assertEqual(t.step_type, "")
        self.assertEqual(t.duration_ms, 0)
        self.assertEqual(t.attempts, 1)
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.error_class, "")
        self.assertEqual(t.outputs, {})
        self.assertEqual(t.tool_calls, [])
        self.assertEqual(t.files, [])

    def test_from_dict_none_returns_default(self):
        t = StepTrace.from_dict(None)
        self.assertEqual(t.step_id, "")
        self.assertEqual(t.status, "pending")

    def test_from_dict_empty_dict_returns_default(self):
        t = StepTrace.from_dict({})
        self.assertEqual(t.step_id, "")
        self.assertEqual(t.status, "pending")


class TestStepTraceDefaults(unittest.TestCase):
    def test_default_status_is_pending(self):
        t = StepTrace()
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.attempts, 1)
        self.assertEqual(t.duration_ms, 0)
        self.assertEqual(t.tool_calls, [])
        self.assertEqual(t.files, [])

    def test_allowed_statuses_contains_terminal_states(self):
        for s in ("success", "failed", "skipped", "fallback", "timeout"):
            self.assertIn(s, ALLOWED_STEP_STATUSES)


class TestStepTraceLifecycle(unittest.TestCase):
    def test_mark_started_sets_running(self):
        t = StepTrace(step_id="s1")
        t.mark_started()
        self.assertEqual(t.status, "running")
        self.assertIsNotNone(t.started_at)

    def test_mark_finished_computes_duration(self):
        t = StepTrace(step_id="s1")
        start = datetime(2026, 7, 5, 10, 0, 0)
        end = datetime(2026, 7, 5, 10, 0, 5)  # 5 秒后
        t.mark_started(start)
        t.mark_finished("success", end)
        self.assertEqual(t.status, "success")
        self.assertEqual(t.duration_ms, 5000)

    def test_mark_finished_with_error_info(self):
        t = StepTrace(step_id="s1")
        t.mark_started()
        t.mark_finished(
            "failed",
            error_class="transient",
            error_message="LLM 限流",
        )
        self.assertEqual(t.status, "failed")
        self.assertEqual(t.error_class, "transient")
        self.assertEqual(t.error_message, "LLM 限流")

    def test_add_tool_call_appends(self):
        t = StepTrace(step_id="s1")
        t.add_tool_call({"name": "file_read", "result": "data"})
        t.add_tool_call({"name": "web_fetch", "result": "html"})
        self.assertEqual(len(t.tool_calls), 2)
        self.assertEqual(t.tool_calls[1]["name"], "web_fetch")

    def test_is_terminal(self):
        t = StepTrace(step_id="s1", status="running")
        self.assertFalse(t.is_terminal())
        t.status = "success"
        self.assertTrue(t.is_terminal())
        t.status = "failed"
        self.assertTrue(t.is_terminal())


if __name__ == "__main__":
    unittest.main()
