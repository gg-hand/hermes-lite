"""协作消息读写 + 串行化测试（Task 2，含 A1/A2 修复）"""
import asyncio
import pytest
from pathlib import Path
from teage_liu.multiagent.blackboard import (
    read_collab_messages,
    append_collab_message,
    CollabWriter,
    set_collab_consensus_dedup_enabled,
    set_collab_sanitize_enabled,
    _parse_frontmatter_blocks,
)


@pytest.fixture
def bb_root(tmp_path):
    """临时黑板根目录"""
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


# ========== A1 修复：_parse_frontmatter_blocks 辅助函数 ==========

def test_parse_frontmatter_blocks_empty():
    """空内容返回空列表"""
    assert _parse_frontmatter_blocks("") == []
    assert _parse_frontmatter_blocks("   \n  ") == []


def test_parse_frontmatter_blocks_single():
    """单个 frontmatter 块"""
    content = "---\nfrom: agent_A\ntype: announce\ncontent: hi\nseq: 1\n"
    records = _parse_frontmatter_blocks(content)
    assert len(records) == 1
    assert records[0]["from"] == "agent_A"
    assert records[0]["seq"] == 1


def test_parse_frontmatter_blocks_multiple():
    """多个 frontmatter 块"""
    content = (
        "---\nfrom: A\ntype: relay\ncontent: m1\nseq: 1\n"
        "---\nfrom: B\ntype: relay\ncontent: m2\nseq: 2\n"
    )
    records = _parse_frontmatter_blocks(content)
    assert len(records) == 2
    assert records[0]["seq"] == 1
    assert records[1]["seq"] == 2


# ========== collab 消息读写 ==========

def test_append_global_collab_message(bb_root):
    """写入全局协作消息"""
    msg = {
        "from": "agent_A",
        "type": "announce",
        "action": "online",
        "content": "我上线了",
    }
    seq, deduped = asyncio.run(append_collab_message(bb_root, msg))
    assert seq == 1
    assert deduped is False

    msg2 = {
        "from": "agent_B",
        "type": "announce",
        "action": "online",
        "content": "我也上线了",
    }
    seq2, _ = asyncio.run(append_collab_message(bb_root, msg2))
    assert seq2 == 2


def test_read_global_collab_messages(bb_root):
    """读取全局协作消息"""
    asyncio.run(append_collab_message(bb_root, {"from": "A", "type": "announce", "content": "1"}))
    asyncio.run(append_collab_message(bb_root, {"from": "B", "type": "announce", "content": "2"}))

    messages = asyncio.run(read_collab_messages(bb_root))
    assert len(messages) == 2
    assert messages[0]["seq"] == 1
    assert messages[1]["seq"] == 2


def test_append_collab_with_id(bb_root):
    """写入带 collab_id 的协作消息"""
    msg = {
        "from": "agent_A",
        "type": "request",
        "content": "需要协作",
        "collab_id": "collab_001",
    }
    seq, _ = asyncio.run(append_collab_message(bb_root, msg, collab_id="collab_001"))
    assert seq == 1

    messages = asyncio.run(read_collab_messages(bb_root, collab_id="collab_001"))
    assert len(messages) == 1
    assert messages[0]["seq"] == 1
    assert messages[0]["collab_id"] == "collab_001"


def test_global_and_collab_files_separate(bb_root):
    """全局和单协作文件互不干扰"""
    asyncio.run(append_collab_message(bb_root, {"from": "A", "type": "announce", "content": "global"}))
    asyncio.run(append_collab_message(bb_root, {"from": "B", "type": "request", "content": "collab1"}, collab_id="collab_001"))

    global_msgs = asyncio.run(read_collab_messages(bb_root))
    collab_msgs = asyncio.run(read_collab_messages(bb_root, collab_id="collab_001"))

    assert len(global_msgs) == 1
    assert len(collab_msgs) == 1
    assert global_msgs[0]["content"] == "global"
    assert collab_msgs[0]["content"] == "collab1"


# ========== A2 修复：message_id 去重 + (seq, deduplicated) 返回值 ==========

def test_message_id_dedup(bb_root):
    """相同 message_id 的消息去重，返回 (seq, deduplicated=True)"""
    msg1 = {
        "from": "agent_A",
        "to": "agent_B",
        "type": "relay",
        "content": "A2A 消息",
        "via": "a2a",
        "forwarded_by": "agent_A",
        "message_id": "msg_001",
    }
    msg2 = {
        "from": "agent_A",
        "to": "agent_B",
        "type": "relay",
        "content": "A2A 消息",
        "via": "a2a",
        "forwarded_by": "agent_B",
        "message_id": "msg_001",  # 相同 message_id
    }

    seq1, deduped1 = asyncio.run(append_collab_message(bb_root, msg1))
    seq2, deduped2 = asyncio.run(append_collab_message(bb_root, msg2))

    assert seq1 == 1
    assert deduped1 is False
    # 第二条应返回已存在的 seq（去重）
    assert seq2 == 1
    assert deduped2 is True

    messages = asyncio.run(read_collab_messages(bb_root))
    assert len(messages) == 1  # 只有一条


def test_collab_writer_concurrent(bb_root):
    """CollabWriter 并发写入保证 seq 唯一"""
    writer = CollabWriter(bb_root)

    async def write_msg(content):
        return await writer.append({"from": "A", "type": "relay", "content": content})

    async def run_concurrent():
        tasks = [write_msg(f"msg_{i}") for i in range(10)]
        results = await asyncio.gather(*tasks)
        return results

    results = asyncio.run(run_concurrent())
    seqs = [r[0] for r in results]
    assert len(set(seqs)) == 10  # 10 个不同的 seq
    assert set(seqs) == set(range(1, 11))  # 1-10
    # 全部不是去重
    assert all(r[1] is False for r in results)


def test_append_returns_tuple(bb_root):
    """append_collab_message 返回 (seq, deduplicated) 元组"""
    result = asyncio.run(append_collab_message(bb_root, {
        "from": "A", "type": "announce", "content": "test"
    }))
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], int)
    assert isinstance(result[1], bool)


# ========== before_seq / limit 分页扩展 ==========

def test_read_collab_messages_before_seq(bb_root):
    """read_collab_messages 支持 before_seq 参数：返回 seq 严格小于 before_seq 的消息。"""
    async def run():
        for i in range(1, 4):
            await append_collab_message(bb_root, {
                "type": "status", "from": "agent-x",
                "content": f"msg-{i}", "timestamp": f"2026-07-27T1{i}:00:00+00:00",
            })
        # 取 seq < 3 的消息（应为 seq=1, 2）
        msgs = await read_collab_messages(bb_root, before_seq=3)
        seqs = [m["seq"] for m in msgs]
        assert seqs == [1, 2]
    asyncio.run(run())


def test_read_collab_messages_limit(bb_root):
    """limit 参数限制返回数量（取最新的 N 条）。"""
    async def run():
        for i in range(1, 6):
            await append_collab_message(bb_root, {
                "type": "status", "from": "agent-y",
                "content": f"msg-{i}", "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
            })
        # 取最新 2 条（应为 seq=4, 5）
        msgs = await read_collab_messages(bb_root, limit=2)
        seqs = [m["seq"] for m in msgs]
        assert seqs == [4, 5]
    asyncio.run(run())


def test_read_collab_messages_backward_compatible(bb_root):
    """新参数不传时行为与旧版完全一致。"""
    async def run():
        await append_collab_message(bb_root, {
            "type": "status", "from": "agent-z", "content": "test",
        })
        msgs = await read_collab_messages(bb_root)
        assert len(msgs) == 1
    asyncio.run(run())


def test_read_collab_messages_before_seq_with_limit(bb_root):
    """before_seq 与 limit 组合：先过滤 seq，再取最新 N 条。"""
    async def run():
        for i in range(1, 11):
            await append_collab_message(bb_root, {
                "type": "status", "from": "agent-w",
                "content": f"msg-{i}", "timestamp": f"2026-07-27T{i+9:02d}:00:00+00:00",
            })
        # 取 seq < 8 的消息中最新的 3 条（应为 seq=5, 6, 7）
        msgs = await read_collab_messages(bb_root, before_seq=8, limit=3)
        seqs = [m["seq"] for m in msgs]
        assert seqs == [5, 6, 7]
    asyncio.run(run())


# ========== P3-2: consensus 熔断 ==========

@pytest.fixture(autouse=True)
def _reset_collab_toggles():
    """每个测试前后重置 P3-1/P3-2 开关到默认 True，避免跨测试污染。"""
    set_collab_sanitize_enabled(True)
    set_collab_consensus_dedup_enabled(True)
    yield
    set_collab_sanitize_enabled(True)
    set_collab_consensus_dedup_enabled(True)


def test_consensus_dedup_blocks_second_consensus(bb_root):
    """已有 consensus 时，第二条 consensus 被熔断丢弃，返回旧 seq。"""
    async def run():
        cid = "collab_consensus_1"
        await append_collab_message(bb_root, {
            "from": "A", "type": "request", "content": "开始协作",
        }, collab_id=cid)
        seq1, _ = await append_collab_message(bb_root, {
            "from": "A", "type": "consensus", "content": "共识达成",
        }, collab_id=cid)
        # 第二条 consensus 应被熔断
        seq2, deduped = await append_collab_message(bb_root, {
            "from": "B", "type": "consensus", "content": "我也确认共识",
        }, collab_id=cid)
        assert deduped is True
        assert seq2 == seq1
        # 文件中只应有 1 条 consensus
        msgs = await read_collab_messages(bb_root, collab_id=cid)
        consensus_msgs = [m for m in msgs if m.get("type") == "consensus"]
        assert len(consensus_msgs) == 1
    asyncio.run(run())


def test_end_terminator_blocks_consensus(bb_root):
    """已有 end 终止信号时，后续 consensus 也被熔断（end 与 consensus 等价终止）。"""
    async def run():
        cid = "collab_end_1"
        _, _ = await append_collab_message(bb_root, {
            "from": "A", "type": "end", "content": "协作结束",
        }, collab_id=cid)
        seq2, deduped = await append_collab_message(bb_root, {
            "from": "B", "type": "consensus", "content": "共识",
        }, collab_id=cid)
        assert deduped is True
        msgs = await read_collab_messages(bb_root, collab_id=cid)
        # 只保留第一条终止信号
        terminators = [m for m in msgs if m.get("type") in ("consensus", "end")]
        assert len(terminators) == 1
        assert terminators[0]["type"] == "end"
    asyncio.run(run())


def test_consensus_first_write_not_blocked(bb_root):
    """无任何终止信号时，首条 consensus 正常写入。"""
    async def run():
        cid = "collab_consensus_first"
        seq, deduped = await append_collab_message(bb_root, {
            "from": "A", "type": "consensus", "content": "首次共识",
        }, collab_id=cid)
        assert deduped is False
        assert seq == 1
    asyncio.run(run())


def test_consensus_dedup_disabled_writes_both(bb_root):
    """关闭熔断开关后，两条 consensus 都写入。"""
    set_collab_consensus_dedup_enabled(False)
    async def run():
        cid = "collab_consensus_off"
        await append_collab_message(bb_root, {
            "from": "A", "type": "consensus", "content": "共识1",
        }, collab_id=cid)
        seq2, deduped = await append_collab_message(bb_root, {
            "from": "B", "type": "consensus", "content": "共识2",
        }, collab_id=cid)
        assert deduped is False
        assert seq2 == 2
        msgs = await read_collab_messages(bb_root, collab_id=cid)
        consensus_msgs = [m for m in msgs if m.get("type") == "consensus"]
        assert len(consensus_msgs) == 2
    asyncio.run(run())


def test_consensus_dedup_scoped_per_collab(bb_root):
    """consensus 熔断按 collab_id 隔离：A 协作的 consensus 不影响 B 协作。"""
    async def run():
        await append_collab_message(bb_root, {
            "from": "A", "type": "consensus", "content": "A 共识",
        }, collab_id="collab_A")
        seq_b, deduped_b = await append_collab_message(bb_root, {
            "from": "B", "type": "consensus", "content": "B 共识",
        }, collab_id="collab_B")
        assert deduped_b is False
        assert seq_b == 1  # B 协作独立 seq
    asyncio.run(run())


def test_response_not_blocked_by_consensus(bb_root):
    """consensus 熔断仅作用于 consensus 类型，response 正常写入。"""
    async def run():
        cid = "collab_resp_after"
        await append_collab_message(bb_root, {
            "from": "A", "type": "consensus", "content": "共识",
        }, collab_id=cid)
        seq2, deduped = await append_collab_message(bb_root, {
            "from": "B", "type": "response", "content": "后续回复",
        }, collab_id=cid)
        assert deduped is False
        assert seq2 == 2
    asyncio.run(run())


def test_consensus_dedup_global_collab_not_blocked(bb_root):
    """无 collab_id 的全局 consensus 不触发熔断（熔断需 collab_id 定位文件）。"""
    async def run():
        await append_collab_message(bb_root, {
            "from": "A", "type": "consensus", "content": "全局共识1",
        })
        seq2, deduped = await append_collab_message(bb_root, {
            "from": "B", "type": "consensus", "content": "全局共识2",
        })
        assert deduped is False
        assert seq2 == 2
    asyncio.run(run())
