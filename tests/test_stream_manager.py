"""StreamManager 单元测试。"""
import threading
import pytest
from src.stream_manager import StreamManager, StreamCancelled


class TestStreamManager:
    def setup_method(self):
        self.sm = StreamManager()

    def test_register_returns_event(self):
        event = self.sm.register("s1")
        assert isinstance(event, threading.Event)
        assert not event.is_set()

    def test_cancel_sets_event(self):
        self.sm.register("s1")
        assert self.sm.cancel("s1") is True
        assert self.sm.is_cancelled("s1") is True

    def test_cancel_nonexistent_returns_false(self):
        assert self.sm.cancel("nosession") is False

    def test_cancel_idempotent(self):
        self.sm.register("s1")
        assert self.sm.cancel("s1") is True
        assert self.sm.cancel("s1") is True
        assert self.sm.is_cancelled("s1") is True

    def test_unregister_removes_event(self):
        self.sm.register("s1")
        self.sm.unregister("s1")
        assert self.sm.cancel("s1") is False
        assert self.sm.is_cancelled("s1") is False

    def test_unregister_idempotent(self):
        self.sm.register("s1")
        self.sm.unregister("s1")
        self.sm.unregister("s1")

    def test_multiple_sessions_independent(self):
        e1 = self.sm.register("s1")
        e2 = self.sm.register("s2")
        self.sm.cancel("s1")
        assert e1.is_set()
        assert not e2.is_set()

    def test_graceful_register_and_pop(self):
        self.sm.register("s1")
        self.sm.register_graceful("s1", "new message")
        assert self.sm.is_graceful_pending("s1")
        msg = self.sm.pop_graceful_message("s1")
        assert msg == "new message"
        assert not self.sm.is_graceful_pending("s1")

    def test_graceful_pop_nonexistent(self):
        assert self.sm.pop_graceful_message("nosession") is None
        self.sm.register("s1")
        assert self.sm.pop_graceful_message("s1") is None

    def test_graceful_cleaned_by_unregister(self):
        self.sm.register("s1")
        self.sm.register_graceful("s1", "msg")
        self.sm.unregister("s1")
        assert not self.sm.is_graceful_pending("s1")

    def test_concurrent_cancel_thread_safe(self):
        NUM = 10
        self.sm.register("s1")
        errors = []
        def do_cancel():
            try:
                for _ in range(100):
                    self.sm.cancel("s1")
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=do_cancel) for _ in range(NUM)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert self.sm.is_cancelled("s1") is True

    def test_stream_cancelled_is_exception(self):
        exc = StreamCancelled("test")
        assert isinstance(exc, Exception)
        assert str(exc) == "test"

    # ── 所有权感知的 unregister ──

    def test_unregister_event_ownership(self):
        e1 = self.sm.register("s1")
        e2 = self.sm.register("s1")
        # 用 e1 反注册：不应删 e2
        self.sm.unregister_event("s1", e1)
        assert self.sm.is_cancelled("s1") is False
        # e2 仍在
        self.sm.cancel("s1")
        assert self.sm.is_cancelled("s1") is True

    def test_unregister_event_correct(self):
        e1 = self.sm.register("s1")
        self.sm.unregister_event("s1", e1)
        assert self.sm.cancel("s1") is False

    def test_unregister_event_nonexistent(self):
        e1 = threading.Event()
        self.sm.unregister_event("nosession", e1)  # 无异常

    def test_unregister_event_backward_compat(self):
        """旧 unregister 仍可用。"""
        self.sm.register("s1")
        self.sm.unregister("s1")
        assert self.sm.cancel("s1") is False

    # ── 自动取消旧流 ──

    def test_register_auto_cancels_old(self):
        e1 = self.sm.register("s1")
        e2 = self.sm.register("s1")
        assert e1.is_set() is True   # 旧 event 被取消
        assert e2.is_set() is False  # 新 event 未设置
        assert self.sm.is_cancelled("s1") is False  # 取消状态以最新 event 为准

    def test_register_auto_cancel_first_time(self):
        e1 = self.sm.register("s1")
        assert e1.is_set() is False  # 首次注册，无旧流

    def test_register_auto_cancel_idempotent(self):
        e1 = self.sm.register("s1")
        self.sm.cancel("s1")
        e2 = self.sm.register("s1")
        # e1 已 cancel，e2 正常
        assert self.sm.is_cancelled("s1") is False

    def test_auto_cancel_clears_graceful(self):
        self.sm.register("s1")
        self.sm.register_graceful("s1", "new msg")
        # 重新注册自动取消旧流，graceful 也被清理
        e2 = self.sm.register("s1")
        assert self.sm.pop_graceful_message("s1") is None

    # ── 并发安全 ──

    def test_concurrent_register_ownership(self):
        errors = []
        events = {}

        def do_register(n):
            try:
                ev = self.sm.register("shared")
                events[n] = ev
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=do_register, args=(i,))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # 最后注册的 event 在 dict 中
        assert self.sm.is_cancelled("shared") is False

    def test_concurrent_register_timing(self):
        """两个线程交错注册/取消，最终状态正确。"""
        queue = []
        e1 = self.sm.register("s1")

        def delayed_register():
            import time
            time.sleep(0.01)
            e2 = self.sm.register("s1")
            queue.append(e2)

        t = threading.Thread(target=delayed_register)
        t.start()
        t.join()

        e2 = queue[0]
        assert e1.is_set() is True  # 被自动取消
        assert e2.is_set() is False

    # ── 两段式取消 ──

    def test_force_cancel_graceful_pending(self):
        """graceful pending 后 force_cancel → force_killed + 返回消息。"""
        self.sm.register("s1")
        self.sm.register_graceful("s1", "stop and do X")
        status, msg = self.sm.force_cancel("s1")
        assert status == "force_killed"
        assert msg == "stop and do X"
        assert self.sm.is_cancelled("s1") is True

    def test_force_cancel_no_graceful(self):
        """没有 graceful pending 时 force_cancel → cancelling。"""
        self.sm.register("s1")
        status, msg = self.sm.force_cancel("s1")
        assert status == "cancelling"
        assert msg is None
        assert self.sm.is_cancelled("s1") is True

    def test_force_cancel_nonexistent(self):
        """不存在的 session force_cancel 正常返回。"""
        status, msg = self.sm.force_cancel("nosession")
        assert status == "cancelling"
        assert msg is None

    def test_force_cancel_pops_graceful_message(self):
        """force_cancel 后 is_graceful_pending 为 False。"""
        self.sm.register("s1")
        self.sm.register_graceful("s1", "new msg")
        self.sm.force_cancel("s1")
        assert self.sm.is_graceful_pending("s1") is False

    def test_force_cancel_idempotent(self):
        """force_cancel 多次调用无副作用。"""
        self.sm.register("s1")
        self.sm.register_graceful("s1", "msg")
        self.sm.force_cancel("s1")
        # 第二次：已无 graceful pending
        status, msg = self.sm.force_cancel("s1")
        assert status == "cancelling"
        assert msg is None

    def test_force_cancel_sets_cancel_event(self):
        """cancel_event 被设置。"""
        ev = self.sm.register("s1")
        assert ev.is_set() is False
        self.sm.register_graceful("s1", "msg")
        self.sm.force_cancel("s1")
        assert ev.is_set() is True
