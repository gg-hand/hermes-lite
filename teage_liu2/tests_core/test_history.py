"""HistoryStore SPI 测试:SQLite 落盘 / 重启恢复 / FTS 检索 / 标题。"""

from __future__ import annotations

from teage_liu2.core.history import SQLiteHistoryStore


def test_log_and_read(tmp_path):
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.ensure_session("s1")
    store.log_message("s1", "user", "你好")
    store.log_message("s1", "assistant", "你好呀")
    msgs = store.get_session_messages("s1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "你好"
    assert "created_at" in msgs[0]
    store.close()


def test_restart_recovery_same_file(tmp_path):
    """验收:新实例读同一文件拿到历史(重启恢复)。"""
    path = str(tmp_path / "h.db")
    store1 = SQLiteHistoryStore(path)
    store1.ensure_session("s2")
    store1.log_message("s2", "user", "第一句")
    store1.close()

    store2 = SQLiteHistoryStore(path)
    msgs = store2.get_session_messages("s2")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "第一句"
    store2.close()


def test_search_messages_fts(tmp_path):
    """验收:FTS5 全文检索(中文按字符分词)。"""
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.ensure_session("s3")
    store.log_message("s3", "user", "我喜欢吃苹果")
    store.log_message("s3", "user", "今天天气不错")

    hits = store.search_messages("苹果")
    assert len(hits) == 1
    assert hits[0]["content"] == "我喜欢吃苹果"

    hits2 = store.search_messages("天气")
    assert len(hits2) == 1
    assert hits2[0]["content"] == "今天天气不错"
    store.close()


def test_session_title(tmp_path):
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.ensure_session("s4")
    assert store.get_session_title("s4") is None
    store.update_session_title("s4", "测试会话")
    assert store.get_session_title("s4") == "测试会话"
    store.close()


def test_ensure_session_idempotent(tmp_path):
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.ensure_session("s5")
    store.ensure_session("s5")  # 重复 ensure 不报错
    store.log_message("s5", "user", "x")
    assert len(store.get_session_messages("s5")) == 1
    store.close()


def test_get_session_messages_limit_takes_latest(tmp_path):
    """验收(D2):limit 取**最近** N 条(按时间正序返回)。"""
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.ensure_session("s_limit")
    for i in range(10):
        store.log_message("s_limit", "user", f"u{i}")
    msgs = store.get_session_messages("s_limit", limit=3)
    assert [m["content"] for m in msgs] == ["u7", "u8", "u9"]  # 最近 3 条,正序
    store.close()


def test_create_session_auto_uuid(tmp_path):
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    sid = store.create_session()
    assert sid and len(sid) > 10
    store.close()
