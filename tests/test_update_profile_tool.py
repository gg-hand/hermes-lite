"""update_profile 工具与延迟合并写入机制的单元测试。

覆盖以下场景：
1. handler 入队成功：调用 update_profile handler，验证 pending_profile_updates 队列新增
2. consolidate 合并 pending 队列：入队 2 条 add 操作，调用 consolidate，验证
   memory.md 更新且队列清空
3. add 操作：在 memory.md 的指定 section 追加内容；section 不存在则新建
4. replace 操作：替换指定 section 的全部内容；section 不存在则新建
5. delete 操作：删除指定 section（含标题与 body）
6. PolicyEngine 返回 confirm：DEFAULT_RULES 中 update_profile 为 confirm 决策
7. handler 参数校验：非法 action / 空 section / add 无 content 返回错误提示
8. register_builtin_tools 向后兼容：consolidation_engine=None 时不注册 update_profile

运行方式:
    python -m unittest tests.test_update_profile_tool -v
    python tests/test_update_profile_tool.py

mock 策略:
- ConsolidationEngine 的 LLM 与 chroma_store 用 unittest.mock.MagicMock 替代
- MemoryMdManager 使用真实实例，文件路径指向 tempfile.TemporaryDirectory
- 这样可以端到端验证 enqueue → consolidate → apply_profile_updates → 文件落盘
  的完整链路
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.builtin_tools import register_builtin_tools  # noqa: E402
from src.agent.policy import PolicyEngine, DEFAULT_RULES  # noqa: E402
from src.agent.tool_registry import ToolRegistry  # noqa: E402
from src.memory.consolidation import ConsolidationEngine  # noqa: E402
from src.memory.memory_md import MemoryMdManager  # noqa: E402


def _make_llm_response(text: str) -> MagicMock:
    """构造 mock LLM 响应对象，.content 为含单个 text block 的列表。

    与 test_consolidation.py 保持一致，兼容 ConsolidationEngine
    ._extract_response_text 的 dict block 解析逻辑。
    """
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


class _FakeConsolidationEngine:
    """轻量 mock ConsolidationEngine，仅实现 enqueue_profile_update。

    用于 handler 入队测试，避免依赖 LLM / chroma_store 等重型依赖。
    """

    def __init__(self) -> None:
        self.pending_profile_updates: list = []

    def enqueue_profile_update(
        self, action: str, section: str, content: str
    ) -> None:
        self.pending_profile_updates.append(
            {"action": action, "section": section, "content": content}
        )


# ===========================================================================
# 1. handler 入队成功
# ===========================================================================


class TestUpdateProfileHandlerEnqueue(unittest.TestCase):
    """验证 update_profile handler 正确入队到 pending_profile_updates。"""

    def test_handler_enqueues_add_operation(self):
        """add 操作入队后 pending 队列新增一条对应记录。"""
        registry = ToolRegistry()
        engine = _FakeConsolidationEngine()
        register_builtin_tools(registry, consolidation_engine=engine)

        result = registry.execute_tool(
            "profile_update",
            {"action": "add", "section": "背景", "content": "用户是后端工程师"},
        )

        # 返回成功提示
        self.assertIn("已加入待合并队列", result)
        self.assertIn("add", result)
        self.assertIn("背景", result)
        # 队列新增一条
        self.assertEqual(len(engine.pending_profile_updates), 1)
        entry = engine.pending_profile_updates[0]
        self.assertEqual(entry["action"], "add")
        self.assertEqual(entry["section"], "背景")
        self.assertEqual(entry["content"], "用户是后端工程师")

    def test_handler_enqueues_replace_and_delete(self):
        """replace 与 delete 操作均能正确入队。"""
        registry = ToolRegistry()
        engine = _FakeConsolidationEngine()
        register_builtin_tools(registry, consolidation_engine=engine)

        # replace
        registry.execute_tool(
            "profile_update",
            {"action": "replace", "section": "偏好", "content": "新的偏好内容"},
        )
        # delete（content 可省略）
        registry.execute_tool(
            "profile_update",
            {"action": "delete", "section": "过时信息"},
        )

        self.assertEqual(len(engine.pending_profile_updates), 2)
        self.assertEqual(engine.pending_profile_updates[0]["action"], "replace")
        self.assertEqual(engine.pending_profile_updates[1]["action"], "delete")
        # delete 入队时 content 为空串（handler 默认值）
        self.assertEqual(engine.pending_profile_updates[1]["content"], "")


# ===========================================================================
# 2. handler 参数校验
# ===========================================================================


class TestUpdateProfileHandlerValidation(unittest.TestCase):
    """验证 handler 对非法参数返回友好错误提示（不抛异常）。"""

    def setUp(self):
        """每个测试创建独立的 registry 与 engine。"""
        self.registry = ToolRegistry()
        self.engine = _FakeConsolidationEngine()
        register_builtin_tools(self.registry, consolidation_engine=self.engine)

    def test_invalid_action_returns_error(self):
        """action 非 add/replace/delete 时返回错误提示，不入队。"""
        result = self.registry.execute_tool(
            "profile_update",
            {"action": "modify", "section": "背景", "content": "x"},
        )
        self.assertIn("错误", result)
        self.assertIn("action", result)
        self.assertEqual(len(self.engine.pending_profile_updates), 0)

    def test_empty_section_returns_error(self):
        """section 为空时返回错误提示，不入队。"""
        result = self.registry.execute_tool(
            "profile_update",
            {"action": "add", "section": "", "content": "x"},
        )
        self.assertIn("错误", result)
        self.assertIn("section", result)
        self.assertEqual(len(self.engine.pending_profile_updates), 0)

    def test_add_without_content_returns_error(self):
        """add 操作无 content 时返回错误提示，不入队。"""
        result = self.registry.execute_tool(
            "profile_update",
            {"action": "add", "section": "背景", "content": ""},
        )
        self.assertIn("错误", result)
        self.assertIn("content", result)
        self.assertEqual(len(self.engine.pending_profile_updates), 0)

    def test_replace_without_content_returns_error(self):
        """replace 操作无 content 时返回错误提示，不入队。"""
        result = self.registry.execute_tool(
            "profile_update",
            {"action": "replace", "section": "背景"},
        )
        self.assertIn("错误", result)
        self.assertEqual(len(self.engine.pending_profile_updates), 0)

    def test_delete_without_content_succeeds(self):
        """delete 操作可省略 content，正常入队。"""
        result = self.registry.execute_tool(
            "profile_update",
            {"action": "delete", "section": "过时信息"},
        )
        self.assertIn("已加入待合并队列", result)
        self.assertEqual(len(self.engine.pending_profile_updates), 1)


# ===========================================================================
# 3. consolidate 合并 pending 队列
# ===========================================================================


class TestConsolidateMergesPendingQueue(unittest.TestCase):
    """验证 consolidate() 时 pending_profile_updates 被合并到 memory.md 并清空。"""

    def test_consolidate_applies_pending_and_clears_queue(self):
        """入队 2 条 add 操作，consolidate 后 memory.md 更新且队列清空。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_md_path = os.path.join(tmpdir, "memory.md")
            memory_md_manager = MemoryMdManager(file_path=memory_md_path)

            # 写入初始 memory.md
            memory_md_manager._write_raw_text(
                "# 用户画像\n\n## 背景\n- 用户是 Python 开发者\n"
            )

            # 构造 mock LLM 返回空 facts（只测试 pending 合并路径）
            llm_client = MagicMock()
            llm_client.chat_consolidation.return_value = _make_llm_response("[]")
            chroma_store = MagicMock()

            engine = ConsolidationEngine(
                llm_client=llm_client,
                chroma_store=chroma_store,
                memory_md_manager=memory_md_manager,
                threshold=1,
            )
            engine.add_info({"role": "user", "content": "msg"})

            # 入队 2 条 add 操作
            engine.enqueue_profile_update(
                "add", "背景", "- 用户也是 Rust 爱好者"
            )
            engine.enqueue_profile_update(
                "add", "偏好", "- 偏好函数式编程"
            )
            self.assertEqual(len(engine.pending_profile_updates), 2)

            # 调用 consolidate
            stats = engine.consolidate()

            # 队列已清空
            self.assertEqual(engine.pending_profile_updates, [])

            # memory.md 包含两条新增内容
            content = memory_md_manager.read()
            self.assertIn("用户也是 Rust 爱好者", content)
            self.assertIn("偏好函数式编程", content)
            # 原 "背景" section 与 "用户是 Python 开发者" 仍保留
            self.assertIn("用户是 Python 开发者", content)
            self.assertIn("## 背景", content)
            self.assertIn("## 偏好", content)

            # consolidate 正常完成（LLM 返回空 facts，统计为 0）
            self.assertEqual(stats["facts_extracted"], 0)

    def test_consolidate_without_memory_md_manager_warns_and_clears(self):
        """未注入 memory_md_manager 时 pending 队列被丢弃并清空（向后兼容）。"""
        llm_client = MagicMock()
        llm_client.chat_consolidation.return_value = _make_llm_response("[]")
        chroma_store = MagicMock()

        engine = ConsolidationEngine(
            llm_client=llm_client,
            chroma_store=chroma_store,
            memory_md_manager=None,  # 未注入
            threshold=1,
        )
        engine.add_info({"role": "user", "content": "msg"})
        engine.enqueue_profile_update("add", "背景", "x")
        self.assertEqual(len(engine.pending_profile_updates), 1)

        engine.consolidate()

        # 队列已清空（即使未实际写入文件）
        self.assertEqual(engine.pending_profile_updates, [])


# ===========================================================================
# 4. apply_profile_updates 的 add/replace/delete 操作
# ===========================================================================


class TestApplyProfileUpdatesOperations(unittest.TestCase):
    """验证 MemoryMdManager.apply_profile_updates 的 add/replace/delete 语义。"""

    def setUp(self):
        """每个测试创建独立的临时 memory.md。"""
        self.tmpdir = tempfile.mkdtemp(prefix="update_profile_test_")
        self.memory_md_path = os.path.join(self.tmpdir, "memory.md")
        self.manager = MemoryMdManager(file_path=self.memory_md_path)
        # 初始 memory.md 含两个 section
        self.manager._write_raw_text(
            "# 用户画像\n"
            "\n"
            "## 背景\n"
            "- 用户是 Python 开发者\n"
            "\n"
            "## 偏好\n"
            "- 喜欢简洁的代码\n"
            "- 偏好暗色主题\n"
        )

    def tearDown(self):
        """清理临时目录。"""
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_to_existing_section(self):
        """add 在已存在 section 末尾追加内容，保留原 body。"""
        self.manager.apply_profile_updates(
            [{"action": "add", "section": "背景", "content": "- 也写 TypeScript"}]
        )
        content = self.manager.read()
        # 原 "用户是 Python 开发者" 仍存在
        self.assertIn("用户是 Python 开发者", content)
        # 新内容已追加
        self.assertIn("也写 TypeScript", content)
        # "## 背景" 标题仍只有一个
        self.assertEqual(content.count("## 背景"), 1)
        # "偏好" section 未受影响
        self.assertIn("喜欢简洁的代码", content)

    def test_add_to_nonexistent_section_creates_new(self):
        """add 到不存在的 section 时新建该 section。"""
        self.manager.apply_profile_updates(
            [{"action": "add", "section": "技术栈", "content": "- Rust\n- Go"}]
        )
        content = self.manager.read()
        self.assertIn("## 技术栈", content)
        self.assertIn("Rust", content)
        self.assertIn("Go", content)
        # 原 section 仍保留
        self.assertIn("## 背景", content)
        self.assertIn("## 偏好", content)

    def test_replace_existing_section(self):
        """replace 替换已存在 section 的全部 body，保留标题行。"""
        self.manager.apply_profile_updates(
            [
                {
                    "action": "replace",
                    "section": "偏好",
                    "content": "- 只喜欢函数式编程",
                }
            ]
        )
        content = self.manager.read()
        # 标题仍存在
        self.assertIn("## 偏好", content)
        # 新内容存在
        self.assertIn("只喜欢函数式编程", content)
        # 原 body 被替换掉
        self.assertNotIn("喜欢简洁的代码", content)
        self.assertNotIn("偏好暗色主题", content)
        # "## 偏好" 标题仍只有一个
        self.assertEqual(content.count("## 偏好"), 1)

    def test_replace_nonexistent_section_creates_new(self):
        """replace 到不存在的 section 时新建该 section。"""
        self.manager.apply_profile_updates(
            [
                {
                    "action": "replace",
                    "section": "目标",
                    "content": "- 学完 Rust",
                }
            ]
        )
        content = self.manager.read()
        self.assertIn("## 目标", content)
        self.assertIn("学完 Rust", content)

    def test_delete_existing_section(self):
        """delete 删除已存在 section 的标题与全部 body。"""
        self.manager.apply_profile_updates(
            [{"action": "delete", "section": "偏好"}]
        )
        content = self.manager.read()
        # "偏好" section 整体被删除
        self.assertNotIn("## 偏好", content)
        self.assertNotIn("喜欢简洁的代码", content)
        self.assertNotIn("偏好暗色主题", content)
        # 其他 section 未受影响
        self.assertIn("## 背景", content)
        self.assertIn("用户是 Python 开发者", content)

    def test_delete_nonexistent_section_noop(self):
        """delete 不存在的 section 时为 no-op，不影响其他内容。"""
        original = self.manager.read()
        self.manager.apply_profile_updates(
            [{"action": "delete", "section": "不存在的 section"}]
        )
        content = self.manager.read()
        self.assertEqual(content, original)

    def test_batch_operations_in_order(self):
        """批量操作按顺序应用：先 add，再 replace，最后 delete。"""
        self.manager.apply_profile_updates(
            [
                # 在 "背景" 追加一条
                {"action": "add", "section": "背景", "content": "- 新条目"},
                # 替换 "偏好" 全部内容
                {
                    "action": "replace",
                    "section": "偏好",
                    "content": "- 替换后的偏好",
                },
                # 删除 "背景"（包含刚才 add 的条目）
                {"action": "delete", "section": "背景"},
            ]
        )
        content = self.manager.read()
        # "背景" 已被删除（包括 add 进去的 "新条目"）
        self.assertNotIn("## 背景", content)
        self.assertNotIn("用户是 Python 开发者", content)
        self.assertNotIn("新条目", content)
        # "偏好" 已被替换
        self.assertIn("## 偏好", content)
        self.assertIn("替换后的偏好", content)
        self.assertNotIn("喜欢简洁的代码", content)

    def test_add_multiline_content(self):
        """add 多行 content 时全部行被追加到 section。"""
        self.manager.apply_profile_updates(
            [
                {
                    "action": "add",
                    "section": "背景",
                    "content": "- 第二行\n- 第三行",
                }
            ]
        )
        content = self.manager.read()
        self.assertIn("第二行", content)
        self.assertIn("第三行", content)
        # 原 "用户是 Python 开发者" 仍保留
        self.assertIn("用户是 Python 开发者", content)


# ===========================================================================
# 5. PolicyEngine 决策
# ===========================================================================


class TestPolicyEngineUpdateProfileDecision(unittest.TestCase):
    """验证 PolicyEngine 对 update_profile 返回 confirm / high 决策。"""

    def test_default_rules_confirm_update_profile(self):
        """DEFAULT_RULES 中 update_profile 为 confirm。"""
        engine = PolicyEngine()
        decision = engine.check(
            "profile_update",
            {"action": "add", "section": "背景", "content": "x"},
        )
        self.assertEqual(decision.action, "confirm")
        self.assertEqual(decision.risk_level, "high")

    def test_default_rules_contains_update_profile(self):
        """DEFAULT_RULES 列表中包含 update_profile 规则。"""
        rules_for_update = [r for r in DEFAULT_RULES if r.get("tool") == "profile_update"]
        self.assertEqual(len(rules_for_update), 1)
        self.assertEqual(rules_for_update[0]["risk"], "confirm")

    def test_disabled_policy_allows_update_profile(self):
        """enabled=False 时 update_profile 一律放行（与其它工具一致）。"""
        engine = PolicyEngine(enabled=False)
        decision = engine.check(
            "profile_update",
            {"action": "delete", "section": "背景"},
        )
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.risk_level, "low")

    def test_call_tool_introspection_update_profile(self):
        """call_tool 内省：内层为 update_profile 时返回 confirm 决策。"""
        engine = PolicyEngine()
        decision = engine.check(
            "tool_call",
            {"name": "profile_update", "arguments": {}},
        )
        self.assertEqual(decision.action, "confirm")
        self.assertEqual(decision.risk_level, "high")


# ===========================================================================
# 6. register_builtin_tools 向后兼容
# ===========================================================================


class TestRegisterBuiltinToolsBackwardCompat(unittest.TestCase):
    """验证 consolidation_engine=None 时不注册 update_profile（向后兼容）。"""

    def test_no_consolidation_engine_no_update_profile(self):
        """consolidation_engine=None 时 update_profile 工具未注册。"""
        registry = ToolRegistry()
        register_builtin_tools(registry, consolidation_engine=None)
        schemas = registry.get_tools_schema()
        names = [s["name"] for s in schemas]
        self.assertNotIn("profile_update", names)
        # 其他内置工具仍正常注册
        self.assertIn("file_read", names)
        self.assertIn("file_write", names)

    def test_with_consolidation_engine_registers_update_profile(self):
        """consolidation_engine 非 None 时 update_profile 工具被注册为 Core Tier。"""
        registry = ToolRegistry()
        engine = _FakeConsolidationEngine()
        register_builtin_tools(registry, consolidation_engine=engine)
        schemas = registry.get_tools_schema()
        names = [s["name"] for s in schemas]
        self.assertIn("profile_update", names)
        # 验证是完整 schema（Core Tier，含 input_schema）
        update_profile_schema = next(
            s for s in schemas if s["name"] == "profile_update"
        )
        self.assertIn("input_schema", update_profile_schema)
        self.assertNotIn("defer_loading", update_profile_schema)


# ===========================================================================
# 7. 端到端：handler 入队 + consolidate 合并到 memory.md
# ===========================================================================


class TestEndToEndEnqueueAndConsolidate(unittest.TestCase):
    """端到端验证：handler 入队 → consolidate 合并 → memory.md 落盘。"""

    def test_handler_enqueue_then_consolidate_writes_to_memory_md(self):
        """通过 handler 入队，consolidate 后 memory.md 反映修改。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_md_path = os.path.join(tmpdir, "memory.md")
            memory_md_manager = MemoryMdManager(file_path=memory_md_path)
            memory_md_manager._write_raw_text(
                "# 用户画像\n\n## 背景\n- 用户是 Python 开发者\n"
            )

            llm_client = MagicMock()
            llm_client.chat_consolidation.return_value = _make_llm_response("[]")
            chroma_store = MagicMock()

            engine = ConsolidationEngine(
                llm_client=llm_client,
                chroma_store=chroma_store,
                memory_md_manager=memory_md_manager,
                threshold=1,
            )

            # 通过 ToolRegistry + handler 入队（端到端路径）
            registry = ToolRegistry()
            register_builtin_tools(registry, consolidation_engine=engine)

            registry.execute_tool(
                "profile_update",
                {
                    "action": "add",
                    "section": "背景",
                    "content": "- 也写 Rust",
                },
            )
            registry.execute_tool(
                "profile_update",
                {
                    "action": "replace",
                    "section": "偏好",
                    "content": "- 偏好简洁代码",
                },
            )

            # 入队后但 consolidate 前，memory.md 不应被修改（延迟合并）
            pre_content = memory_md_manager.read()
            self.assertNotIn("也写 Rust", pre_content)
            self.assertNotIn("## 偏好", pre_content)

            # 触发 consolidate
            engine.add_info({"role": "user", "content": "msg"})
            engine.consolidate()

            # consolidate 后 memory.md 反映修改
            post_content = memory_md_manager.read()
            self.assertIn("也写 Rust", post_content)
            self.assertIn("## 偏好", post_content)
            self.assertIn("偏好简洁代码", post_content)
            # 原 "用户是 Python 开发者" 仍保留
            self.assertIn("用户是 Python 开发者", post_content)
            # 队列已清空
            self.assertEqual(engine.pending_profile_updates, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
