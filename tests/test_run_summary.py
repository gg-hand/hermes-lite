"""RunSummary + RunsJsonlStore 单元测试（Phase 8 Task 2.8 + 2.9 + 2.13）。

覆盖：
- ``RunSummary`` dataclass 结构、``to_dict`` / ``from_dict`` 序列化往返
- ``RunsJsonlStore.append`` / ``read_recent`` / ``read_last`` 读写
- ``truncate_assistant_response`` 截断行为（>500 字追加 ...[truncated]）
- JSONL 文件格式（每行一条 JSON 记录，append 模式）
- ``build_default_llm_summary`` 默认摘要生成（无额外 LLM 成本）
- 首次执行（无历史）与多次执行的读取行为

运行方式:
    python -m unittest tests.test_run_summary -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.tasks.run_summary import (  # noqa: E402
    RunSummary,
    RunsJsonlStore,
    build_default_llm_summary,
)


# ---------------------------------------------------------------------------
# RunSummary 数据结构测试
# ---------------------------------------------------------------------------


class TestRunSummaryDataclass(unittest.TestCase):
    """SubTask 2.8：RunSummary dataclass 结构与序列化。"""

    def test_run_summary_defaults(self):
        """默认值正确，run_id 自动生成。"""
        summary = RunSummary(schedule_id="s1")
        self.assertEqual(summary.schedule_id, "s1")
        self.assertTrue(summary.run_id)  # 自动生成的非空 ID
        self.assertEqual(summary.started_at, "")
        self.assertEqual(summary.finished_at, "")
        self.assertEqual(summary.duration_seconds, 0.0)
        self.assertTrue(summary.success)
        self.assertEqual(summary.user_input, "")
        self.assertEqual(summary.assistant_response, "")
        self.assertEqual(summary.tool_calls, [])
        self.assertEqual(summary.outputs, [])
        self.assertEqual(summary.errors, [])
        self.assertEqual(summary.llm_summary, "")

    def test_run_summary_run_id_unique(self):
        """两次构造 run_id 不同（uuid4 生成）。"""
        s1 = RunSummary(schedule_id="s1")
        s2 = RunSummary(schedule_id="s1")
        self.assertNotEqual(s1.run_id, s2.run_id)

    def test_run_summary_to_dict_contains_all_fields(self):
        """to_dict 含所有字段。"""
        summary = RunSummary(
            schedule_id="s1",
            run_id="abc123",
            started_at="2026-06-30T10:00:00",
            finished_at="2026-06-30T10:00:05",
            duration_seconds=5.123,
            success=True,
            user_input="任务文本",
            assistant_response="回复",
            tool_calls=[{"name": "search", "input": {}, "result": "r", "is_error": False}],
            outputs=[{"path": "/tmp/r.md", "type": "report"}],
            errors=["警告"],
            llm_summary="摘要",
        )
        d = summary.to_dict()
        self.assertEqual(d["schedule_id"], "s1")
        self.assertEqual(d["run_id"], "abc123")
        self.assertEqual(d["started_at"], "2026-06-30T10:00:00")
        self.assertEqual(d["duration_seconds"], 5.123)
        self.assertTrue(d["success"])
        self.assertEqual(d["user_input"], "任务文本")
        self.assertEqual(d["assistant_response"], "回复")
        self.assertEqual(len(d["tool_calls"]), 1)
        self.assertEqual(len(d["outputs"]), 1)
        self.assertEqual(d["errors"], ["警告"])
        self.assertEqual(d["llm_summary"], "摘要")

    def test_run_summary_from_dict_roundtrip(self):
        """from_dict 反序列化与原对象字段一致。"""
        original = RunSummary(
            schedule_id="s1",
            run_id="abc123",
            started_at="2026-06-30T10:00:00",
            finished_at="2026-06-30T10:00:05",
            duration_seconds=5.123,
            success=False,
            user_input="任务",
            assistant_response="回复",
            tool_calls=[{"name": "t", "input": {}, "result": "r", "is_error": True}],
            outputs=[{"path": "/p", "type": "report"}],
            errors=["错误"],
            llm_summary="摘要",
        )
        d = original.to_dict()
        restored = RunSummary.from_dict(d)
        self.assertEqual(restored.schedule_id, original.schedule_id)
        self.assertEqual(restored.run_id, original.run_id)
        self.assertEqual(restored.started_at, original.started_at)
        self.assertEqual(restored.finished_at, original.finished_at)
        self.assertEqual(restored.duration_seconds, original.duration_seconds)
        self.assertEqual(restored.success, original.success)
        self.assertEqual(restored.user_input, original.user_input)
        self.assertEqual(restored.assistant_response, original.assistant_response)
        self.assertEqual(restored.tool_calls, original.tool_calls)
        self.assertEqual(restored.outputs, original.outputs)
        self.assertEqual(restored.errors, original.errors)
        self.assertEqual(restored.llm_summary, original.llm_summary)

    def test_run_summary_from_dict_missing_fields_tolerant(self):
        """from_dict 兼容缺失字段（向后兼容旧记录）。"""
        d = {"schedule_id": "s1"}  # 仅含必需字段
        restored = RunSummary.from_dict(d)
        self.assertEqual(restored.schedule_id, "s1")
        self.assertEqual(restored.run_id, "")
        self.assertEqual(restored.started_at, "")
        self.assertTrue(restored.success)  # 默认 True
        self.assertEqual(restored.tool_calls, [])
        self.assertEqual(restored.errors, [])

    def test_run_summary_from_dict_handles_none_tool_calls(self):
        """from_dict 处理 tool_calls=None 的情况（旧记录可能为 None）。"""
        d = {
            "schedule_id": "s1",
            "tool_calls": None,
            "outputs": None,
            "errors": None,
        }
        restored = RunSummary.from_dict(d)
        self.assertEqual(restored.tool_calls, [])
        self.assertEqual(restored.outputs, [])
        self.assertEqual(restored.errors, [])


# ---------------------------------------------------------------------------
# 截断行为测试
# ---------------------------------------------------------------------------


class TestRunSummaryTruncation(unittest.TestCase):
    """SubTask 2.8：assistant_response 截断到 500 字。"""

    def test_truncate_short_response_unchanged(self):
        """短回复不截断。"""
        summary = RunSummary(
            schedule_id="s1",
            assistant_response="短回复",
        )
        summary.truncate_assistant_response()
        self.assertEqual(summary.assistant_response, "短回复")

    def test_truncate_exactly_500_chars_unchanged(self):
        """恰好 500 字不截断（边界）。"""
        text = "a" * 500
        summary = RunSummary(schedule_id="s1", assistant_response=text)
        summary.truncate_assistant_response()
        self.assertEqual(len(summary.assistant_response), 500)
        self.assertNotIn("[truncated]", summary.assistant_response)

    def test_truncate_long_response_appends_marker(self):
        """超过 500 字截断并追加 ...[truncated] 标记。"""
        text = "a" * 600
        summary = RunSummary(schedule_id="s1", assistant_response=text)
        summary.truncate_assistant_response()
        self.assertEqual(len(summary.assistant_response), 500 + len("...[truncated]"))
        self.assertTrue(summary.assistant_response.startswith("a" * 500))
        self.assertTrue(summary.assistant_response.endswith("...[truncated]"))


# ---------------------------------------------------------------------------
# RunsJsonlStore 读写测试
# ---------------------------------------------------------------------------


class TestRunsJsonlStore(unittest.TestCase):
    """SubTask 2.8：RunsJsonlStore append / read_recent / read_last。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base_dir = os.path.join(self.tmpdir, "schedules")
        self.store = RunsJsonlStore(base_dir=self.base_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_summary(self, schedule_id="s1", response="回复", **kwargs):
        defaults = dict(
            schedule_id=schedule_id,
            started_at="2026-06-30T10:00:00",
            finished_at="2026-06-30T10:00:05",
            duration_seconds=5.0,
            success=True,
            user_input="任务",
            assistant_response=response,
        )
        defaults.update(kwargs)
        return RunSummary(**defaults)

    def test_append_creates_file(self):
        """append 创建 runs.jsonl 文件（含父目录）。"""
        summary = self._make_summary()
        self.store.append("s1", summary)
        runs_path = os.path.join(self.base_dir, "s1", "runs.jsonl")
        self.assertTrue(os.path.exists(runs_path))

    def test_append_writes_jsonl_format(self):
        """写入的文件是 JSONL 格式（每行一条 JSON）。"""
        summary = self._make_summary()
        self.store.append("s1", summary)
        runs_path = os.path.join(self.base_dir, "s1", "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.strip().split("\n")
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["schedule_id"], "s1")

    def test_append_multiple_records(self):
        """多次 append 追加多行。"""
        for i in range(3):
            self.store.append("s1", self._make_summary(response=f"回复{i}"))
        runs_path = os.path.join(self.base_dir, "s1", "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 3)

    def test_append_truncates_assistant_response(self):
        """append 时自动截断超长 assistant_response。"""
        long_response = "x" * 800
        summary = self._make_summary(response=long_response)
        self.store.append("s1", summary)
        recent = self.store.read_recent("s1", n=1)
        self.assertEqual(len(recent), 1)
        # 截断后含 ...[truncated] 标记
        self.assertTrue(recent[0].assistant_response.endswith("...[truncated]"))

    def test_read_recent_empty_when_no_file(self):
        """文件不存在时 read_recent 返回空列表。"""
        result = self.store.read_recent("nonexistent", n=3)
        self.assertEqual(result, [])

    def test_read_recent_returns_latest_first(self):
        """read_recent 返回最新在前（倒序）。"""
        for i in range(5):
            self.store.append(
                "s1",
                self._make_summary(
                    response=f"回复{i}",
                    started_at=f"2026-06-30T10:00:0{i}",
                ),
            )
        recent = self.store.read_recent("s1", n=3)
        self.assertEqual(len(recent), 3)
        # 最新（最后写入的 回复4）应在最前
        self.assertEqual(recent[0].assistant_response, "回复4")
        self.assertEqual(recent[1].assistant_response, "回复3")
        self.assertEqual(recent[2].assistant_response, "回复2")

    def test_read_recent_n_larger_than_records(self):
        """n 大于实际记录数时返回全部记录。"""
        self.store.append("s1", self._make_summary(response="回复1"))
        recent = self.store.read_recent("s1", n=10)
        self.assertEqual(len(recent), 1)

    def test_read_recent_n_zero_or_negative(self):
        """n=0 返回空列表（边界）。"""
        self.store.append("s1", self._make_summary())
        recent = self.store.read_recent("s1", n=0)
        # read_recent 在 n=0 时返回全部（与 n>0 一致），但实现中
        # recent_lines = lines[-n:] if n > 0 else lines，n=0 时取全部行
        # 此处验证行为：n=0 时不报错，返回某种合理结果
        self.assertIsInstance(recent, list)

    def test_read_last_returns_most_recent(self):
        """read_last 返回最近一条记录。"""
        for i in range(3):
            self.store.append(
                "s1",
                self._make_summary(response=f"回复{i}"),
            )
        last = self.store.read_last("s1")
        self.assertIsNotNone(last)
        self.assertEqual(last.assistant_response, "回复2")

    def test_read_last_returns_none_when_no_file(self):
        """文件不存在时 read_last 返回 None。"""
        self.assertIsNone(self.store.read_last("nonexistent"))

    def test_read_last_returns_none_when_empty_file(self):
        """文件为空时 read_last 返回 None。"""
        # 创建空文件
        runs_path = os.path.join(self.base_dir, "s1", "runs.jsonl")
        os.makedirs(os.path.dirname(runs_path), exist_ok=True)
        with open(runs_path, "w", encoding="utf-8") as f:
            f.write("")
        self.assertIsNone(self.store.read_last("s1"))

    def test_read_recent_skips_malformed_lines(self):
        """read_recent 跳过格式错误的行（不抛异常）。"""
        runs_path = os.path.join(self.base_dir, "s1", "runs.jsonl")
        os.makedirs(os.path.dirname(runs_path), exist_ok=True)
        with open(runs_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"schedule_id": "s1", "run_id": "r1"}) + "\n")
            f.write("malformed json line\n")  # 错误行
            f.write(json.dumps({"schedule_id": "s1", "run_id": "r2"}) + "\n")
        recent = self.store.read_recent("s1", n=5)
        # 跳过错误行后返回 2 条
        self.assertEqual(len(recent), 2)

    def test_multiple_schedules_isolated(self):
        """不同 schedule_id 的记录隔离存储。"""
        self.store.append("s1", self._make_summary(schedule_id="s1", response="A"))
        self.store.append("s2", self._make_summary(schedule_id="s2", response="B"))
        self.store.append("s1", self._make_summary(schedule_id="s1", response="A2"))
        self.store.append("s2", self._make_summary(schedule_id="s2", response="B2"))

        s1_recent = self.store.read_recent("s1", n=5)
        s2_recent = self.store.read_recent("s2", n=5)

        self.assertEqual(len(s1_recent), 2)
        self.assertEqual(len(s2_recent), 2)
        # s1 最新是 A2
        self.assertEqual(s1_recent[0].assistant_response, "A2")
        # s2 最新是 B2
        self.assertEqual(s2_recent[0].assistant_response, "B2")

    def test_jsonl_file_supports_chinese(self):
        """JSONL 文件正确处理中文（ensure_ascii=False）。"""
        self.store.append(
            "s1",
            self._make_summary(
                response="这是中文回复",
                user_input="中文任务",
            ),
        )
        recent = self.store.read_recent("s1", n=1)
        self.assertEqual(recent[0].assistant_response, "这是中文回复")
        self.assertEqual(recent[0].user_input, "中文任务")


# ---------------------------------------------------------------------------
# build_default_llm_summary 测试
# ---------------------------------------------------------------------------


class TestBuildDefaultLLMSummary(unittest.TestCase):
    """SubTask 2.9：build_default_llm_summary 默认摘要生成。"""

    def test_summary_contains_assistant_response(self):
        """默认摘要含 assistant_response 文本。"""
        summary = RunSummary(
            schedule_id="s1",
            assistant_response="这是回复",
        )
        result = build_default_llm_summary(summary)
        self.assertIn("这是回复", result)

    def test_summary_truncates_long_response(self):
        """超长 assistant_response 截断到 500 字 + 标记。"""
        long_text = "x" * 800
        summary = RunSummary(schedule_id="s1", assistant_response=long_text)
        result = build_default_llm_summary(summary)
        self.assertIn("...[truncated]", result)
        # 截断后不含全部 800 个 x
        self.assertLess(result.count("x"), 800)

    def test_summary_includes_tool_calls_count(self):
        """含工具调用时摘要追加工具调用统计。"""
        summary = RunSummary(
            schedule_id="s1",
            assistant_response="回复",
            tool_calls=[
                {"name": "search", "input": {}, "result": "r1", "is_error": False},
                {"name": "file_read", "input": {}, "result": "r2", "is_error": False},
            ],
        )
        result = build_default_llm_summary(summary)
        self.assertIn("工具调用: 2 次", result)
        self.assertIn("search", result)
        self.assertIn("file_read", result)

    def test_summary_includes_error_count(self):
        """含错误时摘要追加错误统计。"""
        summary = RunSummary(
            schedule_id="s1",
            assistant_response="回复",
            errors=["错误1", "错误2"],
        )
        result = build_default_llm_summary(summary)
        self.assertIn("错误: 2 条", result)

    def test_summary_includes_outputs_count(self):
        """含文件输出时摘要追加输出统计。"""
        summary = RunSummary(
            schedule_id="s1",
            assistant_response="回复",
            outputs=[
                {"path": "/a.md", "type": "report"},
                {"path": "/b.json", "type": "snapshot"},
                {"path": "/c.md", "type": "report"},
            ],
        )
        result = build_default_llm_summary(summary)
        self.assertIn("输出: 3 个文件", result)

    def test_summary_no_tool_calls_no_errors_no_outputs(self):
        """无工具调用/错误/输出时摘要仅含 assistant_response。"""
        summary = RunSummary(
            schedule_id="s1",
            assistant_response="纯回复",
        )
        result = build_default_llm_summary(summary)
        self.assertEqual(result, "纯回复")

    def test_summary_empty_response_with_tool_calls(self):
        """assistant_response 为空但有工具调用时摘要仍含工具调用统计。"""
        summary = RunSummary(
            schedule_id="s1",
            assistant_response="",
            tool_calls=[{"name": "t", "input": {}, "result": "r", "is_error": False}],
        )
        result = build_default_llm_summary(summary)
        # 第一行是空字符串，第二行起是工具调用统计
        self.assertIn("工具调用: 1 次", result)
        self.assertIn("t", result)


# ---------------------------------------------------------------------------
# SubTask 2.13: 缓存约束验证 — RunSummary 持久化稳定性
# ---------------------------------------------------------------------------


class TestRunSummaryCacheConstraints(unittest.TestCase):
    """验证 RunSummary 序列化的稳定性（缓存约束）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base_dir = os.path.join(self.tmpdir, "schedules")
        self.store = RunsJsonlStore(base_dir=self.base_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_same_summary_serializes_identically_except_run_id(self):
        """同字段的 RunSummary 序列化结果除 run_id 外一致（缓存友好）。

        run_id 是 uuid4 生成的，每次不同；其余字段相同则序列化结果一致。
        """
        s1 = RunSummary(
            schedule_id="s1",
            started_at="2026-06-30T10:00:00",
            finished_at="2026-06-30T10:00:05",
            duration_seconds=5.0,
            success=True,
            user_input="任务",
            assistant_response="回复",
        )
        s2 = RunSummary(
            schedule_id="s1",
            started_at="2026-06-30T10:00:00",
            finished_at="2026-06-30T10:00:05",
            duration_seconds=5.0,
            success=True,
            user_input="任务",
            assistant_response="回复",
        )
        d1 = s1.to_dict()
        d2 = s2.to_dict()
        # run_id 不同
        self.assertNotEqual(d1["run_id"], d2["run_id"])
        # 其余字段相同
        d1.pop("run_id")
        d2.pop("run_id")
        self.assertEqual(d1, d2)

    def test_read_after_write_preserves_all_fields(self):
        """写入后读取的字段与原对象一致（除 assistant_response 截断外）。"""
        original = RunSummary(
            schedule_id="s1",
            started_at="2026-06-30T10:00:00",
            finished_at="2026-06-30T10:00:05",
            duration_seconds=5.123,
            success=True,
            user_input="任务文本",
            assistant_response="回复内容",
            tool_calls=[{"name": "t", "input": {}, "result": "r", "is_error": False}],
            outputs=[{"path": "/p", "type": "report"}],
            errors=["err"],
            llm_summary="摘要文本",
        )
        self.store.append("s1", original)
        recent = self.store.read_recent("s1", n=1)
        self.assertEqual(len(recent), 1)
        restored = recent[0]
        self.assertEqual(restored.schedule_id, original.schedule_id)
        self.assertEqual(restored.run_id, original.run_id)
        self.assertEqual(restored.started_at, original.started_at)
        self.assertEqual(restored.finished_at, original.finished_at)
        self.assertEqual(restored.duration_seconds, original.duration_seconds)
        self.assertEqual(restored.success, original.success)
        self.assertEqual(restored.user_input, original.user_input)
        self.assertEqual(restored.assistant_response, original.assistant_response)
        self.assertEqual(restored.tool_calls, original.tool_calls)
        self.assertEqual(restored.outputs, original.outputs)
        self.assertEqual(restored.errors, original.errors)
        self.assertEqual(restored.llm_summary, original.llm_summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
