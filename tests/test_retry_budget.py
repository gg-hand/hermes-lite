"""RetryBudget 单元测试（Task 5.2）。

覆盖 6 类用例：
1. fixed backoff 策略
2. linear backoff 策略
3. exponential backoff 策略
4. max_attempts 边界（耗尽预算后 has_budget=False）
5. attempt 计数（increment 累加）
6. backoff_factor 上限（max_delay_ms 约束）

补充覆盖 should_retry 的 NON_RETRYABLE 拦截与默认 retryable 兜底。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from hermes.tasks.workflow.retry import (
    DEFAULT_RETRYABLE_ERRORS,
    NON_RETRYABLE_ERRORS,
    RetryBudget,
)
from hermes.tasks.workflow.spec import RetryPolicy


class TestRetryBudgetFixed(unittest.TestCase):
    """fixed backoff 策略：每次延迟相同 base_delay_ms。"""

    def test_fixed_strategy_constant_delay(self):
        policy = RetryPolicy(
            max_attempts=3,
            backoff_strategy="fixed",
            base_delay_ms=1000,
            max_delay_ms=30000,
        )
        budget = RetryBudget(policy=policy)

        budget.increment()  # attempt=1
        self.assertEqual(budget.compute_backoff_ms(), 1000)
        budget.increment()  # attempt=2
        self.assertEqual(budget.compute_backoff_ms(), 1000)
        budget.increment()  # attempt=3
        self.assertEqual(budget.compute_backoff_ms(), 1000)


class TestRetryBudgetLinear(unittest.TestCase):
    """linear backoff 策略：delay = base_delay_ms * attempt。"""

    def test_linear_strategy_grows_linearly(self):
        policy = RetryPolicy(
            max_attempts=4,
            backoff_strategy="linear",
            base_delay_ms=500,
            max_delay_ms=60000,
        )
        budget = RetryBudget(policy=policy)

        budget.increment()  # attempt=1 → 500
        self.assertEqual(budget.compute_backoff_ms(), 500)
        budget.increment()  # attempt=2 → 1000
        self.assertEqual(budget.compute_backoff_ms(), 1000)
        budget.increment()  # attempt=3 → 1500
        self.assertEqual(budget.compute_backoff_ms(), 1500)


class TestRetryBudgetExponential(unittest.TestCase):
    """exponential backoff 策略：delay = base * 2^(attempt-1)。"""

    def test_exponential_strategy_doubles(self):
        policy = RetryPolicy(
            max_attempts=4,
            backoff_strategy="exponential",
            base_delay_ms=200,
            max_delay_ms=60000,
        )
        budget = RetryBudget(policy=policy)

        budget.increment()  # attempt=1 → 200 * 2^0 = 200
        self.assertEqual(budget.compute_backoff_ms(), 200)
        budget.increment()  # attempt=2 → 200 * 2^1 = 400
        self.assertEqual(budget.compute_backoff_ms(), 400)
        budget.increment()  # attempt=3 → 200 * 2^2 = 800
        self.assertEqual(budget.compute_backoff_ms(), 800)


class TestRetryBudgetMaxAttemptsBoundary(unittest.TestCase):
    """max_attempts 边界：达到上限后 has_budget=False，should_retry=False。"""

    def test_budget_exhausted_after_max_attempts(self):
        policy = RetryPolicy(
            max_attempts=2,
            backoff_strategy="fixed",
            base_delay_ms=10,
            max_delay_ms=100,
        )
        budget = RetryBudget(policy=policy)

        # 首次执行
        budget.increment()
        self.assertTrue(budget.has_budget())
        self.assertTrue(budget.should_retry("transient"))

        # 第二次执行（已达 max_attempts=2）
        budget.increment()
        self.assertFalse(budget.has_budget())
        self.assertFalse(budget.should_retry("transient"))

    def test_should_retry_returns_false_for_non_retryable(self):
        policy = RetryPolicy(
            max_attempts=5,
            backoff_strategy="fixed",
            base_delay_ms=10,
            max_delay_ms=100,
            retry_on=["transient", "timeout"],
        )
        budget = RetryBudget(policy=policy)
        budget.increment()
        # 即使有预算，PERMANENT 错误也不重试
        for err in NON_RETRYABLE_ERRORS:
            with self.subTest(error_class=err):
                self.assertFalse(budget.should_retry(err))


class TestRetryBudgetAttemptCounting(unittest.TestCase):
    """attempt 计数：increment 累加，has_budget 反映剩余次数。"""

    def test_increment_increases_attempt(self):
        policy = RetryPolicy(max_attempts=3, backoff_strategy="fixed")
        budget = RetryBudget(policy=policy)

        self.assertEqual(budget.attempt, 0)
        self.assertTrue(budget.has_budget())

        ret = budget.increment()
        self.assertEqual(ret, 1)
        self.assertEqual(budget.attempt, 1)
        self.assertTrue(budget.has_budget())

        budget.increment()
        self.assertEqual(budget.attempt, 2)

        budget.increment()
        self.assertEqual(budget.attempt, 3)
        self.assertFalse(budget.has_budget())


class TestRetryBudgetMaxDelayCap(unittest.TestCase):
    """max_delay_ms 上限约束（backoff_factor）。"""

    def test_exponential_capped_by_max_delay(self):
        policy = RetryPolicy(
            max_attempts=10,
            backoff_strategy="exponential",
            base_delay_ms=1000,
            max_delay_ms=5000,  # 上限 5s
        )
        budget = RetryBudget(policy=policy)

        budget.increment()  # attempt=1 → 1000
        self.assertEqual(budget.compute_backoff_ms(), 1000)
        budget.increment()  # attempt=2 → 2000
        self.assertEqual(budget.compute_backoff_ms(), 2000)
        budget.increment()  # attempt=3 → 4000
        self.assertEqual(budget.compute_backoff_ms(), 4000)
        budget.increment()  # attempt=4 → 8000 → capped 5000
        self.assertEqual(budget.compute_backoff_ms(), 5000)
        budget.increment()  # attempt=5 → 16000 → capped 5000
        self.assertEqual(budget.compute_backoff_ms(), 5000)

    def test_linear_capped_by_max_delay(self):
        policy = RetryPolicy(
            max_attempts=20,
            backoff_strategy="linear",
            base_delay_ms=2000,
            max_delay_ms=10000,
        )
        budget = RetryBudget(policy=policy)

        budget.increment()  # attempt=1 → 2000
        self.assertEqual(budget.compute_backoff_ms(), 2000)
        budget.increment()  # attempt=2 → 4000
        self.assertEqual(budget.compute_backoff_ms(), 4000)
        budget.increment()  # attempt=6 → 12000 → capped 10000
        for _ in range(3):  # 累计到 attempt=6
            budget.increment()
        self.assertEqual(budget.attempt, 6)
        self.assertEqual(budget.compute_backoff_ms(), 10000)


class TestRetryBudgetShouldRetryLogic(unittest.TestCase):
    """should_retry 决策逻辑补充。"""

    def test_empty_error_class_defaults_retryable(self):
        """空 error_class 默认 retryable（保守策略让 retry 兜底）。"""
        policy = RetryPolicy(max_attempts=3, retry_on=["transient", "timeout"])
        budget = RetryBudget(policy=policy)
        budget.increment()
        self.assertTrue(budget.should_retry(""))

    def test_retry_on_overrides_default(self):
        """policy.retry_on 优先于 DEFAULT_RETRYABLE_ERRORS。"""
        policy = RetryPolicy(
            max_attempts=3,
            retry_on=["transient"],  # 仅重试 transient
        )
        budget = RetryBudget(policy=policy)
        budget.increment()
        self.assertTrue(budget.should_retry("transient"))
        # timeout 不在 retry_on 中，不应重试
        self.assertFalse(budget.should_retry("timeout"))

    def test_default_retryable_errors_constant(self):
        """DEFAULT_RETRYABLE_ERRORS 含 transient 与 timeout。"""
        self.assertIn("transient", DEFAULT_RETRYABLE_ERRORS)
        self.assertIn("timeout", DEFAULT_RETRYABLE_ERRORS)

    def test_sleep_backoff_calls_time_sleep(self):
        """sleep_backoff 调用 time.sleep(delay_ms/1000)。"""
        policy = RetryPolicy(
            max_attempts=3,
            backoff_strategy="fixed",
            base_delay_ms=500,
            max_delay_ms=10000,
        )
        budget = RetryBudget(policy=policy)
        budget.increment()
        with patch("hermes.tasks.workflow.retry.time.sleep") as mock_sleep:
            budget.sleep_backoff()
            mock_sleep.assert_called_once_with(0.5)

    def test_sleep_backoff_zero_when_no_attempt(self):
        """未 increment 时 sleep_backoff 立即返回（delay=0）。"""
        policy = RetryPolicy(max_attempts=3, base_delay_ms=1000)
        budget = RetryBudget(policy=policy)
        # attempt=0 → compute_backoff_ms 返回 0
        self.assertEqual(budget.compute_backoff_ms(), 0)
        with patch("hermes.tasks.workflow.retry.time.sleep") as mock_sleep:
            budget.sleep_backoff()
            mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
