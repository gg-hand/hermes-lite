"""OpenAICompatBackend 清理孤立 tool_result 单元测试 — Phase 9 Task 6 方案 B。

验证 :meth:`OpenAICompatBackend._clean_orphan_tool_results` 与
:meth:`OpenAICompatBackend._convert_messages` 的边界清理行为，覆盖：

1. test_normal_messages_not_cleaned            - 正常消息列表不被清理
2. test_orphan_tool_result_block_removed       - 含孤立 tool_result 块被清理
3. test_matching_tool_result_retained          - tool_use_id 匹配的 tool_result 保留
4. test_orphan_tool_result_logs_warning        - 清理时记录 warning 日志
5. test_whole_orphan_user_message_dropped      - 整条消息全是孤立 tool_result 时整体跳过
6. test_partial_orphan_blocks_keeps_valid      - 同一 user 消息混合有效+孤立块时只删孤立
7. test_convert_messages_invokes_cleanup       - _convert_messages 入口自动调用清理
8. test_original_messages_not_mutated          - 原消息列表不被修改（浅拷贝）
9. test_string_content_user_messages_preserved - 纯文本 user 消息原样保留
10. test_assistant_text_only_preserved         - assistant 纯文本消息原样保留

运行方式：
    python -m pytest tests/test_client.py -v
    python -m unittest tests.test_client -v
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.llm.client import OpenAICompatBackend  # noqa: E402


# ---------------------------------------------------------------------------
# 测试数据构造辅助
# ---------------------------------------------------------------------------

def make_assistant_tool_use(tool_use_id: str, name: str = "search") -> Dict[str, Any]:
    """构造 assistant(tool_use) 消息。"""
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "调用工具"},
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": name,
                "input": {"q": "test"},
            },
        ],
    }


def make_user_tool_result(tool_use_id: str, result: str = "ok") -> Dict[str, Any]:
    """构造 user(tool_result) 消息。"""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result,
            }
        ],
    }


def make_user_text(text: str) -> Dict[str, Any]:
    """构造纯文本 user 消息（content 为 str）。"""
    return {"role": "user", "content": text}


def make_assistant_text(text: str) -> Dict[str, Any]:
    """构造纯文本 assistant 消息（content 为 str）。"""
    return {"role": "assistant", "content": text}


def _build_backend() -> OpenAICompatBackend:
    """构造一个 OpenAICompatBackend 实例（mock openai SDK 客户端）。

    实例化时仅需 api_key 字符串，不依赖真实网络。
    """
    return OpenAICompatBackend(
        model="deepseek-chat",
        api_key="test-key-not-used",
        base_url="https://api.deepseek.com/v1",
        provider_name="deepseek",
    )


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


class TestCleanOrphanToolResults(unittest.TestCase):
    """验证 _clean_orphan_tool_results 静态方法。"""

    def test_normal_messages_not_cleaned(self):
        """正常消息列表（无孤立 tool_result）不被清理。

        场景：[user_text, assistant_tool_use, user_tool_result(匹配)] 应原样保留。
        """
        messages = [
            make_user_text("你好"),
            make_assistant_tool_use("tu_valid_1"),
            make_user_tool_result("tu_valid_1", "结果"),
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        # 数量不变
        self.assertEqual(len(cleaned), len(messages))
        # 各消息 role 顺序保持
        self.assertEqual([m["role"] for m in cleaned], ["user", "assistant", "user"])

    def test_orphan_tool_result_block_removed(self):
        """含孤立 tool_result 块（tool_use_id 不匹配）被清理。

        场景：user 消息含 tool_result 块，但其 tool_use_id 不在任何
        assistant tool_use 块中，应被删除。
        """
        messages = [
            make_user_text("开始"),
            # 注意：没有对应的 assistant tool_use 消息
            make_user_tool_result("tu_orphan_1", "孤立结果"),
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        # 第 1 条 user_text 保留
        self.assertEqual(cleaned[0]["role"], "user")
        self.assertEqual(cleaned[0]["content"], "开始")
        # 第 2 条整条被跳过（content 全是孤立 tool_result）
        self.assertEqual(len(cleaned), 1)

    def test_matching_tool_result_retained(self):
        """tool_use_id 匹配的 tool_result 保留。"""
        messages = [
            make_assistant_tool_use("tu_match_1"),
            make_user_tool_result("tu_match_1", "有效结果"),
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        # 两条都保留
        self.assertEqual(len(cleaned), 2)
        # tool_result 块仍在
        result_msg = cleaned[1]
        self.assertEqual(result_msg["role"], "user")
        content = result_msg["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "tool_result")
        self.assertEqual(content[0]["tool_use_id"], "tu_match_1")

    def test_orphan_tool_result_logs_warning(self):
        """清理孤立 tool_result 时记录 warning 日志。"""
        messages = [
            make_user_tool_result("tu_orphan_warn", "孤立"),
        ]
        with self.assertLogs(
            "hermes.llm.client", level="WARNING"
        ) as cm:
            OpenAICompatBackend._clean_orphan_tool_results(messages)
        # 至少一条 warning 包含 "清理孤立 tool_result"
        joined = "\n".join(cm.output)
        self.assertIn("清理孤立 tool_result", joined)
        # 包含 tool_use_id
        self.assertIn("tu_orphan_warn", joined)

    def test_whole_orphan_user_message_dropped(self):
        """整条 user 消息全是孤立 tool_result 时整体跳过。"""
        messages = [
            make_user_text("前面"),
            # 整条消息含 2 个孤立 tool_result 块
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "orphan_a", "content": "a"},
                    {"type": "tool_result", "tool_use_id": "orphan_b", "content": "b"},
                ],
            },
            make_user_text("后面"),
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        # 中间整条被跳过：仅保留前后 2 条
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["content"], "前面")
        self.assertEqual(cleaned[1]["content"], "后面")

    def test_partial_orphan_blocks_keeps_valid(self):
        """同一 user 消息混合有效 + 孤立 tool_result 块时只删孤立块。

        场景：user 消息含 2 个 tool_result 块，1 个匹配、1 个孤立。
        应保留匹配块、删除孤立块，整条消息保留。
        """
        messages = [
            make_assistant_tool_use("tu_valid_partial"),
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_valid_partial", "content": "有效"},
                    {"type": "tool_result", "tool_use_id": "tu_orphan_partial", "content": "孤立"},
                ],
            },
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        # 两条消息都保留（assistant + user）
        self.assertEqual(len(cleaned), 2)
        # user 消息仅保留 1 个 tool_result 块（有效的那个）
        user_msg = cleaned[1]
        self.assertEqual(user_msg["role"], "user")
        content = user_msg["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["tool_use_id"], "tu_valid_partial")

    def test_string_content_user_messages_preserved(self):
        """纯文本 user 消息（content 为 str）原样保留，不受清理影响。"""
        messages = [
            make_user_text("纯文本 1"),
            make_assistant_text("回复"),
            make_user_text("纯文本 2"),
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        self.assertEqual(len(cleaned), 3)
        self.assertEqual([m["content"] for m in cleaned], ["纯文本 1", "回复", "纯文本 2"])

    def test_assistant_text_only_preserved(self):
        """assistant 纯文本消息（content 为 str 或仅 text 块）原样保留。"""
        messages = [
            {"role": "assistant", "content": "纯文本回复"},
            {"role": "assistant", "content": [{"type": "text", "text": "块文本"}]},
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        self.assertEqual(len(cleaned), 2)

    def test_original_messages_not_mutated(self):
        """清理后的原消息列表与各 dict 不被修改（浅拷贝语义）。"""
        original_messages = [
            make_assistant_tool_use("tu_orig"),
            make_user_tool_result("tu_orphan_orig", "孤立"),
        ]
        # 深拷贝一份用于事后比对
        import copy
        snapshot = copy.deepcopy(original_messages)

        cleaned = OpenAICompatBackend._clean_orphan_tool_results(original_messages)
        # 原列表长度不变
        self.assertEqual(len(original_messages), 2)
        # 原列表内容未被修改
        self.assertEqual(original_messages, snapshot)
        # 清理后的列表是新的对象
        self.assertIsNot(cleaned, original_messages)

    def test_multiple_tool_use_ids_all_collected(self):
        """多个 assistant tool_use 块的 id 都被收集，对应 tool_result 都保留。"""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_multi_1", "name": "n1", "input": {}},
                    {"type": "tool_use", "id": "tu_multi_2", "name": "n2", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_multi_1", "content": "r1"},
                    {"type": "tool_result", "tool_use_id": "tu_multi_2", "content": "r2"},
                ],
            },
        ]
        cleaned = OpenAICompatBackend._clean_orphan_tool_results(messages)
        # 两条都保留
        self.assertEqual(len(cleaned), 2)
        # user 消息的两个 tool_result 块都保留
        user_content = cleaned[1]["content"]
        self.assertEqual(len(user_content), 2)


class TestConvertMessagesInvokesCleanup(unittest.TestCase):
    """验证 _convert_messages 入口自动调用清理。"""

    def test_convert_messages_invokes_cleanup(self):
        """_convert_messages 在转换前自动调用清理，孤立 tool_result 不会进入 OpenAI 消息。

        场景：构造含孤立 tool_result 的 Anthropic 风格消息，调用 _convert_messages，
        验证输出的 OpenAI 消息中不含 role=tool 的孤立条目。
        """
        backend = _build_backend()
        messages = [
            make_user_text("开始"),
            # 孤立 tool_result（无对应 assistant tool_use）
            make_user_tool_result("tu_orphan_in_convert", "孤立"),
        ]
        openai_messages = backend._convert_messages(messages, system="你是助手")
        # 第 1 条应是 system
        self.assertEqual(openai_messages[0]["role"], "system")
        # 不应出现 role=tool 的孤立消息
        tool_msgs = [m for m in openai_messages if m.get("role") == "tool"]
        self.assertEqual(
            len(tool_msgs), 0,
            f"不应残留 role=tool 的孤立消息，实际: {openai_messages}",
        )

    def test_convert_messages_keeps_valid_tool_pair(self):
        """_convert_messages 保留有效 tool_use/tool_result 配对。"""
        backend = _build_backend()
        messages = [
            make_assistant_tool_use("tu_keep_1"),
            make_user_tool_result("tu_keep_1", "结果"),
        ]
        openai_messages = backend._convert_messages(messages, system=None)
        # 应有 1 条 assistant(tool_calls) + 1 条 tool
        assistant_msgs = [m for m in openai_messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in openai_messages if m.get("role") == "tool"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(len(tool_msgs), 1)
        # tool 消息的 tool_call_id 与 assistant tool_calls 的 id 匹配
        self.assertEqual(
            tool_msgs[0]["tool_call_id"],
            assistant_msgs[0]["tool_calls"][0]["id"],
        )

    def test_convert_messages_logs_warning_for_orphan(self):
        """_convert_messages 清理孤立 tool_result 时记录 warning。"""
        backend = _build_backend()
        messages = [
            make_user_tool_result("tu_orphan_log", "孤立"),
        ]
        with self.assertLogs("hermes.llm.client", level="WARNING") as cm:
            backend._convert_messages(messages, system=None)
        joined = "\n".join(cm.output)
        self.assertIn("清理孤立 tool_result", joined)
        self.assertIn("tu_orphan_log", joined)


class TestOrphanCleanupRegression(unittest.TestCase):
    """回归保护：确认清理逻辑对 logger 级别无副作用。"""

    def test_logger_level_unchanged(self):
        """调用清理函数后 src.llm.client logger 级别不被修改。"""
        backend = _build_backend()
        logger = logging.getLogger("hermes.llm.client")
        original_level = logger.level
        messages = [make_user_tool_result("tu_reg", "孤立")]
        backend._convert_messages(messages, system=None)
        self.assertEqual(logger.level, original_level)


if __name__ == "__main__":
    unittest.main(verbosity=2)
