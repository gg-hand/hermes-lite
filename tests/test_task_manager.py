"""TaskManager 只读解析单元测试（Phase 6 Task 2 重构）。

验证 ``src/tasks/task_manager.py`` 的 TaskManager 类在只读归档模式下
的 Markdown 解析、查询、依赖编排与进度统计能力。

本测试套件不依赖任何写接口（create/update/delete/clear 等已删除），
所有测试数据通过直接构造 Markdown 文件再由 TaskManager 读取的方式准备，
更接近真实归档查阅场景。

每个测试用例使用独立的临时 tasks.md 文件，避免相互污染。

运行方式:
    python -m unittest tests.test_task_manager -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.tasks.task_manager import TaskManager  # noqa: E402


class TestTaskManagerReadOnly(unittest.TestCase):
    """TaskManager 只读解析测试，每例使用独立临时文件。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmpdir, "tasks.md")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_markdown(self, content: str) -> str:
        """将 Markdown 文本写入临时 tasks.md，返回文件路径。

        用于直接构造归档文件，再由 TaskManager 只读解析。
        """
        Path(self.tasks_file).write_text(content, encoding="utf-8")
        return self.tasks_file

    def _new_manager(self) -> TaskManager:
        return TaskManager(file_path=self.tasks_file)

    # ------------------------------------------------------------------
    # list_tasks
    # ------------------------------------------------------------------
    def test_list_tasks_returns_all(self):
        """读取归档返回全部任务，字段与 Markdown 一致。"""
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: 任务一\n"
            "- 状态: pending\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T002: 任务二\n"
            "- 状态: completed\n"
            "- 依赖: T001\n"
            "- 创建: 2026-06-28T12:01:00.000000\n"
            "- 更新: 2026-06-28T12:05:00.000000\n"
            "- 结果: 完成结果\n"
        )
        tm = self._new_manager()
        tasks = tm.list_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["id"], "T001")
        self.assertEqual(tasks[0]["description"], "任务一")
        self.assertEqual(tasks[0]["status"], "pending")
        self.assertEqual(tasks[0]["depends_on"], [])
        self.assertEqual(tasks[0]["updated_at"], None)
        self.assertEqual(tasks[0]["result"], None)
        self.assertEqual(tasks[1]["id"], "T002")
        self.assertEqual(tasks[1]["status"], "completed")
        self.assertEqual(tasks[1]["depends_on"], ["T001"])
        self.assertEqual(tasks[1]["updated_at"], "2026-06-28T12:05:00.000000")
        self.assertEqual(tasks[1]["result"], "完成结果")

    def test_list_tasks_filter_by_status(self):
        """按 status 过滤。"""
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: t1\n"
            "- 状态: completed\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T002: t2\n"
            "- 状态: pending\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
        )
        tm = self._new_manager()
        completed = tm.list_tasks(status="completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["id"], "T001")
        pending = tm.list_tasks(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], "T002")

    # ------------------------------------------------------------------
    # get_next_ready
    # ------------------------------------------------------------------
    def test_get_next_ready_respects_dependency(self):
        """依赖未满足返回 None，满足后返回 ID。"""
        # 仅 T001 依赖尚未存在的 T002 → 依赖未满足，无就绪任务
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: 后续\n"
            "- 状态: pending\n"
            "- 依赖: T002\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
        )
        tm = self._new_manager()
        # T002 不存在于归档，T001 依赖未满足 → 无就绪任务
        self.assertIsNone(tm.get_next_ready())

        # 改写归档：补上已完成的 T002，T001 依赖满足后可执行
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: 后续\n"
            "- 状态: pending\n"
            "- 依赖: T002\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T002: 前置\n"
            "- 状态: completed\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
            "- 更新: 2026-06-28T12:05:00.000000\n"
            "- 结果: 前置完成\n"
        )
        tm2 = self._new_manager()
        self.assertEqual(tm2.get_next_ready(), "T001")

    def test_get_next_ready_skips_non_pending(self):
        """in_progress 任务不被选中。"""
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: 进行中\n"
            "- 状态: in_progress\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
        )
        tm = self._new_manager()
        self.assertIsNone(tm.get_next_ready())

    def test_get_next_ready_no_file_returns_none(self):
        """归档文件不存在时返回 None。"""
        tm = self._new_manager()
        self.assertIsNone(tm.get_next_ready())

    # ------------------------------------------------------------------
    # get_progress_summary
    # ------------------------------------------------------------------
    def test_get_progress_summary_format(self):
        """摘要字符串格式正确（含「📋 任务进度:」前缀）。"""
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: t1\n"
            "- 状态: completed\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T002: t2\n"
            "- 状态: pending\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
        )
        tm = self._new_manager()
        summary = tm.get_progress_summary()
        self.assertTrue(summary.startswith("📋 任务进度:"))
        self.assertIn("1/2 完成", summary)

    def test_get_progress_summary_counts_all_statuses(self):
        """摘要正确统计 completed/in_progress/failed/pending。"""
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: c1\n"
            "- 状态: completed\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T002: c2\n"
            "- 状态: completed\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T003: p1\n"
            "- 状态: pending\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T004: i1\n"
            "- 状态: in_progress\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T005: f1\n"
            "- 状态: failed\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
        )
        tm = self._new_manager()
        summary = tm.get_progress_summary()
        self.assertIn("2/5 完成", summary)
        self.assertIn("1 进行中", summary)
        self.assertIn("1 失败", summary)
        self.assertIn("1 待办", summary)

    def test_get_progress_summary_empty_returns_empty_string(self):
        """无任务时返回空字符串。"""
        tm = self._new_manager()
        self.assertEqual(tm.get_progress_summary(), "")

    # ------------------------------------------------------------------
    # _parse_markdown（直接调用解析逻辑）
    # ------------------------------------------------------------------
    def test_parse_markdown_full_fields(self):
        """解析含全部字段的任务。"""
        text = (
            "# 任务清单\n\n"
            "## T001: 调研 RAG\n"
            "- 状态: completed\n"
            "- 依赖: T002, T003\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
            "- 更新: 2026-06-28T12:05:00.000000\n"
            "- 结果: Milvus 是开源向量数据库\n"
        )
        tm = self._new_manager()
        tasks = tm._parse_markdown(text)
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t.id, "T001")
        self.assertEqual(t.description, "调研 RAG")
        self.assertEqual(t.status, "completed")
        self.assertEqual(t.depends_on, ["T002", "T003"])
        self.assertEqual(t.created_at, "2026-06-28T12:00:00.000000")
        self.assertEqual(t.updated_at, "2026-06-28T12:05:00.000000")
        self.assertEqual(t.result, "Milvus 是开源向量数据库")

    def test_parse_markdown_minimal_fields_defaults(self):
        """缺省字段降级为默认值（status=pending，依赖空，更新/结果 None）。"""
        text = (
            "## T001: 仅标题与创建\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
        )
        tm = self._new_manager()
        tasks = tm._parse_markdown(text)
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t.id, "T001")
        self.assertEqual(t.description, "仅标题与创建")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.depends_on, [])
        self.assertEqual(t.created_at, "2026-06-28T12:00:00.000000")
        self.assertIsNone(t.updated_at)
        self.assertIsNone(t.result)

    def test_parse_markdown_empty_status_defaults_pending(self):
        """状态行为空时默认 pending。"""
        text = (
            "## T001: 空状态\n"
            "- 状态:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
        )
        tm = self._new_manager()
        tasks = tm._parse_markdown(text)
        self.assertEqual(tasks[0].status, "pending")

    def test_parse_markdown_multiple_tasks(self):
        """解析多个任务，顺序与文件一致。"""
        text = (
            "## T001: a\n"
            "- 状态: pending\n"
            "- 创建: 2026-06-28T12:00:00.000000\n\n"
            "## T002: b\n"
            "- 状态: completed\n"
            "- 创建: 2026-06-28T12:01:00.000000\n"
        )
        tm = self._new_manager()
        tasks = tm._parse_markdown(text)
        self.assertEqual([t.id for t in tasks], ["T001", "T002"])
        self.assertEqual([t.description for t in tasks], ["a", "b"])

    def test_parse_markdown_empty_text_returns_empty(self):
        """空文本返回空列表。"""
        tm = self._new_manager()
        self.assertEqual(tm._parse_markdown(""), [])
        self.assertEqual(tm._parse_markdown("# 任务清单\n"), [])

    # ------------------------------------------------------------------
    # _load（文件读取）
    # ------------------------------------------------------------------
    def test_load_nonexistent_file_returns_empty(self):
        """归档文件不存在时 _load 返回空列表，不抛异常。"""
        tm = self._new_manager()
        self.assertEqual(tm._load(), [])

    # ------------------------------------------------------------------
    # 端到端：直接构造 markdown 文件验证只读解析能力
    # ------------------------------------------------------------------
    def test_read_only_parse_from_markdown_file(self):
        """端到端：直接构造归档 markdown 文件，验证只读解析全链路。

        覆盖 list_tasks / get_next_ready / get_progress_summary 三个
        只读接口均能正确从文件解析数据。
        """
        self._write_markdown(
            "# 任务清单\n\n"
            "## T001: 调研 RAG 方案\n"
            "- 状态: completed\n"
            "- 依赖:\n"
            "- 创建: 2026-06-28T12:00:00.000000\n"
            "- 更新: 2026-06-28T12:05:00.000000\n"
            "- 结果: Milvus 是开源向量数据库\n\n"
            "## T002: 整理对比文档\n"
            "- 状态: pending\n"
            "- 依赖: T001\n"
            "- 创建: 2026-06-28T12:01:00.000000\n\n"
            "## T003: 编写示例代码\n"
            "- 状态: pending\n"
            "- 依赖: T001, T002\n"
            "- 创建: 2026-06-28T12:02:00.000000\n"
        )
        tm = self._new_manager()

        # list_tasks 全量
        all_tasks = tm.list_tasks()
        self.assertEqual(len(all_tasks), 3)
        self.assertEqual([t["id"] for t in all_tasks], ["T001", "T002", "T003"])

        # list_tasks 过滤
        pending = tm.list_tasks(status="pending")
        self.assertEqual([t["id"] for t in pending], ["T002", "T003"])

        # get_next_ready：T001 已完成，T002 依赖 T001 满足 → 返回 T002
        self.assertEqual(tm.get_next_ready(), "T002")

        # get_progress_summary：1/3 完成，2 待办
        summary = tm.get_progress_summary()
        self.assertIn("1/3 完成", summary)
        self.assertIn("2 待办", summary)

        # file_path 属性保留
        self.assertEqual(tm.file_path, Path(self.tasks_file))


if __name__ == "__main__":
    unittest.main(verbosity=2)
