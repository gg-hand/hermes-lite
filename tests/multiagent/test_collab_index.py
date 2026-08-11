"""collabs/index.md 索引维护测试（Task 12）。"""
import asyncio

import pytest

from teage_liu.multiagent.blackboard import (
    append_collab_message,
    read_collab_index,
    update_collab_index,
)


@pytest.fixture
def bb_root(tmp_path):
    """临时黑板根目录"""
    root = tmp_path / "blackboard"
    root.mkdir()
    (root / "collabs").mkdir()
    return root


# ========== 基础读写 ==========


def test_read_empty_collab_index(bb_root):
    """空索引返回空列表"""
    index = asyncio.run(read_collab_index(bb_root))
    assert index == []


def test_update_and_read_collab_index(bb_root):
    """更新和读取协作索引"""
    asyncio.run(update_collab_index(
        bb_root, "collab_001",
        title="弹幕分析", status="active",
        participants=["agent_A", "agent_B"],
    ))
    index = asyncio.run(read_collab_index(bb_root))
    assert len(index) == 1
    assert index[0]["collab_id"] == "collab_001"
    assert index[0]["status"] == "active"
    assert index[0]["title"] == "弹幕分析"
    assert "agent_A" in index[0]["participants"]


# ========== Upsert 行为 ==========


def test_update_existing_collab_index(bb_root):
    """更新已存在的协作状态（upsert）"""
    asyncio.run(update_collab_index(
        bb_root, "collab_001",
        title="t", status="active", participants=[],
    ))
    asyncio.run(update_collab_index(
        bb_root, "collab_001",
        title="t", status="completed", participants=[],
    ))
    index = asyncio.run(read_collab_index(bb_root))
    assert len(index) == 1  # 不重复
    assert index[0]["status"] == "completed"


def test_update_multiple_collabs(bb_root):
    """多个协作共存"""
    for i in range(3):
        asyncio.run(update_collab_index(
            bb_root, f"collab_{i:03d}",
            title=f"task_{i}", status="active", participants=[],
        ))
    index = asyncio.run(read_collab_index(bb_root))
    assert len(index) == 3
    ids = [e["collab_id"] for e in index]
    assert "collab_000" in ids
    assert "collab_002" in ids


# ========== 自动更新（写入带 collab_id 的 request 时）==========


def test_collab_index_auto_update_on_request(bb_root):
    """写入带 collab_id 的 request 时自动更新索引"""
    asyncio.run(append_collab_message(
        bb_root,
        {"from": "agent_A", "type": "request", "content": "需要协作"},
        collab_id="collab_001",
    ))
    index = asyncio.run(read_collab_index(bb_root))
    assert any(c["collab_id"] == "collab_001" for c in index)
    entry = next(c for c in index if c["collab_id"] == "collab_001")
    assert entry["status"] == "initiated"
    assert "agent_A" in entry["participants"]


def test_collab_index_no_auto_update_for_announce(bb_root):
    """announce 消息不触发索引更新"""
    asyncio.run(append_collab_message(
        bb_root,
        {"from": "agent_A", "type": "announce", "action": "online", "content": "up"},
        collab_id="collab_001",
    ))
    index = asyncio.run(read_collab_index(bb_root))
    assert index == []  # announce 不创建索引


def test_collab_index_no_auto_update_without_collab_id(bb_root):
    """无 collab_id 的消息不触发索引更新"""
    asyncio.run(append_collab_message(bb_root, {
        "from": "agent_A", "type": "request", "content": "global",
    }))
    index = asyncio.run(read_collab_index(bb_root))
    assert index == []


# ========== 并发安全 ==========


def test_concurrent_update_collab_index(bb_root):
    """并发更新不同 collab_id 不丢失"""
    async def run():
        await asyncio.gather(
            update_collab_index(bb_root, "collab_a", title="a", status="active", participants=[]),
            update_collab_index(bb_root, "collab_b", title="b", status="active", participants=[]),
            update_collab_index(bb_root, "collab_c", title="c", status="active", participants=[]),
        )
    asyncio.run(run())
    index = asyncio.run(read_collab_index(bb_root))
    assert len(index) == 3
