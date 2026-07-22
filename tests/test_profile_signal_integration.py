"""用户画像意识建模机制集成测试。

覆盖 P0 计划中 L1/L3 改造的端到端链路：
- L1 profile_update 工具：add 走信号池累积，replace/delete 直接入 pending 队列
- L3 consolidation：user_profile facts 走信号池（weight=2），未注入时回退
- 硬上限 8000：apply_profile_updates 拒绝超限 add
- 第二道去重防线：_dedupe_add_updates 过滤 section 内相似 add
- 黑名单扩充：新增 2 条一次性上下文正则
- mark_written_by_contents：consolidate apply 后回写信号状态

运行方式:
    python -m pytest tests/test_profile_signal_integration.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from teage_liu.agent._cancel_context import current_session_id
from teage_liu.agent.tools.memory_tools import _register_update_profile
from teage_liu.agent.tool_registry import ToolRegistry
from teage_liu.memory.consolidation import ConsolidationEngine
from teage_liu.memory.memory_md import MemoryMdManager
from teage_liu.memory.signal_pool import SignalPool


# ---------------------------------------------------------------------------
# Mock 工具：记录 enqueue 调用、最小化 LLM 客户端
# ---------------------------------------------------------------------------


class _MockConsolidationEngine:
    """记录 enqueue_profile_update 调用的最小化 mock。

    用于 L1 工具层测试：signal_pool 注入后 add 不应触发 enqueue，
    replace/delete 应触发 enqueue。
    """

    def __init__(self) -> None:
        self.enqueued: List[tuple] = []

    def enqueue_profile_update(
        self, action: str, section: str, content: str
    ) -> None:
        self.enqueued.append((action, section, content))


class _MockLLMResponse:
    """最小化 LLM 响应 mock，提供 .content 属性（与 anthropic SDK 一致）。

    consolidation._extract_response_text 通过 .content 遍历 block 列表，
    每个 block 期望是 dict 含 type/text 字段。
    """

    def __init__(self, text: str) -> None:
        self.content = [{"type": "text", "text": text}]


class _MockLLMClient:
    """最小化 LLM 客户端 mock，返回固定的 user_profile facts。

    用于 L3 改造测试：consolidate() 调用 chat_consolidation_sync 时返回
    预设的 facts 列表（包装在 _MockLLMResponse 中），验证 facts 是否走
    signal_pool.add。
    """

    def __init__(self, facts: List[Dict[str, Any]]) -> None:
        self._facts = facts

    def chat_consolidation_sync(
        self, messages: List[Dict[str, Any]], system: str = "",
        reasoning_cfg: Any = None,
    ) -> _MockLLMResponse:
        """返回 JSON 格式的事实列表（包装为 _MockLLMResponse）。"""
        import json

        text = json.dumps({"facts": self._facts}, ensure_ascii=False)
        return _MockLLMResponse(text)


class _MockChromaStore:
    """空实现 chroma_store 接口，consolidate 中 fact 写入走空操作。"""

    def find_duplicates(self, content: str, namespace: str = "user", **kwargs):
        return []

    def add_memory(self, content: str, **kwargs) -> str:
        return "mock_id"

    def update_memory(self, memory_id: str, content: str, **kwargs) -> None:
        pass

    def delete_memory(self, memory_id: str) -> None:
        pass


class _CapturingConsolidationEngine(ConsolidationEngine):
    """扩展 ConsolidationEngine，捕获 signal_pool.add 调用。

    用于 L3 改造测试：通过覆盖 signal_pool 为捕获版，验证 L3 facts
    走 signal_pool 而非 memory_md_writer。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.l3_adds: List[Dict[str, Any]] = []

    def _make_capture_pool(self, real_pool: SignalPool) -> SignalPool:
        """包装真实 signal_pool，捕获 add 调用后转发。"""
        capture = self

        class _CapturePool:
            """代理 signal_pool，捕获 add 调用。"""

            THRESHOLD = real_pool.THRESHOLD

            def add(
                self,
                content: str,
                source: str,
                category: str = "",
                weight: int = 1,
                section: str = "沉淀笔记",
            ) -> None:
                capture.l3_adds.append(
                    {
                        "content": content,
                        "source": source,
                        "weight": weight,
                        "section": section,
                    }
                )
                real_pool.add(
                    content=content,
                    source=source,
                    category=category,
                    weight=weight,
                    section=section,
                )

            def mark_written_by_contents(self, contents: List[str]) -> None:
                real_pool.mark_written_by_contents(contents)

            def cleanup(self) -> None:
                real_pool.cleanup()

            def get_status(self) -> List[Dict[str, Any]]:
                return real_pool.get_status()

            def flush(self) -> None:
                real_pool.flush()

        return _CapturePool()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 测试基类
# ---------------------------------------------------------------------------


class _IntegrationTestBase(unittest.TestCase):
    """集成测试公共 fixture。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.pool_path = self.tmpdir / "signal_pool.json"
        self.profile_path = self.tmpdir / "memory.md"
        # 初始画像（含一个 section 供查重测试）
        self._write_profile("# 用户画像\n\n## 背景\n\n用户是后端工程师\n")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_profile(self, text: str) -> None:
        self.profile_path.write_text(text, encoding="utf-8")

    def _make_signal_pool(
        self, consolidation_engine=None
    ) -> SignalPool:
        """创建真实 SignalPool 实例（持久化到临时目录）。"""
        return SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=consolidation_engine,
            profile_path=self.profile_path,
        )

    def _set_session(self, session_id: str) -> Any:
        return current_session_id.set(session_id)


# ---------------------------------------------------------------------------
# 1. L1 改造：profile_update 工具操作分流
# ---------------------------------------------------------------------------


class TestL1ActionDispatch(_IntegrationTestBase):
    """L1 profile_update 工具 add/replace/delete 操作分流测试。

    验证：
    - add 走 signal_pool.add（不入 enqueue 队列）
    - replace/delete 直接入 enqueue 队列（不走信号池）
    - signal_pool 为 None 时 add 回退到 enqueue
    - 频次限制仅约束 add，不约束 replace/delete
    """

    def setUp(self) -> None:
        super().setUp()
        self.registry = ToolRegistry()
        self.consolidation = _MockConsolidationEngine()
        self.pool = self._make_signal_pool(self.consolidation)
        _register_update_profile(
            self.registry, self.consolidation, signal_pool=self.pool
        )
        self.handler = self.registry._core_tools["profile_update"].handler
        self._session_tokens: List[Any] = []

    def tearDown(self) -> None:
        for token in reversed(self._session_tokens):
            try:
                current_session_id.reset(token)
            except (ValueError, LookupError):
                pass
        self._session_tokens.clear()
        super().tearDown()

    def _set_session(self, session_id: str) -> None:
        token = current_session_id.set(session_id)
        self._session_tokens.append(token)

    def test_add_goes_through_signal_pool(self) -> None:
        """add 操作走 signal_pool.add，不触发 enqueue_profile_update。"""
        result = self.handler(action="add", section="偏好", content="喜欢二次元动漫")
        self.assertIn("信号已加入池累积", result)
        # enqueue 不应被调用（add 走信号池）
        self.assertEqual(len(self.consolidation.enqueued), 0)
        # 信号池应有 1 条信号
        self.assertEqual(len(self.pool.get_status()), 1)

    def test_replace_bypasses_signal_pool(self) -> None:
        """replace 操作直接入 pending 队列，不走信号池。"""
        result = self.handler(
            action="replace", section="偏好", content="用户偏好简洁回复风格"
        )
        self.assertIn("已加入待合并队列", result)
        # enqueue 应被调用 1 次
        self.assertEqual(len(self.consolidation.enqueued), 1)
        self.assertEqual(self.consolidation.enqueued[0][0], "replace")
        # 信号池不应有信号
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_delete_bypasses_signal_pool(self) -> None:
        """delete 操作直接入 pending 队列，不走信号池。"""
        result = self.handler(action="delete", section="偏好")
        self.assertIn("已加入待合并队列", result)
        self.assertEqual(len(self.consolidation.enqueued), 1)
        self.assertEqual(self.consolidation.enqueued[0][0], "delete")
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_add_falls_back_to_enqueue_when_pool_none(self) -> None:
        """signal_pool 未注入时，add 回退到直接入 pending 队列（向后兼容）。"""
        registry = ToolRegistry()
        consolidation = _MockConsolidationEngine()
        _register_update_profile(registry, consolidation, signal_pool=None)
        handler = registry._core_tools["profile_update"].handler

        result = handler(action="add", section="偏好", content="喜欢二次元动漫")
        self.assertIn("已加入待合并队列", result)
        self.assertEqual(len(consolidation.enqueued), 1)
        self.assertEqual(consolidation.enqueued[0][0], "add")

    def test_frequency_limit_only_applies_to_add(self) -> None:
        """频次限制仅约束 add，replace/delete 不受限。"""
        self._set_session("sess_dispatch")

        # 连续 5 次 add 触发频次上限
        for i in range(5):
            result = self.handler(
                action="add", section=f"sec_{i}", content=f"偏好内容_{i}"
            )
            self.assertIn("信号已加入池累积", result)

        # 第 6 次 add 应被拒绝
        result = self.handler(action="add", section="sec_5", content="偏好内容_5")
        self.assertIn("已达单会话 add 上限", result)

        # replace 不受频次限制，仍可执行
        result = self.handler(
            action="replace", section="sec_5", content="替换内容"
        )
        self.assertIn("已加入待合并队列", result)

        # delete 不受频次限制，仍可执行
        result = self.handler(action="delete", section="sec_5")
        self.assertIn("已加入待合并队列", result)

        # enqueue 应有 2 条（1 replace + 1 delete）
        self.assertEqual(len(self.consolidation.enqueued), 2)


# ---------------------------------------------------------------------------
# 2. L1 改造：信号累积达阈值触发入队
# ---------------------------------------------------------------------------


class TestL1ThresholdTrigger(_IntegrationTestBase):
    """L1 信号累积达阈值（7）后触发入 pending 队列测试。"""

    def setUp(self) -> None:
        super().setUp()
        self.registry = ToolRegistry()
        self.consolidation = _MockConsolidationEngine()
        self.pool = self._make_signal_pool(self.consolidation)
        _register_update_profile(
            self.registry, self.consolidation, signal_pool=self.pool
        )
        self.handler = self.registry._core_tools["profile_update"].handler

    def tearDown(self) -> None:
        super().tearDown()

    def test_seven_adds_trigger_enqueue(self) -> None:
        """同一信号累积 7 次后触发 enqueue_profile_update。"""
        # 画像清空，避免查重干扰
        self._write_profile("")
        self.pool._profile_text_hash = None

        # v2: 用非情感动词内容，避免"用户偏好"触发情感增强使 count 翻倍
        # 前 6 次：累积但未达阈值
        for _ in range(6):
            self.handler(action="add", section="习惯", content="用户是后端工程师")
        self.assertEqual(len(self.consolidation.enqueued), 0)
        # 信号池有 1 条 pending 信号，count=6
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["count"], 6)
        self.assertEqual(status[0]["status"], "pending")

        # 第 7 次：达阈值，触发 enqueue
        self.handler(action="add", section="习惯", content="用户是后端工程师")
        # enqueue 被调用 1 次
        self.assertEqual(len(self.consolidation.enqueued), 1)
        self.assertEqual(self.consolidation.enqueued[0][0], "add")
        self.assertEqual(self.consolidation.enqueued[0][1], "习惯")
        # 信号状态变 triggered
        status = self.pool.get_status()
        self.assertEqual(status[0]["status"], "triggered")

    def test_emotion_boost_accelerates_threshold(self) -> None:
        """情感增强器加速阈值达成：'我爱 Rust' 每次 +4（基础1 + 强情感3）。"""
        self._write_profile("")
        self.pool._profile_text_hash = None

        # 3 次"我爱 Rust" → 每次 +4 → count=12 ≥ 7，触发
        for _ in range(3):
            self.handler(action="add", section="技术栈", content="我爱 Rust")
        # 应已触发 enqueue
        self.assertEqual(len(self.consolidation.enqueued), 1)
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 12)
        self.assertEqual(status[0]["status"], "triggered")


# ---------------------------------------------------------------------------
# 3. L3 改造：consolidation 走信号池
# ---------------------------------------------------------------------------


class TestL3ConsolidationDispatch(_IntegrationTestBase):
    """L3 consolidation 中 user_profile facts 走信号池测试。

    验证：
    - signal_pool 注入时 L3 facts 走 signal_pool.add（weight=2）
    - signal_pool 未注入时回退到 memory_md_writer（向后兼容）
    """

    def setUp(self) -> None:
        super().setUp()
        # 画像清空避免查重
        self._write_profile("")

    def _make_consolidation_engine(
        self,
        llm_facts: List[Dict[str, Any]],
        memory_md_manager: Any = None,
        signal_pool: Any = None,
    ) -> ConsolidationEngine:
        return ConsolidationEngine(
            llm_client=_MockLLMClient(llm_facts),
            chroma_store=_MockChromaStore(),
            memory_md_manager=memory_md_manager,
            signal_pool=signal_pool,
        )

    def test_l3_facts_go_through_signal_pool(self) -> None:
        """signal_pool 注入时，L3 user_profile facts 走 signal_pool.add。"""
        facts = [
            {"type": "user_profile", "content": "用户主力语言是 Python"},
            # v2: 避免"用户偏好"触发情感增强使 count=3，改用非情感动词内容
            {"type": "user_profile", "content": "用户编写简洁的代码"},
            {"type": "other", "content": "本次对话讨论了画像机制"},
        ]
        pool = self._make_signal_pool()
        engine = self._make_consolidation_engine(facts, signal_pool=pool)

        # 注入一些 pending 消息触发 consolidate
        for i in range(15):
            engine.add_info({"role": "user", "content": f"消息 {i}"})

        engine.consolidate(session_id="test_sess")

        # 信号池应有 2 条信号（user_profile facts，other 类型不入池）
        status = pool.get_status()
        self.assertEqual(len(status), 2)
        sources = {s["sources"][0] for s in status}
        self.assertIn("L3", sources)
        # 每条 L3 信号 weight=2，count 应为 2
        for s in status:
            self.assertEqual(s["count"], 2)

    def test_l3_facts_fallback_to_memory_md_writer(self) -> None:
        """signal_pool 未注入时，L3 facts 回退到 memory_md_writer。"""
        facts = [
            {"type": "user_profile", "content": "用户喜欢 Go 语言"},
        ]

        # 用 MemoryMdManager 真实实例作为 memory_md_writer 的载体
        manager = MemoryMdManager(file_path=str(self.profile_path))

        # memory_md_writer 回调：调 manager.write（按 fact 类别合并）
        written_facts: List[List[Dict[str, Any]]] = []

        def writer(facts_list: List[Dict[str, Any]]) -> None:
            written_facts.append(facts_list)
            for fact in facts_list:
                manager.write(fact.get("content", ""), fact.get("type", "其他"))

        engine = ConsolidationEngine(
            llm_client=_MockLLMClient(facts),
            chroma_store=_MockChromaStore(),
            memory_md_writer=writer,
            signal_pool=None,  # 未注入
        )

        for i in range(15):
            engine.add_info({"role": "user", "content": f"msg {i}"})

        engine.consolidate(session_id="test_sess")

        # memory_md_writer 应被调用 1 次，含 1 条 fact
        self.assertEqual(len(written_facts), 1)
        self.assertEqual(len(written_facts[0]), 1)
        self.assertEqual(written_facts[0][0]["content"], "用户喜欢 Go 语言")

    def test_l3_emotion_boost_accelerates_threshold(self) -> None:
        """L3 facts 含情感词时也走情感增强：'我爱 Rust' weight=2+3=5。"""
        facts = [{"type": "user_profile", "content": "我爱 Rust"}]
        pool = self._make_signal_pool()
        engine = self._make_consolidation_engine(facts, signal_pool=pool)

        for i in range(15):
            engine.add_info({"role": "user", "content": f"msg {i}"})

        engine.consolidate(session_id="test_sess")

        status = pool.get_status()
        self.assertEqual(len(status), 1)
        # weight=2 + emotion_boost=3（强情感"爱"）= 5
        self.assertEqual(status[0]["count"], 5)

    def test_l3_cron_session_skips_signal_pool(self) -> None:
        """cron 会话 consolidate 时 L3 user_profile facts 不入信号池。

        cron 与普通用户会话隔离，避免自动任务提取的"事实"污染用户画像。
        cron namespace 的事实仍正常写入向量库（其他类型 fact），仅跳过
        user_profile 类事实的画像写入。
        """
        facts = [
            {"type": "user_profile", "content": "用户主力语言是 Python"},
            {"type": "other", "content": "cron 任务执行了某操作"},
        ]
        pool = self._make_signal_pool()
        engine = self._make_consolidation_engine(facts, signal_pool=pool)

        for i in range(15):
            engine.add_info({"role": "user", "content": f"msg {i}"})

        # cron 会话
        engine.consolidate(session_id="cron:test_schedule_l3")

        # 信号池应为空：cron 会话跳过 user_profile facts 入池
        status = pool.get_status()
        self.assertEqual(len(status), 0)


# ---------------------------------------------------------------------------
# 4. mark_written_by_contents 回写
# ---------------------------------------------------------------------------


class TestMarkWrittenAfterApply(_IntegrationTestBase):
    """consolidate _apply_pending_ops 成功后调 mark_written_by_contents 测试。

    验证：consolidate 完成后，已写入画像的 triggered 信号被标记为 written。
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_profile("")

    def _make_engine_with_manager(
        self, pool: SignalPool
    ) -> ConsolidationEngine:
        manager = MemoryMdManager(file_path=str(self.profile_path))
        # 空 facts，仅触发 _apply_pending_ops
        engine = ConsolidationEngine(
            llm_client=_MockLLMClient([]),
            chroma_store=_MockChromaStore(),
            memory_md_manager=manager,
            signal_pool=pool,
        )
        return engine

    def test_triggered_signal_marked_written_after_apply(self) -> None:
        """triggered 信号在 _apply_pending_ops 应用后从信号池移除。"""
        pool = self._make_signal_pool()
        engine = self._make_engine_with_manager(pool)

        # 手动构造一条 triggered 信号（模拟达阈值后的状态）
        # 关键词使用 _extract_keywords 真实产出，确保 mark_written_by_contents
        # 的 Jaccard 匹配能命中
        from teage_liu.memory.signal_pool import Signal, _extract_keywords, _now_iso

        with pool._lock:
            pool._signals.append(
                Signal(
                    id="sig_test_001",
                    content="用户偏好简洁回复",
                    keywords=list(_extract_keywords("用户偏好简洁回复")),
                    count=7,
                    sources=["L1"],
                    first_seen=_now_iso(),
                    last_seen=_now_iso(),
                    status="triggered",
                    section="习惯",
                )
            )
            pool._save_debounced()

        # 入 pending 队列（与 triggered 信号 content 一致）
        engine.enqueue_profile_update(
            "add", "习惯", "用户偏好简洁回复"
        )

        # 触发 consolidate 应用 pending
        for i in range(15):
            engine.add_info({"role": "user", "content": f"msg {i}"})
        engine.consolidate(session_id="test_sess")

        # v5: 信号写入画像后立即从信号池移除（不再保留 written 状态）
        status = pool.get_status()
        self.assertEqual(len(status), 0)

    def test_unrelated_triggered_signal_not_marked(self) -> None:
        """未在本次 apply 中的 triggered 信号不被标记为 written。"""
        pool = self._make_signal_pool()
        engine = self._make_engine_with_manager(pool)

        from teage_liu.memory.signal_pool import Signal, _now_iso

        with pool._lock:
            # 两条 triggered 信号，仅一条会在本次 apply 中写入
            pool._signals.append(
                Signal(
                    id="sig_apply",
                    content="用户偏好简洁回复",
                    keywords=["偏好", "简洁"],
                    count=7,
                    sources=["L1"],
                    first_seen=_now_iso(),
                    last_seen=_now_iso(),
                    status="triggered",
                    section="习惯",
                )
            )
            pool._signals.append(
                Signal(
                    id="sig_not_apply",
                    content="用户喜欢二次元",
                    keywords=["喜欢", "二次元"],
                    count=7,
                    sources=["L1"],
                    first_seen=_now_iso(),
                    last_seen=_now_iso(),
                    status="triggered",
                    section="兴趣",
                )
            )
            pool._save_debounced()

        # 仅入队 sig_apply
        engine.enqueue_profile_update("add", "习惯", "用户偏好简洁回复")

        for i in range(15):
            engine.add_info({"role": "user", "content": f"msg {i}"})
        engine.consolidate(session_id="test_sess")

        status = pool.get_status()
        statuses = {s["content"]: s["status"] for s in status}
        # v5: 已写入画像的信号从信号池移除，不在 status 中
        self.assertNotIn("用户偏好简洁回复", statuses)
        # 未在 apply 中的信号仍为 triggered
        self.assertEqual(statuses["用户喜欢二次元"], "triggered")


# ---------------------------------------------------------------------------
# 5. 硬上限 8000 测试
# ---------------------------------------------------------------------------


class TestProfileHardLimit(unittest.TestCase):
    """apply_profile_updates 硬上限 MAX_PROFILE_TOTAL_CHARS=8000 测试。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.profile_path = Path(self._tmpdir.name) / "memory.md"
        self.manager = MemoryMdManager(file_path=str(self.profile_path))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_under_limit_add_accepted(self) -> None:
        """画像总长度 < 8000 时 add 操作正常应用。"""
        # 初始画像 1000 字符
        initial = "# 用户画像\n\n## 背景\n\n" + "x" * 1000 + "\n"
        self.profile_path.write_text(initial, encoding="utf-8")

        self.manager.apply_profile_updates(
            [{"action": "add", "section": "偏好", "content": "喜欢简洁回复"}]
        )
        text = self.profile_path.read_text(encoding="utf-8")
        self.assertIn("喜欢简洁回复", text)

    def test_over_limit_add_rejected(self) -> None:
        """画像总长度 > 8000 时拒绝 add 操作（仅 apply replace/delete）。"""
        # 初始画像 7900 字符（接近上限）
        initial = "# 用户画像\n\n## 背景\n\n" + "x" * 7900 + "\n"
        self.profile_path.write_text(initial, encoding="utf-8")

        # 尝试 add 200 字符 → 总长度 8100+ > 8000
        self.manager.apply_profile_updates(
            [
                {"action": "add", "section": "偏好", "content": "y" * 200},
                # replace 不受硬上限影响
                {
                    "action": "replace",
                    "section": "备注",
                    "content": "已替换",
                },
            ]
        )

        text = self.profile_path.read_text(encoding="utf-8")
        # add 应被拒绝（不含 y*200）
        self.assertNotIn("y" * 200, text)
        # replace 应正常应用
        self.assertIn("已替换", text)

    def test_replace_not_blocked_by_hard_limit(self) -> None:
        """replace 操作不受硬上限影响（即使总长度超限也能替换）。"""
        # 初始画像 8500 字符（已超上限）
        initial = "# 用户画像\n\n## 背景\n\n" + "x" * 8500 + "\n"
        self.profile_path.write_text(initial, encoding="utf-8")

        self.manager.apply_profile_updates(
            [
                {
                    "action": "replace",
                    "section": "背景",
                    "content": "替换后的短内容",
                }
            ]
        )

        text = self.profile_path.read_text(encoding="utf-8")
        self.assertIn("替换后的短内容", text)


# ---------------------------------------------------------------------------
# 6. 第二道去重防线：_dedupe_add_updates
# ---------------------------------------------------------------------------


class TestSecondDedupDefense(unittest.TestCase):
    """apply_profile_updates 中 _dedupe_add_updates 第二道去重测试。

    验证：section 已有相似内容时，pending 队列中的 add 操作被跳过。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.profile_path = Path(self._tmpdir.name) / "memory.md"
        self.manager = MemoryMdManager(file_path=str(self.profile_path))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_similar_add_to_existing_section_skipped(self) -> None:
        """section 已有内容与新 add 关键词重叠时跳过。

        memory_md._extract_keywords 按非汉字字符切分（无 2 字滑窗），
        因此需要让两条内容都包含空格/标点切出公共 token：
        - 现有：'偏好 简洁回复 风格' → tokens {偏好, 简洁回复, 风格}
        - 新加：'偏好 详细回复 风格' → tokens {偏好, 详细回复, 风格}
        - 交集 = {偏好, 风格} 非空 → 跳过
        """
        initial = "# 用户画像\n\n## 偏好\n\n偏好 简洁回复 风格\n"
        self.profile_path.write_text(initial, encoding="utf-8")

        # 关键词与现有内容重叠（"偏好"/"风格"）
        self.manager.apply_profile_updates(
            [{"action": "add", "section": "偏好", "content": "偏好 详细回复 风格"}]
        )

        text = self.profile_path.read_text(encoding="utf-8")
        # 新内容应被跳过（不写入）
        self.assertNotIn("偏好 详细回复 风格", text)
        # 原内容保留
        self.assertIn("偏好 简洁回复 风格", text)

    def test_disjoint_add_to_existing_section_applied(self) -> None:
        """section 已有内容与新 add 关键词不重叠时正常写入。

        - 现有：'偏好 简洁回复' → tokens {偏好, 简洁回复}
        - 新加：'喜欢 二次元动漫' → tokens {喜欢, 二次元动漫}
        - 交集 = {} → 应用
        """
        initial = "# 用户画像\n\n## 偏好\n\n偏好 简洁回复\n"
        self.profile_path.write_text(initial, encoding="utf-8")

        self.manager.apply_profile_updates(
            [{"action": "add", "section": "偏好", "content": "喜欢 二次元动漫"}]
        )

        text = self.profile_path.read_text(encoding="utf-8")
        # 新内容应写入
        self.assertIn("喜欢 二次元动漫", text)
        # 原内容保留
        self.assertIn("偏好 简洁回复", text)

    def test_add_to_nonexistent_section_not_deduped(self) -> None:
        """section 不存在时，add 必新建，不走去重。"""
        initial = "# 用户画像\n\n## 背景\n\n用户是工程师\n"
        self.profile_path.write_text(initial, encoding="utf-8")

        self.manager.apply_profile_updates(
            [{"action": "add", "section": "新section", "content": "新内容"}]
        )

        text = self.profile_path.read_text(encoding="utf-8")
        self.assertIn("新内容", text)
        self.assertIn("## 新section", text)

    def test_replace_not_deduped(self) -> None:
        """replace 操作不走去重防线（用户显式替换）。"""
        initial = "# 用户画像\n\n## 偏好\n\n偏好简洁回复\n"
        self.profile_path.write_text(initial, encoding="utf-8")

        self.manager.apply_profile_updates(
            [{"action": "replace", "section": "偏好", "content": "偏好详细回复"}]
        )

        text = self.profile_path.read_text(encoding="utf-8")
        # replace 应替换原内容
        self.assertIn("偏好详细回复", text)
        self.assertNotIn("偏好简洁回复", text)


# ---------------------------------------------------------------------------
# 7. 黑名单扩充：一次性上下文正则
# ---------------------------------------------------------------------------


class TestBlacklistOneShotContext(unittest.TestCase):
    """profile_update 黑名单新增 2 条一次性上下文正则测试。

    新增模式：
    - (今天|现在|当前|正在|这次|刚刚|刚才).{0,20}(改|修|调试|部署|运行|执行|跑|测试|重构|开发)
    - (session_id|会话ID|临时变量|这次任务的具体)
    """

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.consolidation = _MockConsolidationEngine()
        _register_update_profile(
            self.registry, self.consolidation, signal_pool=None
        )
        self.handler = self.registry._core_tools["profile_update"].handler

    def test_today_modifying_rejected(self) -> None:
        """'今天在改 login.py' 命中一次性上下文正则，应拒绝。"""
        result = self.handler(
            action="add", section="任务", content="今天在改 login.py 的认证逻辑"
        )
        self.assertTrue(result.startswith("拒绝：内容包含系统架构或项目实现细节"))
        self.assertEqual(len(self.consolidation.enqueued), 0)

    def test_now_debugging_rejected(self) -> None:
        """'现在正在调试 scheduler 模块' 命中一次性上下文正则。"""
        result = self.handler(
            action="replace", section="状态", content="现在正在调试 scheduler 模块"
        )
        self.assertTrue(result.startswith("拒绝：内容包含系统架构或项目实现细节"))

    def test_currently_running_rejected(self) -> None:
        """'当前正在运行测试' 命中一次性上下文正则。"""
        result = self.handler(
            action="add", section="状态", content="当前正在运行测试套件"
        )
        self.assertTrue(result.startswith("拒绝：内容包含系统架构或项目实现细节"))

    def test_session_id_rejected(self) -> None:
        """'session_id 是 abc123' 命中临时变量正则。"""
        result = self.handler(
            action="add", section="状态", content="session_id 是 abc123"
        )
        self.assertTrue(result.startswith("拒绝：内容包含系统架构或项目实现细节"))

    def test_temp_variable_rejected(self) -> None:
        """'临时变量保存了用户输入' 命中临时变量正则。"""
        result = self.handler(
            action="add", section="状态", content="临时变量保存了用户输入"
        )
        self.assertTrue(result.startswith("拒绝：内容包含系统架构或项目实现细节"))

    def test_stable_preference_not_rejected(self) -> None:
        """'用户偏好简洁回复' 不含一次性上下文，正常入队（不误伤）。"""
        result = self.handler(
            action="add", section="偏好", content="用户偏好简洁回复"
        )
        self.assertTrue(result.startswith("已加入待合并队列"))
        self.assertEqual(len(self.consolidation.enqueued), 1)

    def test_today_in_stable_context_not_rejected(self) -> None:
        """'用户每天早上 9 点开始工作' 中'每天'不命中'今天'，正常入队。"""
        result = self.handler(
            action="add", section="习惯", content="用户每天早上 9 点开始工作"
        )
        # 不应被一次性上下文正则拒绝
        self.assertFalse(result.startswith("拒绝"))
        self.assertEqual(len(self.consolidation.enqueued), 1)


# ---------------------------------------------------------------------------
# 8. 端到端：L1 信号累积 → consolidate → 画像写入
# ---------------------------------------------------------------------------


class TestEndToEndProfileWriting(_IntegrationTestBase):
    """端到端：L1 多次 add → 信号池累积 → consolidate 写入画像 → 标记 written。"""

    def setUp(self) -> None:
        super().setUp()
        self._write_profile("")
        self.manager = MemoryMdManager(file_path=str(self.profile_path))
        # 先创建 pool 不带 consolidation_engine（engine 还没创建）
        self.pool = self._make_signal_pool()
        self.engine = ConsolidationEngine(
            llm_client=_MockLLMClient([]),  # 不产生 L3 facts，仅触发 _apply_pending_ops
            chroma_store=_MockChromaStore(),
            memory_md_manager=self.manager,
            signal_pool=self.pool,
        )
        # 反向注入：让 signal_pool 达阈值后能调 engine.enqueue_profile_update
        self.pool._consolidation_engine = self.engine
        self.registry = ToolRegistry()
        _register_update_profile(
            self.registry, self.engine, signal_pool=self.pool
        )
        self.handler = self.registry._core_tools["profile_update"].handler

    def tearDown(self) -> None:
        super().tearDown()

    def test_seven_adds_trigger_consolidate_writes_profile(self) -> None:
        """7 次 add 同一信号 → 触发入队 → 异步 consolidate 写入画像 → 信号从池中移除。

        v6: 信号达阈值后立即异步触发 consolidate（_trigger_consolidate_async），
        不再需要等 info_counter 达 15。测试等待异步线程完成后直接验证画像。
        """
        # 7 次 add（每次都走 signal_pool 累积）
        for _ in range(7):
            self.handler(
                action="add", section="习惯", content="用户偏好简洁回复"
            )

        # 等待异步 consolidate 线程完成（consolidate 内部 _consolidation_lock
        # 串行化，多次触发安全；这里等待守护线程消费 pending_profile_updates）
        import time
        for _ in range(20):  # 最多等 2 秒
            if "用户偏好简洁回复" in self.profile_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)

        # 画像应包含该内容（异步 consolidate 已写入）
        text = self.profile_path.read_text(encoding="utf-8")
        self.assertIn("用户偏好简洁回复", text)

        # v5: 信号写入画像后立即从池中移除（不再保留 written 状态）
        status = self.pool.get_status()
        self.assertEqual(len(status), 0)

    def test_below_threshold_not_written(self) -> None:
        """6 次 add（未达阈值）→ consolidate 不写入画像。"""
        # v2: 用非情感动词内容，避免"用户偏好"触发情感增强使 count 翻倍达阈值
        for _ in range(6):
            self.handler(
                action="add", section="习惯", content="用户是后端工程师"
            )

        # 信号池有 1 条 pending 信号，未触发入队
        self.assertEqual(len(self.engine.pending_profile_updates), 0)

        # 触发 consolidate
        for i in range(15):
            self.engine.add_info({"role": "user", "content": f"msg {i}"})
        self.engine.consolidate(session_id="e2e_sess")

        # 画像不应包含该内容（未达阈值）
        text = self.profile_path.read_text(encoding="utf-8")
        self.assertNotIn("用户是后端工程师", text)

        # 信号状态仍为 pending
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["status"], "pending")
        self.assertEqual(status[0]["count"], 6)

    def test_existing_triggered_signal_flushed_on_startup(self) -> None:
        """存量 triggered 信号（启动加载时已在池中）通过 flush_triggered_signals 写入画像。

        模拟场景：服务重启后信号池文件中已有 triggered 信号 count=18，
        但未消费。flush_triggered_signals 应让它们重新入队 + 异步触发
        consolidate 立即写入画像，避免永远卡在池中。
        """
        import json as _json
        from teage_liu.memory.signal_pool import Signal as _Signal

        # 模拟启动加载时已有 triggered 信号（绕过 _add_single 路径）
        triggered_signal = _Signal(
            id="sig_test_flush",
            content="用户讨厌装傻的沟通风格",
            keywords=["用户讨厌", "讨厌", "装傻", "沟通风格"],
            category="",
            count=18,
            sources=["L3", "L1"],
            first_seen="2026-07-04T19:00:26.196465",
            last_seen="2026-07-07T13:22:01.978133",
            status="triggered",
            section="沉淀笔记",
        )
        self.pool._signals.append(triggered_signal)
        # pending_profile_updates 应为空（没经过 _add_single 路径）
        self.assertEqual(len(self.engine.pending_profile_updates), 0)

        # 调用 flush_triggered_signals
        self.pool.flush_triggered_signals()

        # 等待异步 consolidate 完成
        import time
        for _ in range(20):
            if "讨厌装傻" in self.profile_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)

        # 画像应包含该内容
        text = self.profile_path.read_text(encoding="utf-8")
        self.assertIn("讨厌装傻", text)

        # 信号应从池中移除
        status = self.pool.get_status()
        sig_ids = [s["id"] for s in status]
        self.assertNotIn("sig_test_flush", sig_ids)

    def test_consolidate_triggers_signal_pool_cleanup(self) -> None:
        """consolidate 调用后自动清理过期信号（pending 30 天 / triggered 7 天）。

        验证：consolidate() 在 _apply_pending_ops 之后调用 signal_pool.cleanup()，
        过期信号（基于 last_seen）被自动移除，避免无限堆积。
        """
        from datetime import datetime, timedelta

        # 添加一条 pending 信号,模拟 31 天前活动
        self.pool.add("用户偏好简短回复", source="L1")
        old_time = (datetime.now() - timedelta(days=31)).isoformat()
        self.pool._signals[0].last_seen = old_time
        self.assertEqual(len(self.pool.get_status()), 1)

        # 触发 consolidate(注入 15 条消息让 LLM 路径不早返回)
        for i in range(15):
            self.engine.add_info({"role": "user", "content": f"msg {i}"})
        self.engine.consolidate(session_id="e2e_sess")

        # 信号应被自动清理
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_consolidate_no_pending_still_triggers_cleanup(self) -> None:
        """无 pending 消息的 consolidate 早返回路径也需触发清理。

        场景：用户长期不聊天,信号池中累积了过期 triggered 信号,
        此时 consolidate 因无 pending 消息走早返回路径,
        但仍需触发 cleanup 清理过期信号。
        """
        from datetime import datetime, timedelta

        # 添加一条 triggered 信号,模拟 8 天前入队
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        old_time = (datetime.now() - timedelta(days=8)).isoformat()
        self.pool._signals[0].last_seen = old_time

        # 不 add_info,让 consolidate 走 if not pending: 早返回路径
        self.engine.consolidate(session_id="e2e_sess")

        # 过期 triggered 信号应被清理
        self.assertEqual(len(self.pool.get_status()), 0)


# ---------------------------------------------------------------------------
# 8.5 cron 会话隔离：cron 不更新用户画像
# ---------------------------------------------------------------------------


class TestCronSessionIsolation(_IntegrationTestBase):
    """cron 会话（session_id 以 ``cron:`` 开头）不更新用户画像。

    验证两个入口的隔离：
    - L1 路径（builtin_tools.py）：profile_update 工具 add/replace/delete 全跳过
    - L3 路径（consolidation.py）：consolidate 提取的 user_profile 事实跳过 signal_pool
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_profile("")
        self.manager = MemoryMdManager(file_path=str(self.profile_path))
        self.pool = self._make_signal_pool()
        self.engine = ConsolidationEngine(
            llm_client=_MockLLMClient([]),
            chroma_store=_MockChromaStore(),
            memory_md_manager=self.manager,
            signal_pool=self.pool,
        )
        self.pool._consolidation_engine = self.engine
        self.registry = ToolRegistry()
        _register_update_profile(
            self.registry, self.engine, signal_pool=self.pool
        )
        self.handler = self.registry._core_tools["profile_update"].handler

    def tearDown(self) -> None:
        super().tearDown()

    def test_cron_add_does_not_enter_signal_pool(self) -> None:
        """cron 会话 add 操作不进入信号池累积。"""
        from teage_liu.agent._cancel_context import current_session_id

        token = current_session_id.set("cron:test_schedule_1")
        try:
            for _ in range(7):  # 远超阈值 7
                result = self.handler(
                    action="add", section="偏好",
                    content="用户喜欢在 cron 中累积"
                )
            # 应返回跳过提示
            self.assertIn("cron 会话不更新用户画像", result)
        finally:
            current_session_id.reset(token)

        # 信号池应为空（未入池）
        status = self.pool.get_status()
        self.assertEqual(len(status), 0)
        # 画像应为空
        self.assertEqual(self.profile_path.read_text(encoding="utf-8").strip(), "")

    def test_cron_replace_delete_does_not_enqueue(self) -> None:
        """cron 会话 replace/delete 操作不入 pending 队列。"""
        from teage_liu.agent._cancel_context import current_session_id

        token = current_session_id.set("cron:test_schedule_2")
        try:
            result_replace = self.handler(
                action="replace", section="偏好",
                content="cron 试图替换偏好"
            )
            result_delete = self.handler(
                action="delete", section="偏好",
                content=""
            )
        finally:
            current_session_id.reset(token)

        self.assertIn("cron 会话不更新用户画像", result_replace)
        self.assertIn("cron 会话不更新用户画像", result_delete)
        # pending_profile_updates 应为空
        self.assertEqual(len(self.engine.pending_profile_updates), 0)

    def test_normal_session_still_updates_profile(self) -> None:
        """普通会话（非 cron: 前缀）仍正常更新画像（回归保护）。"""
        from teage_liu.agent._cancel_context import current_session_id

        token = current_session_id.set("normal_session_xyz")
        try:
            for _ in range(7):
                self.handler(
                    action="add", section="偏好",
                    content="用户偏好简洁回复"
                )
        finally:
            current_session_id.reset(token)

        # 等待异步 consolidate 完成
        import time
        for _ in range(20):
            if "用户偏好简洁回复" in self.profile_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)

        # 画像应包含该内容
        text = self.profile_path.read_text(encoding="utf-8")
        self.assertIn("用户偏好简洁回复", text)


# ---------------------------------------------------------------------------
# 9. 入池前画像查重：L1 与 L3 联动
# ---------------------------------------------------------------------------


class TestProfileDedupAcrossSources(_IntegrationTestBase):
    """入池前查重画像：L1 与 L3 联动测试。

    验证：画像已包含的信息，无论 L1 还是 L3 都不再入池。
    """

    def setUp(self) -> None:
        super().setUp()
        # 画像已包含"偏好简洁回复"
        self._write_profile("# 用户画像\n\n## 偏好\n\n偏好简洁回复\n")
        self.pool = self._make_signal_pool()

    def tearDown(self) -> None:
        super().tearDown()

    def test_l1_signal_already_in_profile_skipped(self) -> None:
        """L1 信号'偏好简洁回复'已在画像中，跳过入池。"""
        self.pool._profile_text_hash = None  # 清除缓存
        self.pool.add("偏好简洁回复", source="L1", section="偏好")
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_l3_signal_already_in_profile_skipped(self) -> None:
        """L3 信号'用户偏好简洁回复'已在画像中（覆盖率 ≥0.5），跳过入池。"""
        self.pool._profile_text_hash = None
        self.pool.add(
            "用户偏好简洁回复", source="L3", weight=2, section="沉淀笔记"
        )
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_l1_new_signal_enters_pool(self) -> None:
        """L1 新信号'喜欢二次元动漫'不在画像中，正常入池。"""
        self.pool._profile_text_hash = None
        self.pool.add("喜欢二次元动漫", source="L1", section="兴趣")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["sources"], ["L1"])


if __name__ == "__main__":
    unittest.main()
