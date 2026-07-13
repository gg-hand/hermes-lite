"""HistoryBuffer JSONL 持久化层单元测试 — Phase 10 SubTask 1.x。

验证新增的磁盘持久化行为，覆盖以下场景：

1. test_add_message_persists_to_disk        - add_message 后 JSONL 文件存在且内容正确
2. test_get_history_loads_from_disk_after_restart - 重启后新实例能从磁盘恢复历史
3. test_fifo_truncation_on_load             - 磁盘有 30 条、max_turns=20，加载后内存只剩 20 条
4. test_tool_use_tool_result_roundtrip      - Anthropic content blocks 写入加载后结构完整
5. test_corrupt_file_renamed                - 损坏 JSONL 被重命名为 .corrupt，get_history 返回 []
6. test_clear_session_removes_disk_file     - clear_session 后磁盘文件被删除
7. test_persistence_dir_none_pure_memory   - persistence_dir=None 时无磁盘操作，行为同旧版
8. test_cron_session_filename               - session_id="cron:abc123" 时文件名为 cron_abc123.jsonl

运行方式：
    python -m pytest tests/test_history_buffer_persistence.py -v
"""

from __future__ import annotations

import json
import os
import sys

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from hermes.storage.history_buffer import HistoryBuffer  # noqa: E402


# ---------------------------------------------------------------------------
# 测试数据构造辅助
# ---------------------------------------------------------------------------

def make_tool_use_content(tool_use_id: str, name: str = "file_read", input_data: dict = None):
    """构造 assistant(tool_use) 的 Anthropic content block 列表。"""
    return [
        {"type": "text", "text": "调用工具"},
        {
            "type": "tool_use",
            "id": tool_use_id,
            "name": name,
            "input": input_data or {"path": "/x"},
        },
    ]


def make_tool_result_content(tool_use_id: str, content: str = "file content"):
    """构造 user(tool_result) 的 Anthropic content block 列表。"""
    return [
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
    ]


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def test_add_message_persists_to_disk(tmp_path):
    """add_message 后 JSONL 文件存在且内容正确。

    验证：
    - 文件路径为 ``{persistence_dir}/{session_id}.jsonl``；
    - 文件内每行是一个 JSON 消息，按 add 顺序排列；
    - 消息含 role/content/timestamp 字段。
    """
    buf = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    sid = "session-1"
    buf.add_message(sid, "user", "你好")
    buf.add_message(sid, "assistant", "世界")

    jsonl_path = tmp_path / "session-1.jsonl"
    assert jsonl_path.exists(), "JSONL 文件应存在"

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, f"应有 2 行，实际 {len(lines)} 行"

    msg1 = json.loads(lines[0])
    msg2 = json.loads(lines[1])
    assert msg1["role"] == "user"
    assert msg1["content"] == "你好"
    assert "timestamp" in msg1
    assert msg2["role"] == "assistant"
    assert msg2["content"] == "世界"


def test_get_history_loads_from_disk_after_restart(tmp_path):
    """重启后新实例能从磁盘恢复历史。

    场景：buf1 add 几条消息后销毁 → 用相同 persistence_dir 构造 buf2 →
    get_history 能恢复完整历史（内存未命中时从磁盘加载）。
    """
    # 第一阶段：buf1 写入消息
    buf1 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    sid = "session-restart"
    buf1.add_message(sid, "user", "问题 1")
    buf1.add_message(sid, "assistant", "回答 1")
    buf1.add_message(sid, "user", "问题 2")
    buf1.add_message(sid, "assistant", "回答 2")
    # 模拟重启：销毁 buf1（释放内存）
    del buf1

    # 第二阶段：新实例从磁盘恢复
    buf2 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    history = buf2.get_history(sid)
    assert len(history) == 4, f"应恢复 4 条消息，实际 {len(history)} 条"
    assert history[0]["content"] == "问题 1"
    assert history[1]["content"] == "回答 1"
    assert history[2]["content"] == "问题 2"
    assert history[3]["content"] == "回答 2"
    # 角色顺序正确
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_fifo_truncation_on_load(tmp_path):
    """磁盘有 30 条、max_turns=20，加载后内存只剩 20 条（最近的）。

    场景：用 max_turns=30 写入 30 条消息（不触发 FIFO）→ 新 buf2 以
    max_turns=20 加载 → 内存工作集截断为 20 条，保留最新的 20 条
    （索引 10~29）。
    """
    # 第一阶段：max_turns=30 写入 30 条，不触发 FIFO
    buf1 = HistoryBuffer(max_turns=30, persistence_dir=str(tmp_path))
    sid = "session-fifo"
    for i in range(30):
        buf1.add_message(sid, "user", f"msg-{i}")
    del buf1

    # 第二阶段：max_turns=20 加载，应截断为 20 条
    buf2 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    history = buf2.get_history(sid)
    assert len(history) == 20, f"加载后应截断为 20 条，实际 {len(history)} 条"
    # 保留最新的 20 条（msg-10 ~ msg-29）
    assert history[0]["content"] == "msg-10", (
        f"首条应为 msg-10，实际 {history[0]['content']}"
    )
    assert history[-1]["content"] == "msg-29", (
        f"末条应为 msg-29，实际 {history[-1]['content']}"
    )

    # 磁盘文件仍保留全量 30 条（FIFO 截断不删磁盘）
    jsonl_path = tmp_path / "session-fifo.jsonl"
    disk_lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(disk_lines) == 30, (
        f"磁盘应保留全量 30 条，实际 {len(disk_lines)} 条"
    )


def test_tool_use_tool_result_roundtrip(tmp_path):
    """Anthropic content blocks（tool_use + tool_result）写入加载后结构完整。

    验证 content 为 list 形式（含 tool_use / tool_result dict）的消息经
    JSONL 序列化-反序列化后，结构与字段完整保留，配对关系不丢失。
    """
    buf1 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    sid = "session-tool"
    tu_content = make_tool_use_content("tool_1", name="file_read", input_data={"path": "/x"})
    tr_content = make_tool_result_content("tool_1", content="file content")
    buf1.add_message(sid, "assistant", tu_content)
    buf1.add_message(sid, "user", tr_content)
    del buf1

    buf2 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    history = buf2.get_history(sid)
    assert len(history) == 2

    # 第 1 条：assistant(tool_use)
    msg_tu = history[0]
    assert msg_tu["role"] == "assistant"
    assert isinstance(msg_tu["content"], list)
    assert msg_tu["content"][0] == {"type": "text", "text": "调用工具"}
    tu_block = msg_tu["content"][1]
    assert tu_block["type"] == "tool_use"
    assert tu_block["id"] == "tool_1"
    assert tu_block["name"] == "file_read"
    assert tu_block["input"] == {"path": "/x"}

    # 第 2 条：user(tool_result)
    msg_tr = history[1]
    assert msg_tr["role"] == "user"
    assert isinstance(msg_tr["content"], list)
    tr_block = msg_tr["content"][0]
    assert tr_block["type"] == "tool_result"
    assert tr_block["tool_use_id"] == "tool_1"
    assert tr_block["content"] == "file content"

    # 配对关系保留：tool_use.id == tool_result.tool_use_id
    assert tu_block["id"] == tr_block["tool_use_id"]


def test_corrupt_file_renamed(tmp_path):
    """损坏 JSONL 被重命名为 .corrupt，get_history 返回 []。

    场景：手动写入一行非法 JSON 到 JSONL 文件 → get_history 应捕获
    JSONDecodeError，将文件重命名为 {session_id}.jsonl.corrupt，
    返回 []，且原文件不再存在。
    """
    sid = "session-corrupt"
    jsonl_path = tmp_path / "session-corrupt.jsonl"
    # 写入损坏内容（非法 JSON）
    jsonl_path.write_text("这不是合法 JSON\n", encoding="utf-8")

    buf = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    history = buf.get_history(sid)
    assert history == [], "损坏文件应返回空列表"

    # 原文件已不存在
    assert not jsonl_path.exists(), "原 JSONL 文件应已被重命名"
    # 损坏文件被重命名为 .corrupt
    corrupt_path = tmp_path / "session-corrupt.jsonl.corrupt"
    assert corrupt_path.exists(), ".corrupt 文件应存在"


def test_clear_session_removes_disk_file(tmp_path):
    """clear_session 后磁盘文件被删除。

    验证：add 几条消息产生磁盘文件 → clear_session 后文件与内存都被清除。
    """
    buf = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    sid = "session-clear"
    buf.add_message(sid, "user", "A")
    buf.add_message(sid, "assistant", "B")

    jsonl_path = tmp_path / "session-clear.jsonl"
    assert jsonl_path.exists(), "add 后磁盘文件应存在"

    buf.clear_session(sid)

    # 磁盘文件已删除
    assert not jsonl_path.exists(), "clear_session 后磁盘文件应被删除"
    # 内存也已清除
    assert buf.get_history(sid) == []


def test_persistence_dir_none_pure_memory(tmp_path):
    """persistence_dir=None 时无磁盘操作，行为同旧版。

    验证：
    - add_message 不创建任何磁盘文件；
    - get_history 在内存未命中时返回 []（不尝试加载磁盘）；
    - clear_session 不抛异常（无文件可删）。
    """
    buf = HistoryBuffer(max_turns=20, persistence_dir=None)
    sid = "session-memory"
    buf.add_message(sid, "user", "纯内存消息")
    buf.add_message(sid, "assistant", "回复")

    # persistence_dir 目录下无任何 JSONL 文件
    files = list(tmp_path.glob("*.jsonl"))
    assert files == [], "纯内存模式不应创建任何 JSONL 文件"

    # 内存命中正常返回
    history = buf.get_history(sid)
    assert len(history) == 2
    assert history[0]["content"] == "纯内存消息"

    # clear_session 不抛异常
    buf.clear_session(sid)
    assert buf.get_history(sid) == []

    # 新会话内存未命中时返回 []（不尝试加载磁盘）
    assert buf.get_history("never-existed") == []


def test_cron_session_filename(tmp_path):
    """session_id="cron:abc123" 时文件名为 cron_abc123.jsonl（冒号替换）。

    验证 _persist_path 对含冒号的 session_id（如 cron:xxx）做安全替换，
    冒号替换为下划线，避免 Windows 文件名非法字符问题。
    """
    buf = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    sid = "cron:abc123"
    buf.add_message(sid, "user", "cron 任务消息")

    # 文件名应为 cron_abc123.jsonl（冒号替换为下划线）
    expected_path = tmp_path / "cron_abc123.jsonl"
    assert expected_path.exists(), (
        f"cron session 文件名应为 cron_abc123.jsonl，实际不存在"
    )

    # 原始含冒号的文件名不应存在（Windows 下也无法创建）
    bad_path = tmp_path / "cron:abc123.jsonl"
    assert not bad_path.exists(), "不应创建含冒号的文件名"

    # 内容可正常加载
    history = buf.get_history(sid)
    assert len(history) == 1
    assert history[0]["content"] == "cron 任务消息"


def test_disk_full_history_preserved_after_fifo_eviction(tmp_path):
    """FIFO 淘汰时磁盘保留全量历史（设计决策：磁盘=全量归档，内存=工作集）。

    场景：max_turns=3，写入 5 条消息触发 2 次 FIFO 淘汰 →
    磁盘应保留全量 5 条，内存仅保留最新 3 条。
    """
    buf = HistoryBuffer(max_turns=3, persistence_dir=str(tmp_path))
    sid = "session-disk-full"
    for i in range(5):
        buf.add_message(sid, "user", f"msg-{i}")

    # 内存仅保留最新 3 条（msg-2, msg-3, msg-4）
    history = buf.get_history(sid)
    assert len(history) == 3
    assert history[0]["content"] == "msg-2"
    assert history[-1]["content"] == "msg-4"

    # 磁盘保留全量 5 条
    jsonl_path = tmp_path / "session-disk-full.jsonl"
    disk_lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(disk_lines) == 5, (
        f"磁盘应保留全量 5 条，实际 {len(disk_lines)} 条"
    )
    # 磁盘首条是最早的 msg-0
    first_disk_msg = json.loads(disk_lines[0])
    assert first_disk_msg["content"] == "msg-0"


def test_append_to_disk_does_not_throw_on_io_error(tmp_path, monkeypatch):
    """_append_to_disk 在 IO 异常时不抛出，仅记 warning（不影响主流程）。

    场景：mock open 抛 OSError，add_message 仍应正常完成，内存历史正确。
    """
    buf = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    sid = "session-io-error"

    # mock 内建 open 抛 OSError（仅影响本测试内的写入）
    original_open = open

    def faulty_open(*args, **kwargs):
        # 仅当以写入模式打开 JSONL 文件时抛错；其他读取不受影响
        mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
        if "a" in mode or "w" in mode:
            raise OSError("模拟磁盘满")
        return original_open(*args, **kwargs)

    import builtins

    monkeypatch.setattr(builtins, "open", faulty_open)

    # 不应抛异常
    buf.add_message(sid, "user", "消息 1")
    buf.add_message(sid, "assistant", "消息 2")

    # 内存历史仍正确（IO 失败不影响内存）
    history = buf.get_history(sid)
    assert len(history) == 2
    assert history[0]["content"] == "消息 1"
    assert history[1]["content"] == "消息 2"
