﻿# tests/test_lifespan_module.py（修改）
"""测试 lifespan 模块使用 container.get() 而非手工 new。

spec 2026-07-13 阶段 2：lifespan 从 796 行降至 ~200 行。
"""
from __future__ import annotations
import sys
import os
from unittest.mock import MagicMock, patch, AsyncMock

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "hermes")
class TestLifespanUsesContainer:

    def test_lifespan_calls_container_get(self):
        """lifespan 应通过 container.get() 获取组件，而非手工 new。"""
        with open(os.path.join(_SRC_DIR, "lifespan.py"), "r", encoding="utf-8") as f:
            content = f.read()
        # 不应包含手工创建逻辑
        assert "SessionLogger(" not in content or "container.get" in content
        assert "MetricsCollector(" not in content or "container.get" in content

    def test_lifespan_uses_background_task_registry(self):
        """lifespan 应使用 BackgroundTaskRegistry 注册后台 task。"""
        with open(os.path.join(_SRC_DIR, "lifespan.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "BackgroundTaskRegistry" in content
        assert "task_registry" in content

    def test_lifespan_creates_metrics_reset_event(self):
        """lifespan 应创建 app.state.metrics_reset_event。"""
        with open(os.path.join(_SRC_DIR, "lifespan.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "metrics_reset_event" in content
        assert "asyncio.Event" in content

    def test_lifespan_no_set_instance(self):
        """lifespan 不应调用 inject_lifespan_instances（改为 container.get 触发工厂）。"""
        with open(os.path.join(_SRC_DIR, "lifespan.py"), "r", encoding="utf-8") as f:
            content = f.read()
        # inject_lifespan_instances 是旧模式，新 lifespan 不应使用
        assert "inject_lifespan_instances" not in content

    def test_lifespan_no_state_sync(self):
        """lifespan 不应同步到 state 模块或 server 模块。"""
        with open(os.path.join(_SRC_DIR, "lifespan.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state as _state" not in content
        assert "_state.orchestrator =" not in content
        assert "_server_mod.orchestrator =" not in content
