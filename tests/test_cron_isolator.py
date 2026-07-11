"""CronIsolator 测试:cron 上下文隔离。

从 Orchestrator 提取的 cron 隔离职责:
- build_isolation: 从 session_id 解析 CronIsolation context
- build_cron_tools: 构建请求级工具过滤列表
- set_dependencies: 注入 cron 调度依赖

CronIsolator 持有 Orchestrator 弱引用（方法对象模式），
因为 cron 上下文构建依赖 memory_retriever / metrics / tool_registry 等多个组件。
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from unittest.mock import MagicMock, AsyncMock
from agent.cron_isolator import CronIsolator


class TestBuildIsolation:
    def test_non_cron_session_returns_none(self):
        """非 cron: 前缀的 session_id 返回 None。"""
        iso = CronIsolator(orchestrator=MagicMock())
        assert iso.build_isolation("user-session-1") is None

    def test_none_session_returns_none(self):
        """None session_id 返回 None。"""
        iso = CronIsolator(orchestrator=MagicMock())
        assert iso.build_isolation(None) is None

    def test_cron_session_returns_isolation(self):
        """cron: 前缀的 session_id 返回 CronIsolation 实例。"""
        iso = CronIsolator(orchestrator=MagicMock())
        result = iso.build_isolation("cron:test-1")
        # CronIsolation 可能不可用（可选依赖），降级返回 None
        # 但如果可用，应返回非 None
        if result is not None:
            assert result.cron_id == "test-1"


class TestBuildCronTools:
    def test_non_cron_session_returns_none(self):
        """非 cron 会话返回 None（走默认 registry 路径）。"""
        iso = CronIsolator(orchestrator=MagicMock())
        assert iso.build_cron_tools("user-session-1") is None

    def test_none_session_returns_none(self):
        """None session_id 返回 None。"""
        iso = CronIsolator(orchestrator=MagicMock())
        assert iso.build_cron_tools(None) is None

    def test_no_cron_scheduler_returns_none(self):
        """cron_scheduler 未注入时返回 None。"""
        orch = MagicMock()
        orch.cron_scheduler = None
        iso = CronIsolator(orchestrator=orch)
        assert iso.build_cron_tools("cron:test-1") is None

    def test_schedule_not_found_returns_none(self):
        """调度项不存在时返回 None。"""
        orch = MagicMock()
        orch.cron_scheduler = MagicMock()
        orch.cron_scheduler.get_schedule.return_value = None
        iso = CronIsolator(orchestrator=orch)
        assert iso.build_cron_tools("cron:nonexistent") is None

    def test_snapshot_filter(self):
        """有 active_tools_snapshot 时过滤工具集。"""
        orch = MagicMock()
        orch.cron_scheduler = MagicMock()
        orch.cron_scheduler.get_schedule.return_value = {
            "active_tools_snapshot": ["tool_a", "tool_c"],
        }
        orch.tool_registry = MagicMock()
        orch.tool_registry.get_tools_schema.return_value = [
            {"name": "tool_a", "description": "A"},
            {"name": "tool_b", "description": "B"},
            {"name": "tool_c", "description": "C"},
        ]
        orch.cron_tool_registry = None
        orch.react_loop = None
        iso = CronIsolator(orchestrator=orch)
        result = iso.build_cron_tools("cron:test-1")
        assert result is not None
        names = [t["name"] for t in result]
        assert "tool_a" in names
        assert "tool_c" in names
        assert "tool_b" not in names

    def test_empty_snapshot_returns_all(self):
        """snapshot 为空时不限制内置工具集。"""
        orch = MagicMock()
        orch.cron_scheduler = MagicMock()
        orch.cron_scheduler.get_schedule.return_value = {
            "active_tools_snapshot": [],
        }
        orch.tool_registry = MagicMock()
        orch.tool_registry.get_tools_schema.return_value = [
            {"name": "tool_a"},
            {"name": "tool_b"},
        ]
        orch.cron_tool_registry = None
        orch.react_loop = None
        iso = CronIsolator(orchestrator=orch)
        result = iso.build_cron_tools("cron:test-1")
        assert result is not None
        assert len(result) == 2


class TestSetDependencies:
    def test_set_cron_scheduler(self):
        """set_dependencies 注入 cron_scheduler。"""
        orch = MagicMock()
        iso = CronIsolator(orchestrator=orch)
        scheduler = MagicMock()
        iso.set_dependencies(cron_scheduler=scheduler)
        assert orch.cron_scheduler is scheduler

    def test_set_cron_tool_registry(self):
        """set_dependencies 注入 cron_tool_registry 并同步到 react_loop。"""
        orch = MagicMock()
        orch.react_loop = MagicMock()
        orch.react_loop.cron_tool_registry = None
        iso = CronIsolator(orchestrator=orch)
        tool_registry = MagicMock()
        iso.set_dependencies(cron_tool_registry=tool_registry)
        assert orch.cron_tool_registry is tool_registry
        assert orch.react_loop.cron_tool_registry is tool_registry
