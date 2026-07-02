"""ContextManager 单元测试 — 验证 build_prompt 结构与缓存区字节级稳定性。

运行方式：
    python -m unittest tests.test_context_manager -v
    python tests/test_context_manager.py
"""

from __future__ import annotations

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.llm.prompts import SYSTEM_PROMPT  # noqa: E402
from src.memory.context_manager import ContextManager  # noqa: E402


# ---------------------------------------------------------------------------
# Mock 依赖类（参考 context_manager.py __main__ 块的实现）
# ---------------------------------------------------------------------------

class MockToolRegistry:
    """稳定返回相同 schema 的 mock ToolRegistry。"""

    def get_tools_schema(self):
        return [
            {
                "name": "test_tool",
                "description": "测试工具",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]


class UnstableMockToolRegistry:
    """每次调用返回不同 schema 的 mock ToolRegistry，用于验证缓存不稳定检测。"""

    def __init__(self):
        self._call_count = 0

    def get_tools_schema(self):
        self._call_count += 1
        return [
            {
                "name": f"tool_{self._call_count}",
                "description": f"测试工具 {self._call_count}",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]


class MockMemoryMdManager:
    def read(self):
        return "# 用户画像\n\n## 基本信息\n- 用户是测试用户"


class MockMemoryRetriever:
    def get_injection_text(self, user_input):
        return "## 相关记忆\n1. 测试记忆 (相关度: 0.90)"


class MockEmptyRetriever:
    def get_injection_text(self, user_input):
        return ""


class MockHistoryBuffer:
    def get_history(self, session_id):
        return [
            {"role": "user", "content": "历史用户", "timestamp": "2024-01-01"},
            {"role": "assistant", "content": "历史助手", "timestamp": "2024-01-01"},
        ]


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

class TestContextManagerBuildPrompt(unittest.TestCase):
    """验证 build_prompt 的结构正确性。"""

    def test_build_prompt_basic_no_deps(self):
        """无依赖时 build_prompt 的基本结构。"""
        cm = ContextManager()
        prompt = cm.build_prompt("test-session", "你好")

        self.assertIn(SYSTEM_PROMPT, prompt["system"])
        self.assertEqual(prompt["messages"][-1], {"role": "user", "content": "你好"})
        self.assertEqual(prompt["tools"], [])

    def test_build_prompt_with_deps(self):
        """带依赖时 system + messages + tools 结构。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=MockMemoryRetriever(),
            history_buffer=MockHistoryBuffer(),
        )
        prompt = cm.build_prompt("test-session", "当前问题")

        # system 包含 SYSTEM_PROMPT + 用户画像 + 分隔符
        self.assertIn(SYSTEM_PROMPT, prompt["system"])
        self.assertIn("用户是测试用户", prompt["system"])
        self.assertIn("---", prompt["system"])

        # tools 是完整列表
        self.assertEqual(len(prompt["tools"]), 1)
        self.assertEqual(prompt["tools"][0]["name"], "test_tool")

        # messages[0] 是检索记忆注入
        self.assertEqual(prompt["messages"][0]["role"], "user")
        self.assertIn("相关记忆", prompt["messages"][0]["content"])

        # 历史在中间
        self.assertEqual(prompt["messages"][1]["content"], "历史用户")
        self.assertEqual(prompt["messages"][2]["content"], "历史助手")

        # 最后一条是当前用户输入
        self.assertEqual(prompt["messages"][-1], {"role": "user", "content": "当前问题"})

        # 历史消息仅含 role + content 字段（符合 Anthropic API 规范）
        self.assertNotIn("timestamp", prompt["messages"][1])
        self.assertEqual(set(prompt["messages"][1].keys()), {"role", "content"})

    def test_empty_retriever_skips_injection(self):
        """检索记忆为空时跳过注入，messages[0] 为历史第一条。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_retriever=MockEmptyRetriever(),
            history_buffer=MockHistoryBuffer(),
        )
        prompt = cm.build_prompt("test-session", "当前问题")

        self.assertEqual(prompt["messages"][0]["content"], "历史用户")
        self.assertEqual(prompt["messages"][-1], {"role": "user", "content": "当前问题"})


class TestContextManagerCacheStability(unittest.TestCase):
    """验证缓存命中区与失效点。"""

    def test_cache_stable_prefix(self):
        """get_cache_stable_prefix 等于 build_prompt 的 system 字段。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
        )
        prompt = cm.build_prompt("test-session", "你好")
        prefix = cm.get_cache_stable_prefix()

        self.assertEqual(prefix, prompt["system"])
        self.assertIn("用户是测试用户", prefix)

    def test_cache_break_point(self):
        """get_cache_break_point 固定返回 0。"""
        cm = ContextManager()
        self.assertEqual(cm.get_cache_break_point(), 0)

    def test_verify_cache_stability_stable(self):
        """稳定 ToolRegistry 下 verify_cache_stability 返回 True。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=MockMemoryRetriever(),
            history_buffer=MockHistoryBuffer(),
        )
        self.assertTrue(cm.verify_cache_stability("test-session", "你好"))

    def test_verify_cache_stability_no_deps(self):
        """无依赖时 verify_cache_stability 返回 True（空值天然稳定）。"""
        cm = ContextManager()
        self.assertTrue(cm.verify_cache_stability("test-session", "你好"))

    def test_verify_cache_stability_unstable(self):
        """动态 ToolRegistry 下 verify_cache_stability 返回 False。"""
        cm = ContextManager(
            tool_registry=UnstableMockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
        )
        self.assertFalse(cm.verify_cache_stability("test-session", "你好"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
