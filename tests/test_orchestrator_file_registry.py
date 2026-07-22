"""Orchestrator 装配 file_registry 集成测试（T9）。

验证 Orchestrator.__init__ 正确装配 FileOperationRegistry：
1. self.file_registry 属性存在且为 FileOperationRegistry 实例
2. PolicyEngine 接收到 file_registry（通过 _check_write_file 决策验证）
3. register_builtin_tools 透传 file_registry（通过 write_file 工具执行后验证记录）

测试策略：
- 通过 unittest.mock.patch 替换 LLMClient，避免真实 LLM 调用
- 使用项目根目录的 config.yaml 作为配置源
- 直接调用工具 handler 验证 file_registry 已注入

运行方式:
    python -m unittest tests.test_orchestrator_file_registry -v
    python tests/test_orchestrator_file_registry.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()


class TestOrchestratorFileRegistryAssembly(unittest.TestCase):
    """验证 Orchestrator 装配 file_registry 的端到端集成。"""

    @classmethod
    def setUpClass(cls):
        """一次性 mock LLMClient 类，避免 Orchestrator.__init__ 触发真实 LLM 初始化。

        Orchestrator.__init__ 会创建 LLMClient(config=self.config, metrics_collector=...)，
        我们 patch LLMClient 为一个简单的 stub，避免加载 DeepSeek API key 等。
        """
        # 延迟导入，确保 install_mocks() 已执行
        from teage_liu.orchestrator import Orchestrator  # noqa: E402

        cls.Orchestrator = Orchestrator

    def _make_orchestrator(self, config_path: str):
        """构造一个 Orchestrator 实例，mock 掉 LLMClient 避免真实 API 调用。

        参数:
            config_path: config.yaml 路径。

        返回:
            Orchestrator 实例（LLMClient 被 mock 替换）。
        """
        # patch LLMClient 类本身，返回一个简单 stub 实例
        with patch("teage_liu.orchestrator.LLMClient") as mock_llm_cls:
            mock_llm_cls.return_value = object()  # 简单 stub
            return self.Orchestrator(config_path=config_path)

    def test_file_registry_attribute_exists(self):
        """Orchestrator 实例化后 self.file_registry 属性存在。"""
        orch = self._make_orchestrator("config.yaml")
        self.assertTrue(hasattr(orch, "file_registry"))

    def test_file_registry_is_instance(self):
        """self.file_registry 为 FileOperationRegistry 实例（非 None）。"""
        from teage_liu.agent.file_registry import FileOperationRegistry

        orch = self._make_orchestrator("config.yaml")
        self.assertIsNotNone(orch.file_registry)
        self.assertIsInstance(orch.file_registry, FileOperationRegistry)

    def test_policy_engine_receives_file_registry(self):
        """PolicyEngine 接收到 file_registry（通过 write_file 决策验证）。

        验证逻辑：write_file 对工作空间内新建文件应返回 allow（v2 参数感知路径），
        而非 confirm（v1 DEFAULT_RULES 路径或跨工作空间路径）。
        """
        orch = self._make_orchestrator("config.yaml")
        self.assertIsNotNone(orch.policy_engine)

        # 在工作空间内新建临时文件，验证走 v2 新建文件 allow 路径
        with tempfile.NamedTemporaryFile(
            dir=_PROJECT_ROOT, prefix="policy_test_", suffix=".py", delete=False
        ) as f:
            new_file = f.name
        try:
            os.unlink(new_file)  # 先删除，确认文件不存在
            decision = orch.policy_engine.check(
                "file_write",
                {"path": new_file},
                session_id="test-integration-session",
            )
            # v2 参数感知 + 工作空间内：新建文件 → allow / low
            self.assertEqual(decision.action, "allow")
            self.assertEqual(decision.reason, "新建文件")
            self.assertEqual(decision.risk_level, "low")
        finally:
            try:
                os.unlink(new_file)
            except OSError:
                pass

    def test_builtin_tools_receives_file_registry(self):
        """register_builtin_tools 透传 file_registry（通过 write_file 工具执行验证）。

        验证逻辑：调用 ToolRegistry.execute_tool("file_write", ...) 后，
        file_registry 中应记录到 created 集合（v2 closure 版本生效）。
        """
        orch = self._make_orchestrator("config.yaml")
        self.assertIsNotNone(orch.tool_registry)
        self.assertIsNotNone(orch.file_registry)

        # 模拟设置 session_id（与 chat / chat_stream 入口一致）
        orch._current_session_id = "test-builtin-tools-session"

        with tempfile.TemporaryDirectory() as tmpdir:
            new_file = os.path.join(tmpdir, "v2_write_test.py")
            # 调用 write_file 工具（v2 closure 版本）
            result = orch.tool_registry.execute_tool(
                "file_write", {"path": new_file, "content": "hello"}
            )
            self.assertIn("已写入文件", result)
            # 验证 file_registry 记录到 created 集合
            self.assertTrue(
                orch.file_registry.is_created(
                    "test-builtin-tools-session", new_file
                )
            )

    def test_delete_file_tool_registered(self):
        """delete_file 工具已注册到 ToolRegistry（Core Tier）。"""
        orch = self._make_orchestrator("config.yaml")
        self.assertIsNotNone(orch.tool_registry)
        tools_schema = orch.tool_registry.get_tools_schema()
        tool_names = {t["name"] for t in tools_schema}
        self.assertIn("file_delete", tool_names)

    def test_delete_file_v2_removes_from_registry(self):
        """delete_file v2 版本执行后从 file_registry 移除。"""
        orch = self._make_orchestrator("config.yaml")
        orch._current_session_id = "test-delete-v2-session"

        with tempfile.TemporaryDirectory() as tmpdir:
            # 先创建文件并记录到 created 集合
            new_file = os.path.join(tmpdir, "to_delete.py")
            orch.tool_registry.execute_tool(
                "file_write", {"path": new_file, "content": "x"}
            )
            self.assertTrue(
                orch.file_registry.is_created(
                    "test-delete-v2-session", new_file
                )
            )
            # 执行 delete_file（v2 closure 版本）
            result = orch.tool_registry.execute_tool(
                "file_delete", {"path": new_file}
            )
            self.assertIn("已删除文件", result)
            # 验证从 created 集合移除
            self.assertFalse(
                orch.file_registry.is_created(
                    "test-delete-v2-session", new_file
                )
            )
            # 文件确实被删除
            self.assertFalse(os.path.exists(new_file))


if __name__ == "__main__":
    unittest.main(verbosity=2)
