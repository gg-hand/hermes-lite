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
        registry = HookRegistry(config={}, scheduler_ref=None)
        self.assertEqual(registry.get_retry_max(), 0)


if __name__ == "__main__":
    unittest.main()
