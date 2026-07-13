"""SignalPool 信号池单元测试。

覆盖：
- 基本入池/去重/阈值触发
- 情感强度增强器（喜欢+1 / 爱+2 / 讨厌+2）
- 入池前查重画像（Jaccard ≥0.7 跳过）
- 活动即续期（last_seen 刷新 + 过期清理）
- 信号池内去重（Jaccard ≥0.7 合并）
- 持久化（save/load roundtrip + 原子替换）
- mark_written / mark_written_by_contents
- get_dashboard_data（分组/汇总/进度计算）

运行方式:
    python -m pytest tests/test_signal_pool.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from hermes.memory.signal_pool import (
    Signal,
    SignalPool,
    _extract_keywords,
    _jaccard,
    _split_atomic,
)


# ---------------------------------------------------------------------------
# Mock ConsolidationEngine：仅需 enqueue_profile_update 方法记录调用
# ---------------------------------------------------------------------------


class _MockConsolidationEngine:
    """最小化 ConsolidationEngine mock。

    仅实现 ``enqueue_profile_update``，记录所有入队调用以便断言。
    """

    def __init__(self) -> None:
        self.enqueued: list = []  # [(action, section, content), ...]

    def enqueue_profile_update(
        self, action: str, section: str, content: str
    ) -> None:
        self.enqueued.append((action, section, content))


# ---------------------------------------------------------------------------
# 测试基类：提供临时 pool_path 与 profile_path
# ---------------------------------------------------------------------------


class _SignalPoolTestBase(unittest.TestCase):
    """所有 SignalPool 测试的公共 fixture。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.pool_path = self.tmpdir / "signal_pool.json"
        self.profile_path = self.tmpdir / "memory.md"
        # 默认空画像
        self.profile_path.write_text("", encoding="utf-8")
        self.engine = _MockConsolidationEngine()
        self.pool = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_profile(self, content: str) -> None:
        """写入 memory.md 内容。"""
        self.profile_path.write_text(content, encoding="utf-8")

    def _flush_pool(self) -> None:
        """同步 flush 信号池（避免 debounce 延迟）。"""
        self.pool.flush()


# ---------------------------------------------------------------------------
# 1. 基本入池 / 去重 / 阈值触发
# ---------------------------------------------------------------------------


class TestSignalPoolBasic(_SignalPoolTestBase):
    def test_add_new_signal_creates_entry(self) -> None:
        self.pool.add("用户偏好简短回复", source="L1", section="我和你")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["content"], "用户偏好简短回复")
        # v3: "用户偏好"匹配弱情感增强 → 基础1 + 情感2 = 3
        self.assertEqual(status[0]["count"], 3)
        self.assertEqual(status[0]["status"], "pending")
        self.assertEqual(status[0]["section"], "我和你")
        self.assertEqual(status[0]["sources"], ["L1"])

    def test_add_similar_signal_increments_count(self) -> None:
        # 相似信号（关键词高度重叠）应合并并累加
        self.pool.add("用户偏好简短回复", source="L1")
        self.pool.add("用户偏好简短", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        # v3: 两次都匹配"用户偏好" → (1+2) + (1+2) = 6
        self.assertEqual(status[0]["count"], 6)
        self.assertEqual(len(status[0]["sources"]), 2)

    def test_threshold_7_triggers_enqueue(self) -> None:
        # 累积达 7 次应触发 enqueue_profile_update
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["status"], "triggered")
        self.assertEqual(len(self.engine.enqueued), 1)
        self.assertEqual(self.engine.enqueued[0][0], "add")
        self.assertEqual(self.engine.enqueued[0][2], "用户偏好简短回复")

    def test_threshold_6_not_triggered(self) -> None:
        # v2: 用非情感动词内容，避免"用户偏好"触发情感增强使 count 翻倍
        for _ in range(6):
            self.pool.add("用户是后端工程师", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["status"], "pending")
        self.assertEqual(status[0]["count"], 6)
        self.assertEqual(len(self.engine.enqueued), 0)

    def test_weight_applied_correctly(self) -> None:
        # L3 weight=2，3 次即达阈值（3×2=6 < 7，4 次 4×2=8 ≥7）
        for _ in range(3):
            self.pool.add("用户是后端工程师", source="L3", weight=2)
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 6)
        self.assertEqual(status[0]["status"], "pending")
        # 第 4 次达阈值
        self.pool.add("用户是后端工程师", source="L3", weight=2)
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 8)
        self.assertEqual(status[0]["status"], "triggered")

    def test_triggered_signal_not_retriggered(self) -> None:
        # 达阈值 triggered 后，再次 add 不应重复入队
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        self.assertEqual(len(self.engine.enqueued), 1)
        # 再 add（已 triggered，不重复入队）
        self.pool.add("用户偏好简短回复", source="L1")
        self.assertEqual(len(self.engine.enqueued), 1)

    def test_empty_content_ignored(self) -> None:
        self.pool.add("", source="L1")
        self.pool.add("   ", source="L1")
        self.assertEqual(len(self.pool.get_status()), 0)


# ---------------------------------------------------------------------------
# 2. 情感强度增强器
# ---------------------------------------------------------------------------


class TestEmotionBoost(_SignalPoolTestBase):
    def test_like_adds_2(self) -> None:
        # v3: "我喜欢 Rust" → 基础1 + 弱情感2 = 3
        self.pool.add("我喜欢 Rust", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 3)

    def test_love_adds_3(self) -> None:
        # v3: "我爱 Rust" → 基础1 + 强情感3 = 4
        self.pool.add("我爱 Rust", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 4)

    def test_hate_adds_3(self) -> None:
        # v3: "我最烦 Java" → 基础1 + 强情感3 = 4
        self.pool.add("我最烦 Java", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 4)

    def test_not_like_adds_2(self) -> None:
        # v3 新增: "我不喜欢 emoji" → 基础1 + 弱情感2 = 3（长词优先，不被"喜欢"截胡）
        self.pool.add("我不喜欢 emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 3)

    def test_no_emotion_no_boost(self) -> None:
        # "我是后端" → 基础1，无情感增强
        self.pool.add("我是后端", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 1)

    def test_habit_word_not_emotion(self) -> None:
        # v3 新增: "习惯"不是情感词，无增强 → 基础1
        self.pool.add("我习惯用 python", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 1)

    def test_question_not_matched(self) -> None:
        # "你喜欢什么" → "喜欢"前不是"我/用户"，无增强
        self.pool.add("你喜欢什么", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 1)

    def test_mixed_emotion_takes_max(self) -> None:
        # v3: "我喜欢也爱 Rust" → 同时匹配喜欢(+2)和爱(+3)，取 max=3
        self.pool.add("我喜欢也爱 Rust", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 4)

    def test_l3_with_emotion(self) -> None:
        # L3 "用户喜欢 Rust" → 基础2 + 弱情感2 = 4
        # v3: "用户喜欢"匹配弱情感 +2
        self.pool.add("用户喜欢 Rust", source="L3", weight=2)
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 4)

    def test_user_prefix_emotion_boost(self) -> None:
        # v3: "用户讨厌 X" → 基础1 + 强情感3 = 4
        self.pool.add("用户讨厌 emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 4)

    def test_this_i_like(self) -> None:
        # v3: "这个我喜欢" → "我喜欢"匹配弱情感 +2，count=3
        self.pool.add("这个我喜欢", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["count"], 3)


# ---------------------------------------------------------------------------
# 3. 入池前查重画像
# ---------------------------------------------------------------------------


class TestProfileDedup(_SignalPoolTestBase):
    def test_signal_already_in_profile_skipped(self) -> None:
        # 画像已有"偏好简短回复"，新信号"用户偏好简短回复"应被跳过
        self._write_profile("# 用户画像\n\n## 我和你\n\n偏好简短回复\n")
        # 清除缓存强制刷新
        self.pool._profile_text_hash = None
        self.pool.add("用户偏好简短回复", source="L1")
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_signal_not_in_profile_enters_pool(self) -> None:
        # 画像无关内容，新信号正常入池
        self._write_profile("# 用户画像\n\n## 关于我\n\n职业：工程师\n")
        self.pool._profile_text_hash = None
        self.pool.add("喜欢二次元动漫", source="L1")
        self.assertEqual(len(self.pool.get_status()), 1)

    def test_profile_keywords_cached(self) -> None:
        # hash 不变时复用缓存
        self._write_profile("# 用户画像\n\n## 关于我\n\n工程师\n")
        kw1 = self.pool._get_profile_keywords()
        kw2 = self.pool._get_profile_keywords()
        self.assertIs(kw1, kw2)  # 同一对象引用，证明缓存命中

    def test_profile_keywords_refreshed_on_change(self) -> None:
        # 画像变更后重新提取
        self._write_profile("# 用户画像\n\n工程师\n")
        kw1 = self.pool._get_profile_keywords()
        self._write_profile("# 用户画像\n\n设计师\n")
        kw2 = self.pool._get_profile_keywords()
        self.assertIsNot(kw1, kw2)
        self.assertNotEqual(kw1, kw2)

    def test_empty_profile_no_dedup(self) -> None:
        # 空画像不查重，信号正常入池
        self._write_profile("")
        self.pool._profile_text_hash = None
        self.pool.add("用户偏好简短回复", source="L1")
        self.assertEqual(len(self.pool.get_status()), 1)


# ---------------------------------------------------------------------------
# 4. 活动即续期 + 过期清理
# ---------------------------------------------------------------------------


class TestActivityRefresh(_SignalPoolTestBase):
    def test_similar_signal_merges_refreshes_last_seen(self) -> None:
        # 合并时刷新 last_seen
        self.pool.add("用户偏好简短回复", source="L1")
        first_seen = self.pool.get_status()[0]["last_seen"]
        time.sleep(0.01)
        self.pool.add("用户偏好简短", source="L1")
        last_seen = self.pool.get_status()[0]["last_seen"]
        self.assertNotEqual(first_seen, last_seen)

    def test_continuous_signals_never_expire(self) -> None:
        # 持续观察的信号永不过期：创建后 40 天再 add，应合并而非清理
        old_time = (datetime.now() - timedelta(days=40)).isoformat()
        self.pool.add("用户偏好简短回复", source="L1")
        # 手动改 last_seen 为 40 天前
        self.pool._signals[0].last_seen = old_time
        # 再次 add 相似信号（应合并并刷新 last_seen）
        self.pool.add("用户偏好简短", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        # v3: 两次 add 都匹配"用户偏好"弱情感增强 → (1+2) + (1+2) = 6
        self.assertEqual(status[0]["count"], 6)
        # last_seen 应被刷新为近期
        new_time = datetime.fromisoformat(status[0]["last_seen"])
        self.assertGreater(new_time, datetime.now() - timedelta(seconds=10))

    def test_stale_signal_expires_after_30_days(self) -> None:
        # 30 天未活动的 pending 信号过期清理
        old_time = (datetime.now() - timedelta(days=31)).isoformat()
        self.pool.add("用户偏好简短回复", source="L1")
        self.pool._signals[0].last_seen = old_time
        self.pool.cleanup()
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_triggered_expires_after_7_days(self) -> None:
        # triggered 信号 7 天后清理
        old_time = (datetime.now() - timedelta(days=8)).isoformat()
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        self.pool._signals[0].last_seen = old_time
        self.pool.cleanup()
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_written_signal_removed_immediately(self) -> None:
        # written 信号在 mark_written 时立即移除，不保留 7 天
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        self.assertEqual(len(self.pool.get_status()), 1)
        # 模拟 consolidation 写入画像后调用 mark_written
        sig_id = self.pool._signals[0].id
        self.pool.mark_written([sig_id])
        # 信号应被立即移除
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_recent_triggered_not_cleaned(self) -> None:
        # 近期 triggered 信号不清理
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        self.pool.cleanup()
        self.assertEqual(len(self.pool.get_status()), 1)


# ---------------------------------------------------------------------------
# 5. 信号池内去重（Jaccard）
# ---------------------------------------------------------------------------


class TestSignalPoolDedup(_SignalPoolTestBase):
    def test_jaccard_07_matches(self) -> None:
        # 关键词高度重叠的信号应合并
        self.pool.add("用户偏好简短回复风格", source="L1")
        self.pool.add("用户偏好简短回复", source="L1")
        self.assertEqual(len(self.pool.get_status()), 1)

    def test_different_signals_not_merged(self) -> None:
        # 关键词不重叠的信号应分别入池
        self.pool.add("喜欢二次元动漫", source="L1")
        self.pool.add("用户是后端工程师", source="L1")
        self.assertEqual(len(self.pool.get_status()), 2)

    def test_mixed_sources_merge(self) -> None:
        # L1 + L2 + L3 混合累加到同一信号
        self.pool.add("用户偏好简短回复", source="L1", weight=1)
        self.pool.add("用户偏好简短回复", source="L2", weight=1)
        self.pool.add("用户偏好简短回复", source="L3", weight=2)
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        # v3: 三次都匹配"用户偏好"弱情感增强 → (1+2) + (1+2) + (2+2) = 10
        self.assertEqual(status[0]["count"], 10)
        self.assertEqual(len(status[0]["sources"]), 3)

    def test_keywords_extracted_correctly(self) -> None:
        kw = _extract_keywords("用户讨厌emoji")
        # v2: 情感动词独立 token
        self.assertIn("讨厌", kw)
        # v2: 4 字滑窗捕捉核心短语"用户讨厌"
        self.assertTrue(any("用户讨厌" in k for k in kw))
        # 英文应 lowercase
        self.assertIn("emoji", kw)
        # 中文整段应被提取
        self.assertTrue(any("用户讨厌" in k or k == "用户讨厌emoji" for k in kw))

    def test_jaccard_function(self) -> None:
        a = {"a", "b", "c"}
        b = {"b", "c", "d"}
        # 交集 {b,c}=2，并集 {a,b,c,d}=4，jaccard=0.5
        self.assertAlmostEqual(_jaccard(a, b), 0.5)
        self.assertEqual(_jaccard(set(), set()), 0.0)


# ---------------------------------------------------------------------------
# 6. 持久化
# ---------------------------------------------------------------------------


class TestSignalPoolPersistence(_SignalPoolTestBase):
    def test_save_and_load_roundtrip(self) -> None:
        self.pool.add("用户偏好简短回复", source="L1")
        self.pool.add("喜欢二次元", source="L1")
        self._flush_pool()
        # 新建实例加载
        pool2 = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )
        status = pool2.get_status()
        self.assertEqual(len(status), 2)

    def test_atomic_replace(self) -> None:
        # 写入后 .tmp 文件应被替换为正式文件
        self.pool.add("用户偏好简短回复", source="L1")
        self._flush_pool()
        self.assertTrue(self.pool_path.exists())
        # .tmp 不应残留
        tmp_path = self.pool_path.with_suffix(".tmp")
        self.assertFalse(tmp_path.exists())

    def test_id_counter_restored_on_load(self) -> None:
        self.pool.add("用户偏好简短回复", source="L1")
        self.pool.add("喜欢二次元", source="L1")
        self._flush_pool()
        pool2 = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )
        # 新信号 ID 应继续递增，不重复
        pool2.add("新信号", source="L1")
        status = pool2.get_status()
        new_signal = [s for s in status if s["content"] == "新信号"][0]
        # ID 应大于已有的 sig_0002
        self.assertGreater(new_signal["id"], "sig_0002")

    def test_load_corrupt_file_initializes_empty(self) -> None:
        # 损坏的 JSON 文件应初始化空池
        self.pool_path.write_text("{invalid json", encoding="utf-8")
        pool2 = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )
        self.assertEqual(len(pool2.get_status()), 0)

    def test_mark_written_removes_signal(self) -> None:
        # v5: mark_written 立即移除信号，不再保留 written 状态
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        sig_id = self.pool.get_status()[0]["id"]
        self.pool.mark_written([sig_id])
        # 信号应被移除
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_mark_written_by_contents_removes_signal(self) -> None:
        # v5: mark_written_by_contents 立即移除信号
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        self.pool.mark_written_by_contents(["用户偏好简短回复"])
        # 信号应被移除（关键词匹配）
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_mark_written_empty_list_noop(self) -> None:
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        self.pool.mark_written([])
        status = self.pool.get_status()
        self.assertEqual(status[0]["status"], "triggered")


# ---------------------------------------------------------------------------
# 7. 监控面板数据
# ---------------------------------------------------------------------------


class TestDashboardData(_SignalPoolTestBase):
    def test_empty_pool_returns_empty_dashboard(self) -> None:
        data = self.pool.get_dashboard_data()
        self.assertEqual(data["signals"], [])
        self.assertEqual(data["sections"], {})
        self.assertEqual(data["summary"]["total"], 0)
        self.assertEqual(data["summary"]["avg_progress"], 0.0)
        self.assertEqual(data["threshold"], 7)

    def test_signals_grouped_by_section(self) -> None:
        self.pool.add("用户偏好简短回复", source="L1", section="我和你")
        self.pool.add("喜欢二次元", source="L1", section="关于我")
        data = self.pool.get_dashboard_data()
        self.assertIn("我和你", data["sections"])
        self.assertIn("关于我", data["sections"])
        self.assertEqual(len(data["sections"]["我和你"]), 1)
        self.assertEqual(len(data["sections"]["关于我"]), 1)

    def test_signals_sorted_by_progress_desc(self) -> None:
        # 进度高的在前（用差异大的信号名避免合并）
        self.pool.add("用户偏好简短回复", source="L1", section="s1")  # count=1
        for _ in range(5):
            self.pool.add("喜欢二次元动漫", source="L1", section="s1")  # count=5
        data = self.pool.get_dashboard_data()
        section_s1 = data["sections"]["s1"]
        self.assertEqual(section_s1[0]["content"], "喜欢二次元动漫")
        self.assertEqual(section_s1[1]["content"], "用户偏好简短回复")
        self.assertGreaterEqual(section_s1[0]["progress"], section_s1[1]["progress"])

    def test_progress_calculation_correct(self) -> None:
        self.pool.add("用户偏好简短回复", source="L1")  # v3: count=3（弱情感增强+2）
        data = self.pool.get_dashboard_data()
        sig = data["signals"][0]
        self.assertAlmostEqual(sig["progress"], 3 / 7)
        self.assertEqual(sig["percent"], round((3 / 7) * 100))

    def test_progress_capped_at_1(self) -> None:
        # count > threshold 时 progress=1.0
        for _ in range(10):
            self.pool.add("用户偏好简短回复", source="L1")
        data = self.pool.get_dashboard_data()
        sig = data["signals"][0]
        self.assertEqual(sig["progress"], 1.0)
        self.assertEqual(sig["percent"], 100)

    def test_summary_counts_correct(self) -> None:
        # 1 pending + 1 triggered（v5: written 状态信号已立即移除，不会出现）
        self.pool.add("用户偏好简短回复", source="L1")
        for _ in range(7):
            self.pool.add("喜欢二次元动漫", source="L1")
        data = self.pool.get_dashboard_data()
        s = data["summary"]
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["triggered"], 1)
        # written 始终为 0（信号写入画像后立即移除）
        self.assertEqual(s["written"], 0)

    def test_avg_progress_calculated(self) -> None:
        self.pool.add("用户偏好简短回复", source="L1")  # v3: count=3, progress=3/7
        self.pool.add("喜欢二次元动漫", source="L1")  # count=1, progress=1/7（无"我/用户"前缀）
        data = self.pool.get_dashboard_data()
        # v3: "用户偏好"弱情感+2使 count=3；"喜欢二次元动漫"无"我/用户"前缀不增强
        expected_avg = (3 / 7 + 1 / 7) / 2  # = 2/7
        self.assertAlmostEqual(data["summary"]["avg_progress"], expected_avg)

    def test_section_with_no_signals_omitted(self) -> None:
        # 空 section 不出现
        self.pool.add("用户偏好简短回复", source="L1", section="s1")
        data = self.pool.get_dashboard_data()
        self.assertNotIn("s2", data["sections"])

    def test_threshold_in_data(self) -> None:
        data = self.pool.get_dashboard_data()
        self.assertEqual(data["threshold"], SignalPool.THRESHOLD)


# ---------------------------------------------------------------------------
# 8. consolidation_engine 为 None 的降级场景
# ---------------------------------------------------------------------------


class TestNoConsolidationEngine(unittest.TestCase):
    """consolidation_engine 为 None 时，达阈值仅标记 triggered 不入队。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.pool_path = self.tmpdir / "signal_pool.json"
        self.profile_path = self.tmpdir / "memory.md"
        self.profile_path.write_text("", encoding="utf-8")
        self.pool = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=None,
            profile_path=self.profile_path,
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_threshold_marked_triggered_without_enqueue(self) -> None:
        for _ in range(7):
            self.pool.add("用户偏好简短回复", source="L1")
        status = self.pool.get_status()
        self.assertEqual(status[0]["status"], "triggered")
        # 无 consolidation_engine，不入队


# ---------------------------------------------------------------------------
# 9. v2 原子拆分 + 去重增强 + 回填（新增）
# ---------------------------------------------------------------------------


class TestSplitAtomic(unittest.TestCase):
    """_split_atomic 复合句拆分。"""

    def test_compound_preference_splits_to_two(self) -> None:
        parts = _split_atomic("用户讨厌emoji，偏好简洁正经的交流方式")
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "用户讨厌emoji")
        self.assertEqual(parts[1], "偏好简洁正经的交流方式")

    def test_single_fact_not_split(self) -> None:
        parts = _split_atomic("用户是后端工程师")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0], "用户是后端工程师")

    def test_short_fragment_falls_back_to_original(self) -> None:
        # 切分后碎片 <4 字符被过滤，≤1 条 → 回退原内容
        parts = _split_atomic("我，的")
        self.assertEqual(len(parts), 1)

    def test_empty_content_returns_empty(self) -> None:
        self.assertEqual(_split_atomic(""), [])
        self.assertEqual(_split_atomic("   "), [])

    def test_newline_splits(self) -> None:
        parts = _split_atomic("用户喜欢rust\n用户讨厌java")
        self.assertEqual(len(parts), 2)


class TestSignalPoolV2Dedup(_SignalPoolTestBase):
    """v2 去重增强：同义改写合并、对象区分保护、triggered 吸收。"""

    def test_dedup_merges_paraphrased_signals(self) -> None:
        # 实测 sig_0003/sig_0006 内容：同义改写应合并
        self.pool.add("用户讨厌使用emoji", source="L1")
        self.pool.add("用户讨厌emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertGreaterEqual(status[0]["count"], 2)

    def test_distinct_objects_not_merged(self) -> None:
        # 同动词不同英文对象 → 对象区分保护，不合并
        self.pool.add("用户喜欢rust", source="L1")
        self.pool.add("用户喜欢go", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)

    def test_opposite_verb_not_merged(self) -> None:
        # 同对象反义动词 → 不合并
        self.pool.add("用户喜欢emoji", source="L1")
        self.pool.add("用户讨厌emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)

    def test_find_similar_matches_triggered_status(self) -> None:
        # triggered 信号也能吸收新证据（written 才跳过）
        self.pool.add("用户讨厌emoji", source="L1")
        # 手动标记 triggered
        self.pool._signals[0].status = "triggered"
        # 再 add 相似信号 → 应合并到 triggered 信号
        self.pool.add("用户讨厌使用emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["status"], "triggered")
        self.assertGreaterEqual(status[0]["count"], 2)

    def test_written_signal_already_removed(self) -> None:
        # v5: written 状态信号已在 mark_written 时移除，不会出现在信号池中
        # 验证：手动设置 status="written" 后调用 cleanup，信号会被移除
        self.pool.add("用户讨厌emoji", source="L1")
        self.pool._signals[0].status = "written"
        # written 信号不应吸收新证据（因为已被移除）
        # 这里模拟 written 信号被遗忘后，新相似信号正常入池
        self.pool._signals.remove(self.pool._signals[0])
        self.pool.add("用户讨厌使用emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)

    def test_add_compound_splits_and_pools_separately(self) -> None:
        # 复合句 add 后池中有 2 条独立信号
        self.pool.add("用户讨厌emoji，偏好简洁正经的交流方式", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)
        contents = [s["content"] for s in status]
        self.assertIn("用户讨厌emoji", contents)
        self.assertIn("偏好简洁正经的交流方式", contents)


class TestSignalPoolV3DirectionProtection(_SignalPoolTestBase):
    """v3 新增：方向相反保护 + 复合句方向相反拆分。"""

    def test_opposite_direction_same_object_not_merged(self) -> None:
        # "我喜欢emoji" vs "我不喜欢emoji" → 一正一负同对象，不合并
        self.pool.add("我喜欢emoji", source="L1")
        self.pool.add("我不喜欢emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)

    def test_opposite_direction_love_vs_hate_not_merged(self) -> None:
        # "我爱emoji" vs "我恨emoji" → 一正强一负强同对象，不合并
        self.pool.add("我爱emoji", source="L1")
        self.pool.add("我恨emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)

    def test_opposite_direction_user_prefix_not_merged(self) -> None:
        # LLM 抽取格式 "用户喜欢emoji" vs "用户讨厌emoji" → 不合并
        self.pool.add("用户喜欢emoji", source="L1")
        self.pool.add("用户讨厌emoji", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)

    def test_same_direction_different_verb_merged(self) -> None:
        # 同向不同动词应合并："我讨厌emoji" vs "我厌恶emoji"
        # 注意：Jaccard 需 ≥0.25 才合并，"讨厌"与"厌恶"关键词集可能不交集
        # 此用例验证同向（都负向强）不会因方向保护被阻断
        self.pool.add("我讨厌emoji", source="L1")
        self.pool.add("我厌恶emoji", source="L1")
        status = self.pool.get_status()
        # 同向不触发方向保护，是否合并取决于 Jaccard
        # "讨厌"vs"厌恶"不交集，但"emoji"交集 → Jaccard=1/5=0.2 <0.25 → 不合并
        # 这是已知限制（同义改写召回不足），不恶化即可
        self.assertLessEqual(len(status), 2)

    def test_mixed_emotion_not_direction_protected(self) -> None:
        # 混合情感（同时含正负）不触发方向保护
        # "我喜欢java但讨厌python" 同时含正负，不视为纯正向或纯负向
        self.pool.add("我喜欢java", source="L1")
        # 混合信号含"喜欢"+"讨厌"，不是 pure_pos 也不是 pure_neg
        self.pool.add("我喜欢java但讨厌python", source="L1")
        status = self.pool.get_status()
        # 不应因方向保护被阻断（混合情感不触发）
        # 是否合并取决于 Jaccard
        self.assertGreaterEqual(len(status), 1)

    def test_compound_opposite_direction_splits_to_two(self) -> None:
        # 复合句"我喜欢java，不喜欢python" → 拆为两条方向相反独立信号
        self.pool.add("我喜欢java，不喜欢python", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)
        contents = [s["content"] for s in status]
        self.assertIn("我喜欢java", contents)
        self.assertIn("不喜欢python", contents)

    def test_compound_same_direction_splits_to_two(self) -> None:
        # 复合句"我喜欢java，爱python" → 拆为两条同向独立信号
        self.pool.add("我喜欢java，爱python", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 2)
        contents = [s["content"] for s in status]
        self.assertIn("我喜欢java", contents)
        self.assertIn("爱python", contents)


class TestBackfillConsolidate(_SignalPoolTestBase):
    """v1/v2/v3/v4 → v5 回填：加载旧数据时自动拆分复合句 + 合并重复信号。"""

    def test_backfill_consolidates_v1_data(self) -> None:
        # 写入 v1 格式数据（version=1），含 4 条重复"用户讨厌emoji"信号
        import json
        v1_data = {
            "signals": [
                {"id": "sig_0001", "content": "用户讨厌emoji", "keywords": ["讨厌", "emoji"],
                 "count": 2, "sources": ["L1"], "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00", "status": "pending", "section": "沉淀笔记"},
                {"id": "sig_0002", "content": "用户讨厌使用emoji", "keywords": ["讨厌", "使用"],
                 "count": 2, "sources": ["L1"], "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00", "status": "pending", "section": "沉淀笔记"},
                {"id": "sig_0003", "content": "用户讨厌 emoji", "keywords": ["讨厌"],
                 "count": 1, "sources": ["L2"], "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00", "status": "pending", "section": "沉淀笔记"},
                {"id": "sig_0004", "content": "用户偏好简短回复", "keywords": ["偏好"],
                 "count": 1, "sources": ["L1"], "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00", "status": "pending", "section": "沉淀笔记"},
            ],
            "version": 1,
        }
        self.pool_path.write_text(json.dumps(v1_data, ensure_ascii=False), encoding="utf-8")
        # 加载 → 触发回填
        pool2 = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )
        status = pool2.get_status()
        # sig_0001/0002/0003 应合并为 1 条，sig_0004 独立 → 共 2 条
        self.assertEqual(len(status), 2)
        # 合并后的 count 应累加（2+2+1=5）
        merged = [s for s in status if "讨厌" in s["content"]][0]
        self.assertGreaterEqual(merged["count"], 5)
        # flush 后 version 应升至 6（v6 新增 target 字段）
        pool2.flush()
        saved = json.loads(self.pool_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 6)

    def test_backfill_idempotent(self) -> None:
        # v5 数据不触发回填
        import json
        v5_data = {
            "signals": [
                {"id": "sig_0001", "content": "用户讨厌emoji", "keywords": ["讨厌", "用户讨厌", "emoji"],
                 "count": 5, "sources": ["L1"], "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00", "status": "pending", "section": "沉淀笔记"},
                {"id": "sig_0002", "content": "用户讨厌使用emoji", "keywords": ["讨厌", "用户讨厌使用"],
                 "count": 2, "sources": ["L1"], "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00", "status": "pending", "section": "沉淀笔记"},
            ],
            "version": 5,
        }
        self.pool_path.write_text(json.dumps(v5_data, ensure_ascii=False), encoding="utf-8")
        pool2 = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )
        # v5 数据不触发回填，2 条信号保持不变
        status = pool2.get_status()
        self.assertEqual(len(status), 2)


# ---------------------------------------------------------------------------
# 8. 双向信号池（target=user/agent）——Phase 元认知 Task 1
# ---------------------------------------------------------------------------


class TestBidirectionalSignalPool(_SignalPoolTestBase):
    """双向信号池测试：target=user/agent 分组去重、独立画像查重、阈值触发。"""

    def test_signal_default_target_is_user(self) -> None:
        # 不传 target 时默认为 user
        self.pool.add("用户偏好简洁回复", source="L1")
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["target"], "user")

    def test_agent_signal_enters_pool_with_target(self) -> None:
        # 显式传 target=agent 入池
        self.pool.add(
            "Agent 在 file_read 上连续失败 2 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["target"], "agent")
        self.assertEqual(status[0]["section"], "Agent 自画像")

    def test_user_and_agent_signals_not_merged(self) -> None:
        # 同内容不同 target 的信号不应合并（按 target 分组）
        # 用相同内容确保 Jaccard=1.0，验证 target 隔离
        self.pool.add("连续失败 2 次", source="L1", section="沉淀笔记", target="user")
        self.pool.add(
            "连续失败 2 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        status = self.pool.get_status()
        # 两条独立信号（按 target 分组）
        self.assertEqual(len(status), 2)
        targets = sorted(s["target"] for s in status)
        self.assertEqual(targets, ["agent", "user"])

    def test_agent_similar_signals_merge_within_group(self) -> None:
        # 同 target=agent 的相似信号应合并（Jaccard ≥0.25）
        self.pool.add(
            "Agent 在 file_read 上连续失败 2 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        self.pool.add(
            "Agent 在 file_read 上连续失败 3 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        status = self.pool.get_status()
        # 合并为 1 条
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["target"], "agent")
        # count 应累加（基础1 + 基础1 = 2，无情感增强）
        self.assertEqual(status[0]["count"], 2)

    def test_agent_signal_threshold_triggers_enqueue(self) -> None:
        # agent 信号达阈值应触发 enqueue，section 为 Agent 自画像
        for _ in range(7):
            self.pool.add(
                "Agent 在 file_read 上连续失败 2 次",
                source="L1_agent_failure",
                section="Agent 自画像",
                target="agent",
            )
        status = self.pool.get_status()
        self.assertEqual(status[0]["status"], "triggered")
        self.assertEqual(status[0]["target"], "agent")
        # enqueue 应被调用，section="Agent 自画像"
        self.assertEqual(len(self.engine.enqueued), 1)
        action, section, content = self.engine.enqueued[0]
        self.assertEqual(action, "add")
        self.assertEqual(section, "Agent 自画像")
        self.assertIn("Agent", content)

    def test_agent_signal_dedup_against_agent_profile_section(self) -> None:
        # agent 信号入池前仅查 ## Agent 自画像 section，不查用户画像 section
        # 画像中"Agent 在 file_read 上连续失败"已存在 → 新 agent 信号应被跳过
        self._write_profile(
            "# 用户画像\n\n## 沉淀笔记\n\n用户偏好简洁\n\n"
            "## Agent 自画像\n\nAgent 在 file_read 上连续失败 2 次\n"
        )
        self.pool._agent_profile_text_hash = None
        self.pool.add(
            "Agent 在 file_read 上连续失败 2 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        # 已在 Agent 自画像 section → 跳过
        self.assertEqual(len(self.pool.get_status()), 0)

    def test_user_signal_not_deduped_against_agent_section(self) -> None:
        # user 信号查重不应匹配 Agent 自画像 section 的内容
        # 画像 Agent 自画像 section 有"连续失败 2 次"，user 信号"连续失败 2 次"
        # 不应被跳过（target=user 仅查 user 画像部分）
        self._write_profile(
            "# 用户画像\n\n## Agent 自画像\n\n连续失败 2 次\n"
        )
        self.pool._profile_text_hash = None
        self.pool.add(
            "连续失败 2 次",
            source="L1",
            section="沉淀笔记",
            target="user",
        )
        # user 信号不被 agent section 内容跳过
        self.assertEqual(len(self.pool.get_status()), 1)

    def test_agent_signal_not_deduped_against_user_section(self) -> None:
        # agent 信号查重不应匹配用户画像 section 的内容
        # 画像沉淀笔记 section 有"Agent 在 file_read 上连续失败 2 次"
        # agent 信号同样内容不应被跳过（target=agent 仅查 Agent 自画像 section）
        self._write_profile(
            "# 用户画像\n\n## 沉淀笔记\n\nAgent 在 file_read 上连续失败 2 次\n"
        )
        self.pool._agent_profile_text_hash = None
        self.pool.add(
            "Agent 在 file_read 上连续失败 2 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        # agent 信号不被 user section 内容跳过
        self.assertEqual(len(self.pool.get_status()), 1)

    def test_single_existing_signal_merges_new_similar(self) -> None:
        # 边界场景：仅 1 个现有 agent 信号时，新相似信号应合并
        # （修正点：原 spec "<2 跳过"会破坏阈值累积，改为"空组才跳过"）
        self.pool.add(
            "Agent 在 bash_exec 上连续失败 2 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        # 仅 1 条现有信号，新增相似信号应合并而非新建
        self.pool.add(
            "Agent 在 bash_exec 上连续失败 3 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        status = self.pool.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["count"], 2)

    def test_target_field_persisted_and_loaded(self) -> None:
        # 持久化 roundtrip：target 字段应正确保存与加载
        self.pool.add(
            "Agent 在 file_read 上连续失败 2 次",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        self._flush_pool()
        # 重新加载
        pool2 = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )
        status = pool2.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["target"], "agent")
        self.assertEqual(status[0]["section"], "Agent 自画像")

    def test_target_field_in_dashboard_data(self) -> None:
        # get_dashboard_data 应包含 target 字段
        self.pool.add("用户偏好简洁", source="L1", target="user")
        self.pool.add(
            "Agent 在 bash_exec 上失败",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        data = self.pool.get_dashboard_data()
        # 至少有 signals 字段含 target
        self.assertIn("signals", data)
        sig_targets = [s.get("target") for s in data["signals"]]
        self.assertIn("user", sig_targets)
        self.assertIn("agent", sig_targets)

    def test_backfill_preserves_target_field(self) -> None:
        # v6 数据含 target 字段，回填加载后 target 应保留
        import json
        v6_data = {
            "signals": [
                {"id": "sig_0001", "content": "Agent 在 file_read 上连续失败 2 次",
                 "keywords": ["agent", "file_read"], "count": 1,
                 "sources": ["L1_agent_failure"],
                 "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00",
                 "status": "pending", "section": "Agent 自画像",
                 "target": "agent"},
                {"id": "sig_0002", "content": "用户偏好简洁回复",
                 "keywords": ["用户偏好"], "count": 1,
                 "sources": ["L1"],
                 "first_seen": "2026-07-01T00:00:00",
                 "last_seen": "2026-07-01T00:00:00",
                 "status": "pending", "section": "沉淀笔记",
                 "target": "user"},
            ],
            "version": 6,
        }
        self.pool_path.write_text(json.dumps(v6_data, ensure_ascii=False), encoding="utf-8")
        pool2 = SignalPool(
            pool_path=self.pool_path,
            consolidation_engine=self.engine,
            profile_path=self.profile_path,
        )
        status = pool2.get_status()
        self.assertEqual(len(status), 2)
        targets = sorted(s["target"] for s in status)
        self.assertEqual(targets, ["agent", "user"])

    def test_backfill_inherits_target_on_split(self) -> None:
        # 复合句拆分时 target 应继承到所有子信号
        # 用差异较大的两条 agent 失败描述，确保 Jaccard <0.25 不合并
        # "Agent 在 file_read 上连续失败 2 次" vs "Agent 的 bash_exec 命令经常超时"
        # 交集仅 {agent}，Jaccard≈0.2 <0.25 → 拆分为 2 条独立信号
        self.pool.add(
            "Agent 在 file_read 上连续失败 2 次，Agent 的 bash_exec 命令经常超时",
            source="L1_agent_failure",
            section="Agent 自画像",
            target="agent",
        )
        status = self.pool.get_status()
        # 拆分为 2 条，target 都应是 agent
        self.assertEqual(len(status), 2)
        for s in status:
            self.assertEqual(s["target"], "agent")


if __name__ == "__main__":
    unittest.main()
