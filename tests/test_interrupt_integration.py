"""中断功能集成测试 — 跨组件交互验证。"""
import threading
import pytest
from src.stream_manager import StreamManager, StreamCancelled
from src.breakpoint_detector import BreakpointDetector
from src.llm.client import _is_retryable


class TestInterruptIntegration:
    """跨 StreamManager + BreakpointDetector + client 的集成场景。"""

    def test_full_graceful_flow(self):
        """完整优雅中断流程：register → graceful → 断点检测 → cancel → pop。"""
        sm = StreamManager()
        d = BreakpointDetector(40)
        sid = "graceful-test"
        cancel_event = sm.register(sid)

        # 1. 注册 graceful 中断
        sm.register_graceful(sid, "用户新消息")
        assert sm.is_graceful_pending(sid)

        # 2. 模拟 LLM 输出，累积文本
        tokens = ["第一", "部分", "完成", "。"]
        text = ""
        for t in tokens:
            text += t
        # 3. 断点检测到达
        assert d.should_break(text)

        # 4. 触发中断
        cancel_event.set()
        assert sm.is_cancelled(sid)

        # 5. 取出消息
        msg = sm.pop_graceful_message(sid)
        assert msg == "用户新消息"
        assert not sm.is_graceful_pending(sid)

        sm.unregister(sid)

    def test_immediate_cancel_flow(self):
        """立即中断：register → cancel → is_set → unregister。"""
        sm = StreamManager()
        sid = "immediate-test"
        cancel_event = sm.register(sid)

        assert not cancel_event.is_set()
        sm.cancel(sid)
        assert cancel_event.is_set()
        assert sm.is_cancelled(sid)

        sm.unregister(sid)
        assert not sm.is_cancelled(sid)

    def test_cancel_while_graceful_pending(self):
        """graceful pending 中收到 immediate cancel，两者互不干扰。"""
        sm = StreamManager()
        sid = "mixed-test"
        sm.register(sid)
        sm.register_graceful(sid, "pending")
        sm.cancel(sid)
        assert sm.is_cancelled(sid)
        assert sm.is_graceful_pending(sid)
        msg = sm.pop_graceful_message(sid)
        assert msg == "pending"
        sm.unregister(sid)

    def test_stream_cancelled_not_retried(self):
        """StreamCancelled 不被 retry 机制重试。"""
        assert _is_retryable(StreamCancelled("cancel")) is False

    def test_network_error_still_retried(self):
        """普通网络错误保持可重试。"""
        assert _is_retryable(Exception("connection error")) is True

    def test_http_429_is_retried(self):
        """429 限流保持可重试。"""
        exc = Exception("rate limit")
        exc.status_code = 429
        assert _is_retryable(exc) is True

    def test_http_400_not_retried(self):
        """400 客户端错误不重试。"""
        exc = Exception("bad request")
        exc.status_code = 400
        assert _is_retryable(exc) is False

    @pytest.mark.parametrize("inp", [
        "", "a", "\n", "    ", "123", "!@#$", "中" * 1000,
        "```", "test```\n```more",
    ])
    def test_breakpoint_detector_edge_inputs(self, inp):
        """各种边缘输入不抛异常。"""
        d = BreakpointDetector(40)
        d.score(inp)  # should not raise

    def test_stream_manager_mass_cleanup(self):
        """大量 session 注册后正确清理。"""
        sm = StreamManager()
        sessions = [f"s{i}" for i in range(100)]
        for sid in sessions:
            sm.register(sid)
        for sid in sessions:
            sm.unregister(sid)
        # 验证无残留
        for sid in sessions:
            assert not sm.is_cancelled(sid)
        # 也验证 graceful 无残留
        for sid in sessions:
            assert not sm.is_graceful_pending(sid)
