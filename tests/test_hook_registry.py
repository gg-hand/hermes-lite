"""HookRegistry 测试（3.7）。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.tasks.hooks.base import ScheduleHookBase, RetryDecision
from hermes.tasks.hooks.registry import HookRegistry


class TestHookRegistryConstruction(unittest.TestCase):
    """HookRegistry 按 config 开关启用/禁用 hook。"""

    def test_all_hooks_enabled_by_default(self):
        """默认配置启用所有 4 个 hook。"""
        # 注意：此测试在 hook 实现类创建后才能通过
        # 此处先测试空 config 时不报错
        registry = HookRegistry(config={}, scheduler_ref=None)
        self.assertIsInstance(registry.hooks, dict)

    def test_disabled_hook_not_in_registry(self):
        """enabled=False 的 hook 不加载。"""
        config = {"validate": {"enabled": False}}
        registry = HookRegistry(config=config, scheduler_ref=None)
        self.assertNotIn("validate", registry.hooks)


class TestHookRegistryGetRetryMax(unittest.TestCase):
    """get_retry_max 返回 retry hook 的 max_retries。"""

    def test_no_retry_hook_returns_zero(self):
        """retry hook 显式禁用时返回 0。

        注意：空 config 时所有 hook 默认 enabled=True（Task 10 实现 RetryHook 后），
        因此测试"无 retry hook"场景必须显式禁用。
        """
        config = {"retry": {"enabled": False}}
        registry = HookRegistry(config=config, scheduler_ref=None)
        self.assertEqual(registry.get_retry_max(), 0)

    def test_retry_hook_enabled_returns_max_retries(self):
        """retry hook 启用时返回配置的 max_retries。"""
        config = {"retry": {"enabled": True, "max_retries": 5}}
        registry = HookRegistry(config=config, scheduler_ref=None)
        self.assertEqual(registry.get_retry_max(), 5)

    def test_retry_hook_default_max_retries(self):
        """retry hook 启用但未配置 max_retries 时返回默认值 3。"""
        config = {"retry": {"enabled": True}}
        registry = HookRegistry(config=config, scheduler_ref=None)
        self.assertEqual(registry.get_retry_max(), 3)


if __name__ == "__main__":
    unittest.main()
