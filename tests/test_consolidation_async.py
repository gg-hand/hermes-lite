"""P0-2 异步 Consolidation 测试 — 验证并发安全与数据完整性。

运行方式:
    python -m unittest tests.test_consolidation_async -v

测试目标:
- add_info 和 consolidate 并发时不丢数据
- 异步 force_consolidate 立即返回（不阻塞）
- 正在执行 consolidate 时再次触发被正确跳过
- close() 等待异步完成并执行最终 consolidate
- 异常路径锁正确释放
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.memory.consolidation import ConsolidationEngine


def _make_llm_response(text: str) -> MagicMock:
    """构造 mock LLM 响应（与 test_consolidation.py 一致）。"""
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


class TestAsyncConsolidationDataIntegrity(unittest.TestCase):
    """验证异步 consolidate 的数据完整性。"""

    def setUp(self):
        self.chroma_mock = MagicMock()
        self.chroma_mock.collection.count.return_value = 0
        self.chroma_mock.find_duplicates.return_value = []
        self.chroma_mock.add_memory.return_value = "new_mem_id"

        self.llm_mock = MagicMock()
        self.llm_mock.chat_consolidation.return_value = _make_llm_response(
            '{"facts": [{"content": "test fact", "type": "fact", "importance": 0.5}]}'
        )

        self.engine = ConsolidationEngine(
            llm_client=self.llm_mock,
            chroma_store=self.chroma_mock,
            threshold=3,
        )

    def test_add_info_and_consolidate_message_count(self):
        """add_info 后 consolidate 应处理所有 pending 消息。"""
        self.engine.add_info({"role": "user", "content": "msg1"})
        self.engine.add_info({"role": "assistant", "content": "resp1"})
        self.engine.add_info({"role": "user", "content": "msg2"})

        self.assertEqual(self.engine.info_counter, 3)
        stats = self.engine.consolidate()
        self.assertGreater(stats.get("facts_extracted", 0), 0)

    def test_concurrent_add_info_during_consolidate(self):
        """consolidate 执行期间 add_info 不丢消息。

        模拟策略：让 mock LLM 延迟响应，模拟耗时场景。
        consolidate 的数据锁在开始时即释放（原子 swap 后），
        所以主线程的 add_info 在 LLM 调用期间不受阻塞。
        """
        # 让 llm 调用变慢（模拟真实 LLM 延迟）
        def slow_llm(*args, **kwargs):
            time.sleep(0.15)
            return _make_llm_response(
                '{"facts": [{"content": "fact", "type": "fact", "importance": 0.5}]}'
            )

        self.engine.llm_client.chat_consolidation.side_effect = slow_llm

        # 先加背景消息（足够触发 consolidation）
        self.engine.add_info({"role": "user", "content": "background msg"})
        self.engine.add_info({"role": "user", "content": "background msg2"})
        self.engine.add_info({"role": "user", "content": "background msg3"})

        # 在后台启动 consolidate（将触发 slow_llm）
        stats_result = {}

        def run_consolidate():
            stats_result["stats"] = self.engine.consolidate()

        t = threading.Thread(target=run_consolidate)
        t.start()

        time.sleep(0.03)  # consolidate 已获取数据锁并 swap，正在 LLM 调用
        # consolidate 执行期间 add_info（不应被阻塞）
        self.engine.add_info({"role": "user", "content": "during consolidate"})
        self.engine.add_info({"role": "assistant", "content": "during response"})

        t.join()

        # consolidate 期间添加的消息应计入新计数器
        self.assertGreaterEqual(self.engine.info_counter, 2,
                                "consolidate 执行期间添加的消息不应丢失")
        # pending_messages 应包含 consolidate 期间添加的消息
        self.assertGreaterEqual(len(self.engine.pending_messages), 2)

    def test_consolidate_resets_counter_and_messages(self):
        """consolidate 后 info_counter 归零，pending_messages 清空。"""
        self.engine.add_info({"role": "user", "content": "msg1"})
        self.engine.add_info({"role": "user", "content": "msg2"})
        self.engine.consolidate()

        self.assertEqual(self.engine.info_counter, 0)
        self.assertEqual(len(self.engine.pending_messages), 0)


class TestAsyncConsolidationForceConsolidate(unittest.TestCase):
    """验证 force_consolidate 的异步语义。"""

    def setUp(self):
        self.chroma_mock = MagicMock()
        self.chroma_mock.collection.count.return_value = 0
        self.chroma_mock.find_duplicates.return_value = []
        self.chroma_mock.add_memory.return_value = "new_mem_id"

        self.llm_mock = MagicMock()
        self.llm_mock.chat_consolidation.return_value = _make_llm_response(
            '{"facts": [{"content": "fact", "type": "fact", "importance": 0.5}]}'
        )

        self.engine = ConsolidationEngine(
            llm_client=self.llm_mock,
            chroma_store=self.chroma_mock,
            threshold=3,
        )

    def test_force_consolidate_returns_immediately(self):
        """force_consolidate 应立即返回（不阻塞等待 consolidate 完成）。"""
        # 模拟 consolidate 耗时
        original_consolidate = self.engine.consolidate

        def slow_consolidate(session_id=None):
            time.sleep(0.5)
            return original_consolidate(session_id=session_id)

        self.engine.consolidate = slow_consolidate

        self.engine.add_info({"role": "user", "content": "test"})
        self.engine.add_info({"role": "user", "content": "test2"})

        t0 = time.perf_counter()
        result = self.engine.force_consolidate()
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 0.2, "force_consolidate 不应阻塞超过 200ms")
        self.assertIsInstance(result, dict)

    def test_force_consolidate_skip_when_in_progress(self):
        """正在执行 consolidate 时再次 force_consolidate 应跳过。"""
        self.engine.add_info({"role": "user", "content": "test"})
        self.engine.add_info({"role": "user", "content": "test2"})

        # 直接占用 _consolidation_lock 模拟正在执行中
        acquired = self.engine._consolidation_lock.acquire(blocking=False)
        self.assertTrue(acquired, "测试前应能获取锁")

        try:
            # 锁已持有，force_consolidate 应跳过
            result = self.engine.force_consolidate()
            self.assertEqual(result, {}, "skip 时应返回空字典")

            # 验证 consolidation 未真正执行（info_counter 不变）
            self.assertEqual(self.engine.info_counter, 2)
        finally:
            self.engine._consolidation_lock.release()

        # 锁释放后，force_consolidate 应能正常触发
        result = self.engine.force_consolidate()
        # 此时 consolidate 已异步启动，等待完成
        time.sleep(0.1)
        # info_counter 应为 0（被 consolidate 的原子 swap 重置了）
        self.assertEqual(self.engine.info_counter, 0)

    def test_force_consolidate_with_no_pending(self):
        """无 pending 消息时 force_consolidate 应安全返回空。"""
        result = self.engine.force_consolidate()
        self.assertIsInstance(result, dict)
        # 不应有异常


class TestAsyncConsolidationClose(unittest.TestCase):
    """验证 close() 的正确行为。"""

    def setUp(self):
        self.chroma_mock = MagicMock()
        self.chroma_mock.collection.count.return_value = 0
        self.chroma_mock.find_duplicates.return_value = []
        self.chroma_mock.add_memory.return_value = "new_mem_id"

        self.llm_mock = MagicMock()
        self.llm_mock.chat_consolidation.return_value = _make_llm_response(
            '{"facts": [{"content": "close fact", "type": "fact", "importance": 0.5}]}'
        )

        self.engine = ConsolidationEngine(
            llm_client=self.llm_mock,
            chroma_store=self.chroma_mock,
            threshold=2,
        )

    def test_close_processes_pending_messages(self):
        """close() 应处理所有 pending 消息。"""
        self.engine.add_info({"role": "user", "content": "pre-close msg"})
        self.engine.add_info({"role": "user", "content": "pre-close msg2"})

        self.engine.close()

        # LLM 应被调用（consolidate 执行了）
        self.assertGreater(self.engine.llm_client.chat_consolidation.call_count, 0)

    def test_close_no_pending(self):
        """无 pending 消息时 close() 安全。"""
        try:
            self.engine.close()
        except Exception as e:
            self.fail(f"close() 在无 pending 消息时抛异常: {e}")

    def test_close_idempotent(self):
        """close() 多次调用安全。"""
        self.engine.add_info({"role": "user", "content": "msg"})
        self.engine.add_info({"role": "user", "content": "msg2"})
        self.engine.close()
        # 第二次 close 不应抛异常
        try:
            self.engine.close()
        except Exception as e:
            self.fail(f"close() 第二次调用抛异常: {e}")


class TestAsyncConsolidationExceptionSafety(unittest.TestCase):
    """验证异常路径下锁正确释放。"""

    def setUp(self):
        self.chroma_mock = MagicMock()
        self.chroma_mock.collection.count.return_value = 0
        self.chroma_mock.find_duplicates.side_effect = RuntimeError("模拟异常")

        self.llm_mock = MagicMock()
        self.llm_mock.chat_consolidation.return_value = _make_llm_response(
            '{"facts": [{"content": "fact", "type": "fact", "importance": 0.5}]}'
        )

        self.engine = ConsolidationEngine(
            llm_client=self.llm_mock,
            chroma_store=self.chroma_mock,
            threshold=2,
        )

    def test_consolidate_exception_releases_lock(self):
        """consolidate 内部异常后锁应被释放（异常被内部处理，不传播）。"""
        self.engine.add_info({"role": "user", "content": "msg1"})
        self.engine.add_info({"role": "user", "content": "msg2"})

        # consolidate 内部处理 find_duplicates 异常（降级去重），不传播
        try:
            stats = self.engine.consolidate()
        except Exception as e:
            self.fail(f"consolidate 不应向外传播异常: {e}")

        # 异常后仍可正常 add_info
        try:
            self.engine.add_info({"role": "user", "content": "post-error msg"})
        except Exception as e:
            self.fail(f"consolidate 异常后 add_info 失败: {e}")

    def test_force_consolidate_exception_safety(self):
        """force_consolidate 在异常路径不留下锁定状态。"""
        self.engine.add_info({"role": "user", "content": "msg"})

        # 模拟线程执行异常
        with patch.object(self.engine, '_run_async_consolidation',
                          side_effect=RuntimeError("模拟")):
            # force_consolidate 启动后台线程，线程抛异常
            try:
                result = self.engine.force_consolidate()
                time.sleep(0.1)  # 等线程执行
            except Exception:
                pass

        # 锁已释放，可以再次 force_consolidate
        try:
            self.engine.add_info({"role": "user", "content": "recovery msg"})
            self.engine.add_info({"role": "user", "content": "recovery msg2"})
            result = self.engine.force_consolidate()
        except Exception as e:
            self.fail(f"异常后 force_consolidate 失败: {e}")


class TestAsyncConsolidationThresholdTrigger(unittest.TestCase):
    """验证阈值触发的 consolidate 仍正常工作。"""

    def setUp(self):
        self.chroma_mock = MagicMock()
        self.chroma_mock.collection.count.return_value = 0
        self.chroma_mock.find_duplicates.return_value = []
        self.chroma_mock.add_memory.return_value = "new_mem_id"

        self.llm_mock = MagicMock()
        self.llm_mock.chat_consolidation.return_value = _make_llm_response(
            '{"facts": [{"content": "threshold fact", "type": "fact", "importance": 0.5}]}'
        )

        self.engine = ConsolidationEngine(
            llm_client=self.llm_mock,
            chroma_store=self.chroma_mock,
            threshold=3,
        )

    def test_threshold_consolidation_still_triggers(self):
        """达到阈值时 should_consolidate 返回 True。"""
        self.engine.add_info({"role": "user", "content": "m1"})
        self.engine.add_info({"role": "user", "content": "m2"})
        self.assertFalse(self.engine.should_consolidate())
        self.engine.add_info({"role": "user", "content": "m3"})
        self.assertTrue(self.engine.should_consolidate())

    def test_threshold_consolidation_still_processes(self):
        """达到阈值后 consolidate 正常执行。"""
        for i in range(3):
            self.engine.add_info({"role": "user", "content": f"msg{i}"})

        stats = self.engine.consolidate()
        self.assertEqual(stats["facts_extracted"], 1)
        self.assertEqual(stats["facts_added"], 1)


if __name__ == "__main__":
    unittest.main()
