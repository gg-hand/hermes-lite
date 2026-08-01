"""Worker 幂等性单元测试（重启场景）。

S2 修正（2026-08-02）：测试改用 `_dk` 字符串键匹配生产幂等集键格式
（`f"mid:{message_id}"` / `f"{collab_id or 'global'}:{seq}"`），并适配
- `_processed_msg_seqs` 已改 `OrderedSet`（不可直接 `== set()`，用 `set(...)` 或 `len`）
- `append_collab_message` 自动生成 `message_id`（生产强制），故 key 多为 `mid:xxx`
- P3-4 rebuild 用"已参与协作"启发式初始化 `processed_msg_seqs`（不再为空）
- Phase2 L-1 LLM 异常入重试队列（默认 enabled），不立即写 error
- `_poll_collab_once` 用 `_processed_msg_seqs` 判断新消息（不再用 `_last_collab_seq`）

生产代码语义正确，仅测试过时，不改生产。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from teage_liu.multiagent.blackboard import append_collab_message, read_collab_messages
from teage_liu.multiagent.worker_adapter import OrderedSet, WorkerAdapter
from teage_liu.multiagent.worker_state import WorkerState, WorkerStateStore


@pytest.fixture
def bb_root(tmp_path: Path) -> Path:
    bb = tmp_path / "blackboard"
    (bb / "agents").mkdir(parents=True)
    (bb / "collabs").mkdir()
    return bb


def _make_worker(bb_root, agent_id="agent_A", orchestrator=None):
    config = {
        "multiagent": {
            "worker": {"capabilities": ["sentiment_analysis"]},
            "director": {},
            "collab": {"poll_interval_seconds": 2, "idle_timeout_seconds": 60},
        }
    }
    return WorkerAdapter(
        bb_root=bb_root, config=config, agent_id=agent_id,
        orchestrator=orchestrator or AsyncMock(),
    )


def _dk(msg: dict) -> str:
    """计算消息的幂等集键（与生产 `_dk` 一致）。"""
    return WorkerAdapter._dk(msg.get("collab_id"), msg.get("seq"), msg.get("message_id"))


def test_worker_loads_last_collab_seq_from_state_on_start(bb_root):
    """启动时从 state 文件恢复 _last_collab_seq。

    S2 修正：`_poll_collab_once` 改用 `_processed_msg_seqs` 判断新消息（不再用
    `_last_collab_seq` 跳过历史）。要测试"重启后跳过历史消息"，需在 state 中同时
    存 `processed_msg_seqs` 包含历史消息的 key（生产强制 message_id，故 key 为
    `mid:xxx`）。`_last_collab_seq` 仍批量推进持久化（向后兼容 + 休眠探针基线）。
    """
    # 1. 写入 seq=1..3 历史消息，并读取其 message_id 计算 _dk key
    historical_keys: set[str] = set()
    for i in range(1, 4):
        asyncio.run(append_collab_message(bb_root, {
            "from": "agent_B", "type": "relay", "content": f"历史消息 {i}",
        }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    for m in msgs:
        historical_keys.add(_dk(m))

    # 2. 预先写入 state 文件：last_collab_seq=5 + processed_msg_seqs 含历史 key
    store = WorkerStateStore(bb_root, "agent_A")
    store.save(WorkerState(
        last_collab_seq=5,
        processed_msg_seqs=historical_keys,
    ))

    # 3. 启动 worker
    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    # 4. 验证：_last_collab_seq 恢复为 5
    assert worker._last_collab_seq == 5

    # 5. 验证：轮询一次后不会处理历史消息（key 已在 _processed_msg_seqs）
    asyncio.run(worker._poll_collab_once())
    assert len(worker._normal_queue) == 0


def test_worker_state_collections_restored_on_start(bb_root):
    """启动时从 state 文件恢复所有幂等集。

    S2 修正：state 字段统一用 `_dk` 字符串键（与生产运行时一致）；
    `_processed_msg_seqs` 是 OrderedSet，断言用 `set(...)` 转换比较。
    """
    store = WorkerStateStore(bb_root, "agent_A")
    store.save(WorkerState(
        last_collab_seq=10,
        responded_request_seqs={"global:1", "global:5"},
        processed_msg_seqs={"global:1", "global:2", "global:3"},
        processed_urgent_seqs={"global:7"},
        executed_op_ids={"op_abc"},
    ))

    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    assert worker._last_collab_seq == 10
    assert worker._responded_request_seqs == {"global:1", "global:5"}
    assert set(worker._processed_msg_seqs) == {"global:1", "global:2", "global:3"}
    assert worker._processed_urgent_seqs == {"global:7"}
    assert worker._executed_op_ids == {"op_abc"}


# =============================================================================
# Task 3: 启动时扫描历史构建幂等集（首次升级兼容）
# =============================================================================


def test_rebuild_state_from_history_builds_responded_set(bb_root):
    """首次启动（无 state.json）时扫描历史构建 _responded_request_seqs。

    S2 修正：rebuild 把 response 的 `reply_to`(int) 直接加入 state.responded_request_seqs
    (int)，`_load_state_on_start` 的 `_migrate` 把 int → `f"global:{int}"`，故内存
    `_responded_request_seqs = {"global:1"}`。P3-4 rebuild 已初始化
    `processed_msg_seqs`（"已参与协作"启发式），不再为空。
    """
    # 1. 模拟历史：用户广播 seq=1，agent_A 已写 response(seq=2, reply_to=1)
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "response", "reply_to": 1,
        "content": "已响应", "accept": True,
    }))
    # 另一条广播 seq=3，未响应
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播2",
    }))

    # 2. 启动 worker（无 state.json，触发重建）
    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    # 3. 验证：seq=1 已在 responded_request_seqs（migrate 后 key="global:1"）
    assert "global:1" in worker._responded_request_seqs
    assert "global:3" not in worker._responded_request_seqs

    # 4. 验证：last_collab_seq 保持为 0（不跳过历史消息）
    assert worker._last_collab_seq == 0

    # 5. 验证 P3-4：processed_msg_seqs 基于已参与协作启发式重建
    #    agent_A 已发 response(cid=None)，故 cid=None 视为已参与，
    #    所有 cid=None 的消息 key 都加入。
    assert len(worker._processed_msg_seqs) >= 1

    # 6. 验证：state.json 已持久化
    state = worker._state_store.load()
    assert state.last_collab_seq == 0
    # state 文件中 responded_request_seqs 是 int（rebuild 直接 add reply_to）
    assert 1 in state.responded_request_seqs


def test_rebuild_state_from_history_builds_executed_op_ids(bb_root):
    """首次启动扫描 messages.md 构建 _executed_op_ids。"""
    from teage_liu.multiagent.blackboard import append_message

    # 1. 写入历史任务消息：agent_A 已执行 op_001
    asyncio.run(append_message(bb_root, {
        "from": "agent_A", "type": "result",
        "task_op_id": "op_001", "content": "任务完成",
    }))
    asyncio.run(append_message(bb_root, {
        "from": "agent_B", "type": "result",
        "task_op_id": "op_002", "content": "B 完成",
    }))

    # 2. 启动 worker（无 state.json）
    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    # 3. 验证：op_001 在 executed_op_ids（自己执行的），op_002 不在
    assert "op_001" in worker._executed_op_ids
    assert "op_002" not in worker._executed_op_ids


def test_rebuild_state_does_not_init_urgent_seqs(bb_root):
    """【深度 Review 修正】首次启动不初始化 processed_urgent_seqs（无法准确判断，留空更安全）。"""
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "directive",
        "rule_type": "intervention", "content": "紧急干预1",
    }))
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "directive",
        "rule_type": "ordering", "content": "顺序指令",
    }))

    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    # 【修正】processed_urgent_seqs 应为空（无法判断是否已处理过）
    assert worker._processed_urgent_seqs == set()


def test_rebuild_state_excludes_error_response_from_responded_set(bb_root):
    """【第三轮 Review 修正】error response 不应被加入 responded_request_seqs。

    场景：LLM 失败时 _trigger_urgent_llm 写入 accept=False, error=True 的 response。
    如果 _rebuild_state_from_history 把它的 reply_to 加入 responded_request_seqs，
    会导致该 request 永久跳过，无法重试（与 _trigger_urgent_llm 异常路径不更新幂等集矛盾）。
    """
    # 用户广播 seq=1
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    # error response seq=2, reply_to=1（LLM 失败时写的）
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "response", "reply_to": 1,
        "content": "[协作响应失败] RuntimeError: LLM 不可用",
        "accept": False, "error": True,
    }))

    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    # error response 不应被标记为已响应（允许重试）
    assert "global:1" not in worker._responded_request_seqs


def test_rebuild_state_excludes_accept_false_response(bb_root):
    """【第三轮 Review 修正】accept=False 的 response 不应被加入 responded_request_seqs。

    补充覆盖：即使没有 error=True 字段，accept=False 也表示不是成功响应。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "response", "reply_to": 1,
        "content": "拒绝参与", "accept": False,
    }))

    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    # accept=False 的 response 不应被标记为已响应
    assert "global:1" not in worker._responded_request_seqs


def test_rebuild_skips_when_state_file_exists(bb_root):
    """state.json 存在时跳过历史扫描，直接用文件状态。

    S2 修正：state 字段用 `_dk` 字符串键；migrate 后内存 `_responded_request_seqs`
    保持字符串键。
    """
    # 1. 预先写入 state.json（字符串键）
    store = WorkerStateStore(bb_root, "agent_A")
    store.save(WorkerState(
        last_collab_seq=100, responded_request_seqs={"global:50"},
    ))

    # 2. 在 collaboration.md 写入历史消息
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "response", "reply_to": 1,
        "content": "历史响应",
    }))

    # 3. 启动 worker
    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())

    # 4. 验证：使用 state.json 的值，不扫描历史
    assert worker._last_collab_seq == 100
    assert worker._responded_request_seqs == {"global:50"}
    # reply_to=1 不应被加入（因为 state.json 已存在，跳过扫描）
    assert "global:1" not in worker._responded_request_seqs


def test_rebuild_allows_unhandled_broadcast_to_be_processed(bb_root):
    """【深度 Review 关键修正】首次启动时未处理的用户广播仍能被轮询处理。

    场景：全新部署，state.json 不存在，历史只有 1 条用户广播（未响应）。
    期望：worker 启动后 _last_collab_seq=0，轮询会读到广播并触发 LLM。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "未处理的广播",
    }))

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._orchestrator.chat.return_value = "首次响应"
    asyncio.run(worker._load_state_on_start())

    # 1. _last_collab_seq=0（不跳过历史）
    assert worker._last_collab_seq == 0
    # 2. _responded_request_seqs 为空（未响应过）
    assert worker._responded_request_seqs == set()

    # 3. 轮询会处理广播
    asyncio.run(worker._poll_collab_once())
    worker._orchestrator.chat.assert_called_once()


# =============================================================================
# Task 4: _handle_request 幂等性检查（用户广播防重复响应）
# =============================================================================


def test_already_responded_request_skipped(bb_root):
    """已响应过的 request 不再触发 LLM（重启场景核心修复）。

    S2 修正：生产 `_mark_request_responded` 用 `_dk(collab_id, seq, message_id)`
    字符串键。`append_collab_message` 自动生成 message_id，故 key 为 `mid:xxx`。
    预设幂等集必须用相同 key 才能命中跳过逻辑。
    """
    # 1. 写入用户广播 seq=1，读取 message_id 计算 _dk key
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    broadcast_msg = msgs[0]
    key = _dk(broadcast_msg)

    # 2. 构造 worker，预设该 key 已在 _responded_request_seqs（模拟重启后状态恢复）
    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._responded_request_seqs = {key}
    worker._last_collab_seq = 0  # 强制让轮询读到 seq=1

    # 3. 轮询一次
    asyncio.run(worker._poll_collab_once())

    # 4. 验证：LLM 未被调用（已响应过，跳过）
    worker._orchestrator.chat.assert_not_called()

    # 5. 验证：未写入新的 response 消息
    msgs_after = asyncio.run(read_collab_messages(bb_root))
    response_msgs = [m for m in msgs_after if m.get("type") == "response"]
    assert len(response_msgs) == 0


def test_already_processed_normal_request_skipped(bb_root):
    """【深度 Review 修正】普通 request（非用户广播）入队后标记 processed，重启后跳过入队。

    S2 修正：`_processed_msg_seqs` 用 `_dk` 字符串键（`mid:xxx`，因消息自动生成
    message_id）。预设需用相同 key。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B", "type": "request", "content": "普通请求",
        "capabilities_needed": ["sentiment_analysis"],
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    # 预设 _processed_msg_seqs 含该 key（OrderedSet）
    worker._processed_msg_seqs = OrderedSet()
    worker._processed_msg_seqs.add(key)
    worker._last_collab_seq = 0

    asyncio.run(worker._poll_collab_once())

    # 普通_request 在 _processed_msg_seqs 中则跳过入队
    assert len(worker._normal_queue) == 0
    worker._orchestrator.chat.assert_not_called()


def test_new_request_still_triggers_llm(bb_root):
    """新 request（不在幂等集中）正常触发 LLM（防止误伤）。"""
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "新广播",
    }))

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._responded_request_seqs = set()  # 空，新 request 应触发
    worker._processed_msg_seqs = OrderedSet()
    worker._last_collab_seq = 0

    asyncio.run(worker._poll_collab_once())

    worker._orchestrator.chat.assert_called_once()


def test_normal_request_marked_processed_after_enqueue(bb_root):
    """普通 request 入队后立即标记为 processed（防止重启后重复入队）。

    S2 修正：`_processed_msg_seqs` key 为 `mid:xxx`（消息自动生成 message_id）。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B", "type": "request", "content": "普通请求",
        "capabilities_needed": ["sentiment_analysis"],
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    expected_key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._last_collab_seq = 0
    asyncio.run(worker._poll_collab_once())

    # 验证：入队了
    assert len(worker._normal_queue) == 1
    # 验证：该消息的 _dk key 已加入 _processed_msg_seqs
    assert expected_key in worker._processed_msg_seqs
    # 验证：state.json 已持久化
    state = worker._state_store.load()
    assert expected_key in state.processed_msg_seqs


# =============================================================================
# Task 5: _trigger_urgent_llm 成功后更新幂等集并持久化
# =============================================================================


def test_trigger_urgent_llm_updates_state_on_success(bb_root):
    """_trigger_urgent_llm 成功后更新 _responded_request_seqs 并持久化到 state.json。

    S2 修正：`_mark_request_responded(msg_seq, context_msg.collab_id, context_msg.message_id)`
    用 `_dk` 字符串键。context_msg 无 collab_id/message_id 时 key = `f"global:{seq}"`。
    """
    # 1. 写入用户广播
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    msg_seq = 1

    # 2. 触发 _trigger_urgent_llm
    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._orchestrator.chat.return_value = "已收到广播"

    context_msg = {
        "from": "director", "type": "request",
        "content": "广播1", "seq": msg_seq,
    }
    asyncio.run(worker._trigger_urgent_llm(
        prompt="Director 广播协作请求：广播1",
        context_msg=context_msg,
    ))

    # 3. 验证：内存集合已更新（key="global:1"，因 context_msg 无 collab_id/message_id）
    expected_key = WorkerAdapter._dk(None, msg_seq)
    assert expected_key in worker._responded_request_seqs

    # 4. 验证：state.json 已持久化
    state = worker._state_store.load()
    assert expected_key in state.responded_request_seqs


def test_trigger_urgent_llm_updates_state_on_directive_intervention(bb_root):
    """intervention directive 触发后更新 _processed_urgent_seqs。

    S2 修正：`_mark_urgent_processed` 用 `_dk` 字符串键。context_msg 无 collab_id/
    message_id 时 key = `f"global:{seq}"`。
    """
    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._orchestrator.chat.return_value = "已处理干预"

    context_msg = {
        "from": "director", "type": "directive",
        "rule_type": "intervention", "content": "紧急停止",
        "seq": 7,
    }
    asyncio.run(worker._trigger_urgent_llm(
        prompt="Director 应急干预：紧急停止",
        context_msg=context_msg,
    ))

    expected_key = WorkerAdapter._dk(None, 7)
    assert expected_key in worker._processed_urgent_seqs

    state = worker._state_store.load()
    assert expected_key in state.processed_urgent_seqs


def test_trigger_urgent_llm_exception_does_not_update_state(bb_root):
    """【深度 Review 修正】LLM 调用异常时不更新幂等集，允许重试。

    S2 修正：Phase2 L-1 改造后，LLM 异常默认入 `_llm_retry_queue`（collab_retry.enabled
    默认 True），不立即写 error。测试期望改为验证"入重试队列"，而非"写 error 消息"。
    """
    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._orchestrator.chat.side_effect = RuntimeError("LLM 不可用")

    context_msg = {
        "from": "director", "type": "request",
        "content": "test", "seq": 99,
    }
    asyncio.run(worker._trigger_urgent_llm(
        prompt="test", context_msg=context_msg,
    ))

    # 【修正】Phase2 L-1 异常路径：
    # - 不更新 _responded_request_seqs（允许重试时 LLM 写 response）
    # - 但更新 _processed_msg_seqs（防重试期间该消息被轮询重复入队）
    # - 入 _llm_retry_queue（默认 enabled），不立即写 error
    expected_key = WorkerAdapter._dk(None, 99)
    assert expected_key not in worker._responded_request_seqs

    # Phase2 L-1：异常入 _llm_retry_queue（默认 enabled），不立即写 error
    assert worker._llm_retry_queue.qsize() == 1


# =============================================================================
# Task 6: _handle_directive intervention 类幂等检查
# =============================================================================


def test_already_processed_intervention_skipped(bb_root):
    """已处理过的 intervention directive 不再触发 LLM。

    S2 修正：`_processed_urgent_seqs` 用 `_dk` 字符串键（`mid:xxx`，因消息自动生成
    message_id）。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "directive",
        "rule_type": "intervention", "content": "紧急干预",
        "target": "*",
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._processed_urgent_seqs = {key}  # 模拟已处理
    worker._last_collab_seq = 0

    asyncio.run(worker._poll_collab_once())

    worker._orchestrator.chat.assert_not_called()


def test_already_processed_ordering_directive_not_re_queued(bb_root):
    """已处理过的 ordering directive 不重复入队。

    S2 修正：`_processed_msg_seqs` 用 `_dk` 字符串键。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "directive",
        "rule_type": "ordering", "content": "顺序指令",
        "target": "*",
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._processed_msg_seqs = OrderedSet()
    worker._processed_msg_seqs.add(key)  # 模拟已处理
    worker._last_collab_seq = 0

    asyncio.run(worker._poll_collab_once())

    assert len(worker._normal_queue) == 0


def test_ordering_directive_marked_processed_after_enqueue(bb_root):
    """ordering directive 入队后立即标记为 processed（防止重启后重复入队）。

    S2 修正：`_processed_msg_seqs` key 为 `mid:xxx`。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "directive",
        "rule_type": "ordering", "content": "顺序指令",
        "target": "*",
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    expected_key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._last_collab_seq = 0
    asyncio.run(worker._poll_collab_once())

    assert len(worker._normal_queue) == 1
    assert expected_key in worker._processed_msg_seqs
    state = worker._state_store.load()
    assert expected_key in state.processed_msg_seqs


# =============================================================================
# Task 7: _handle_collab_message 普通队列消息去重（relay/response/result）
# =============================================================================


def test_already_processed_relay_not_re_queued(bb_root):
    """已处理过的 relay 消息不重复入队。

    S2 修正：`_processed_msg_seqs` 用 `_dk` 字符串键。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B", "type": "relay", "content": "历史 relay",
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._processed_msg_seqs = OrderedSet()
    worker._processed_msg_seqs.add(key)  # 模拟已处理
    worker._last_collab_seq = 0

    asyncio.run(worker._poll_collab_once())

    assert len(worker._normal_queue) == 0


def test_relay_marked_processed_after_poll(bb_root):
    """relay 消息被轮询后立即标记为已处理（防止下次重启重复入队）。

    S2 修正：`_processed_msg_seqs` key 为 `mid:xxx`。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B", "type": "relay", "content": "新 relay",
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    expected_key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._poll_collab_once())

    # 验证：消息入队了
    assert len(worker._normal_queue) == 1
    # 验证：该消息的 _dk key 已加入 _processed_msg_seqs
    assert expected_key in worker._processed_msg_seqs
    # 验证：state.json 已持久化
    state = worker._state_store.load()
    assert expected_key in state.processed_msg_seqs


def test_already_processed_response_not_re_queued(bb_root):
    """已处理过的 response 消息不重复入队。

    S2 修正：`_processed_msg_seqs` 用 `_dk` 字符串键。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_B", "type": "response", "content": "历史 response",
        "reply_to": 5,
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    key = _dk(msgs[0])

    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._processed_msg_seqs = OrderedSet()
    worker._processed_msg_seqs.add(key)
    worker._last_collab_seq = 0

    asyncio.run(worker._poll_collab_once())

    assert len(worker._normal_queue) == 0


# =============================================================================
# Task 8: A2A 任务执行 _executed_op_ids 幂等检查
# =============================================================================


def test_execute_a2a_task_idempotent(bb_root):
    """已执行过的 A2A 任务不重复执行（重启场景）。"""
    # 1. 预设 op_001 已执行
    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._executed_op_ids = {"op_001"}

    # 2. 再次执行 op_001
    result = asyncio.run(worker.execute_a2a_task(
        task_op_id="op_001",
        task_content="测试任务",
    ))

    # 3. 验证：返回 cached=True，LLM 未调用
    assert result["cached"] is True
    worker._orchestrator.chat.assert_not_called()


def test_execute_a2a_task_new_executes_and_marks(bb_root):
    """新 A2A 任务正常执行，完成后标记到 _executed_op_ids。"""
    worker = _make_worker(bb_root, agent_id="agent_A")
    worker._orchestrator.chat.return_value = "任务结果"

    result = asyncio.run(worker.execute_a2a_task(
        task_op_id="op_new",
        task_content="新任务",
    ))

    # 1. 验证：LLM 被调用
    worker._orchestrator.chat.assert_called_once()
    # 2. 验证：写入了 result 消息
    from teage_liu.multiagent.blackboard import read_messages
    msgs = asyncio.run(read_messages(bb_root))
    result_msgs = [m for m in msgs if m.get("type") == "result"]
    assert len(result_msgs) == 1
    assert result_msgs[0]["task_op_id"] == "op_new"
    # 3. 验证：op_new 加入 _executed_op_ids
    assert "op_new" in worker._executed_op_ids
    # 4. 验证：state.json 已持久化
    state = worker._state_store.load()
    assert "op_new" in state.executed_op_ids


# =============================================================================
# Task 9: 配置项热更新支持（persist_state）
# =============================================================================


def test_persist_state_disabled_uses_memory_only(bb_root):
    """persist_state=False 时不读写 state.json，使用内存模式。

    S2 修正：`_mark_request_responded` 用 `_dk` 字符串键。broadcast 消息自动生成
    message_id（如 `director_{ts}_{uuid}`），故 key 为 `mid:xxx`。读取实际 message_id
    计算预期 key。
    """
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    msgs = asyncio.run(read_collab_messages(bb_root))
    expected_key = _dk(msgs[0])

    # 构造 persist_state=False 的 worker
    config = {
        "multiagent": {
            "worker": {
                "capabilities": ["sentiment_analysis"],
                "persist_state": False,  # 关闭持久化
            },
            "director": {},
            "collab": {"poll_interval_seconds": 2, "idle_timeout_seconds": 60},
        }
    }
    worker = WorkerAdapter(
        bb_root=bb_root, config=config, agent_id="agent_A",
        orchestrator=AsyncMock(),
    )
    asyncio.run(worker._load_state_on_start())

    # 启动后 state.json 不应存在
    assert not worker._state_store.state_path.exists()
    assert worker._persist_state is False

    # 轮询触发 LLM
    worker._orchestrator.chat.return_value = "响应"
    asyncio.run(worker._poll_collab_once())
    worker._orchestrator.chat.assert_called_once()

    # state.json 仍不应存在（持久化被禁用）
    assert not worker._state_store.state_path.exists()

    # 但内存集合已更新（key 为 `mid:xxx`，因消息有 message_id）
    assert expected_key in worker._responded_request_seqs


def test_persist_state_enabled_by_default(bb_root):
    """默认启用持久化（不传 persist_state 时为 True）。"""
    worker = _make_worker(bb_root, agent_id="agent_A")
    asyncio.run(worker._load_state_on_start())
    assert worker._persist_state is True


def test_persist_state_disabled_skips_rebuild_history(bb_root):
    """【第三轮 Review 补充】persist_state=False 时不扫描历史（退化为内存模式，重启后历史消息会被重新处理）。"""
    # 写入历史 response（accept=True）
    asyncio.run(append_collab_message(bb_root, {
        "from": "director", "type": "request", "content": "广播1",
    }))
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "response", "reply_to": 1,
        "content": "已响应", "accept": True,
    }))

    config = {
        "multiagent": {
            "worker": {
                "capabilities": ["sentiment_analysis"],
                "persist_state": False,
            },
            "director": {},
            "collab": {},
        }
    }
    worker = WorkerAdapter(
        bb_root=bb_root, config=config, agent_id="agent_A",
        orchestrator=AsyncMock(),
    )
    asyncio.run(worker._load_state_on_start())

    # persist_state=False 时 _load_state_on_start 直接返回，不扫描历史
    # 内存幂等集为空，重启后会重新处理历史消息（这是预期的"内存模式"行为）
    assert worker._responded_request_seqs == set()
    assert worker._last_collab_seq == 0
    # state.json 不应被创建
    assert not worker._state_store.state_path.exists()
