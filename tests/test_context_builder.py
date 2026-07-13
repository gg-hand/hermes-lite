"""ContextBuilder 测试:环境信息 + TODO 格式化 + 续接消息。

从 Orchestrator 提取的上下文构建辅助方法:
- build_environment: 构建运行环境信息段（OS/Shell/Python/CWD）
- format_todo: 将 TodoList dict 格式化为"## 当前计划进度"段
- has_unfinished_steps: 检查 TodoList 是否有未完成步骤
- build_continuation_message: 构造自动续接消息
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes"))

import pytest
from hermes.agent.context_builder import ContextBuilder


class TestBuildEnvironment:
    def test_returns_non_empty_str(self):
        cb = ContextBuilder()
        env = cb.build_environment()
        assert isinstance(env, str)
        assert len(env) > 0

    def test_contains_header(self):
        cb = ContextBuilder()
        env = cb.build_environment()
        assert "## 运行环境" in env

    def test_contains_os_info(self):
        cb = ContextBuilder()
        env = cb.build_environment()
        assert "操作系统" in env

    def test_contains_shell_info(self):
        cb = ContextBuilder()
        env = cb.build_environment()
        assert "Shell" in env

    def test_contains_python_info(self):
        cb = ContextBuilder()
        env = cb.build_environment()
        assert "Python" in env


class TestFormatTodo:
    def test_none_returns_empty(self):
        cb = ContextBuilder()
        assert cb.format_todo(None) == ""

    def test_empty_dict_returns_empty(self):
        cb = ContextBuilder()
        assert cb.format_todo({}) == ""

    def test_no_steps_returns_empty(self):
        cb = ContextBuilder()
        assert cb.format_todo({"goal": "test", "steps": []}) == ""

    def test_with_steps(self):
        cb = ContextBuilder()
        todo = {
            "goal": "完成任务",
            "steps": [
                {"id": 1, "content": "步骤A", "status": "completed"},
                {"id": 2, "content": "步骤B", "status": "pending"},
            ],
        }
        result = cb.format_todo(todo)
        assert "## 当前计划进度" in result
        assert "完成任务" in result
        assert "[x] 步骤A" in result
        assert "[ ] 步骤B" in result
        assert "1/2" in result

    def test_failed_status_shown_as_unchecked(self):
        """failed 状态用 [ ] 表示未完成。"""
        cb = ContextBuilder()
        todo = {
            "goal": "test",
            "steps": [
                {"content": "失败步骤", "status": "failed"},
            ],
        }
        result = cb.format_todo(todo)
        assert "[ ] 失败步骤" in result
        assert "[x]" not in result


class TestHasUnfinishedSteps:
    def test_none_returns_false(self):
        cb = ContextBuilder()
        assert cb.has_unfinished_steps(None) is False

    def test_empty_steps_returns_false(self):
        cb = ContextBuilder()
        assert cb.has_unfinished_steps({"steps": []}) is False

    def test_all_completed_returns_false(self):
        cb = ContextBuilder()
        todo = {"steps": [{"status": "completed"}, {"status": "completed"}]}
        assert cb.has_unfinished_steps(todo) is False

    def test_pending_returns_true(self):
        cb = ContextBuilder()
        todo = {"steps": [{"status": "completed"}, {"status": "pending"}]}
        assert cb.has_unfinished_steps(todo) is True

    def test_failed_returns_true(self):
        cb = ContextBuilder()
        todo = {"steps": [{"status": "failed"}]}
        assert cb.has_unfinished_steps(todo) is True


class TestBuildContinuationMessage:
    def test_none_returns_generic(self):
        cb = ContextBuilder()
        msg = cb.build_continuation_message(None)
        assert "上一轮已达循环上限" in msg
        assert "请继续完成剩余步骤" in msg

    def test_no_steps_returns_generic(self):
        cb = ContextBuilder()
        msg = cb.build_continuation_message({"goal": "test", "steps": []})
        assert "上一轮已达循环上限" in msg

    def test_with_steps_includes_progress(self):
        cb = ContextBuilder()
        todo = {
            "goal": "完成任务",
            "steps": [
                {"content": "步骤A", "status": "completed"},
                {"content": "步骤B", "status": "pending"},
            ],
        }
        msg = cb.build_continuation_message(todo)
        assert "完成任务" in msg
        assert "1/2" in msg
        assert "步骤B" in msg
        assert "步骤A" not in msg  # 已完成的不在未完成列表
