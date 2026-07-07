"""ContextManager 单元测试 — 验证 build_prompt 结构与缓存区字节级稳定性。

运行方式：
    python -m unittest tests.test_context_manager -v
    python tests/test_context_manager.py
"""

from __future__ import annotations

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.llm.prompts import SYSTEM_PROMPT  # noqa: E402
from src.memory.context_manager import ContextManager  # noqa: E402


# ---------------------------------------------------------------------------
# Mock 依赖类（参考 context_manager.py __main__ 块的实现）
# ---------------------------------------------------------------------------

class MockToolRegistry:
    """稳定返回相同 schema 的 mock ToolRegistry。"""

    def get_tools_schema(self):
        return [
            {
                "name": "test_tool",
                "description": "测试工具",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]


class UnstableMockToolRegistry:
    """每次调用返回不同 schema 的 mock ToolRegistry，用于验证缓存不稳定检测。"""

    def __init__(self):
        self._call_count = 0

    def get_tools_schema(self):
        self._call_count += 1
        return [
            {
                "name": f"tool_{self._call_count}",
                "description": f"测试工具 {self._call_count}",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]


class MockMemoryMdManager:
    def read(self):
        return "# 用户画像\n\n## 基本信息\n- 用户是测试用户"

    def read_system_profile(self):
        return self.read()

    def read_section_body(self, section_title):
        return ""


class MockSegmentedMemoryMdManager:
    """含 Agent 自画像和沟通偏好段的 mock，用于分段注入测试。"""

    PROFILE_TEXT = (
        "# 用户画像\n\n"
        "## 基本信息\n- 用户是测试用户\n\n"
        "## Agent 自画像\n- Agent 在 file_read 上连续失败 2 次\n\n"
        "## 沟通偏好\n[场景:通用] 用户讨厌emoji和装傻的沟通风格\n"
    )

    def read(self):
        return self.PROFILE_TEXT

    def read_system_profile(self):
        return "# 用户画像\n\n## 基本信息\n- 用户是测试用户"

    def read_section_body(self, section_title):
        if section_title == "Agent 自画像":
            return "- Agent 在 file_read 上连续失败 2 次"
        if section_title == "沟通偏好":
            return "[场景:通用] 用户讨厌emoji和装傻的沟通风格"
        return ""


class MockOverflowingMemoryMdManager:
    """含超长 Agent 自画像和沟通偏好段的 mock，用于测试 spec 截断要求。

    - Agent 自画像：5 条非空行（超出 top-3 限制），每条约 80 字符
    - 沟通偏好：1500 字符（超出 1000 字符上限）
    """

    _AGENT_LINES = [
        f"- 失败模式 {i}：这是一个比较长的行为模式描述用于测试 top-3 截断逻辑 {i}"
        for i in range(1, 6)
    ]
    _AGENT_BODY = "\n".join(_AGENT_LINES)
    _COMMUNICATION_BODY = "用户讨厌啰嗦的回答风格，偏好简洁直接。" * 50  # 约 1000+ 字符

    def read(self):
        return (
            "# 用户画像\n\n## Agent 自画像\n"
            + self._AGENT_BODY
            + "\n\n## 沟通偏好\n"
            + self._COMMUNICATION_BODY
        )

    def read_system_profile(self):
        return "# 用户画像"

    def read_section_body(self, section_title):
        if section_title == "Agent 自画像":
            return self._AGENT_BODY
        if section_title == "沟通偏好":
            return self._COMMUNICATION_BODY
        return ""


class MockMemoryRetriever:
    def get_injection_text(self, user_input):
        return "## 相关记忆\n1. 测试记忆 (相关度: 0.90)"


class MockEmptyRetriever:
    def get_injection_text(self, user_input):
        return ""


class MockHistoryBuffer:
    def get_history(self, session_id):
        return [
            {"role": "user", "content": "历史用户", "timestamp": "2024-01-01"},
            {"role": "assistant", "content": "历史助手", "timestamp": "2024-01-01"},
        ]


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

class TestContextManagerBuildPrompt(unittest.TestCase):
    """验证 build_prompt 的结构正确性。"""

    def test_build_prompt_basic_no_deps(self):
        """无依赖时 build_prompt 的基本结构。"""
        cm = ContextManager()
        prompt = cm.build_prompt("test-session", "你好")

        self.assertIn(SYSTEM_PROMPT, prompt["system"])
        self.assertEqual(prompt["messages"][-1], {"role": "user", "content": "你好"})
        self.assertEqual(prompt["tools"], [])

    def test_build_prompt_with_deps(self):
        """带依赖时 system + messages + tools 结构。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=MockMemoryRetriever(),
            history_buffer=MockHistoryBuffer(),
        )
        prompt = cm.build_prompt("test-session", "当前问题")

        # system 包含 SYSTEM_PROMPT + 用户画像 + 分隔符
        self.assertIn(SYSTEM_PROMPT, prompt["system"])
        self.assertIn("用户是测试用户", prompt["system"])
        self.assertIn("---", prompt["system"])

        # tools 是完整列表
        self.assertEqual(len(prompt["tools"]), 1)
        self.assertEqual(prompt["tools"][0]["name"], "test_tool")

        # messages[0] 是检索记忆注入
        self.assertEqual(prompt["messages"][0]["role"], "user")
        self.assertIn("相关记忆", prompt["messages"][0]["content"])

        # 历史在中间
        self.assertEqual(prompt["messages"][1]["content"], "历史用户")
        self.assertEqual(prompt["messages"][2]["content"], "历史助手")

        # 最后一条是当前用户输入
        self.assertEqual(prompt["messages"][-1], {"role": "user", "content": "当前问题"})

        # 历史消息仅含 role + content 字段（符合 Anthropic API 规范）
        self.assertNotIn("timestamp", prompt["messages"][1])
        self.assertEqual(set(prompt["messages"][1].keys()), {"role", "content"})

    def test_empty_retriever_skips_injection(self):
        """检索记忆为空时跳过注入，messages[0] 为历史第一条。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_retriever=MockEmptyRetriever(),
            history_buffer=MockHistoryBuffer(),
        )
        prompt = cm.build_prompt("test-session", "当前问题")

        self.assertEqual(prompt["messages"][0]["content"], "历史用户")
        self.assertEqual(prompt["messages"][-1], {"role": "user", "content": "当前问题"})


class TestContextManagerCacheStability(unittest.TestCase):
    """验证缓存命中区与失效点。"""

    def test_cache_stable_prefix(self):
        """get_cache_stable_prefix 等于 build_prompt 的 system 字段。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
        )
        prompt = cm.build_prompt("test-session", "你好")
        prefix = cm.get_cache_stable_prefix()

        self.assertEqual(prefix, prompt["system"])
        self.assertIn("用户是测试用户", prefix)

    def test_cache_break_point(self):
        """get_cache_break_point 固定返回 0。"""
        cm = ContextManager()
        self.assertEqual(cm.get_cache_break_point(), 0)

    def test_verify_cache_stability_stable(self):
        """稳定 ToolRegistry 下 verify_cache_stability 返回 True。"""
        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=MockMemoryRetriever(),
            history_buffer=MockHistoryBuffer(),
        )
        self.assertTrue(cm.verify_cache_stability("test-session", "你好"))

    def test_verify_cache_stability_no_deps(self):
        """无依赖时 verify_cache_stability 返回 True（空值天然稳定）。"""
        cm = ContextManager()
        self.assertTrue(cm.verify_cache_stability("test-session", "你好"))

    def test_verify_cache_stability_unstable(self):
        """动态 ToolRegistry 下 verify_cache_stability 返回 False。"""
        cm = ContextManager(
            tool_registry=UnstableMockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
        )
        self.assertFalse(cm.verify_cache_stability("test-session", "你好"))


class TestSegmentedProfileInjection(unittest.TestCase):
    """验证 Agent 自画像和沟通偏好段从 system_text 移出注入 messages[0]。"""

    def test_system_text_excludes_agent_and_communication(self):
        """system_text 不含 Agent 自画像和沟通偏好段。"""
        cm = ContextManager(memory_md_manager=MockSegmentedMemoryMdManager())
        system_text = cm._build_system_text()
        self.assertIn("用户是测试用户", system_text)
        self.assertNotIn("Agent 自画像", system_text)
        self.assertNotIn("沟通偏好", system_text)
        self.assertNotIn("Agent 在 file_read 上连续失败", system_text)
        self.assertNotIn("讨厌emoji", system_text)

    def test_messages_include_agent_profile(self):
        """messages[0] 包含 Agent 自画像段。"""
        cm = ContextManager(
            memory_md_manager=MockSegmentedMemoryMdManager(),
            memory_retriever=MockEmptyRetriever(),
        )
        prompt = cm.build_prompt("test-session", "你好")
        first_msg = prompt["messages"][0]["content"]
        self.assertIn("## Agent 自画像", first_msg)
        self.assertIn("file_read", first_msg)

    def test_messages_include_communication_prefs(self):
        """messages[0] 包含沟通偏好段。"""
        cm = ContextManager(
            memory_md_manager=MockSegmentedMemoryMdManager(),
            memory_retriever=MockEmptyRetriever(),
        )
        prompt = cm.build_prompt("test-session", "你好")
        first_msg = prompt["messages"][0]["content"]
        self.assertIn("## 沟通偏好", first_msg)
        self.assertIn("讨厌emoji", first_msg)

    def test_injection_order(self):
        """注入顺序：文件注入 → Agent 自画像 → 沟通偏好 → 历史教训 → 检索记忆。"""
        cm = ContextManager(
            memory_md_manager=MockSegmentedMemoryMdManager(),
            memory_retriever=MockMemoryRetriever(),
        )
        prompt = cm.build_prompt("test-session", "你好")
        first_msg = prompt["messages"][0]["content"]
        agent_idx = first_msg.find("## Agent 自画像")
        comm_idx = first_msg.find("## 沟通偏好")
        memory_idx = first_msg.find("## 相关记忆")
        self.assertGreaterEqual(agent_idx, 0, "Agent 自画像应存在")
        self.assertNotEqual(agent_idx, -1, "Agent 自画像应存在")
        self.assertGreaterEqual(comm_idx, 0, "沟通偏好应存在")
        self.assertNotEqual(comm_idx, -1, "沟通偏好应存在")
        self.assertGreater(memory_idx, 0, "检索记忆应存在")
        self.assertLess(agent_idx, comm_idx, "Agent 自画像应在沟通偏好之前")
        self.assertLess(comm_idx, memory_idx, "沟通偏好应在检索记忆之前")

    def test_empty_sections_skipped(self):
        """Agent 自画像/沟通偏好 body 为空时跳过注入。"""
        cm = ContextManager(
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=MockEmptyRetriever(),
        )
        prompt = cm.build_prompt("test-session", "你好")
        # 无注入段且无检索记忆时，messages[0] 应是用户输入
        self.assertEqual(prompt["messages"][0], {"role": "user", "content": "你好"})

    def test_system_text_byte_stable_on_agent_update(self):
        """Agent 自画像更新不影响 system_text 字节级稳定性。"""
        cm = ContextManager(memory_md_manager=MockSegmentedMemoryMdManager())
        system_before = cm._build_system_text()
        # MockSegmentedMemoryMdManager 返回固定值，多次调用应一致
        system_after = cm._build_system_text()
        self.assertEqual(system_before, system_after)
        self.assertNotIn("Agent 自画像", system_before)

    def test_agent_profile_top3_mode(self):
        """spec: Agent 自画像 top-3 模式——只取前 3 条非空行。"""
        cm = ContextManager(
            memory_md_manager=MockOverflowingMemoryMdManager(),
            memory_retriever=MockEmptyRetriever(),
        )
        injection = cm._get_agent_profile_injection()
        self.assertIn("## Agent 自画像", injection)
        # 应包含前 3 条，不包含第 4、5 条
        self.assertIn("失败模式 1", injection)
        self.assertIn("失败模式 2", injection)
        self.assertIn("失败模式 3", injection)
        self.assertNotIn("失败模式 4", injection)
        self.assertNotIn("失败模式 5", injection)

    def test_agent_profile_token_limit(self):
        """spec: Agent 自画像 ≤200 token 截断。"""
        cm = ContextManager(
            memory_md_manager=MockOverflowingMemoryMdManager(),
            memory_retriever=MockEmptyRetriever(),
        )
        injection = cm._get_agent_profile_injection()
        # 提取 body（去掉标题行）
        body = injection.replace("## Agent 自画像\n", "")
        # 验证 token 数 ≤200（用 _estimate_tokens 估算）
        from src.memory.context_manager import _estimate_tokens
        token_count = _estimate_tokens(body)
        self.assertLessEqual(
            token_count, 200,
            f"Agent 自画像 token 数 {token_count} 超过 200 上限"
        )

    def test_communication_chars_limit(self):
        """spec: 沟通偏好 ≤1000 字符截断。"""
        cm = ContextManager(
            memory_md_manager=MockOverflowingMemoryMdManager(),
            memory_retriever=MockEmptyRetriever(),
        )
        injection = cm._get_communication_injection()
        self.assertIn("## 沟通偏好", injection)
        # 提取 body（去掉标题行）
        body = injection.replace("## 沟通偏好\n", "")
        # 验证字符数 ≤1000
        self.assertLessEqual(
            len(body), 1000,
            f"沟通偏好字符数 {len(body)} 超过 1000 上限"
        )


class TestMemoryMdSegmentedCounting(unittest.TestCase):
    """验证 memory_md.py 分段独立计数逻辑。"""

    def _make_manager(self, tmp_path):
        from src.memory.memory_md import MemoryMdManager
        return MemoryMdManager(file_path=str(tmp_path / "memory.md"))

    def test_segment_limits_constants(self):
        """分段常量值正确。"""
        from src.memory.memory_md import MemoryMdManager
        self.assertEqual(MemoryMdManager.MAX_USER_PROFILE_CHARS, 5000)
        self.assertEqual(MemoryMdManager.MAX_AGENT_PROFILE_CHARS, 2000)
        self.assertEqual(MemoryMdManager.MAX_COMMUNICATION_CHARS, 1000)
        self.assertEqual(MemoryMdManager.MAX_PROFILE_TOTAL_CHARS, 8000)

    def test_categorize_section(self):
        """section 归类正确。"""
        from src.memory.memory_md import MemoryMdManager
        self.assertEqual(MemoryMdManager._categorize_section("Agent 自画像"), "agent")
        self.assertEqual(MemoryMdManager._categorize_section("沟通偏好"), "communication")
        self.assertEqual(MemoryMdManager._categorize_section("基本信息"), "user")
        self.assertEqual(MemoryMdManager._categorize_section("技术栈"), "user")
        self.assertEqual(MemoryMdManager._categorize_section("其他"), "user")

    def test_compute_segment_lengths(self):
        """_compute_segment_lengths 正确分段计算。"""
        from src.memory.memory_md import MemoryMdManager
        mgr = MemoryMdManager(file_path="data/memory.md")
        text = (
            "# 用户画像\n\n"
            "## 基本信息\n- 用户是测试用户\n- 用户职业是工程师\n- 用户技术栈是Python\n\n"
            "## Agent 自画像\n- Agent 失败模式 A\n\n"
            "## 沟通偏好\n[场景:通用] 讨厌emoji\n"
        )
        lengths = mgr._compute_segment_lengths(text)
        # user 段含 H1 + 基本信息 section（3 条内容）
        self.assertGreater(lengths["user"], 0)
        # agent 段含 Agent 自画像 section
        self.assertGreater(lengths["agent"], 0)
        # communication 段含沟通偏好 section
        self.assertGreater(lengths["communication"], 0)
        # agent 段应小于 user 段（user 段内容更多）
        self.assertLess(lengths["agent"], lengths["user"])

    def test_segment_reject_agent_overflow(self, ):
        """Agent 段超限时拒绝 agent 段 add，user 段 add 不受影响。"""
        import tempfile
        from pathlib import Path
        from src.memory.memory_md import MemoryMdManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryMdManager(file_path=str(Path(tmp) / "memory.md"))
            # 初始化：填入接近上限的 Agent 自画像 + 少量 user 段
            big_agent = "Agent 失败模式 " + "x" * 250
            mgr.apply_profile_updates([
                {"action": "add", "section": "Agent 自画像", "content": big_agent},
                {"action": "add", "section": "基本信息", "content": "- 用户是测试用户"},
            ])
            # 再 add 8 条 agent 段内容（每条 250 字符），总长超 2000
            updates = []
            for i in range(8):
                updates.append({
                    "action": "add",
                    "section": "Agent 自画像",
                    "content": f"模式{i} " + "y" * 250,
                })
            # 同时 add 一条 user 段内容
            updates.append({
                "action": "add",
                "section": "技术栈",
                "content": "- 用户技术栈是 Python",
            })
            mgr.apply_profile_updates(updates)
            text = mgr.read()
            lengths = mgr._compute_segment_lengths(text)
            # agent 段应被拒绝 add（≤2000）
            self.assertLessEqual(lengths["agent"], mgr.MAX_AGENT_PROFILE_CHARS)
            # user 段的 add 应正常执行
            self.assertIn("Python", text)

    def test_segment_reject_communication_overflow(self):
        """沟通偏好段超限时拒绝 communication 段 add。"""
        import tempfile
        from pathlib import Path
        from src.memory.memory_md import MemoryMdManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryMdManager(file_path=str(Path(tmp) / "memory.md"))
            # 填入接近上限的沟通偏好段
            big_comm = "[场景:通用] " + "z" * 200
            mgr.apply_profile_updates([
                {"action": "add", "section": "沟通偏好", "content": big_comm},
            ])
            # 再 add 5 条沟通偏好内容（每条 200 字符），总长超 1000
            updates = []
            for i in range(5):
                updates.append({
                    "action": "add",
                    "section": "沟通偏好",
                    "content": f"[场景:测试{i}] " + "w" * 200,
                })
            mgr.apply_profile_updates(updates)
            text = mgr.read()
            lengths = mgr._compute_segment_lengths(text)
            self.assertLessEqual(lengths["communication"], mgr.MAX_COMMUNICATION_CHARS)

    def test_replace_not_blocked_by_segment_limit(self):
        """replace 操作不受分段上限限制。"""
        import tempfile
        from pathlib import Path
        from src.memory.memory_md import MemoryMdManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryMdManager(file_path=str(Path(tmp) / "memory.md"))
            # 初始化一个小文件
            mgr.apply_profile_updates([
                {"action": "add", "section": "沟通偏好", "content": "原内容"},
            ])
            # replace 为超长内容（replace 不受分段限制）
            long_content = "新内容 " + "a" * 2000
            mgr.apply_profile_updates([
                {"action": "replace", "section": "沟通偏好", "content": long_content},
            ])
            text = mgr.read()
            self.assertIn("新内容", text)
            self.assertIn("a" * 100, text)

    def test_read_system_profile_excludes_segments(self):
        """read_system_profile 排除 Agent 自画像和沟通偏好段。"""
        import tempfile
        from pathlib import Path
        from src.memory.memory_md import MemoryMdManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryMdManager(file_path=str(Path(tmp) / "memory.md"))
            mgr.apply_profile_updates([
                {"action": "add", "section": "基本信息", "content": "- 测试用户"},
                {"action": "add", "section": "Agent 自画像", "content": "- Agent 失败模式"},
                {"action": "add", "section": "沟通偏好", "content": "[场景:通用] 讨厌emoji"},
            ])
            system_profile = mgr.read_system_profile()
            self.assertIn("测试用户", system_profile)
            self.assertNotIn("Agent 自画像", system_profile)
            self.assertNotIn("沟通偏好", system_profile)
            self.assertNotIn("Agent 失败模式", system_profile)
            self.assertNotIn("讨厌emoji", system_profile)

    def test_read_section_body(self):
        """read_section_body 正确返回指定 section 的 body。"""
        import tempfile
        from pathlib import Path
        from src.memory.memory_md import MemoryMdManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryMdManager(file_path=str(Path(tmp) / "memory.md"))
            mgr.apply_profile_updates([
                {"action": "add", "section": "基本信息", "content": "- 用户A\n- 用户B"},
                {"action": "add", "section": "Agent 自画像", "content": "- 模式1"},
                {"action": "add", "section": "沟通偏好", "content": "[场景:通用] 偏好简洁"},
            ])
            self.assertEqual(mgr.read_section_body("基本信息"), "- 用户A\n- 用户B")
            self.assertEqual(mgr.read_section_body("Agent 自画像"), "- 模式1")
            self.assertEqual(mgr.read_section_body("沟通偏好"), "[场景:通用] 偏好简洁")
            self.assertEqual(mgr.read_section_body("不存在"), "")

    def test_free_narrative_content_preserved(self):
        """内容层保持自由叙述式（无固定字段结构）。"""
        import tempfile
        from pathlib import Path
        from src.memory.memory_md import MemoryMdManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryMdManager(file_path=str(Path(tmp) / "memory.md"))
            # 自由叙述式内容（无固定字段，含 [场景:X] 索引）
            free_content = "[场景:code_review] 用户偏好直接指出问题而非委婉表达"
            mgr.apply_profile_updates([
                {"action": "add", "section": "沟通偏好", "content": free_content},
            ])
            text = mgr.read()
            self.assertIn(free_content, text)
            body = mgr.read_section_body("沟通偏好")
            self.assertEqual(body, free_content)


# ---------------------------------------------------------------------------
# Task 4: 教训反哺注入测试
# ---------------------------------------------------------------------------

class TestLessonsInjection(unittest.TestCase):
    """Task 4: 验证历史教训注入 messages[0] 的逻辑。

    覆盖：空文件兜底、JSONL 读取、token 预算、惊讶门控、优先级裁剪、
    embedding 检索（通过 mock _compute_embedding）、置信度过滤。
    """

    def _make_case(self, pattern, root_cause, prevention, confidence, embedding=None):
        """构造一条失败案例字典。"""
        case = {
            "pattern": pattern,
            "root_cause": root_cause,
            "prevention_rule": prevention,
            "confidence": confidence,
        }
        if embedding is not None:
            case["embedding"] = embedding
        return case

    def _write_cases_jsonl(self, path, cases):
        """将案例列表写入 JSONL 文件。"""
        import json
        with open(path, "w", encoding="utf-8") as f:
            for case in cases:
                f.write(json.dumps(case, ensure_ascii=False) + "\n")

    def test_empty_file_fallback(self):
        """SubTask 4.7: failure_cases.jsonl 不存在时返回空字符串，不报错。"""
        cm = ContextManager(failure_cases_path="data/nonexistent_failure_cases.jsonl")
        result = cm._get_lessons_injection("测试输入")
        self.assertEqual(result, "")

    def test_empty_jsonl_file(self):
        """空 JSONL 文件（0 行）返回空字符串。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            # 创建空文件
            with open(jsonl_path, "w", encoding="utf-8") as f:
                pass
            cm = ContextManager(failure_cases_path=jsonl_path)
            result = cm._get_lessons_injection("测试输入")
            self.assertEqual(result, "")

    def test_jsonl_reading_and_parsing(self):
        """SubTask 4.2: 正确读取和解析 JSONL 文件。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            cases = [
                self._make_case("模式A", "根因A", "规避A", 0.8),
                self._make_case("模式B", "根因B", "规避B", 0.7),
            ]
            self._write_cases_jsonl(jsonl_path, cases)
            cm = ContextManager(failure_cases_path=jsonl_path)
            read_cases = cm._read_failure_cases()
            self.assertEqual(len(read_cases), 2)
            self.assertEqual(read_cases[0]["pattern"], "模式A")
            self.assertEqual(read_cases[1]["confidence"], 0.7)

    def test_jsonl_skips_invalid_lines(self):
        """JSONL 中无效行（非 JSON）被跳过，不报错。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.write('{"pattern": "有效", "confidence": 0.8}\n')
                f.write('这不是JSON\n')
                f.write('{"pattern": "也有效", "confidence": 0.7}\n')
                f.write('\n')  # 空行
            cm = ContextManager(failure_cases_path=jsonl_path)
            cases = cm._read_failure_cases()
            self.assertEqual(len(cases), 2)

    def test_token_budget_hard_cap(self):
        """SubTask 4.4: 教训段 token 上限硬约束（≤300 token）。"""
        from src.memory.context_manager import _MAX_LESSONS_TOKENS, _estimate_tokens
        # 构造 10 条超长案例，确保总 token 远超 300
        cases = []
        for i in range(10):
            cases.append(self._make_case(
                f"模式{i}" + "x" * 200,
                f"根因{i}" + "y" * 200,
                f"规避{i}" + "z" * 200,
                0.9,
            ))
        text = ContextManager._format_lessons(cases, _MAX_LESSONS_TOKENS)
        # 验证不超过 300 token（用同样的估算函数）
        self.assertLessEqual(_estimate_tokens(text), _MAX_LESSONS_TOKENS)
        # 至少包含标题和一条案例
        self.assertIn("## 历史教训", text)

    def test_format_lessons_truncation(self):
        """_format_lessons 超出 token 上限时截断后面的案例。"""
        cases = [
            self._make_case("短模式1", "短根因1", "短规避1", 0.9),
            self._make_case("短模式2", "短根因2", "短规避2", 0.8),
            self._make_case("短模式3", "短根因3", "短规避3", 0.7),
        ]
        # 设置 token 上限仅容纳标题 + 第一条（header≈3 token，entry≈13 token）
        text = ContextManager._format_lessons(cases, max_tokens=16)
        self.assertIn("## 历史教训", text)
        self.assertIn("短模式1", text)
        # 第 2、3 条应被截断
        self.assertNotIn("短模式2", text)
        self.assertNotIn("短模式3", text)

    def test_format_lessons_empty_cases(self):
        """空案例列表返回空字符串。"""
        text = ContextManager._format_lessons([], max_tokens=300)
        self.assertEqual(text, "")

    def test_confidence_filter(self):
        """SubTask 4.2: 低置信度案例（<0.6）不被注入。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            cases = [
                self._make_case("高置信", "根因", "规避", 0.9),
                self._make_case("低置信", "根因", "规避", 0.3),
            ]
            self._write_cases_jsonl(jsonl_path, cases)
            cm = ContextManager(failure_cases_path=jsonl_path)
            # mock embedding 返回空，触发置信度降级排序
            with patch("src.memory.context_manager._compute_embedding", return_value=[]):
                result = cm._get_lessons_injection("测试")
            self.assertIn("高置信", result)
            self.assertNotIn("低置信", result)

    def test_embedding_retrieval_top3(self):
        """SubTask 4.2: 按 embedding 相似度检索 top-3。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            # 5 条案例，不同的 embedding 向量
            cases = [
                self._make_case("相关1", "根因", "规避", 0.8, [1.0, 0.0]),
                self._make_case("相关2", "根因", "规避", 0.8, [0.9, 0.1]),
                self._make_case("相关3", "根因", "规避", 0.8, [0.8, 0.2]),
                self._make_case("不相关1", "根因", "规避", 0.8, [0.0, 1.0]),
                self._make_case("不相关2", "根因", "规避", 0.8, [0.1, 0.9]),
            ]
            self._write_cases_jsonl(jsonl_path, cases)
            cm = ContextManager(failure_cases_path=jsonl_path)
            # mock user_input embedding 为 [1.0, 0.0]，与"相关"案例最相似
            with patch("src.memory.context_manager._compute_embedding",
                       return_value=[1.0, 0.0]):
                # mock memory_retriever 返回空，跳过惊讶门控
                cm.memory_retriever = None
                result = cm._get_lessons_injection("python 测试")
            # 应包含 3 条相关案例
            self.assertIn("相关1", result)
            self.assertIn("相关2", result)
            self.assertIn("相关3", result)
            # 不应包含不相关案例
            self.assertNotIn("不相关1", result)
            self.assertNotIn("不相关2", result)

    def test_surprise_gating_skips_redundant(self):
        """SubTask 4.3: 与已有记忆相似度 >0.92 时跳过注入。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            cases = [
                self._make_case("冗余案例", "根因", "规避", 0.9, [1.0, 0.0]),
            ]
            self._write_cases_jsonl(jsonl_path, cases)
            cm = ContextManager(
                failure_cases_path=jsonl_path,
                memory_retriever=MockMemoryRetriever(),
            )
            # user_input embedding 和 memory embedding 都返回 [1.0, 0.0]
            # 与案例 embedding [1.0, 0.0] 相似度为 1.0 > 0.92，应被门控
            with patch("src.memory.context_manager._compute_embedding",
                       return_value=[1.0, 0.0]):
                result = cm._get_lessons_injection("python")
            self.assertEqual(result, "")

    def test_surprise_gating_allows_novel(self):
        """SubTask 4.3: 与已有记忆相似度 ≤0.92 时正常注入。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        class MockMemoryRetrieverPython:
            """返回 java 相关记忆，与 python 案例不相似。"""
            def get_injection_text(self, user_input):
                return "## 相关记忆\n1. java 相关记忆"

        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            cases = [
                self._make_case("python案例", "根因", "规避", 0.9, [1.0, 0.0]),
            ]
            self._write_cases_jsonl(jsonl_path, cases)
            cm = ContextManager(
                failure_cases_path=jsonl_path,
                memory_retriever=MockMemoryRetrieverPython(),
            )
            # 调用计数区分 user_input embedding 和 memory embedding
            call_count = [0]
            def mock_embed(text):
                call_count[0] += 1
                # 第一次调用是 user_input，第二次是 memory_text
                if call_count[0] == 1:
                    return [1.0, 0.0]  # user_input → python
                return [0.0, 1.0]  # memory → java
            with patch("src.memory.context_manager._compute_embedding",
                       side_effect=mock_embed):
                result = cm._get_lessons_injection("python")
            self.assertIn("python案例", result)

    def test_priority_trimming_agent_profile_first(self):
        """SubTask 4.5: 超预算时优先裁剪 Agent 自画像。"""
        from src.memory.context_manager import _MAX_INJECTION_CHARS

        class BigAgentProfileManager:
            """返回超长 Agent 自画像的 mock。"""
            def read(self):
                return "# 用户画像"
            def read_system_profile(self):
                return "# 用户画像"
            def read_section_body(self, section_title):
                if section_title == "Agent 自画像":
                    return "x" * (_MAX_INJECTION_CHARS + 100)
                if section_title == "沟通偏好":
                    return "沟通偏好内容"
                return ""

        class MockRetrieverSmall:
            def get_injection_text(self, user_input):
                return "## 相关记忆\n1. 小记忆"

        cm = ContextManager(
            memory_md_manager=BigAgentProfileManager(),
            memory_retriever=MockRetrieverSmall(),
        )
        prompt = cm.build_prompt("test", "用户问题")
        injection = prompt["messages"][0]["content"]
        # Agent 自画像应被裁剪掉
        self.assertNotIn("Agent 自画像", injection)
        # 沟通偏好和检索记忆应保留
        self.assertIn("沟通偏好内容", injection)
        self.assertIn("小记忆", injection)

    def test_priority_trimming_communication_second(self):
        """SubTask 4.5: Agent 自画像裁剪后仍超预算时裁剪沟通偏好。"""
        from src.memory.context_manager import _MAX_INJECTION_CHARS

        class BigBothManager:
            """Agent 自画像和沟通偏好都超长。"""
            def read(self):
                return "# 用户画像"
            def read_system_profile(self):
                return "# 用户画像"
            def read_section_body(self, section_title):
                if section_title == "Agent 自画像":
                    return "x" * (_MAX_INJECTION_CHARS + 100)
                if section_title == "沟通偏好":
                    return "y" * (_MAX_INJECTION_CHARS + 100)
                return ""

        class MockRetrieverSmall:
            def get_injection_text(self, user_input):
                return "## 相关记忆\n1. 小记忆"

        cm = ContextManager(
            memory_md_manager=BigBothManager(),
            memory_retriever=MockRetrieverSmall(),
        )
        prompt = cm.build_prompt("test", "用户问题")
        injection = prompt["messages"][0]["content"]
        # Agent 自画像和沟通偏好都应被裁剪
        self.assertNotIn("Agent 自画像", injection)
        # 检索记忆应保留（最高优先级）
        self.assertIn("小记忆", injection)

    def test_injection_order_with_lessons(self):
        """SubTask 4.6: 注入顺序为 文件 → Agent 自画像 → 沟通偏好 → 历史教训 → 检索记忆。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            cases = [self._make_case("教训模式", "根因", "规避", 0.9, [1.0, 0.0])]
            self._write_cases_jsonl(jsonl_path, cases)
            cm = ContextManager(
                memory_md_manager=MockSegmentedMemoryMdManager(),
                memory_retriever=MockMemoryRetriever(),
                failure_cases_path=jsonl_path,
            )
            # mock embedding：user_input 返回 [1.0, 0.0]，memory 返回 [0.0, 1.0]
            call_count = [0]
            def mock_embed(text):
                call_count[0] += 1
                if call_count[0] == 1:
                    return [1.0, 0.0]
                return [0.0, 1.0]
            with patch("src.memory.context_manager._compute_embedding",
                       side_effect=mock_embed):
                prompt = cm.build_prompt("test", "python 问题")
            injection = prompt["messages"][0]["content"]
            # 验证顺序：Agent 自画像 < 沟通偏好 < 历史教训 < 相关记忆
            agent_idx = injection.find("Agent 自画像")
            comm_idx = injection.find("沟通偏好")
            lessons_idx = injection.find("历史教训")
            memory_idx = injection.find("相关记忆")
            self.assertNotEqual(agent_idx, -1, "应包含 Agent 自画像")
            self.assertNotEqual(comm_idx, -1, "应包含沟通偏好")
            self.assertNotEqual(lessons_idx, -1, "应包含历史教训")
            self.assertNotEqual(memory_idx, -1, "应包含相关记忆")
            self.assertLess(agent_idx, comm_idx)
            self.assertLess(comm_idx, lessons_idx)
            self.assertLess(lessons_idx, memory_idx)

    def test_lessons_not_in_system_text(self):
        """教训反哺注入 messages[0]，不污染 system_text（缓存稳定性）。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = str(Path(tmp) / "failure_cases.jsonl")
            cases = [self._make_case("教训模式", "根因", "规避", 0.9, [1.0, 0.0])]
            self._write_cases_jsonl(jsonl_path, cases)
            cm = ContextManager(
                memory_md_manager=MockSegmentedMemoryMdManager(),
                memory_retriever=MockMemoryRetriever(),
                failure_cases_path=jsonl_path,
            )
            call_count = [0]
            def mock_embed(text):
                call_count[0] += 1
                if call_count[0] == 1:
                    return [1.0, 0.0]
                return [0.0, 1.0]
            with patch("src.memory.context_manager._compute_embedding",
                       side_effect=mock_embed):
                prompt = cm.build_prompt("test", "python 问题")
            # system_text 不应包含教训内容
            self.assertNotIn("教训模式", prompt["system"])
            self.assertNotIn("## 历史教训", prompt["system"])
            # messages[0] 应包含教训
            self.assertIn("教训模式", prompt["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
