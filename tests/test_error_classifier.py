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
        """确保 12 种 ErrorClass 全部定义（6 旧 + 5 新 Phase D + 1 reasoning Task 16）。"""
        expected = {
            "SUCCESS", "TRANSIENT", "ANTI_CRAWLER", "PERMANENT",
            "AUTH_REQUIRED", "UNKNOWN",
            "PARAM_ERROR", "NOT_FOUND", "PERMISSION", "TIMEOUT", "INTERNAL_ERROR",
            "REASONING_CONFIG_INVALID",
        }
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


# ---------------------------------------------------------------------------
# 误判防护：源码字面量 / 文档中部关键词 不应被识别为工具错误
# ---------------------------------------------------------------------------


class TestMisjudgmentPrevention(unittest.TestCase):
    """验证三重防护避免对工具返回的大段源码/文档误判。

    防护 1：扫描窗口限制（仅前 1000 字符）
    防护 2：行首约束（关键词必须在行首或紧邻简短主语前缀）
    防护 3：引号内字面量排除（关键词在引号内视为源码字面量）
    """

    # --- 防护 3：引号内字面量排除 ---

    def test_source_code_string_literal_chinese(self):
        """源码中 ``"文件元数据不存在"`` 字面量不应被识别为 PERMANENT。

        复现场景：file_read 读取 etl_engine.py 时，源码第 99 行含
        ``return {"status": "failed", "error": "文件元数据不存在"}``，
        关键词 ``不存在`` 在引号内字面量中。
        """
        source = (
            '        meta = self.upload_manager.get_metadata(file_id)\n'
            '        if meta is None:\n'
            '            return {"status": "failed", "chunk_count": 0, "error": "文件元数据不存在"}\n'
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(
            ec, ErrorClass.UNKNOWN,
            "源码中引号内字面量不应被误判为 PERMANENT",
        )

    def test_source_code_string_literal_english(self):
        """源码中 ``"path not found"`` 字面量不应被识别为 PERMANENT。"""
        source = (
            'def handle_error(err):\n'
            '    msg = "path not found in config"\n'
            '    log.warning(msg)\n'
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_source_code_single_quoted_literal(self):
        """单引号字面量 ``'文件不存在'`` 不应被识别为 PERMANENT。"""
        source = "    raise ValueError('文件不存在')\n"
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_source_code_backtick_literal(self):
        """反引号字面量 ```not found``` 不应被识别为 PERMANENT。"""
        source = "    # docstring says `not found` is the error signal\n"
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    # --- 防护 2：行首约束 ---

    def test_keyword_in_middle_of_line_not_identified(self):
        """关键词在行中段（非行首、非主语前缀）不应被识别为 PERMANENT。"""
        # 关键词前是普通代码字符（非空白/非简短主语），不应被识别
        source = "    return process_error_message_when_文件不存在_occurs()\n"
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_keyword_in_long_sentence_not_identified(self):
        """文档句子中嵌入的关键词不应被识别为 PERMANENT。"""
        source = "本节描述当系统遇到文件元数据不存在的情况时如何处理。\n"
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_real_tool_error_at_line_start_identified(self):
        """真实工具错误 ``文件不存在: <path>`` 应被识别为 PERMANENT。"""
        # builtin_tools.py 实际返回的错误格式
        ec, _ = ErrorClassifier.classify(
            "file_delete", {"path": "x"},
            "文件不存在: /nonexistent/path",
        )
        self.assertIs(ec, ErrorClass.PERMANENT)

    def test_real_tool_error_with_subject_prefix(self):
        """主语前缀 + 关键词（如 ``路径: not found``）应被识别为 PERMANENT。"""
        ec, _ = ErrorClassifier.classify(
            "file_listdir", {"path": "x"},
            "路径: not found: /missing",
        )
        self.assertIs(ec, ErrorClass.PERMANENT)

    def test_real_tool_error_pure_keyword(self):
        """纯关键词行 ``文件不存在`` 应被识别为 PERMANENT（向后兼容）。"""
        ec, _ = ErrorClassifier.classify("unknown_tool", {}, "文件不存在")
        self.assertIs(ec, ErrorClass.PERMANENT)

    def test_real_tool_error_with_colon(self):
        """``文件不存在: xxx`` 应被识别为 PERMANENT。"""
        ec, _ = ErrorClassifier.classify("unknown_tool", {}, "文件不存在: xxx")
        self.assertIs(ec, ErrorClass.PERMANENT)

    def test_english_real_tool_error(self):
        """英文工具错误 ``path not found`` 行首应被识别为 PERMANENT。"""
        ec, _ = ErrorClassifier.classify("unknown_tool", {}, "path not found")
        self.assertIs(ec, ErrorClass.PERMANENT)

    # --- 防护 1：扫描窗口限制 ---

    def test_keyword_beyond_scan_window_not_identified(self):
        """关键词在 1000 字符之后不应被识别为 PERMANENT。"""
        # 1000+ 字符的普通文本，末尾才出现 ``不存在``
        padding = "x" * 1100 + "\n"
        source = padding + "文件不存在\n"
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_keyword_within_scan_window_identified(self):
        """关键词在 1000 字符之内应被识别为 PERMANENT。"""
        # 800 字符的普通文本 + 行首 ``文件不存在``
        padding = "x" * 800 + "\n"
        source = padding + "文件不存在: /xxx\n"
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.PERMANENT)

    # --- 综合场景 ---

    def test_full_source_file_with_literal_in_middle(self):
        """读取整段源码文件，含引号内 ``不存在`` 字面量，不应误判。"""
        # 模拟 etl_engine.py 真实场景：方法注释 + 字面量 + 后续代码
        source = (
            '"""ETL 管道编排引擎。\n\n'
            '协调文件解析 → 分块 → ChromaDB 向量写入。\n'
            '"""\n\n'
            'from __future__ import annotations\n\n'
            'class ETLEngine:\n'
            '    def process(self, file_id):\n'
            '        meta = self.upload_manager.get_metadata(file_id)\n'
            '        if meta is None:\n'
            '            return {"status": "failed", "error": "文件元数据不存在"}\n'
            '        return {"status": "ok"}\n'
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(
            ec, ErrorClass.UNKNOWN,
            "完整源码文件含引号内字面量不应被误判为 PERMANENT",
        )

    def test_bash_output_with_quoted_keyword(self):
        """bash_exec 输出含引号内关键词，不应误判为 PERMANENT。"""
        # 模拟 cat config.json 的输出
        output = '{\n  "error_msg": "文件不存在",\n  "code": 42\n}\n'
        ec, _ = ErrorClassifier.classify("bash_exec", {"command": "cat"}, output)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_tool_error_at_start_with_quoted_literal_later(self):
        """工具错误在开头 + 后续引号内字面量，应识别为 PERMANENT（开头优先）。"""
        output = (
            "文件不存在: /etc/config\n"
            'fallback_msg = "原配置文件不存在"\n'
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, output)
        self.assertIs(ec, ErrorClass.PERMANENT)

    # --- _TIMEOUT_RE 三重防护（避免源码字面量误判） ---

    def test_source_code_function_signature_timeout(self):
        """源码中 ``def foo(timeout=60):`` 函数签名不应被识别为 TRANSIENT。

        复现 audit.jsonl 2026-07-05 误判：file_read 读取 skills/deploy/SKILL.md，
        文件含 ``ssh_run_handler(..., timeout=60)`` 函数签名，被旧版 _TIMEOUT_RE 误判。
        """
        source = (
            "### ssh_run_handler(host, username, command, port=22, "
            "password=None, key_path=None, timeout=60) -> dict\n"
            "在远程服务器执行 shell 命令。\n"
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(
            ec, ErrorClass.UNKNOWN,
            "源码中函数签名 timeout=60 不应被误判为 TRANSIENT",
        )

    def test_source_code_timeout_in_string_literal(self):
        """源码中 ``raise ValueError("操作超时")`` 不应被识别为 TRANSIENT。"""
        source = (
            'def handle():\n'
            '    raise ValueError("操作超时")\n'
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_source_code_timeout_constant_assignment(self):
        """源码中 ``DEFAULT_TIMEOUT = 60`` 不应被识别为 TRANSIENT。"""
        source = (
            "# 默认超时配置（秒）\n"
            "DEFAULT_CMD_TIMEOUT = 60\n"
            "DEFAULT_SCP_TIMEOUT = 120\n"
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_real_timeout_error_at_line_start_identified(self):
        """真实超时错误 ``超时：xxx`` 应被识别为 TRANSIENT。"""
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "x"},
            "超时：命令执行超过 60 秒",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT)

    def test_real_timeout_english_at_line_start(self):
        """英文 ``timeout: xxx`` 行首应被识别为 TRANSIENT。"""
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "x"},
            "timeout: command exceeded 60s",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT)

    # --- "command not found" 三重防护 ---

    def test_source_code_command_not_found_in_comment(self):
        """源码注释 ``# command not found handler`` 不应被识别为 TRANSIENT。"""
        source = (
            "# command not found handler\n"
            "def handle_cmd_not_found(e):\n"
            "    pass\n"
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_source_code_command_not_found_in_string(self):
        """源码字符串 ``"command not found"`` 不应被识别为 TRANSIENT。"""
        source = (
            'msg = "command not found in path"\n'
            'log.warning(msg)\n'
        )
        ec, _ = ErrorClassifier.classify("file_read", {"path": "x"}, source)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_real_command_not_found_at_line_start(self):
        """真实错误 ``command not found`` 行首应被识别为 TRANSIENT。"""
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "nonexistent_cmd"},
            "bash: nonexistent_cmd: command not found",
        )
        self.assertIs(ec, ErrorClass.TRANSIENT)


# ---------------------------------------------------------------------------
# Phase D：ToolError 系统兼容 + 新关键词
# ---------------------------------------------------------------------------


class TestPhaseDCompatibility(unittest.TestCase):
    """Phase D 新增：[失败] 短路 + PARAM_ERROR/PERMISSION 关键词 + DEPRECATED 标记。"""

    def test_failure_prefix_short_circuits_to_unknown(self):
        """[失败] 前缀的 receipt 文本应短路返回 UNKNOWN，避免与 ToolError 路径重复计数。

        见边界 9.15：ErrorClassifier 不应二次分类 ToolError receipt。
        """
        receipt = (
            "[失败] 内部错误\n"
            "原因：RuntimeError: tool broken\n"
            "建议：检查工具实现"
        )
        ec, reason = ErrorClassifier.classify("any_tool", {}, receipt)
        self.assertIs(ec, ErrorClass.UNKNOWN, f"Expected UNKNOWN, got {ec}: {reason}")

    def test_failure_prefix_with_not_found_keyword_still_unknown(self):
        """[失败] receipt 含 'not found' 关键词也不应被识别为 PERMANENT。"""
        receipt = (
            "[失败] 资源不存在\n"
            "原因：FileNotFoundError: not found: /xxx\n"
            "建议：检查路径"
        )
        ec, _ = ErrorClassifier.classify("any_tool", {}, receipt)
        self.assertIs(ec, ErrorClass.UNKNOWN)

    def test_param_error_unexpected_keyword_argument(self):
        """Python 'got an unexpected keyword argument' 应识别为 PARAM_ERROR。"""
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "python -c"},
            "TypeError: get_weather() got an unexpected keyword argument 'foo'",
        )
        self.assertIs(ec, ErrorClass.PARAM_ERROR)

    def test_param_error_schema_validation_chinese(self):
        """中文 '参数校验失败' 应识别为 PARAM_ERROR。"""
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "x"},
            "参数校验失败：缺少必填字段 path",
        )
        self.assertIs(ec, ErrorClass.PARAM_ERROR)

    def test_permission_denied_chinese(self):
        """中文 '权限不足' 应识别为 PERMISSION。"""
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "rm /root/file"},
            "权限不足：无法删除 /root/file",
        )
        self.assertIs(ec, ErrorClass.PERMISSION)

    def test_permission_denied_english(self):
        """英文 'permission denied' 应识别为 PERMISSION。"""
        ec, _ = ErrorClassifier.classify(
            "bash_exec", {"command": "cat /etc/shadow"},
            "cat: /etc/shadow: Permission denied",
        )
        self.assertIs(ec, ErrorClass.PERMISSION)

    def test_deprecated_flag_is_true(self):
        """DEPRECATED 标记位应为 True（Phase E 下线判定用）。"""
        from src.agent.error_classifier import DEPRECATED
        self.assertTrue(DEPRECATED, "DEPRECATED should be True after Phase D")


if __name__ == "__main__":
    unittest.main(verbosity=2)
