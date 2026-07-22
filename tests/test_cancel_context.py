"""cancel_context ContextVar 传播测试。

无需 mock，直接测试 contextvar get/set/reset。

运行方式:
    python -m pytest tests/test_cancel_context.py -v
"""

from __future__ import annotations

import os
import sys
import threading
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from teage_liu.agent._cancel_context import current_cancel_event


class TestCancelContext(unittest.TestCase):
    """验证 ContextVar 的 get/set/reset 语义。"""

    def test_default_is_none(self):
        self.assertIsNone(current_cancel_event.get())

    def test_set_and_get(self):
        event = threading.Event()
        token = current_cancel_event.set(event)
        self.assertIs(current_cancel_event.get(), event)
        current_cancel_event.reset(token)

    def test_reset_restores_default(self):
        event = threading.Event()
        token = current_cancel_event.set(event)
        current_cancel_event.reset(token)
        self.assertIsNone(current_cancel_event.get())

    def test_is_set_on_event(self):
        event = threading.Event()
        event.set()
        token = current_cancel_event.set(event)
        self.assertTrue(current_cancel_event.get().is_set())
        current_cancel_event.reset(token)

    def test_double_set(self):
        e1 = threading.Event()
        e2 = threading.Event()
        token1 = current_cancel_event.set(e1)
        self.assertIs(current_cancel_event.get(), e1)
        token2 = current_cancel_event.set(e2)
        self.assertIs(current_cancel_event.get(), e2)
        current_cancel_event.reset(token2)
        self.assertIs(current_cancel_event.get(), e1)
        current_cancel_event.reset(token1)
        self.assertIsNone(current_cancel_event.get())

    def test_reset_restores_previous(self):
        e1 = threading.Event()
        e2 = threading.Event()
        token1 = current_cancel_event.set(e1)
        token2 = current_cancel_event.set(e2)
        current_cancel_event.reset(token2)
        self.assertIs(current_cancel_event.get(), e1)
        current_cancel_event.reset(token1)

    def test_independent_contexts(self):
        """不同线程的 ContextVar 互不影响。"""
        e1 = threading.Event()
        e2 = threading.Event()
        results = {}

        def set_in_thread(ev, key):
            token = current_cancel_event.set(ev)
            results[key] = current_cancel_event.get()
            current_cancel_event.reset(token)

        t = threading.Thread(target=set_in_thread, args=(e1, "thread1"))
        t.start()
        t.join()

        token = current_cancel_event.set(e2)
        results["main"] = current_cancel_event.get()
        current_cancel_event.reset(token)

        self.assertIs(results["thread1"], e1)
        self.assertIs(results["main"], e2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
