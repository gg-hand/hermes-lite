"""存储平台专项测试:D1 开放落盘平台(StorageProvider 通用通道 + MessageStore 消息级落盘)。

覆盖计划 §1.4 验收:
- 枝干可经 storage_provider.write(kind, doc) 落盘任意信息,重启可读回
- kind 白名单拒绝非法名(防表注入)
- 多 kind 互相隔离(每 kind 独立 doc 表)
- MessageStore 消息级落盘:content_blocks JSON 加列;旧行回退纯文本
"""

from __future__ import annotations

import json

import pytest

from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.storage import SQLiteStorageProvider, MessageStore


# ---------------------------------------------------------------------------
# StorageProvider:通用持久化通道(kind 命名空间)
# ---------------------------------------------------------------------------
def test_write_read_roundtrip(tmp_path):
    """验收:write(kind, doc) → read 按 doc_id 读回,delete 删除。"""
    sp = SQLiteStorageProvider(str(tmp_path / "sp.db"))

    doc_id = sp.write("memory.facts", {"text": "用户喜欢喝咖啡", "tags": ["user"]})
    assert doc_id and len(doc_id) > 10  # 自动生成 doc_id

    doc = sp.read("memory.facts", doc_id)
    assert doc["text"] == "用户喜欢喝咖啡"
    assert doc["tags"] == ["user"]

    sp.delete("memory.facts", doc_id)
    assert sp.read("memory.facts", doc_id) is None
    sp.close()


def test_query_returns_all_for_kind(tmp_path):
    """验收:query(kind) 返回该 kind 全部 doc(按写入序)。"""
    sp = SQLiteStorageProvider(str(tmp_path / "sp.db"))
    sp.write("audit", {"event": "chat_start"})
    sp.write("audit", {"event": "chat_done"})

    docs = sp.query("audit")
    assert [d["event"] for d in docs] == ["chat_start", "chat_done"]
    sp.close()


def test_kind_whitelist_rejects_illegal(tmp_path):
    """验收:非法 kind(非 ^[a-z0-9_.]+$)被拒绝,防表注入。"""
    sp = SQLiteStorageProvider(str(tmp_path / "sp.db"))
    with pytest.raises(ValueError, match="kind"):
        sp.write("Bad; DROP TABLE", {"x": 1})
    with pytest.raises(ValueError, match="kind"):
        sp.write("", {"x": 1})
    sp.close()


def test_kind_isolation(tmp_path):
    """验收:不同 kind 使用独立 doc 表,互不干扰。"""
    sp = SQLiteStorageProvider(str(tmp_path / "sp.db"))
    id_a = sp.write("memory.facts", {"v": 1})
    id_b = sp.write("collab.tasks", {"v": 2})

    assert sp.read("memory.facts", id_a)["v"] == 1
    assert sp.read("collab.tasks", id_b)["v"] == 2
    # 跨 kind 读不到
    assert sp.read("collab.tasks", id_a) is None
    assert sp.query("memory.facts") == [{"v": 1}]
    sp.close()


# ---------------------------------------------------------------------------
# MessageStore 契约:消息级落盘(content_blocks JSON + 旧行回退)
# ---------------------------------------------------------------------------
def test_message_store_blocks_roundtrip(tmp_path):
    """验收:log_message 带 content_blocks → 读回 JSON 结构;旧行(无 blocks)回退纯文本。"""
    store = SQLiteHistoryStore(str(tmp_path / "ms.db"))
    store.ensure_session("s1")
    # 消息级:content_blocks 落盘
    store.log_message(
        "s1", "assistant", "纯文本",
        content_blocks=[
            {"type": "text", "text": "纯文本"},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {"q": "x"}},
        ],
        token_count=42,
        reasoning="思考过程",
        message_type="assistant",
        tool_name=None,
        tool_call_id=None,
    )
    # 旧式调用:仅纯文本(模拟旧库行,content_blocks 为 NULL)
    store.log_message("s1", "user", "旧行文本")

    msgs = store.get_session_messages("s1")
    blocks_msg = msgs[0]
    assert json.loads(blocks_msg["content_blocks"])[1]["type"] == "tool_use"
    assert blocks_msg["token_count"] == 42
    assert blocks_msg["reasoning"] == "思考过程"
    # 旧行:content_blocks 为空 → 回退纯文本
    assert msgs[1]["content_blocks"] is None or msgs[1]["content_blocks"] == ""
    assert msgs[1]["content"] == "旧行文本"
    store.close()


def test_message_store_is_abc_contract():
    """验收:MessageStore 是契约接口(ABC),HistoryStore 实现之。"""
    assert issubclass(SQLiteHistoryStore, MessageStore)
    # 契约方法签名存在
    for method in ("log_message", "get_session_messages"):
        assert callable(getattr(MessageStore, method, None))
