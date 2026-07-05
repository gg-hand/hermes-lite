"""http_request 增强测试（升级阶梯版）。

mock 策略：mock httpx.Client 避免真实 HTTP 请求。
使用 unittest.mock.patch 替换 httpx.Client，构造 mock 响应验证。

运行方式:
    python -m unittest tests.test_builtin_tools_http -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from src.agent.builtin_tools import (
    http_request,
    _classify_quality,
    _extract_domain,
    _build_enhanced_headers,
    _ANTI_CRAWLER_QUALITY_RE,
)


# ---------------------------------------------------------------------------
# 质量标签
# ---------------------------------------------------------------------------


class TestQualityPrefix(unittest.TestCase):
    """验证 [OK] / [BLOCKED] / [ERROR nnn] / [TRANSIENT] 前缀。"""

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_ok_prefix_on_200(self, mock_client_cls):
        self._mock_response(mock_client_cls, 200, "normal body", "text/plain")
        result = http_request("https://example.com")
        self.assertTrue(result.startswith("[OK]"), f"Got: {result[:50]}")

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_blocked_prefix_on_403(self, mock_client_cls):
        self._mock_response(mock_client_cls, 403, "Access Denied", "text/html")
        result = http_request("https://example.com")
        self.assertTrue(result.startswith("[BLOCKED]"), f"Got: {result[:50]}")

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_blocked_prefix_on_412(self, mock_client_cls):
        self._mock_response(mock_client_cls, 412, "Precondition Failed", "text/html")
        result = http_request("https://example.com")
        self.assertTrue(result.startswith("[BLOCKED]"))

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_blocked_prefix_on_body_keyword(self, mock_client_cls):
        """200 但 body 含验证码 → BLOCKED。"""
        self._mock_response(mock_client_cls, 200, "请输入验证码", "text/html")
        result = http_request("https://example.com")
        self.assertTrue(result.startswith("[BLOCKED]"))

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_error_404_prefix(self, mock_client_cls):
        self._mock_response(mock_client_cls, 404, "Not Found", "text/html")
        result = http_request("https://example.com/404")
        self.assertTrue(result.startswith("[ERROR 404]"))

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_transient_prefix_on_429(self, mock_client_cls):
        self._mock_response(mock_client_cls, 429, "Too Many", "text/html")
        result = http_request("https://example.com")
        self.assertTrue(result.startswith("[TRANSIENT]"))

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_transient_prefix_on_502(self, mock_client_cls):
        self._mock_response(mock_client_cls, 502, "Bad Gateway", "text/html")
        result = http_request("https://example.com")
        self.assertTrue(result.startswith("[TRANSIENT]"))

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_contains_http_line(self, mock_client_cls):
        """质量标签后仍有 [HTTP nnn] 元数据行。"""
        self._mock_response(mock_client_cls, 200, "ok", "text/plain")
        result = http_request("https://example.com")
        self.assertIn("[HTTP 200]", result)

    def _mock_response(self, mock_cls, status, text, ct):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.text = text
        mock_resp.headers = {"content-type": ct}
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__enter__.return_value = mock_client


# ---------------------------------------------------------------------------
# 智能重试（升级阶梯）
# ---------------------------------------------------------------------------


class TestAutoRetry(unittest.TestCase):
    """验证反爬虫时工具内部自动升级重试。"""

    @patch("src.agent.builtin_tools._load_domain_state", return_value={})
    @patch("src.agent.builtin_tools.httpx.Client")
    def test_auto_retry_on_first_403(self, mock_client_cls, _mock_load):
        """第一次 403 → 工具自动增强头重试一次。"""
        mock_client = MagicMock()
        resp1 = MagicMock()
        resp1.status_code = 403
        resp1.text = "Forbidden"
        resp1.headers = {"content-type": "text/html"}
        resp2 = MagicMock()
        resp2.status_code = 403
        resp2.text = "Forbidden"
        resp2.headers = {"content-type": "text/html"}
        mock_client.request.side_effect = [resp1, resp2]
        mock_client_cls.return_value.__enter__.return_value = mock_client

        result = http_request("https://blocked.example.com/page")
        self.assertEqual(mock_client.request.call_count, 2)

    @patch("src.agent.builtin_tools._load_domain_state", return_value={})
    @patch("src.agent.builtin_tools.httpx.Client")
    def test_retry_succeeds_second_time(self, mock_client_cls, _mock_load):
        """Tier 1 403 → Tier 2 增强头成功 → 返回 [OK]。"""
        mock_client = MagicMock()
        resp1 = MagicMock()
        resp1.status_code = 403
        resp1.text = "Forbidden"
        resp1.headers = {"content-type": "text/html"}
        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.text = "success"
        resp2.headers = {"content-type": "text/plain"}
        mock_client.request.side_effect = [resp1, resp2]
        mock_client_cls.return_value.__enter__.return_value = mock_client

        result = http_request("https://example.com")
        self.assertTrue(result.startswith("[OK]"))
        self.assertIn("success", result)

    @patch("src.agent.builtin_tools._load_domain_state", return_value={})
    @patch("src.agent.builtin_tools.httpx.Client")
    def test_no_auto_retry_for_404(self, mock_client_cls, _mock_load):
        """404 不触发自动重试。"""
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "Not Found"
        resp.headers = {"content-type": "text/html"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        result = http_request("https://example.com/nonexistent")
        self.assertEqual(mock_client.request.call_count, 1)
        self.assertTrue(result.startswith("[ERROR 404]"))


# ---------------------------------------------------------------------------
# 域名状态
# ---------------------------------------------------------------------------


class TestDomainState(unittest.TestCase):
    """验证域名 Cookie/Referer 持久化与注入。"""

    @patch("src.agent.builtin_tools.httpx.Client")
    @patch("src.agent.builtin_tools._load_domain_state")
    def test_domain_state_cookie_injected(self, mock_load, mock_client_cls):
        """域名缓存中的 Cookie 自动注入到请求头。"""
        mock_load.return_value = {
            "example.com": {"cookie": "session=abc123", "last_success": "2026-01-01T00:00:00"}
        }
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "text/plain"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com/page")

        call = mock_client.request.call_args
        headers = call.kwargs.get("headers", {})
        self.assertIn("Cookie", headers)
        self.assertEqual(headers["Cookie"], "session=abc123")

    @patch("src.agent.builtin_tools.httpx.Client")
    @patch("src.agent.builtin_tools._load_domain_state")
    def test_no_cache_skips_domain_state(self, mock_load, mock_client_cls):
        """no_cache=True 时不注入域名缓存头。"""
        mock_load.return_value = {
            "example.com": {"cookie": "session=abc123"}
        }
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "text/plain"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com/page", no_cache=True)

        call = mock_client.request.call_args
        headers = call.kwargs.get("headers", {})
        self.assertNotIn("Cookie", headers)

    @patch("src.agent.builtin_tools.httpx.Client")
    @patch("src.agent.builtin_tools._load_domain_state")
    def test_user_cookie_overrides_cached(self, mock_load, mock_client_cls):
        """用户显式传 Cookie 时覆盖缓存。"""
        mock_load.return_value = {
            "example.com": {"cookie": "cached=old"}
        }
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "text/plain"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com/page",
                     headers={"Cookie": "explicit=new"})

        call = mock_client.request.call_args
        headers = call.kwargs.get("headers", {})
        self.assertEqual(headers["Cookie"], "explicit=new")


# ---------------------------------------------------------------------------
# 截断 + 元数据
# ---------------------------------------------------------------------------


class TestTruncationAndMetadata(unittest.TestCase):
    """验证截断和元数据行正常。"""

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_short_body_not_truncated(self, mock_client_cls):
        short = "Hello" * 20
        self._mock(mock_client_cls, 200, short, "text/plain")
        result = http_request("https://example.com")
        self.assertIn(short, result)
        self.assertNotIn("截断", result)

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_long_body_truncated(self, mock_client_cls):
        long_body = "x" * 60000
        self._mock(mock_client_cls, 200, long_body, "text/plain")
        result = http_request("https://example.com/large")
        self.assertIn("截断", result)

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_content_type_in_output(self, mock_client_cls):
        self._mock(mock_client_cls, 200, "{}", "application/json")
        result = http_request("https://example.com/api")
        self.assertIn("[Type: application/json]", result)

    def _mock(self, mock_cls, status, text, ct):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        resp.headers = {"content-type": ct}
        mock_client.request.return_value = resp
        mock_cls.return_value.__enter__.return_value = mock_client


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------


class TestRequestHeaders(unittest.TestCase):

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_default_headers_used(self, mock_client_cls):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "text/plain"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com")
        call = mock_client.request.call_args
        headers = call.kwargs.get("headers", {})
        self.assertIn("User-Agent", headers)
        self.assertIn("Mozilla/5.0", headers["User-Agent"])

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_custom_headers_override(self, mock_client_cls):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "text/html"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com",
                     headers={"User-Agent": "CustomBot/1.0", "Cookie": "a=b"})

        call = mock_client.request.call_args
        headers = call.kwargs.get("headers", {})
        self.assertEqual(headers["User-Agent"], "CustomBot/1.0")
        self.assertEqual(headers["Cookie"], "a=b")


# ---------------------------------------------------------------------------
# HTTP 方法与超时
# ---------------------------------------------------------------------------


class TestMethodAndTimeout(unittest.TestCase):

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_method_get_default(self, mock_client_cls):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "text/plain"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com")
        self.assertEqual(mock_client.request.call_args.args[0], "GET")

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_method_post_custom(self, mock_client_cls):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "application/json"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com", method="POST")
        self.assertEqual(mock_client.request.call_args.args[0], "POST")

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_custom_timeout(self, mock_client_cls):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.headers = {"content-type": "text/plain"}
        mock_client.request.return_value = resp
        mock_client_cls.return_value.__enter__.return_value = mock_client

        http_request("https://example.com", timeout=15)
        args, kwargs = mock_client_cls.call_args
        self.assertEqual(kwargs.get("timeout"), 15)


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------


class TestErrorHandling(unittest.TestCase):

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_http_error(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.request.side_effect = OSError("Connection refused")
        mock_client_cls.return_value.__enter__.return_value = mock_client
        result = http_request("https://down.example.com")
        self.assertIn("HTTP 请求失败", result)
        self.assertTrue(result.startswith("[TRANSIENT]"))


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


class TestHelperFunctions(unittest.TestCase):

    def test_extract_domain(self):
        self.assertEqual(_extract_domain("https://www.bilibili.com/v/xxx"), "www.bilibili.com")
        self.assertEqual(_extract_domain("http://example.com/path?a=1"), "example.com")
        self.assertEqual(_extract_domain("not-a-url"), "")

    def test_classify_quality_ok(self):
        self.assertEqual(_classify_quality(200, "normal body"), "OK")
        self.assertEqual(_classify_quality(301, "redirect"), "OK")

    def test_classify_quality_blocked(self):
        self.assertEqual(_classify_quality(403, ""), "BLOCKED")
        self.assertEqual(_classify_quality(412, ""), "BLOCKED")
        self.assertEqual(_classify_quality(200, "请输入验证码"), "BLOCKED")

    def test_classify_quality_error(self):
        self.assertEqual(_classify_quality(404, ""), "ERROR 404")
        self.assertEqual(_classify_quality(410, ""), "ERROR 410")

    def test_classify_quality_transient(self):
        self.assertEqual(_classify_quality(429, ""), "TRANSIENT")
        self.assertEqual(_classify_quality(502, ""), "TRANSIENT")
        self.assertEqual(_classify_quality(503, ""), "TRANSIENT")

    def test_build_enhanced_headers_adds_referer(self):
        enhanced = _build_enhanced_headers("https://example.com/page", {})
        self.assertIn("Referer", enhanced)
        self.assertEqual(enhanced["Referer"], "https://example.com/")
        self.assertIn("Origin", enhanced)
        self.assertEqual(enhanced["Origin"], "https://example.com")

    def test_build_enhanced_headers_respects_existing(self):
        enhanced = _build_enhanced_headers(
            "https://example.com/page",
            {"Referer": "https://other.com/"},
        )
        self.assertEqual(enhanced["Referer"], "https://other.com/")

    def test_build_enhanced_headers_injects_domain_state(self):
        ds = {"example.com": {"cookie": "x=1", "referer": "https://example.com/"}}
        enhanced = _build_enhanced_headers("https://example.com/page", {}, ds)
        self.assertEqual(enhanced["Cookie"], "x=1")


# ---------------------------------------------------------------------------
# curl_cffi 可选后端
# ---------------------------------------------------------------------------


class TestCurlCffiFallback(unittest.TestCase):

    def setUp(self):
        # 保存原始值
        import src.agent.builtin_tools as bt
        self._orig_cffi_avail = bt._CURL_CFFI_AVAILABLE
        self._orig_cf_requests = getattr(bt, "curl_requests", None)

    def tearDown(self):
        import src.agent.builtin_tools as bt
        bt._CURL_CFFI_AVAILABLE = self._orig_cffi_avail
        if self._orig_cf_requests is not None:
            bt.curl_requests = self._orig_cf_requests

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_curl_cffi_used_when_httpx_blocked(self, mock_httpx_cls):
        """httpx 被拦 → 自动尝试 curl_cffi。"""
        import src.agent.builtin_tools as bt
        bt._CURL_CFFI_AVAILABLE = True

        # 注入 mock curl_requests
        mock_cf = MagicMock()
        cf_resp = MagicMock()
        cf_resp.status_code = 200
        cf_resp.text = "success via cffi"
        cf_resp.headers = {"content-type": "text/html"}
        mock_cf.get.return_value = cf_resp
        bt.curl_requests = mock_cf

        # httpx 返回 403
        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "Forbidden"
        resp.headers = {"content-type": "text/html"}
        mock_client.request.return_value = resp
        mock_httpx_cls.return_value.__enter__.return_value = mock_client

        result = http_request("https://blocked.example.com")
        self.assertTrue(result.startswith("[OK]"))
        self.assertIn("success via cffi", result)
        mock_cf.get.assert_called_once()

    @patch("src.agent.builtin_tools.httpx.Client")
    def test_curl_cffi_not_available(self, mock_httpx_cls):
        """curl_cffi 不可用时跳过 Tier 3。"""
        import src.agent.builtin_tools as bt
        bt._CURL_CFFI_AVAILABLE = False

        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "Forbidden"
        resp.headers = {"content-type": "text/html"}
        mock_client.request.return_value = resp
        mock_httpx_cls.return_value.__enter__.return_value = mock_client

        result = http_request("https://blocked.example.com")
        self.assertTrue(result.startswith("[BLOCKED]"))
        self.assertEqual(mock_client.request.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
