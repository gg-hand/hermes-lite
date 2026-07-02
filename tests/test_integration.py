"""端到端集成测试（mock 模式，不依赖真实 LLM API）。

覆盖以下测试用例：
1. test_history_buffer_fifo        - HistoryBuffer 的 FIFO 删除逻辑
2. test_consolidation_threshold    - ConsolidationEngine 的阈值触发
3. test_chroma_store_dedup         - ChromaMemoryStore 的去重逻辑
4. test_context_manager_layering   - ContextManager 的 Prompt 分层
5. test_tool_registry_defer_loading - ToolRegistry 的 Core/Deferred 分层机制
6. test_plan_mode_tools_stable     - Plan 模式不改变工具列表（字节级一致）
7. test_memory_md_async_write      - MemoryMdManager 的异步写入
8. test_retrieval_format           - MemoryRetriever 的 format_for_prompt 输出格式

运行方式：
    python tests/test_integration.py
    python -m unittest tests.test_integration -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

# 将项目根目录加入 sys.path，使 from src.xxx import yyy 可用
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 在导入 src 模块前，先为缺失的可选依赖（chromadb/numpy/sentence_transformers）注入 mock
from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

# 导入被测模块（此时 mock 已就位，chroma_store.py 等可正常导入）
from src.storage.history_buffer import HistoryBuffer  # noqa: E402
from src.memory.consolidation import ConsolidationEngine  # noqa: E402
from src.storage.chroma_store import ChromaMemoryStore  # noqa: E402
from src.memory.context_manager import ContextManager  # noqa: E402
from src.llm.prompts import SYSTEM_PROMPT  # noqa: E402
from src.agent.tool_registry import ToolRegistry  # noqa: E402
from src.memory.memory_md import MemoryMdManager  # noqa: E402
from src.memory.retrieval import MemoryRetriever  # noqa: E402


# ===========================================================================
# 1. HistoryBuffer FIFO 删除逻辑
# ===========================================================================

class TestHistoryBufferFifo(unittest.TestCase):
    """验证 HistoryBuffer 的 FIFO 删除逻辑。"""

    def test_fifo_retains_latest_20(self):
        """添加 25 条消息后只保留 20 条，且保留的是最新的 20 条。"""
        buf = HistoryBuffer(max_turns=20)
        sid = "test-fifo"
        for i in range(25):
            buf.add_message(sid, "user" if i % 2 == 0 else "assistant", f"消息 {i}")

        history = buf.get_history(sid)
        # 只保留 20 条
        self.assertEqual(len(history), 20, "应只保留 20 条消息")
        # 保留的是最新的 20 条（原始索引 5~24）
        self.assertEqual(history[0]["content"], "消息 5", "首条应为消息 5")
        self.assertEqual(history[-1]["content"], "消息 24", "末条应为消息 24")


# ===========================================================================
# 2. ConsolidationEngine 阈值触发
# ===========================================================================

class TestConsolidationThreshold(unittest.TestCase):
    """验证 ConsolidationEngine 的阈值触发逻辑。"""

    def test_threshold_at_15(self):
        """14 条时 should_consolidate 返回 False，15 条时返回 True。"""
        # llm_client 与 chroma_store 仅在 consolidate() 中使用，
        # 测试 add_info/should_consolidate 时传入 None 即可
        engine = ConsolidationEngine(
            llm_client=None,
            chroma_store=None,
            threshold=15,
        )

        # 添加 14 条消息，未达阈值
        for i in range(14):
            engine.add_info({"role": "user", "content": f"消息 {i}"})
        self.assertFalse(
            engine.should_consolidate(), "14 条消息时应返回 False"
        )

        # 添加第 15 条，达到阈值
        engine.add_info({"role": "assistant", "content": "第 15 条"})
        self.assertTrue(
            engine.should_consolidate(), "15 条消息时应返回 True"
        )


# ===========================================================================
# 3. ChromaMemoryStore 去重逻辑
# ===========================================================================

class TestChromaStoreDedup(unittest.TestCase):
    """验证 ChromaMemoryStore 的去重逻辑。

    使用 mock 的 sentence_transformers（按关键词生成确定性 embedding），
    两段都含 "python" 的文本相似度为 1.0（>0.85，判定为重复），
    "python" 与 "java" 相似度为 0.0（<0.85，判定为不重复）。
    """

    def test_find_duplicates_similar(self):
        """添加记忆后，相似内容应被 find_duplicates 检测到。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ChromaMemoryStore(persist_path=tmpdir)

            # 添加一条 Python 相关记忆
            store.add_memory(
                "用户是 Python 开发者",
                metadata={"type": "user_profile", "importance": 0.8},
            )

            # 查找与 "用户是 Python 程序员" 相似的记忆
            # mock embedding：两段都含 "python" → 相似度 1.0 > 0.85 → 重复
            dups = store.find_duplicates("用户是 Python 程序员", threshold=0.85)
            self.assertEqual(len(dups), 1, "应检测到 1 条重复记忆")
            self.assertGreater(dups[0]["similarity"], 0.85, "相似度应大于阈值")
            self.assertIn("Python", dups[0]["content"], "内容应包含 Python")
            self.assertIn("id", dups[0], "结果应包含 id 字段")
            self.assertIn("metadata", dups[0], "结果应包含 metadata 字段")

    def test_find_duplicates_dissimilar(self):
        """不相似内容不应被 find_duplicates 检测到。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ChromaMemoryStore(persist_path=tmpdir)

            # 添加 Python 相关记忆
            store.add_memory("用户是 Python 开发者", metadata={"type": "user_profile"})

            # 查找与 "用户是 Java 开发者" 相似的记忆
            # mock embedding：python=[1,0], java=[0,1] → 相似度 0.0 < 0.85 → 不重复
            dups = store.find_duplicates("用户是 Java 开发者", threshold=0.85)
            self.assertEqual(len(dups), 0, "Java 与 Python 不应被判定为重复")


# ===========================================================================
# 4. ContextManager Prompt 分层
# ===========================================================================

class TestContextManagerLayering(unittest.TestCase):
    """验证 ContextManager 的 Prompt 分层结构。"""

    def test_prompt_layering(self):
        """验证 system/tools/messages 的分层正确性。"""
        # 构建 mock 依赖
        class MockToolRegistry:
            def get_tools_schema(self):
                return [
                    {
                        "name": "test_tool",
                        "description": "测试工具",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]

        class MockMemoryMdManager:
            def read(self):
                return "# 用户画像\n\n## 基本信息\n- 用户是测试用户"

        class MockMemoryRetriever:
            def get_injection_text(self, user_input):
                return "## 相关记忆\n1. 测试记忆 (相关度: 0.90)"

        class MockHistoryBuffer:
            def get_history(self, session_id):
                return [
                    {"role": "user", "content": "历史用户"},
                    {"role": "assistant", "content": "历史助手"},
                ]

        cm = ContextManager(
            tool_registry=MockToolRegistry(),
            memory_md_manager=MockMemoryMdManager(),
            memory_retriever=MockMemoryRetriever(),
            history_buffer=MockHistoryBuffer(),
        )
        prompt = cm.build_prompt("test-session", "当前问题")

        # 1. system 包含 SYSTEM_PROMPT + 用户画像
        self.assertIn(SYSTEM_PROMPT, prompt["system"], "system 应包含 SYSTEM_PROMPT")
        self.assertIn("用户是测试用户", prompt["system"], "system 应包含用户画像")
        self.assertIn("---", prompt["system"], "system 应包含分隔符")

        # 2. tools 是完整列表
        self.assertEqual(len(prompt["tools"]), 1, "tools 应为完整列表（1 个工具）")
        self.assertEqual(prompt["tools"][0]["name"], "test_tool")

        # 3. messages[0] 是检索记忆注入
        self.assertEqual(prompt["messages"][0]["role"], "user")
        self.assertIn(
            "相关记忆", prompt["messages"][0]["content"],
            "messages[0] 应为检索记忆注入",
        )

        # 4. 历史消息在中间
        self.assertEqual(prompt["messages"][1]["content"], "历史用户")
        self.assertEqual(prompt["messages"][2]["content"], "历史助手")

        # 5. 最后一条是当前用户输入
        self.assertEqual(
            prompt["messages"][-1],
            {"role": "user", "content": "当前问题"},
            "最后一条应为当前用户输入",
        )

        # 6. 历史消息不含附加字段（符合 Anthropic API 规范）
        self.assertEqual(
            set(prompt["messages"][1].keys()), {"role", "content"},
            "历史消息应仅含 role 和 content",
        )


# ===========================================================================
# 5. ToolRegistry Core/Deferred 分层机制
# ===========================================================================

class TestToolRegistryDeferLoading(unittest.TestCase):
    """验证 ToolRegistry 的 Core/Deferred 分层机制。

    注：原 threshold-based 全有或全无 stub 切换已废弃（Phase 4 REMOVED）。
    新行为：register() 等价 register_core()（Core Tier 全量注入），
    Deferred Tier 通过 register_deferred() 注册，仅注入 stub。
    """

    @staticmethod
    def _make_tool(idx):
        """构造一个测试工具的注册参数。"""
        return (
            f"tool_{idx}",
            f"测试工具 {idx}",
            {"type": "object", "properties": {}},
            lambda **kwargs: "ok",
        )

    def test_full_schema_below_threshold(self):
        """注册 5 个工具（旧 register 别名 → Core Tier）返回完整 schema。"""
        registry = ToolRegistry(defer_loading_threshold=20)
        for i in range(5):
            name, desc, schema, handler = self._make_tool(i)
            registry.register(name, desc, schema, handler)

        schemas = registry.get_tools_schema()
        self.assertEqual(len(schemas), 5, "应返回 5 个完整 schema")
        # 验证是完整 schema（有真实的 description 和 input_schema）
        for s in schemas:
            self.assertIn("name", s)
            self.assertIn("description", s)
            self.assertIn("input_schema", s)
            self.assertNotIn(
                "defer_loading", s,
                "Core 工具不应是 stub 形式",
            )

    def test_deferred_tools_return_stub(self):
        """Core 工具返回完整 schema，Deferred 工具返回 stub（含 defer_loading）。"""
        registry = ToolRegistry(defer_loading_threshold=20)
        # 注册 5 个 Core 工具
        for i in range(5):
            name, desc, schema, handler = self._make_tool(i)
            registry.register_core(name, desc, schema, handler)
        # 注册 10 个 Deferred 工具
        for i in range(5, 15):
            name, desc, schema, handler = self._make_tool(i)
            registry.register_deferred(name, desc, schema, handler)

        schemas = registry.get_tools_schema()
        # 5 Core + 10 Deferred = 15
        self.assertEqual(
            len(schemas), 15,
            "应返回 5 个完整 schema + 10 个 stub = 15 项",
        )
        # 前 5 个是 Core 完整 schema（含 input_schema，不含 defer_loading）
        for s in schemas[:5]:
            self.assertIn("input_schema", s, "Core 工具应有 input_schema")
            self.assertNotIn("defer_loading", s, "Core 工具不应有 defer_loading")
        # 后 10 个是 Deferred stub（含 defer_loading: True，不含 input_schema）
        for s in schemas[5:]:
            self.assertIn("defer_loading", s, "Deferred 工具应有 defer_loading")
            self.assertTrue(s["defer_loading"], "defer_loading 应为 True")
            self.assertNotIn("input_schema", s, "Deferred stub 不应含 input_schema")


# ===========================================================================
# 6. Plan 模式工具列表字节级稳定
# ===========================================================================

class TestPlanModeToolsStable(unittest.TestCase):
    """验证 Plan 模式不改变工具列表（字节级一致）。"""

    def test_tools_stable_across_plan_mode(self):
        """enter_plan_mode/exit_plan_mode 前后 tools 字节级一致。"""
        class MockToolRegistry:
            def get_tools_schema(self):
                return [
                    {
                        "name": "file_read",
                        "description": "读取文件",
                        "input_schema": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "file_write",
                        "description": "写入文件",
                        "input_schema": {"type": "object", "properties": {}},
                    },
                ]

        cm = ContextManager(tool_registry=MockToolRegistry())

        # 构建初始 prompt（Plan 模式前）
        prompt_before = cm.build_prompt("session", "你好")
        tools_before = json.dumps(prompt_before["tools"], sort_keys=True)

        # 进入 Plan 模式后构建
        enter_text = cm.enter_plan_mode()
        self.assertIn("Plan", enter_text)  # 确认返回了 Plan 模式提示
        prompt_plan = cm.build_prompt("session", "你好")
        tools_plan = json.dumps(prompt_plan["tools"], sort_keys=True)

        # 退出 Plan 模式后构建
        exit_text = cm.exit_plan_mode()
        self.assertIn("Plan", exit_text)  # 确认返回了退出提示
        prompt_after = cm.build_prompt("session", "你好")
        tools_after = json.dumps(prompt_after["tools"], sort_keys=True)

        # 三次构建的 tools 字节级一致
        self.assertEqual(tools_before, tools_plan, "进入 Plan 模式后 tools 不应改变")
        self.assertEqual(tools_before, tools_after, "退出 Plan 模式后 tools 不应改变")


# ===========================================================================
# 7. MemoryMdManager 异步写入
# ===========================================================================

class TestMemoryMdAsyncWrite(unittest.TestCase):
    """验证 MemoryMdManager 的异步写入。"""

    def test_async_write_updates_file(self):
        """async_write 后文件内容应更新。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "memory.md")
            manager = MemoryMdManager(file_path=file_path)

            # 初始文件不存在，read 返回空串
            self.assertEqual(manager.read(), "")

            # 异步写入 user_profile 事实
            facts = [
                {
                    "content": "用户是 Python 开发者",
                    "type": "user_profile",
                    "importance": 0.8,
                }
            ]
            manager.async_write(facts)

            # 轮询等待异步写入完成（最多 5 秒）
            deadline = time.time() + 5.0
            content = ""
            while time.time() < deadline:
                content = manager.read()
                if "用户是 Python 开发者" in content:
                    break
                time.sleep(0.1)

            # 验证文件内容已更新
            self.assertIn("用户是 Python 开发者", content, "文件应包含写入的事实")
            self.assertIn("# 用户画像", content, "文件应包含用户画像标题")


# ===========================================================================
# 8. MemoryRetriever format_for_prompt 输出格式
# ===========================================================================

class TestRetrievalFormat(unittest.TestCase):
    """验证 MemoryRetriever 的 format_for_prompt 输出格式。"""

    def test_empty_memories_returns_empty(self):
        """无长期记忆时 format_for_prompt 返回空字符串。"""
        # mock 依赖：chroma_store 与 memory_md_manager 均返回空
        class MockChromaStore:
            def query_memory(self, query_text, top_k=5, **kwargs):
                return []

        class MockMemoryMdManager:
            def get_summary(self, query=None, max_tokens=500):
                return ""

        retriever = MemoryRetriever(
            chroma_store=MockChromaStore(),
            memory_md_manager=MockMemoryMdManager(),
        )
        result = retriever.format_for_prompt({
            "long_term_memories": [],
            "user_profile_summary": "",
            "total_tokens": 0,
        })
        self.assertEqual(result, "", "无记忆时应返回空字符串")

    def test_with_memories_returns_formatted(self):
        """有长期记忆时 format_for_prompt 返回格式化文本。"""
        class MockChromaStore:
            def query_memory(self, query_text, top_k=5, **kwargs):
                return []

        class MockMemoryMdManager:
            def get_summary(self, query=None, max_tokens=500):
                return ""

        retriever = MemoryRetriever(
            chroma_store=MockChromaStore(),
            memory_md_manager=MockMemoryMdManager(),
        )
        memories = [
            {"content": "用户喜欢 Python", "similarity": 0.9},
            {"content": "用户在做 AI 项目", "similarity": 0.8},
        ]
        result = retriever.format_for_prompt({
            "long_term_memories": memories,
            "user_profile_summary": "",
            "total_tokens": 100,
        })
        # 验证格式化输出包含相关记忆标题与内容
        self.assertIn("## 相关记忆", result)
        self.assertIn("用户喜欢 Python", result)
        self.assertIn("用户在做 AI 项目", result)
        # 验证相关度数值格式（保留两位小数）
        self.assertIn("0.90", result)
        self.assertIn("0.80", result)

    def test_retrieve_returns_empty_for_no_match(self):
        """通过 retrieve 一站式调用时，无匹配记忆返回空字符串。"""
        class MockChromaStore:
            def query_memory(self, query_text, top_k=5, **kwargs):
                return []

        class MockMemoryMdManager:
            def get_summary(self, query=None, max_tokens=500):
                return ""

        retriever = MemoryRetriever(
            chroma_store=MockChromaStore(),
            memory_md_manager=MockMemoryMdManager(),
        )
        # get_injection_text 内部调用 retrieve → format_for_prompt
        result = retriever.get_injection_text("任意查询")
        self.assertEqual(result, "", "无匹配记忆时应返回空字符串")

    def test_retrieve_filters_user_profile_vectors(self):
        """retrieve 过滤掉 type=user_profile 的向量库残留，避免与 system prompt 重复注入。"""
        class MockChromaStore:
            def query_memory(self, query_text, top_k=5, **kwargs):
                return [
                    {
                        "content": "用户喜欢 Python",
                        "metadata": {"type": "fact"},
                        "similarity": 0.9,
                    },
                    {
                        "content": "用户是工程师",
                        "metadata": {"type": "user_profile"},
                        "similarity": 0.95,
                    },
                ]

        class MockMemoryMdManager:
            def get_summary(self, query=None, max_tokens=500):
                return ""

        retriever = MemoryRetriever(
            chroma_store=MockChromaStore(),
            memory_md_manager=MockMemoryMdManager(),
        )
        result = retriever.get_injection_text("任意查询")
        # 保留 fact 类型
        self.assertIn("用户喜欢 Python", result)
        # 过滤掉 user_profile 类型（已由 system prompt 注入）
        self.assertNotIn("用户是工程师", result)
        self.assertNotIn("## 用户画像", result, "不应再输出用户画像段落")


if __name__ == "__main__":
    unittest.main(verbosity=2)
