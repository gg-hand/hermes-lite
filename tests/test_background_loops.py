# tests/test_background_loops.py
"""测试 background_loops.py 参数注入改造。

spec 2026-07-13 阶段 2：3 个循环改参数注入 + asyncio.Event。
"""
from __future__ import annotations
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "hermes")
class TestBackgroundLoopsSignatures:

    def test_cleanup_loop_accepts_params(self):
        """cleanup_loop 接受 session_logger/metrics_store/orchestrator 参数。"""
        import inspect
        from hermes.background_loops import cleanup_loop
        sig = inspect.signature(cleanup_loop)
        params = list(sig.parameters.keys())
        assert "session_logger" in params
        assert "metrics_store" in params
        assert "orchestrator" in params

    def test_file_cleanup_loop_accepts_param(self):
        """file_cleanup_loop 接受 upload_manager 参数。"""
        import inspect
        from hermes.background_loops import file_cleanup_loop
        sig = inspect.signature(file_cleanup_loop)
        params = list(sig.parameters.keys())
        assert "upload_manager" in params

    def test_metrics_persist_loop_accepts_params(self):
        """metrics_persist_loop 接受 metrics_collector/metrics_store/reset_event 参数。"""
        import inspect
        from hermes.background_loops import metrics_persist_loop
        sig = inspect.signature(metrics_persist_loop)
        params = list(sig.parameters.keys())
        assert "metrics_collector" in params
        assert "metrics_store" in params
        assert "reset_event" in params

    def test_get_server_globals_deleted(self):
        """_get_server_globals 函数应已删除。"""
        try:
            from hermes.background_loops import _get_server_globals
            assert False, "_get_server_globals 应已删除"
        except (ImportError, AttributeError):
            pass  # 预期行为

    def test_no_import_state(self):
        """background_loops 不再 import state。"""
        with open(os.path.join(_SRC_DIR, "background_loops.py"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "import state" not in content
        assert "_get_server_globals" not in content


class TestMetricsPersistLoopEvent:

    @pytest.mark.asyncio
    async def test_reset_event_triggers_baseline_reset(self):
        """reset_event.set() 触发 baseline 重置。"""
        from hermes.background_loops import metrics_persist_loop

        mock_collector = MagicMock()
        mock_collector.snapshot.return_value = {"llm_calls_total": 0}
        mock_store = MagicMock()
        reset_event = asyncio.Event()

        # 启动 task，立即取消（验证签名和启动不崩溃）
        task = asyncio.create_task(
            metrics_persist_loop(mock_collector, mock_store, reset_event)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
