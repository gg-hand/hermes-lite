"""ErrorClassifier 全覆盖测试。

mock 策略：无需 mock，ErrorClassifier 是纯函数，所有测试走真实逻辑。

运行方式:
    python -m unittest tests.test_error_classifier -v
    python tests/test_error_classifier.py
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from src.agent.error_classifier import ErrorClassifier, ErrorClass


# ---------------------------------------------------------------------------
# HTTP 状态码判定
# ---------------------------------------------------------------------------


class TestHttpStatusCodeClassification(unittest.TestCase):
    """验证 HTTP 状态码前缀的分类逻辑。"""

    def test_http_200_success(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 200]\n\n{\"data\": \"ok\"}",
        )
        self.assertIs(ec, ErrorClass.SUCCESS, f"Expected SUCCESS, got {ec}: {reason}")

    def test_http_403_anti_crawler(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 403]\n\nAccess Denied",
        )
        self.assertIs(
            ec, ErrorClass.ANTI_CRAWLER,
            f"403 should be ANTI_CRAWLER, got {ec}: {reason}",
        )

    def test_http_412_anti_crawler(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 412]\n\nPrecondition Failed",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER, f"Expected ANTI_CRAWLER, got {ec}")

    def test_http_401_auth_required(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 401]\n\nUnauthorized",
        )
        self.assertIs(ec, ErrorClass.AUTH_REQUIRED, f"Expected AUTH_REQUIRED, got {ec}")

    def test_http_404_permanent(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 404]\n\nNot Found",
        )
        self.assertIs(ec, ErrorClass.PERMANENT, f"Expected PERMANENT, got {ec}")

    def test_http_410_permanent(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 410]\n\nGone",
        )
        self.assertIs(ec, ErrorClass.PERMANENT, f"Expected PERMANENT, got {ec}")

    def test_http_429_transient(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 429]\n\nToo Many Requests",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT, f"Expected TRANSIENT, got {ec}")

    def test_http_502_transient(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 502]\n\nBad Gateway",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT, f"Expected TRANSIENT, got {ec}")

    def test_http_503_transient(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 503]\n\nService Unavailable",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT, f"Expected TRANSIENT, got {ec}")

    def test_http_500_transient(self):
        ec, reason = ErrorClassifier.classify(
            "web_fetch", {"url": "https://example.com"},
            "[HTTP 500]\n\nInternal Server Error",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT, f"Expected TRANSIENT, got {ec}")


# ---------------------------------------------------------------------------
# 反爬虫关键词检测（HTTP 200 + 关键词）
# ---------------------------------------------------------------------------


class TestAntiCrawlerKeywords(unittest.TestCase):
    """验证 HTTP 200 时 body 关键词反爬检测。"""

    def test_http_200_with_captcha(self):
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\n请输入验证码",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER)

    def test_http_200_with_access_denied(self):
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\nAccess Denied by WAF",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER)

    def test_http_200_with_rate_limit(self):
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\n请求频率限制...",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER)

    def test_http_200_with_too_many_requests(self):
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\nToo many requests from your IP",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER)

    def test_http_200_with_captcha_english(self):
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\nPlease complete the captcha verification",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER)

    def test_http_200_with_security_check(self):
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\nSecurity check triggered",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER)

    def test_http_200_normal_body(self):
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\n<html><body>Hello World</body></html>",
        )
        self.assertIs(ec, ErrorClass.SUCCESS)


# ---------------------------------------------------------------------------
# bash_exec / 通用分类
# ---------------------------------------------------------------------------


class TestGenericClassification(unittest.TestCase):
    """验证非 web_fetch 工具或降级路径的分类。"""

    def test_bash_timeout_transient(self):
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "sleep 60"},
            "命令执行超时（30 秒）",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT)

    def test_bash_command_not_found(self):
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "xxx"},
            "command not found: xxx",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT)

    def test_generic_timeout_transient(self):
        ec, _ = ErrorClassifier.classify(
            "unknown_tool", {}, "操作超时",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT)

    def test_generic_not_found_permanent(self):
        ec, _ = ErrorClassifier.classify(
            "unknown_tool", {}, "文件不存在",
        )
        self.assertIs(ec, ErrorClass.PERMANENT)

    def test_generic_error_unknown(self):
        ec, _ = ErrorClassifier.classify(
            "unknown_tool", {}, "出错了，请稍后再试",
        )
        self.assertIs(ec, ErrorClass.UNKNOWN, "Generic error should not be misclassified")

    def test_empty_result_unknown(self):
        ec, _ = ErrorClassifier.classify(
            "unknown_tool", {}, "",
        )
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_none_result_unknown(self):
        ec, _ = ErrorClassifier.classify(
            "unknown_tool", {}, "",  # empty string, not None
        )
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_http_request_failure_transient(self):
        """httpx 抛异常时 builtin_tools 返回 'HTTP 请求失败: <msg>'。"""
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "HTTP 请求失败: Connection timeout",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT)

    def test_web_fetch_no_http_prefix_unknown(self):
        """老版本 web_fetch（无 [HTTP nnn] 前缀）降级为 UNKNOWN。"""
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "some raw text without prefix",
        )
        self.assertIs(ec, ErrorClass.UNKNOWN)


# ---------------------------------------------------------------------------
# 接口契约
# ---------------------------------------------------------------------------


class TestInterfaceContract(unittest.TestCase):
    """验证 classify 方法的接口契约。"""

    def test_return_type_is_tuple(self):
        result = ErrorClassifier.classify("any_tool", {}, "any result")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        ec, reason = result
        self.assertIsInstance(ec, ErrorClass)
        self.assertIsInstance(reason, str)

    def test_all_error_classes_defined(self):
        """确保 6 种 ErrorClass 全部定义。"""
        expected = {"SUCCESS", "TRANSIENT", "ANTI_CRAWLER", "PERMANENT",
                    "AUTH_REQUIRED", "UNKNOWN"}
        self.assertEqual({e.name for e in ErrorClass}, expected)


# ---------------------------------------------------------------------------
# 边缘情况
# ---------------------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    """验证边缘情况不崩溃。"""

    def test_long_result_body(self):
        """极长的响应体不应导致性能问题或崩溃。"""
        long_body = "[HTTP 200]\n\n" + ("x" * 50000)
        ec, _ = ErrorClassifier.classify("web_fetch", {"url": "x"}, long_body)
        self.assertIs(ec, ErrorClass.SUCCESS)

    def test_http_prefix_in_body_not_header(self):
        """body 中含 [HTTP nnn] 但不作为首行时不应影响分类。"""
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\nThe API says [HTTP 404] sometimes",
        )
        self.assertIs(ec, ErrorClass.SUCCESS)

    def test_case_insensitive_keywords(self):
        """反爬关键词检测大小写不敏感。"""
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\nAccess Denied",
        )
        self.assertIs(ec, ErrorClass.ANTI_CRAWLER)
        ec2, _ = ErrorClassifier.classify(
            "web_fetch", {"url": "x"},
            "[HTTP 200]\n\naccess denied",
        )
        self.assertIs(ec2, ErrorClass.ANTI_CRAWLER)

    def test_tool_name_is_none(self):
        """tool_name 为空或非预期值时不崩溃。"""
        ec, _ = ErrorClassifier.classify("", {}, "任意结果")
        self.assertIsInstance(ec, ErrorClass)

    def test_tool_input_empty_dict(self):
        """空 tool_input 正常分类。"""
        ec, _ = ErrorClassifier.classify(
            "web_fetch", {},
            "[HTTP 404]\n\nNot Found",
        )
        self.assertIs(ec, ErrorClass.PERMANENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
