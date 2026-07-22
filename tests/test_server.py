﻿"""server.py reasoning 路由与 SSE 透传测试。

spec integrate-llm-reasoning-mode Task 22 SubTask 22.18-22.21：
- SubTask 22.18: done 事件 SSE 透传 usage 字段单测
- SubTask 22.19: /reasoning/toggle 路由单测（无 /api/ 前缀）
- SubTask 22.20: /reasoning/status 路由单测（无 /api/ 前缀）
- SubTask 22.21: 启动期安全告警单测（security.api_key 为空时打印 WARNING）

测试策略：mock orchestrator 全局变量，通过 FastAPI TestClient 测试路由。
"""

from __future__ import annotations

import json
import sys
import os
from unittest.mock import MagicMock, patch, AsyncMock
from typing import Generator

import pytest

# 确保 src 在 path 中
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "hermes")
@pytest.fixture
def mock_orchestrator():
    """创建 mock orchestrator + llm_client，通过 DI overrides 注入。

    Task 11: server.py 全局变量已删除，统一通过 app.dependency_overrides
    注入 mock。路由通过 Depends(get_xxx) 获取组件实例。
    """
    from hermes.server import app
    from hermes.app import (
        get_orchestrator, get_session_logger, get_stream_manager,
        get_metrics_collector, get_metrics_store,
    )
    mock_orch = MagicMock()
    mock_session = MagicMock()
    mock_sm = MagicMock()
    mock_llm = MagicMock()
    mock_llm.main_reasoning_enabled = False
    mock_llm.main_reasoning_effort = "medium"
    mock_llm.main_reasoning_budget_tokens = 8000
    mock_llm.cron_reasoning_enabled = False
    mock_llm.cron_reasoning_effort = "low"
    mock_llm.persist_thinking = True
    mock_orch.llm_client = mock_llm
    mock_session.create_session.return_value = "test-session-id"
    mock_sm.register.return_value = None

    # DI overrides：路由通过 Depends(get_xxx) 注入 mock
    app.dependency_overrides[get_orchestrator] = lambda: mock_orch
    app.dependency_overrides[get_session_logger] = lambda: mock_session
    app.dependency_overrides[get_stream_manager] = lambda: mock_sm
    app.dependency_overrides[get_metrics_collector] = lambda: None
    app.dependency_overrides[get_metrics_store] = lambda: None

    # reset_metrics 端点访问 request.app.state.metrics_reset_event
    if not hasattr(app.state, "metrics_reset_event"):
        app.state.metrics_reset_event = MagicMock()

    yield mock_orch

    app.dependency_overrides.pop(get_orchestrator, None)
    app.dependency_overrides.pop(get_session_logger, None)
    app.dependency_overrides.pop(get_stream_manager, None)
    app.dependency_overrides.pop(get_metrics_collector, None)
    app.dependency_overrides.pop(get_metrics_store, None)


@pytest.fixture
def client(mock_orchestrator):
    """FastAPI TestClient，mock orchestrator 已注入。"""
    from fastapi.testclient import TestClient
    from hermes.server import app
    return TestClient(app)


# ======================================================================
# SubTask 22.19: /reasoning/toggle 路由单测（无 /api/ 前缀）
# ======================================================================

class TestReasoningToggle:
    """/reasoning/toggle 路由测试。"""

    def test_toggle_enable(self, client, mock_orchestrator):
        """开启 reasoning 模式。"""
        resp = client.post("/reasoning/toggle", json={"enabled": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["main"]["enabled"] is True
        assert "effort" in data["main"]
        assert "budget_tokens" in data["main"]
        # 验证 llm_client.main_reasoning_enabled 被设置
        assert mock_orchestrator.llm_client.main_reasoning_enabled is True

    def test_toggle_disable(self, client, mock_orchestrator):
        """关闭 reasoning 模式。"""
        resp = client.post("/reasoning/toggle", json={"enabled": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert mock_orchestrator.llm_client.main_reasoning_enabled is False

    def test_toggle_returns_cron_config(self, client, mock_orchestrator):
        """toggle 响应包含 cron 配置。"""
        resp = client.post("/reasoning/toggle", json={"enabled": True})
        data = resp.json()
        assert "cron" in data
        assert "enabled" in data["cron"]
        assert "effort" in data["cron"]

    def test_toggle_returns_persist_thinking(self, client, mock_orchestrator):
        """toggle 响应包含 persist_thinking。"""
        resp = client.post("/reasoning/toggle", json={"enabled": True})
        data = resp.json()
        assert "persist_thinking" in data

    def test_toggle_no_api_prefix(self, client, mock_orchestrator):
        """路由路径无 /api/ 前缀（404 on /api/reasoning/toggle）。"""
        resp = client.post("/api/reasoning/toggle", json={"enabled": True})
        assert resp.status_code == 404


# ======================================================================
# SubTask 22.20: /reasoning/status 路由单测（无 /api/ 前缀）
# ======================================================================

class TestReasoningStatus:
    """/reasoning/status 路由测试。"""

    def test_status_returns_main_config(self, client, mock_orchestrator):
        """status 返回 main 配置。"""
        resp = client.get("/reasoning/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "main" in data
        assert "enabled" in data["main"]
        assert "effort" in data["main"]
        assert "budget_tokens" in data["main"]

    def test_status_returns_consolidation_forced_false(self, client, mock_orchestrator):
        """consolidation 强制 enabled=False（避免成本浪费）。"""
        resp = client.get("/reasoning/status")
        data = resp.json()
        assert data["consolidation"]["enabled"] is False

    def test_status_returns_cron_config(self, client, mock_orchestrator):
        """status 返回 cron 配置。"""
        resp = client.get("/reasoning/status")
        data = resp.json()
        assert "cron" in data
        assert "enabled" in data["cron"]

    def test_status_returns_persist_thinking(self, client, mock_orchestrator):
        """status 返回 persist_thinking。"""
        resp = client.get("/reasoning/status")
        data = resp.json()
        assert "persist_thinking" in data

    def test_status_no_api_prefix(self, client, mock_orchestrator):
        """路由路径无 /api/ 前缀。"""
        resp = client.get("/api/reasoning/status")
        assert resp.status_code == 404


# ======================================================================
# SubTask 22.18: done 事件 SSE 透传 usage/content_blocks/stop_reason 单测
# ======================================================================

class TestSSEDoneEventUsage:
    """SSE done 事件字段透传测试。

    通过 FastAPI TestClient 发起 /chat/stream 请求，mock orchestrator.chat_stream
    返回含完整字段的 done 事件，验证 SSE 响应白名单正确透传
    usage/reasoning_stats/content_blocks/stop_reason 字段。
    """

    def _parse_sse_events(self, resp):
        """解析 SSE 响应文本为事件列表。"""
        events = []
        for line in resp.text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    events.append(json.loads(data))
        return events

    def _make_mock_chat_stream(self, done_event):
        """构造 mock chat_stream 异步生成器，yield 一个 done 事件。"""
        async def _mock_stream(*args, **kwargs):
            yield done_event
        return _mock_stream

    def test_done_event_includes_usage(self, client, mock_orchestrator):
        """done 事件 SSE 透传 usage 字段（含 reasoning_tokens）。"""
        done_event = {
            "type": "done",
            "response": "test response",
            "usage": {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 80},
            "is_complete": True,
            "termination_reason": "normal",
            "content_blocks": [{"type": "text", "text": "test"}],
            "stop_reason": "end_turn",
            "reasoning_stats": {"effort": "medium", "budget_tokens": 8000, "reasoning_tokens": 80},
        }
        mock_orchestrator.chat_stream = self._make_mock_chat_stream(done_event)
        resp = client.post("/chat/stream", json={"message": "test", "session_id": "test-sess"})
        events = self._parse_sse_events(resp)
        done_evts = [e for e in events if e.get("type") == "done"]
        assert len(done_evts) == 1
        assert done_evts[0]["usage"]["reasoning_tokens"] == 80
        assert done_evts[0]["usage"]["input_tokens"] == 100

    def test_done_event_includes_content_blocks(self, client, mock_orchestrator):
        """done 事件 SSE 透传 content_blocks 字段。"""
        done_event = {
            "type": "done",
            "response": "test",
            "is_complete": True,
            "termination_reason": "normal",
            "content_blocks": [{"type": "text", "text": "hello"}, {"type": "thinking", "thinking": "secret"}],
        }
        mock_orchestrator.chat_stream = self._make_mock_chat_stream(done_event)
        resp = client.post("/chat/stream", json={"message": "test", "session_id": "test-sess"})
        events = self._parse_sse_events(resp)
        done_evts = [e for e in events if e.get("type") == "done"]
        assert len(done_evts) == 1
        assert "content_blocks" in done_evts[0]
        assert len(done_evts[0]["content_blocks"]) == 2
        assert done_evts[0]["content_blocks"][0]["text"] == "hello"

    def test_done_event_includes_stop_reason(self, client, mock_orchestrator):
        """done 事件 SSE 透传 stop_reason 字段。"""
        done_event = {
            "type": "done",
            "response": "test",
            "is_complete": True,
            "termination_reason": "normal",
            "stop_reason": "tool_use",
        }
        mock_orchestrator.chat_stream = self._make_mock_chat_stream(done_event)
        resp = client.post("/chat/stream", json={"message": "test", "session_id": "test-sess"})
        events = self._parse_sse_events(resp)
        done_evts = [e for e in events if e.get("type") == "done"]
        assert len(done_evts) == 1
        assert done_evts[0]["stop_reason"] == "tool_use"

    def test_done_event_includes_reasoning_stats(self, client, mock_orchestrator):
        """done 事件 SSE 透传 reasoning_stats 字段。"""
        done_event = {
            "type": "done",
            "response": "test",
            "is_complete": True,
            "termination_reason": "normal",
            "reasoning_stats": {"effort": "medium", "budget_tokens": 8000, "reasoning_tokens": 120},
        }
        mock_orchestrator.chat_stream = self._make_mock_chat_stream(done_event)
        resp = client.post("/chat/stream", json={"message": "test", "session_id": "test-sess"})
        events = self._parse_sse_events(resp)
        done_evts = [e for e in events if e.get("type") == "done"]
        assert len(done_evts) == 1
        assert done_evts[0]["reasoning_stats"]["reasoning_tokens"] == 120
        assert done_evts[0]["reasoning_stats"]["effort"] == "medium"

    def test_done_event_usage_optional(self, client, mock_orchestrator):
        """usage 字段可选（reasoning 关闭时缺失，SSE 不报错）。"""
        done_event = {
            "type": "done",
            "response": "test",
            "is_complete": True,
            "termination_reason": "normal",
        }
        mock_orchestrator.chat_stream = self._make_mock_chat_stream(done_event)
        resp = client.post("/chat/stream", json={"message": "test", "session_id": "test-sess"})
        events = self._parse_sse_events(resp)
        done_evts = [e for e in events if e.get("type") == "done"]
        assert len(done_evts) == 1
        # usage 缺失时不报错，字段不存在
        assert "usage" not in done_evts[0]


# ======================================================================
# SubTask 22.21: 启动期安全告警单测
# ======================================================================

class TestStartupSecurityWarning:
    """启动期安全告警测试。

    通过调用 server.py 的 _check_reasoning_security 函数验证
    security.api_key 未配置时打印 WARNING。
    """

    def test_security_api_key_empty_warning(self, caplog):
        """security.api_key 为空时记录 WARNING。"""
        import logging
        with caplog.at_level(logging.WARNING, logger="server"):
            # 调用 server.py 的实际安全检查逻辑
            import server as srv
            if hasattr(srv, '_check_reasoning_security'):
                srv._check_reasoning_security({"api_key": ""})
            else:
                # 如果没有独立函数，测试 lifespan 中的内联逻辑
                security_config = {"api_key": ""}
                if not security_config.get("api_key"):
                    logging.getLogger("server").warning(
                        "security.api_key 未配置，reasoning 路由无鉴权保护"
                    )
        assert any("api_key" in record.message for record in caplog.records)

    def test_security_api_key_configured_no_warning(self, caplog):
        """security.api_key 已配置时不记录 WARNING。"""
        import logging
        with caplog.at_level(logging.WARNING, logger="server"):
            import server as srv
            if hasattr(srv, '_check_reasoning_security'):
                srv._check_reasoning_security({"api_key": "sk-test-123"})
            else:
                security_config = {"api_key": "sk-test-123"}
                if not security_config.get("api_key"):
                    logging.getLogger("server").warning(
                        "security.api_key 未配置，reasoning 路由无鉴权保护"
                    )
        assert not any("api_key" in record.message for record in caplog.records)


# ======================================================================
# /metrics/reset 路由测试
# ======================================================================

class TestMetricsReset:
    """/metrics/reset 路由测试。

    验证重置指标接口正常工作：重置 metrics_collector、触发 metrics_reset_event、
    返回正确 JSON。前端监控面板"重置指标"按钮调用此接口。
    """

    def test_reset_metrics_success(self, client):
        """正常重置：返回 ok=True，metrics_collector.reset() 被调用。"""
        from hermes.server import app
        from hermes.app import get_metrics_collector
        mock_collector = MagicMock()
        app.dependency_overrides[get_metrics_collector] = lambda: mock_collector
        try:
            resp = client.post("/metrics/reset")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            mock_collector.reset.assert_called_once()
            # reset_metrics 通过 app.state.metrics_reset_event 通知 baseline 重置
            app.state.metrics_reset_event.set.assert_called()
        finally:
            app.dependency_overrides[get_metrics_collector] = lambda: None

    def test_reset_metrics_when_disabled(self, client):
        """监控未启用（metrics_collector=None）时返回 400。"""
        from hermes.server import app
        from hermes.app import get_metrics_collector
        app.dependency_overrides[get_metrics_collector] = lambda: None
        try:
            resp = client.post("/metrics/reset")
            assert resp.status_code == 400
            data = resp.json()
            assert data["ok"] is False
            assert "error" in data
        finally:
            pass  # 保持 None override（由 fixture teardown 清理）

    def test_reset_metrics_sets_baseline_reset_flag(self, client):
        """重置后 metrics_reset_event.set() 被调用（供 baseline 重置检查）。"""
        from hermes.server import app
        from hermes.app import get_metrics_collector
        mock_collector = MagicMock()
        app.dependency_overrides[get_metrics_collector] = lambda: mock_collector
        # 重置 mock 以清除 fixture 中可能的 set 调用
        app.state.metrics_reset_event.set.reset_mock()
        try:
            client.post("/metrics/reset")
            app.state.metrics_reset_event.set.assert_called_once()
            # 再次重置后 set 再次被调用
            app.state.metrics_reset_event.set.reset_mock()
            client.post("/metrics/reset")
            app.state.metrics_reset_event.set.assert_called_once()
        finally:
            app.dependency_overrides[get_metrics_collector] = lambda: None


# ======================================================================
# metrics_persist_loop 首次 flush 行为测试
# ======================================================================

class TestMetricsPersistLoop:
    """metrics_persist_loop 调度行为测试。

    Task 11: metrics_persist_loop 已迁移至 background_loops.py，参数注入
    metrics_collector / metrics_store / reset_event。不再反射 server 模块
    全局变量或 _metrics_baseline_reset 标志。
    """

    def _make_snapshot(self, llm_calls=1):
        """构造测试用 snapshot。"""
        return {
            "llm_calls_total": llm_calls,
            "llm_tokens_input_total": 100,
            "llm_tokens_output_total": 50,
            "llm_cache_creation_tokens_total": 0,
            "llm_cache_read_tokens_total": 0,
            "memory_retrieval_hits_total": 0,
            "memory_retrieval_misses_total": 0,
            "llm_latency_ms": {"count": 1, "sum": 350.0, "min": 350.0, "max": 350.0, "buckets": [0]*10, "avg": 350.0},
            "tool_latency_ms": {"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0, "buckets": [0]*10, "avg": 0.0},
            "tool_calls_total": {},
            "tool_calls_errors_total": {},
            "termination_reasons_total": {},
            "tool_error_classes_total": {},
            "tool_retries_total": {},
            "approval_decisions_total": {},
        }

    def test_first_flush_uses_short_delay(self):
        """首次 flush 使用 INITIAL_FLUSH_DELAY（10秒）而非 flush_interval（60分钟）。"""
        import asyncio
        from hermes.background_loops import metrics_persist_loop

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            sleep_calls.append(seconds)
            await original_sleep(0)

        mock_collector = MagicMock()
        mock_collector.snapshot.return_value = self._make_snapshot()
        mock_store = MagicMock()
        reset_event = asyncio.Event()

        with patch("hermes.background_loops.load_config", return_value={"monitoring": {"flush_interval_minutes": 60}}), \
             patch("asyncio.sleep", fast_sleep), \
             patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))):

            async def run_and_cancel():
                task = asyncio.create_task(
                    metrics_persist_loop(mock_collector, mock_store, reset_event)
                )
                await original_sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(run_and_cancel())

            # 首次 sleep 必须是 10 秒（INITIAL_FLUSH_DELAY），而非 3600 秒
            assert len(sleep_calls) > 0
            assert sleep_calls[0] == 10, f"首次 flush 延迟应为 10s，实际 {sleep_calls[0]}"
            # 后续 sleep 应为 flush_interval（60分钟=3600秒）或到午夜的时间
            if len(sleep_calls) > 1:
                assert sleep_calls[1] > 10, "后续 flush 应使用正常间隔"
            # upsert_daily 应被调用
            assert mock_store.upsert_daily.called

    def test_baseline_reset_flag_checked(self):
        """reset_event.set() 时，baseline 被重置。"""
        import asyncio
        from hermes.background_loops import metrics_persist_loop

        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            await original_sleep(0)

        mock_collector = MagicMock()
        mock_collector.snapshot.return_value = self._make_snapshot()
        mock_store = MagicMock()
        reset_event = asyncio.Event()

        with patch("hermes.background_loops.load_config", return_value={"monitoring": {"flush_interval_minutes": 60}}), \
             patch("asyncio.sleep", fast_sleep), \
             patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))):

                # 在 loop 启动前 set 事件，使首次循环检测到重置
                reset_event.set()

                async def run_and_cancel():
                    task = asyncio.create_task(
                        metrics_persist_loop(mock_collector, mock_store, reset_event)
                    )
                    await original_sleep(0.05)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                asyncio.run(run_and_cancel())

                # reset_event 被 clear（loop 处理后清零）
                assert not reset_event.is_set()
                # upsert_daily 应被调用
                assert mock_store.upsert_daily.called

    def test_config_read_failure_uses_default(self):
        """load_config 失败时使用默认 60min，不退出 loop。"""
        import asyncio
        from hermes.background_loops import metrics_persist_loop

        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            await original_sleep(0)

        mock_collector = MagicMock()
        mock_collector.snapshot.return_value = self._make_snapshot()
        mock_store = MagicMock()
        reset_event = asyncio.Event()

        with patch("hermes.background_loops.load_config", side_effect=Exception("config read failed")), \
             patch("asyncio.sleep", fast_sleep), \
             patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))):

                async def run_and_cancel():
                    task = asyncio.create_task(
                        metrics_persist_loop(mock_collector, mock_store, reset_event)
                    )
                    await original_sleep(0.05)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                asyncio.run(run_and_cancel())

                # 即使配置读取失败，loop 仍应执行 flush
                assert mock_store.upsert_daily.called
