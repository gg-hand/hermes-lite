"""profile_update 工具三层防线（黑名单 + 长度 + 频次）单元测试。

覆盖规范 9.2 Task 0b SubTask 0b.8-0b.12：
- 0b.8 黑名单拒绝：12 条正则模式覆盖系统架构/项目描述同义词
- 0b.9 长度上限拒绝：>2000 字符的合法用户信息仍拒绝
- 0b.10 频次上限拒绝：单会话 >3 次写入拒绝
- 0b.11 正常入队不误伤：合法用户偏好（含"后端"等敏感词但不属于系统描述）正常入队
- 0b.12 ContextVar 透传：session_id 通过 current_session_id ContextVar 正确传入 handler

运行方式:
    python -m pytest tests/test_update_profile_tool.py -v
"""

from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from src.agent._cancel_context import current_session_id
from src.agent.tools.memory_tools import _register_update_profile
from src.agent.tool_registry import ToolRegistry


# ---------------------------------------------------------------------------
# Mock ConsolidationEngine：仅需 enqueue_profile_update 方法记录调用
# ---------------------------------------------------------------------------


class _MockConsolidationEngine:
    """最小化 ConsolidationEngine mock。

    仅实现 ``enqueue_profile_update``，记录所有入队调用以便断言。
    不涉及真实文件写入或异步合并逻辑。
    """

    def __init__(self) -> None:
        self.enqueued: list = []  # [(action, section, content), ...]

    def enqueue_profile_update(
        self, action: str, section: str, content: str
    ) -> None:
        self.enqueued.append((action, section, content))


# ---------------------------------------------------------------------------
# 测试基类：每个测试方法独立注册工具，保证 session_write_counts 隔离
# ---------------------------------------------------------------------------


class _ProfileToolTestBase(unittest.TestCase):
    """所有 profile_update 测试的公共 fixture。

    每个测试方法 setUp 时重新注册工具，获得独立的 closure（独立
    ``session_write_counts`` dict），避免测试间状态污染。
    """

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.consolidation = _MockConsolidationEngine()
        _register_update_profile(self.registry, self.consolidation)
        # 获取注册的 handler（直接从 _core_tools 取，避免 execute_tool 的参数校验层）
        self.handler = self.registry._core_tools["profile_update"].handler
        # 跟踪 ContextVar token，tearDown 时 reset
        self._session_tokens: list = []

    def tearDown(self) -> None:
        for token in reversed(self._session_tokens):
            try:
                current_session_id.reset(token)
            except (ValueError, LookupError):
                pass
        self._session_tokens.clear()

    def _set_session(self, session_id) -> None:
        """设置 current_session_id ContextVar，tearDown 自动 reset。"""
        token = current_session_id.set(session_id)
        self._session_tokens.append(token)

    def _call(self, action: str, section: str, content: str = "") -> str:
        """便捷调用 handler。"""
        return self.handler(action=action, section=section, content=content)


# ---------------------------------------------------------------------------
# 0b.8 黑名单拒绝测试
# ---------------------------------------------------------------------------


class TestProfileUpdateBlacklist(_ProfileToolTestBase):
    """12 条黑名单正则模式逐条覆盖测试。

    每个测试构造一条命中特定模式的 content，验证 handler 返回
    "拒绝：内容包含系统架构或项目实现细节" 前缀，且未入队。
    """

    def assert_rejected(self, result: str, content_preview: str = "") -> None:
        """断言 handler 返回拒绝消息且未入队。"""
        self.assertTrue(
            result.startswith("拒绝：内容包含系统架构或项目实现细节"),
            f"应被拒绝，实际返回: {result!r} (content={content_preview!r})",
        )
        self.assertEqual(len(self.consolidation.enqueued), 0)

    def test_pattern1_system_architecture(self):
        """原始模式 1：系统架构 / 核心模块 / 服务层 / 编排层 / 部署架构。"""
        result = self._call(
            "add", "系统架构", "系统架构采用微服务，核心模块包括 API 网关"
        )
        self.assert_rejected(result, "系统架构")

    def test_pattern2_source_paths(self):
        """原始模式 2：src/ agent/ llm/ 等代码路径。"""
        result = self._call(
            "replace", "代码结构", "主要代码位于 src/agent/ 目录下"
        )
        self.assert_rejected(result, "src/")

    def test_pattern3_config_files(self):
        """原始模式 3：config.yaml / requirements.txt / .venv / __pycache__。"""
        result = self._call(
            "add", "项目配置", "依赖见 requirements.txt 文件"
        )
        self.assert_rejected(result, "requirements.txt")

    def test_pattern4_tech_stack(self):
        """原始模式 4：FastAPI / uvicorn / ChromaDB / SQLite / Redis / PostgreSQL。"""
        result = self._call(
            "replace", "技术栈", "后端使用 FastAPI + SQLite 存储数据"
        )
        self.assert_rejected(result, "FastAPI")

    def test_pattern5_project_meta(self):
        """原始模式 5：Hermes Lite 是一个 / 项目路径 / 项目作者 / 作者：。"""
        result = self._call(
            "add", "项目信息", "Hermes Lite 是一个个人 agent 项目"
        )
        self.assert_rejected(result, "Hermes Lite")

    def test_pattern6_streaming_arch(self):
        """扩充模式 6：流式架构 / 事件循环 / 异步后端 / AsyncBaseBackend。"""
        result = self._call(
            "replace", "架构", "流式架构基于事件循环实现异步处理"
        )
        self.assert_rejected(result, "流式架构")

    def test_pattern7_core_modules(self):
        """扩充模式 7：react_loop / orchestrator / tool_registry / policy_engine。"""
        result = self._call(
            "add", "模块", "react_loop 是核心循环，orchestrator 负责编排"
        )
        self.assert_rejected(result, "react_loop")

    def test_pattern8_api_key(self):
        """扩充模式 8：API key / DEEPSEEK / ANTHROPIC / OPENAI / access_token。"""
        result = self._call(
            "replace", "凭证", "DEEPSEEK API key 配置在 .env 文件"
        )
        self.assert_rejected(result, "API key")

    def test_pattern9_layered_design(self):
        """扩充模式 9：分为 X 层 / 三层架构 / 分层设计 / 模块化设计。"""
        result = self._call(
            "add", "设计", "系统分为三层架构，每层职责清晰"
        )
        self.assert_rejected(result, "三层架构")

    def test_pattern10_vector_store(self):
        """扩充模式 10：向量库 / embedding / consolidation / condenser / cron_tool。"""
        result = self._call(
            "replace", "存储", "向量库使用 embedding 进行相似度检索"
        )
        self.assert_rejected(result, "向量库")

    def test_pattern11_scheduler(self):
        """扩充模式 11：调度器 / scheduler / 定时任务 / cron 调度。"""
        result = self._call(
            "add", "调度", "调度器 scheduler 负责定时任务执行"
        )
        self.assert_rejected(result, "调度器")

    def test_pattern12_daemon(self):
        """扩充模式 12：守护进程 / daemon / 微服务 / microservice。"""
        result = self._call(
            "replace", "部署", "采用守护进程 daemon 模式运行"
        )
        self.assert_rejected(result, "守护进程")

    def test_blacklist_case_insensitive(self):
        """正则 IGNORECASE 标志：FastAPI / fastapi / FASTAPI 均命中。"""
        result = self._call(
            "add", "stack", "uses fastapi and chromadb"
        )
        self.assert_rejected(result, "fastapi lowercase")

    def test_delete_action_skips_blacklist(self):
        """delete 操作无需 content，不触发黑名单校验。"""
        result = self._call("delete", "系统架构")
        # delete 应正常入队（不校验 content）
        self.assertTrue(result.startswith("已加入待合并队列"))
        self.assertEqual(len(self.consolidation.enqueued), 1)
        self.assertEqual(self.consolidation.enqueued[0][0], "delete")


# ---------------------------------------------------------------------------
# 0b.9 长度上限测试
# ---------------------------------------------------------------------------


class TestProfileUpdateLengthLimit(_ProfileToolTestBase):
    """长度上限 MAX_PROFILE_CONTENT_LEN=4000 测试。

    注：P0 信号池改造后上限从 2000 调整到 4000（配合信号池累积机制放宽）。
    """

    def test_exactly_4000_chars_accepted(self):
        """恰好 4000 字符的合法用户信息正常入队（边界值）。"""
        content = "用户偏好：" + "x" * (4000 - len("用户偏好："))
        self.assertEqual(len(content), 4000)
        result = self._call("add", "偏好", content)
        self.assertTrue(result.startswith("已加入待合并队列"))
        self.assertEqual(len(self.consolidation.enqueued), 1)

    def test_4001_chars_rejected(self):
        """4001 字符触发拒绝（>上限）。"""
        content = "x" * 4001
        result = self._call("add", "偏好", content)
        self.assertTrue(result.startswith("拒绝：内容长度 4001 超过上限 4000"))
        self.assertEqual(len(self.consolidation.enqueued), 0)

    def test_5000_chars_rejected(self):
        """5000 字符的合法用户信息仍被拒绝（长度优先于黑名单）。"""
        # 13 字 × 400 = 5200 字符，确保 > 4000
        content = "用户喜欢 Python 和 Go 语言。" * 400
        self.assertGreater(len(content), 4000)
        result = self._call("replace", "偏好", content)
        self.assertIn("超过上限 4000", result)
        self.assertEqual(len(self.consolidation.enqueued), 0)

    def test_length_check_before_blacklist(self):
        """长度校验先于黑名单：长 content 含系统关键词时返回长度错误而非黑名单错误。"""
        # 构造一条既超长又含系统关键词的 content
        content = "系统架构" + "x" * 5000
        result = self._call("add", "test", content)
        # 应返回长度错误（先校验），而非黑名单错误
        self.assertIn("超过上限", result)
        self.assertNotIn("系统架构或项目实现细节", result)


# ---------------------------------------------------------------------------
# 0b.10 per-session 频次上限测试
# ---------------------------------------------------------------------------


class TestProfileUpdateSessionFrequency(_ProfileToolTestBase):
    """MAX_PROFILE_WRITES_PER_SESSION=5 测试。

    注：P0 信号池改造后上限从 3 调整到 5（配合信号池累积机制放宽）。
    """

    def test_five_writes_allowed_in_same_session(self):
        """同一会话连续 5 次写入均成功入队。"""
        self._set_session("sess_freq_5")
        for i in range(5):
            result = self._call("add", f"section_{i}", f"content_{i}")
            self.assertTrue(
                result.startswith("已加入待合并队列"),
                f"第 {i + 1} 次应成功，实际: {result!r}",
            )
        self.assertEqual(len(self.consolidation.enqueued), 5)

    def test_sixth_write_rejected_in_same_session(self):
        """第 6 次写入触发频次上限拒绝。"""
        self._set_session("sess_freq_6")
        # 前 5 次正常
        for i in range(5):
            self._call("add", f"s{i}", f"c{i}")
        # 第 6 次应被拒绝（注：P0 改造后消息为"add 上限"而非"写入上限"，
        # 因为频次限制仅约束 add，replace/delete 不受限）
        result = self._call("add", "s5", "c5")
        self.assertTrue(result.startswith("拒绝：会话 sess_freq_6 已达单会话 add 上限 5 次"))
        # 入队总数仍为 5
        self.assertEqual(len(self.consolidation.enqueued), 5)

    def test_frequency_limit_isolated_between_sessions(self):
        """不同会话的频次计数相互独立。"""
        # 会话 A 写入 5 次（显式管理 token，避免污染 ContextVar 栈）
        token_a = current_session_id.set("sess_A")
        try:
            for i in range(5):
                self._call("add", f"a{i}", f"c{i}")
        finally:
            current_session_id.reset(token_a)

        # 切换到会话 B（独立计数）
        token_b = current_session_id.set("sess_B")
        try:
            # 会话 B 也应能写入 5 次（独立计数）
            for i in range(5):
                result = self._call("add", f"b{i}", f"c{i}")
                self.assertTrue(result.startswith("已加入待合并队列"))
        finally:
            current_session_id.reset(token_b)
        # 总入队 10 次
        self.assertEqual(len(self.consolidation.enqueued), 10)

    def test_no_session_id_skips_frequency_limit(self):
        """session_id 为 None 时（测试/cron 路径）跳过频次限制。"""
        # 不设置 ContextVar，session_id 默认 None
        for i in range(10):
            result = self._call("add", f"s{i}", f"c{i}")
            self.assertTrue(result.startswith("已加入待合并队列"))
        self.assertEqual(len(self.consolidation.enqueued), 10)

    def test_frequency_count_increments_only_after_enqueue_success(self):
        """入队失败时不累加频次计数（避免失败调用浪费配额）。"""
        self._set_session("sess_fail")

        # 构造一个会让 enqueue 抛异常的 consolidation engine
        class _FailingConsolidation:
            def __init__(self):
                self.call_count = 0

            def enqueue_profile_update(self, action, section, content):
                self.call_count += 1
                raise RuntimeError("mock enqueue failure")

        # 重新注册工具，使用 failing consolidation
        registry = ToolRegistry()
        failing = _FailingConsolidation()
        _register_update_profile(registry, failing)
        handler = registry._core_tools["profile_update"].handler

        # 第一次调用就失败
        result = handler(action="add", section="s", content="c")
        self.assertIn("入队失败", result)
        self.assertEqual(failing.call_count, 1)

        # 第二次调用仍应被允许（频次计数未累加）
        # 但 failing consolidation 仍会失败，所以再次返回"入队失败"
        result2 = handler(action="add", section="s2", content="c2")
        self.assertIn("入队失败", result2)
        self.assertEqual(failing.call_count, 2)


# ---------------------------------------------------------------------------
# 0b.11 正常入队不误伤测试
# ---------------------------------------------------------------------------


class TestProfileUpdateNormalEnqueue(_ProfileToolTestBase):
    """合法用户信息不误伤测试。

    验证含"后端"/"前端"/"框架"等敏感词但语义属于合法用户画像的 content
    不被黑名单误拒。
    """

    def test_backend_engineer_not_rejected(self):
        """"用户是后端工程师" 含"后端"但属合法用户画像，应正常入队。"""
        result = self._call("add", "背景", "用户是后端工程师，主力语言 Python 和 Go")
        self.assertTrue(result.startswith("已加入待合并队列"))
        self.assertEqual(len(self.consolidation.enqueued), 1)
        self.assertEqual(self.consolidation.enqueued[0][1], "背景")

    def test_frontend_preference_not_rejected(self):
        """"偏好前端开发" 含"前端"但属合法偏好，应正常入队。"""
        result = self._call("replace", "偏好", "用户偏好前端开发，使用 React 框架")
        self.assertTrue(result.startswith("已加入待合并队列"))

    def test_tech_stack_personal_not_rejected(self):
        """"用户技术栈包括 Python" 不含系统描述关键词，应正常入队。"""
        result = self._call("add", "技术栈", "用户技术栈包括 Python、Go、JavaScript")
        self.assertTrue(result.startswith("已加入待合并队列"))

    def test_personal_habits_not_rejected(self):
        """用户个人习惯描述，无系统关键词，应正常入队。"""
        result = self._call(
            "add", "习惯",
            "习惯早上 9 点开始工作，下午 5 点结束。喜欢使用 Vim 编辑器。"
        )
        self.assertTrue(result.startswith("已加入待合并队列"))

    def test_empty_content_add_rejected_at_param_check(self):
        """add 操作空 content 在参数校验阶段被拒绝（早于黑名单）。"""
        result = self._call("add", "测试", "")
        self.assertIn("需要 content", result)
        self.assertEqual(len(self.consolidation.enqueued), 0)

    def test_invalid_action_rejected(self):
        """非法 action 在参数校验阶段被拒绝。"""
        result = self._call("invalid_action", "测试", "content")
        self.assertIn("action 必须是 add/replace/delete", result)

    def test_empty_section_rejected(self):
        """空 section 在参数校验阶段被拒绝。"""
        result = self._call("add", "", "content")
        self.assertIn("section 不能为空", result)

    def test_delete_action_enqueued_without_content(self):
        """delete 操作忽略 content，正常入队。"""
        result = self._call("delete", "测试")
        self.assertTrue(result.startswith("已加入待合并队列"))
        # 验证入队的 content 为空串
        self.assertEqual(self.consolidation.enqueued[0], ("delete", "测试", ""))


# ---------------------------------------------------------------------------
# 0b.12 ContextVar session_id 透传测试
# ---------------------------------------------------------------------------


class TestProfileUpdateContextVar(_ProfileToolTestBase):
    """验证 current_session_id ContextVar 透传到 handler。"""

    def test_session_id_visible_in_handler(self):
        """设置 ContextVar 后 handler 能读取 session_id 并应用频次限制。"""
        self._set_session("ctx_test_sess")
        # 前 5 次正常
        for i in range(5):
            result = self._call("add", f"s{i}", f"c{i}")
            self.assertTrue(result.startswith("已加入待合并队列"))
        # 第 6 次因频次上限拒绝，错误消息含 session_id
        result = self._call("add", "s5", "c5")
        self.assertIn("ctx_test_sess", result)

    def test_no_session_id_no_frequency_limit(self):
        """未设置 ContextVar 时 session_id 为 None，跳过频次限制。"""
        # 默认 current_session_id.get() 返回 None
        self.assertIsNone(current_session_id.get())
        # 调用 6 次都应成功
        for i in range(6):
            result = self._call("add", f"s{i}", f"c{i}")
            self.assertTrue(result.startswith("已加入待合并队列"))

    def test_session_id_reset_after_token_reset(self):
        """reset token 后 session_id 恢复 None，频次限制不再生效。"""
        token = current_session_id.set("reset_test")
        try:
            # 写入 5 次（达到上限）
            for i in range(5):
                self._call("add", f"s{i}", f"c{i}")
            # 第 6 次应被拒绝（P0 改造后消息为"add 上限"）
            result = self._call("add", "s5", "c5")
            self.assertIn("已达单会话 add 上限", result)
        finally:
            current_session_id.reset(token)
            # 从 self._session_tokens 移除，避免 tearDown 重复 reset
            if token in self._session_tokens:
                self._session_tokens.remove(token)

        # reset 后 session_id 为 None
        self.assertIsNone(current_session_id.get())
        # 现在再调用应跳过频次限制（session_id 为 None）
        result = self._call("add", "after_reset", "content")
        self.assertTrue(result.startswith("已加入待合并队列"))

    def test_session_id_isolated_between_threads(self):
        """不同线程的 ContextVar 互不影响（线程隔离性）。"""
        import threading

        results = {}

        def worker(thread_name, count):
            token = current_session_id.set(thread_name)
            try:
                for i in range(count):
                    r = self._call("add", f"{thread_name}_{i}", f"c{i}")
                    results.setdefault(thread_name, []).append(r)
            finally:
                current_session_id.reset(token)

        # 线程 A 写入 6 次（第 6 次应失败）
        t_a = threading.Thread(target=worker, args=("thread_A", 6))
        # 线程 B 写入 6 次（第 6 次应失败）
        t_b = threading.Thread(target=worker, args=("thread_B", 6))

        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        # 每个线程前 5 次成功，第 6 次失败
        for thread_name in ("thread_A", "thread_B"):
            thread_results = results[thread_name]
            self.assertEqual(len(thread_results), 6)
            for i in range(5):
                self.assertTrue(
                    thread_results[i].startswith("已加入待合并队列"),
                    f"{thread_name} 第 {i + 1} 次应成功",
                )
            self.assertIn(
                "已达单会话 add 上限", thread_results[5],
                f"{thread_name} 第 6 次应被频次限制拒绝",
            )


# ---------------------------------------------------------------------------
# 端到端：通过 execute_tool 调用（验证完整链路）
# ---------------------------------------------------------------------------


class TestProfileUpdateViaExecuteTool(_ProfileToolTestBase):
    """通过 ToolRegistry.execute_tool 调用，验证完整链路（含参数校验层）。"""

    def test_execute_tool_blacklist_rejection(self):
        """execute_tool 路径下黑名单仍生效。"""
        result = self.registry.execute_tool(
            "profile_update",
            {
                "action": "add",
                "section": "架构",
                "content": "系统采用 FastAPI + ChromaDB 三层架构",
            },
        )
        self.assertIn("系统架构或项目实现细节", result)
        self.assertEqual(len(self.consolidation.enqueued), 0)

    def test_execute_tool_normal_enqueue(self):
        """execute_tool 路径下正常 content 入队。"""
        result = self.registry.execute_tool(
            "profile_update",
            {
                "action": "add",
                "section": "背景",
                "content": "用户是后端工程师",
            },
        )
        self.assertIn("已加入待合并队列", result)
        self.assertEqual(len(self.consolidation.enqueued), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
