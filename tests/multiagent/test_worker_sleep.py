"""Worker 休眠模式单元测试。

覆盖：
- WorkerState 新增休眠字段序列化 roundtrip
- _track_empty_poll 计数器递增与重置
- _enter_sleep / _wake_up 状态转换 + 持久化
- _sleep_probe_once mtime 变化触发唤醒
- sleep_after_empty_polls=0 时特性禁用
- 配置从 multiagent.collab 加载
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio

from teage_liu.multiagent.blackboard import Blackboard
from teage_liu.multiagent.worker_adapter import WorkerAdapter
from teage_liu.multiagent.worker_state import WorkerState, WorkerStateStore


@pytest_asyncio.fixture
async def bb_root(tmp_path: Path) -> Path:
    """初始化黑板目录（含 collaboration.md）。"""
    bb = Blackboard(tmp_path)
    await bb.init_blackboard()
    # init_blackboard 不创建 collaboration.md，手动建一个空文件供 mtime 探针
    (tmp_path / "collaboration.md").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def worker_config() -> dict:
    """带休眠配置的 worker_config。"""
    return {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": "/tmp/bb",
            "worker": {
                "agent_id": "worker_001",
                "heartbeat_interval_seconds": 10,
                "capabilities": ["file_read"],
                "dangerous_tools": [],
            },
            "director": {
                "heartbeat_timeout_seconds": 30,
                "degraded_threshold_seconds": 20,
            },
            "collab": {
                "poll_interval_seconds": 2,
                "idle_timeout_seconds": 60,
                "sleep_after_empty_polls": 3,
                "sleep_poll_interval_seconds": 30,
            },
        }
    }


@pytest.fixture
def worker_config_disabled() -> dict:
    """休眠特性禁用的 worker_config（sleep_after_empty_polls=0）。"""
    cfg = {
        "multiagent": {
            "enabled": True,
            "role": "worker",
            "blackboard_dir": "/tmp/bb",
            "worker": {
                "agent_id": "worker_001",
                "heartbeat_interval_seconds": 10,
                "capabilities": ["file_read"],
                "dangerous_tools": [],
            },
            "director": {
                "heartbeat_timeout_seconds": 30,
                "degraded_threshold_seconds": 20,
            },
            "collab": {
                "poll_interval_seconds": 2,
                "idle_timeout_seconds": 60,
                "sleep_after_empty_polls": 0,
                "sleep_poll_interval_seconds": 30,
            },
        }
    }
    return cfg


class FakeOrchestrator:
    """模拟 Orchestrator。"""
    async def chat(self, session_id, user_input, **kwargs):
        return f"已处理: {user_input}"


# ========== WorkerState 序列化测试 ==========

class TestWorkerStateSleepFields:
    """WorkerState 新增休眠字段序列化。"""

    def test_roundtrip_with_sleep_state(self, tmp_path: Path):
        """休眠状态字段 roundtrip 保持一致。"""
        (tmp_path / "agents").mkdir()
        store = WorkerStateStore(tmp_path, "worker_001")
        state = WorkerState(
            last_collab_seq=10,
            sleep_state="sleeping",
            empty_poll_count=5,
            sleep_entered_at="2026-07-28T10:00:00+00:00",
        )
        store.save(state)

        loaded = store.load()
        assert loaded.sleep_state == "sleeping"
        assert loaded.empty_poll_count == 5
        assert loaded.sleep_entered_at == "2026-07-28T10:00:00+00:00"

    def test_default_values(self, tmp_path: Path):
        """文件不存在时返回默认值（active/0/空串）。"""
        (tmp_path / "agents").mkdir()
        store = WorkerStateStore(tmp_path, "worker_001")
        state = store.load()
        assert state.sleep_state == "active"
        assert state.empty_poll_count == 0
        assert state.sleep_entered_at == ""

    def test_update_sleep_state(self, tmp_path: Path):
        """update 方法支持休眠字段写入。"""
        (tmp_path / "agents").mkdir()
        store = WorkerStateStore(tmp_path, "worker_001")
        store.update({
            "sleep_state": "sleeping",
            "empty_poll_count": 2,
            "sleep_entered_at": "2026-07-28T10:00:00+00:00",
        })
        loaded = store.load()
        assert loaded.sleep_state == "sleeping"
        assert loaded.empty_poll_count == 2


# ========== 休眠状态机测试 ==========

class TestSleepStateMachine:
    """休眠状态机 _track_empty_poll / _enter_sleep / _wake_up。"""

    def _make_adapter(self, bb_root: Path, config: dict) -> WorkerAdapter:
        """构造未启动的 WorkerAdapter（仅初始化字段，不调用 start）。"""
        return WorkerAdapter(
            bb_root=bb_root,
            config=config,
            agent_id="worker_001",
            orchestrator=FakeOrchestrator(),
        )

    def test_config_loaded(self, bb_root: Path, worker_config: dict):
        """休眠配置从 multiagent.collab 正确加载。"""
        adapter = self._make_adapter(bb_root, worker_config)
        assert adapter._sleep_after_empty_polls == 3
        assert adapter._sleep_poll_interval == 30.0

    def test_config_defaults_when_missing(self, bb_root: Path):
        """collab 段缺失时使用默认值（0=禁用）。"""
        cfg = {"multiagent": {"enabled": True, "role": "worker", "worker": {"agent_id": "w1"}}}
        adapter = self._make_adapter(bb_root, cfg)
        assert adapter._sleep_after_empty_polls == 0
        assert adapter._sleep_poll_interval == 30.0

    def test_track_empty_poll_increments(self, bb_root: Path, worker_config: dict):
        """空轮询计数递增。"""
        adapter = self._make_adapter(bb_root, worker_config)
        adapter._track_empty_poll(False)
        adapter._track_empty_poll(False)
        assert adapter._empty_poll_count == 2
        assert adapter._sleep_state == "active"

    def test_track_empty_poll_resets_on_new_message(self, bb_root: Path, worker_config: dict):
        """有新消息时计数归零。"""
        adapter = self._make_adapter(bb_root, worker_config)
        adapter._track_empty_poll(False)
        adapter._track_empty_poll(False)
        adapter._track_empty_poll(True)
        assert adapter._empty_poll_count == 0
        assert adapter._sleep_state == "active"

    def test_enter_sleep_at_threshold(self, bb_root: Path, worker_config: dict):
        """达到阈值后进入休眠。"""
        adapter = self._make_adapter(bb_root, worker_config)
        # worker_config 阈值=3
        adapter._track_empty_poll(False)  # 1
        adapter._track_empty_poll(False)  # 2
        assert adapter._sleep_state == "active"
        adapter._track_empty_poll(False)  # 3 → 进入休眠
        assert adapter._sleep_state == "sleeping"
        assert adapter._sleep_entered_at != ""

    def test_wake_up_resets_state(self, bb_root: Path, worker_config: dict):
        """唤醒切回活跃模式并重置计数。"""
        adapter = self._make_adapter(bb_root, worker_config)
        adapter._enter_sleep()
        assert adapter._sleep_state == "sleeping"
        adapter._wake_up(reason="test")
        assert adapter._sleep_state == "active"
        assert adapter._empty_poll_count == 0
        assert adapter._sleep_entered_at == ""

    def test_disabled_when_threshold_zero(self, bb_root: Path, worker_config_disabled: dict):
        """sleep_after_empty_polls=0 时计数不递增、不进入休眠。"""
        adapter = self._make_adapter(bb_root, worker_config_disabled)
        for _ in range(10):
            adapter._track_empty_poll(False)
        assert adapter._empty_poll_count == 0
        assert adapter._sleep_state == "active"

    def test_enter_sleep_idempotent(self, bb_root: Path, worker_config: dict):
        """重复调用 _enter_sleep 不重复记录日志/时间。"""
        adapter = self._make_adapter(bb_root, worker_config)
        adapter._enter_sleep()
        first_ts = adapter._sleep_entered_at
        time.sleep(0.01)
        adapter._enter_sleep()
        assert adapter._sleep_entered_at == first_ts

    def test_wake_up_idempotent(self, bb_root: Path, worker_config: dict):
        """活跃态调用 _wake_up 无副作用。"""
        adapter = self._make_adapter(bb_root, worker_config)
        adapter._wake_up(reason="no-op")
        assert adapter._sleep_state == "active"
        assert adapter._empty_poll_count == 0


# ========== 唤醒探针测试 ==========

class TestSleepProbe:
    """_sleep_probe_once mtime 唤醒探针。"""

    def _make_adapter(self, bb_root: Path, config: dict) -> WorkerAdapter:
        return WorkerAdapter(
            bb_root=bb_root,
            config=config,
            agent_id="worker_001",
            orchestrator=FakeOrchestrator(),
        )

    @pytest.mark.asyncio
    async def test_probe_wakes_on_mtime_change(self, bb_root: Path, worker_config: dict):
        """collaboration.md mtime 变化 → 唤醒。"""
        adapter = self._make_adapter(bb_root, worker_config)
        # 建立基线 mtime
        adapter._refresh_collab_file_mtime()
        assert adapter._last_collab_file_mtime is not None
        adapter._enter_sleep()
        assert adapter._sleep_state == "sleeping"

        # 触发 mtime 变化（写入新内容）
        await asyncio.sleep(0.05)
        (bb_root / "collaboration.md").write_text("---\nseq: 1\n---\nnew\n", encoding="utf-8")

        await adapter._sleep_probe_once()
        assert adapter._sleep_state == "active"

    @pytest.mark.asyncio
    async def test_probe_stays_sleeping_when_mtime_unchanged(self, bb_root: Path, worker_config: dict):
        """mtime 未变 → 继续休眠。"""
        adapter = self._make_adapter(bb_root, worker_config)
        adapter._refresh_collab_file_mtime()
        adapter._enter_sleep()

        await adapter._sleep_probe_once()
        assert adapter._sleep_state == "sleeping"

    @pytest.mark.asyncio
    async def test_probe_wakes_when_no_baseline(self, bb_root: Path, worker_config: dict):
        """首次探针无 mtime 基线 → 立即唤醒建立基线。"""
        adapter = self._make_adapter(bb_root, worker_config)
        # 不调用 _refresh_collab_file_mtime，模拟首次进入休眠无基线
        adapter._sleep_state = "sleeping"
        adapter._last_collab_file_mtime = None

        await adapter._sleep_probe_once()
        assert adapter._sleep_state == "active"

    @pytest.mark.asyncio
    async def test_probe_handles_missing_file(self, bb_root: Path, worker_config: dict):
        """collaboration.md 不存在时不崩溃，保持休眠。"""
        adapter = self._make_adapter(bb_root, worker_config)
        adapter._enter_sleep()
        # 删除文件
        (bb_root / "collaboration.md").unlink()

        await adapter._sleep_probe_once()
        assert adapter._sleep_state == "sleeping"


# ========== 持久化测试 ==========

class TestSleepStatePersistence:
    """休眠状态跨重启持久化。"""

    def _make_adapter(self, bb_root: Path, config: dict) -> WorkerAdapter:
        return WorkerAdapter(
            bb_root=bb_root,
            config=config,
            agent_id="worker_001",
            orchestrator=FakeOrchestrator(),
        )

    @pytest.mark.asyncio
    async def test_sleep_state_persisted_across_restart(self, bb_root: Path, worker_config: dict):
        """休眠状态写入 state.json，新实例加载后保留。"""
        adapter1 = self._make_adapter(bb_root, worker_config)
        await adapter1._load_state_on_start()  # 加载初始空状态
        adapter1._enter_sleep()
        assert adapter1._sleep_state == "sleeping"

        # 模拟重启：新实例从同一 state.json 加载
        adapter2 = self._make_adapter(bb_root, worker_config)
        await adapter2._load_state_on_start()
        assert adapter2._sleep_state == "sleeping"
        assert adapter2._sleep_entered_at == adapter1._sleep_entered_at

    @pytest.mark.asyncio
    async def test_wake_up_persisted(self, bb_root: Path, worker_config: dict):
        """唤醒后状态持久化为 active。"""
        adapter1 = self._make_adapter(bb_root, worker_config)
        await adapter1._load_state_on_start()
        adapter1._enter_sleep()
        adapter1._wake_up(reason="test")

        adapter2 = self._make_adapter(bb_root, worker_config)
        await adapter2._load_state_on_start()
        assert adapter2._sleep_state == "active"
        assert adapter2._empty_poll_count == 0
