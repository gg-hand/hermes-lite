"""HistoryBuffer JSONL 持久化端到端集成测试 — Phase 10 SubTask 5.x。

聚焦"重启恢复"与"内存/磁盘一致性"端到端场景，区别于
``test_history_buffer_persistence.py`` 的单元细节测试。覆盖：

1. test_restart_recovers_full_tool_use_tool_result_pairs
   - 一轮含工具调用的对话（user→assistant(tool_use)→user(tool_result)→assistant(text)）
   - 重启后 get_history 恢复全部 4 条，tool_use/tool_result 字段与配对关系完整保留
2. test_restart_recovers_multiple_tool_rounds
   - 3 轮工具调用（12 条消息），重启后全部恢复，3 个 tool_use_id 互不相同且配对正确
3. test_fifo_eviction_keeps_disk_full_history
   - max_turns=5 写 10 条消息：内存仅 5 条，磁盘 10 条；重启后内存仍 5 条、磁盘仍 10 条
4. test_clear_session_removes_both_memory_and_disk
   - clear_session 后内存与磁盘文件均清除，重启后 get_history 返回 []
5. test_cron_session_persistence
   - session_id="cron:abc123"，文件名 cron_abc123.jsonl，重启后恢复完整 tool 配对
6. test_original_tool_result_not_summarized_in_history
   - 验证 Task 2 效果：超长 tool_result 原始内容（非 "[原始结果 N chars，已摘要]"）写入内存与磁盘，
     重启后仍是完整原始字符串

运行方式：
    python -m pytest tests/test_history_persistence_integration.py -v
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

from teage_liu.storage.history_buffer import HistoryBuffer  # noqa: E402


# ---------------------------------------------------------------------------
# 测试数据构造辅助
# ---------------------------------------------------------------------------

def make_tool_use_content(
    tool_use_id: str,
    name: str = "file_read",
    input_data: dict = None,
    prefix_text: str = "调用工具",
):
    """构造 assistant(tool_use) 的 Anthropic content block 列表。"""
    return [
        {"type": "text", "text": prefix_text},
        {
            "type": "tool_use",
            "id": tool_use_id,
            "name": name,
            "input": input_data or {"path": "/x"},
        },
    ]


def make_tool_result_content(
    tool_use_id: str, content: str = "file content here"
):
    """构造 user(tool_result) 的 Anthropic content block 列表。"""
    return [
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
    ]


# ---------------------------------------------------------------------------
# SubTask 5.1：端到端持久化场景测试
# ---------------------------------------------------------------------------


def test_restart_recovers_full_tool_use_tool_result_pairs(tmp_path):
    """重启后能恢复完整一轮工具调用的 4 条消息与配对关系。

    场景：buf1 模拟一轮含工具调用的对话，按 Anthropic messages API 顺序
    写入 user(文本) → assistant(tool_use blocks) → user(tool_result blocks)
    → assistant(文本) 共 4 条；销毁 buf1 模拟进程退出；用相同
    persistence_dir 新建 buf2 模拟重启；get_history 应恢复全部 4 条，
    且 tool_use block 的 id/name/input 与 tool_result block 的
    tool_use_id/content 完整保留，配对关系（tool_use_id == "tool_1"）正确。
    """
    sid = "session-one-round"

    # 第一阶段：buf1 写入一轮完整工具调用对话
    buf1 = HistoryBuffer(max_turns=50, persistence_dir=str(tmp_path))
    buf1.add_message(sid, "user", "请读取 /x 文件")
    buf1.add_message(
        sid,
        "assistant",
        make_tool_use_content(
            "tool_1", name="file_read", input_data={"path": "/x"}
        ),
    )
    buf1.add_message(
        sid,
        "user",
        make_tool_result_content("tool_1", content="file content here"),
    )
    buf1.add_message(sid, "assistant", "已读取文件，内容是 file content here")
    del buf1

    # 第二阶段：新实例从磁盘恢复
    buf2 = HistoryBuffer(max_turns=50, persistence_dir=str(tmp_path))
    history = buf2.get_history(sid)

    # 全部 4 条消息恢复
    assert len(history) == 4, (
        f"应恢复 4 条消息，实际 {len(history)} 条"
    )

    # 角色顺序正确
    assert [m["role"] for m in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ], f"角色顺序错误: {[m['role'] for m in history]}"

    # 第 1 条：user 文本
    assert history[0]["content"] == "请读取 /x 文件"

    # 第 2 条：assistant(tool_use)，block 字段完整
    msg_tu = history[1]
    assert msg_tu["role"] == "assistant"
    assert isinstance(msg_tu["content"], list)
    tu_block = next(
        b for b in msg_tu["content"] if b.get("type") == "tool_use"
    )
    assert tu_block["id"] == "tool_1", (
        f"tool_use.id 应为 tool_1，实际 {tu_block['id']}"
    )
    assert tu_block["name"] == "file_read"
    assert tu_block["input"] == {"path": "/x"}

    # 第 3 条：user(tool_result)，block 字段完整
    msg_tr = history[2]
    assert msg_tr["role"] == "user"
    assert isinstance(msg_tr["content"], list)
    tr_block = next(
        b for b in msg_tr["content"] if b.get("type") == "tool_result"
    )
    assert tr_block["tool_use_id"] == "tool_1", (
        f"tool_result.tool_use_id 应为 tool_1，实际 {tr_block['tool_use_id']}"
    )
    assert tr_block["content"] == "file content here"

    # 配对关系：tool_use.id == tool_result.tool_use_id
    assert tu_block["id"] == tr_block["tool_use_id"], (
        "tool_use 与 tool_result 的 id 配对关系丢失"
    )

    # 第 4 条：assistant 文本
    assert history[3]["content"] == "已读取文件，内容是 file content here"


def test_restart_recovers_multiple_tool_rounds(tmp_path):
    """重启后能恢复多轮工具调用，3 个 tool_use_id 互不相同且配对正确。

    场景：模拟 3 轮工具调用，每轮 user→assistant(tool_use)→user(tool_result)
    →assistant(text) 共 4 条，3 轮共 12 条；重启后 get_history 应恢复全部
    12 条，3 个 tool_use_id（tool_a / tool_b / tool_c）互不相同且与对应
    tool_result 的 tool_use_id 一一配对。
    """
    sid = "session-multi-round"
    tool_ids = ["tool_a", "tool_b", "tool_c"]

    # 第一阶段：buf1 写入 3 轮工具调用对话
    buf1 = HistoryBuffer(max_turns=50, persistence_dir=str(tmp_path))
    for i, tid in enumerate(tool_ids):
        buf1.add_message(sid, "user", f"第 {i + 1} 轮请求")
        buf1.add_message(
            sid,
            "assistant",
            make_tool_use_content(
                tid, name="search", input_data={"q": f"q{i}"}
            ),
        )
        buf1.add_message(
            sid,
            "user",
            make_tool_result_content(tid, content=f"result-{i}"),
        )
        buf1.add_message(sid, "assistant", f"第 {i + 1} 轮总结")
    del buf1

    # 第二阶段：新实例从磁盘恢复
    buf2 = HistoryBuffer(max_turns=50, persistence_dir=str(tmp_path))
    history = buf2.get_history(sid)

    # 全部 12 条消息恢复
    assert len(history) == 12, (
        f"应恢复 12 条消息，实际 {len(history)} 条"
    )

    # 收集所有 tool_use 与 tool_result 的 id
    tu_ids = []
    tr_ids = []
    for msg in history:
        if not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tu_ids.append(block["id"])
            elif block.get("type") == "tool_result":
                tr_ids.append(block["tool_use_id"])

    # 3 个 tool_use_id 互不相同
    assert len(tu_ids) == 3, f"应有 3 个 tool_use block，实际 {len(tu_ids)}"
    assert len(set(tu_ids)) == 3, (
        f"3 个 tool_use_id 应互不相同，实际 {tu_ids}"
    )
    assert sorted(tu_ids) == sorted(tool_ids), (
        f"tool_use_id 集合应为 {tool_ids}，实际 {tu_ids}"
    )

    # 3 个 tool_result 的 tool_use_id 与 tool_use 一一配对
    assert len(tr_ids) == 3, (
        f"应有 3 个 tool_result block，实际 {len(tr_ids)}"
    )
    assert sorted(tr_ids) == sorted(tool_ids), (
        f"tool_result.tool_use_id 集合应为 {tool_ids}，实际 {tr_ids}"
    )

    # 验证每轮配对顺序：assistant(tool_use) 紧跟 user(tool_result)，id 一致
    for i, tid in enumerate(tool_ids):
        # 第 i 轮的起始索引（每轮 4 条）
        base = i * 4
        tu_msg = history[base + 1]
        tr_msg = history[base + 2]
        assert tu_msg["role"] == "assistant"
        assert tr_msg["role"] == "user"
        tu_block = next(
            b for b in tu_msg["content"] if b.get("type") == "tool_use"
        )
        tr_block = next(
            b for b in tr_msg["content"] if b.get("type") == "tool_result"
        )
        assert tu_block["id"] == tid
        assert tr_block["tool_use_id"] == tid
        assert tr_block["content"] == f"result-{i}"


def test_fifo_eviction_keeps_disk_full_history(tmp_path):
    """FIFO 截断时内存仅保留最近 N 条，磁盘保留全量历史。

    场景：buf1 max_turns=5，写入 10 条消息（含 tool 调用）触发 FIFO
    淘汰；内存仅保留最近 5 条，但磁盘 JSONL 仍是 10 行；重启 buf2
    max_turns=5，get_history 返回 5 条（最近），磁盘文件仍是 10 行。
    验证"内存=工作集 / 磁盘=全量归档"的设计决策在重启后依然成立。
    """
    sid = "session-fifo-disk-full"

    # 第一阶段：buf1 max_turns=5，写 10 条消息触发 FIFO
    buf1 = HistoryBuffer(max_turns=5, persistence_dir=str(tmp_path))
    # 前 2 条为 tool 配对，后 8 条为普通文本（便于断言最近 5 条）
    buf1.add_message(
        sid,
        "assistant",
        make_tool_use_content("tu_x", name="file_read"),
    )
    buf1.add_message(
        sid,
        "user",
        make_tool_result_content("tu_x", content="content-x"),
    )
    for i in range(8):
        buf1.add_message(sid, "user", f"msg-{i}")

    # 内存仅保留最近 5 条（msg-3, msg-4, msg-5, msg-6, msg-7）
    mem_history = buf1.get_history(sid)
    assert len(mem_history) == 5, (
        f"内存应保留最近 5 条，实际 {len(mem_history)} 条"
    )
    assert mem_history[0]["content"] == "msg-3", (
        f"首条应为 msg-3，实际 {mem_history[0]['content']}"
    )
    assert mem_history[-1]["content"] == "msg-7", (
        f"末条应为 msg-7，实际 {mem_history[-1]['content']}"
    )

    # 磁盘保留全量 10 条（FIFO 截断不删磁盘）
    jsonl_path = tmp_path / "session-fifo-disk-full.jsonl"
    disk_lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(disk_lines) == 10, (
        f"磁盘应保留全量 10 条，实际 {len(disk_lines)} 条"
    )

    # 第二阶段：重启 buf2 max_turns=5
    del buf1
    buf2 = HistoryBuffer(max_turns=5, persistence_dir=str(tmp_path))
    restarted_history = buf2.get_history(sid)

    # 内存工作集仍是 5 条（最近）
    assert len(restarted_history) == 5, (
        f"重启后内存应保留最近 5 条，实际 {len(restarted_history)} 条"
    )
    assert restarted_history[0]["content"] == "msg-3", (
        f"重启后首条应为 msg-3，实际 {restarted_history[0]['content']}"
    )
    assert restarted_history[-1]["content"] == "msg-7", (
        f"重启后末条应为 msg-7，实际 {restarted_history[-1]['content']}"
    )

    # 磁盘文件仍是 10 行（重启加载不删磁盘）
    disk_lines_after = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(disk_lines_after) == 10, (
        f"重启后磁盘应仍为 10 行，实际 {len(disk_lines_after)} 行"
    )

    # 磁盘首条仍是最早的 tool_use（tu_x），完整保留
    first_disk_msg = json.loads(disk_lines_after[0])
    assert first_disk_msg["role"] == "assistant"
    tu_block = first_disk_msg["content"][1]
    assert tu_block["type"] == "tool_use"
    assert tu_block["id"] == "tu_x"


def test_clear_session_removes_both_memory_and_disk(tmp_path):
    """clear_session 同步清除内存与磁盘文件，重启后无法恢复。

    场景：buf1 写入消息产生磁盘文件 → clear_session 删除内存与磁盘 →
    重启 buf2 后 get_history 返回空列表，磁盘文件不存在。
    """
    sid = "session-clear-integration"
    jsonl_path = tmp_path / "session-clear-integration.jsonl"

    # 第一阶段：buf1 写入消息
    buf1 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    buf1.add_message(sid, "user", "A")
    buf1.add_message(sid, "assistant", "B")
    buf1.add_message(
        sid,
        "assistant",
        make_tool_use_content("tu_clr", name="file_read"),
    )
    buf1.add_message(
        sid,
        "user",
        make_tool_result_content("tu_clr", content="clr-content"),
    )
    assert jsonl_path.exists(), "add 后磁盘文件应存在"

    # clear_session 同步清除内存与磁盘
    buf1.clear_session(sid)
    assert not jsonl_path.exists(), "clear_session 后磁盘文件应被删除"
    assert buf1.get_history(sid) == [], "clear_session 后内存应清空"
    del buf1

    # 第二阶段：重启 buf2，磁盘文件不存在，get_history 返回 []
    buf2 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    history = buf2.get_history(sid)
    assert history == [], (
        f"clear_session 后重启应返回空列表，实际 {len(history)} 条"
    )
    assert not jsonl_path.exists(), "重启后磁盘文件不应被重新创建"


def test_cron_session_persistence(tmp_path):
    """cron 会话（session_id 含冒号）持久化与重启恢复。

    场景：session_id="cron:abc123"，文件名应为 cron_abc123.jsonl
    （冒号替换为下划线）；写入含 tool 配对的消息；重启后 get_history
    能恢复完整历史，tool_use/tool_result 配对关系完整保留。
    """
    sid = "cron:abc123"
    # 期望的磁盘文件名：冒号替换为下划线
    jsonl_path = tmp_path / "cron_abc123.jsonl"

    # 第一阶段：buf1 写入 cron 会话消息（含 tool 配对）
    buf1 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    buf1.add_message(sid, "user", "cron 任务输入")
    buf1.add_message(
        sid,
        "assistant",
        make_tool_use_content(
            "cron_tool_1", name="schedule", input_data={"when": "daily"}
        ),
    )
    buf1.add_message(
        sid,
        "user",
        make_tool_result_content("cron_tool_1", content="scheduled ok"),
    )
    buf1.add_message(sid, "assistant", "cron 任务完成")
    del buf1

    # 文件名含冒号的非法文件不应存在
    bad_path = tmp_path / "cron:abc123.jsonl"
    assert not bad_path.exists(), "不应创建含冒号的非法文件名"
    # 期望的安全文件名应存在
    assert jsonl_path.exists(), "cron_abc123.jsonl 应存在"

    # 第二阶段：重启 buf2，从磁盘恢复
    buf2 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    history = buf2.get_history(sid)

    # 全部 4 条恢复
    assert len(history) == 4, (
        f"cron 会话应恢复 4 条消息，实际 {len(history)} 条"
    )
    assert [m["role"] for m in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]

    # tool_use/tool_result 配对完整保留
    msg_tu = history[1]
    msg_tr = history[2]
    tu_block = next(
        b for b in msg_tu["content"] if b.get("type") == "tool_use"
    )
    tr_block = next(
        b for b in msg_tr["content"] if b.get("type") == "tool_result"
    )
    assert tu_block["id"] == "cron_tool_1"
    assert tu_block["name"] == "schedule"
    assert tu_block["input"] == {"when": "daily"}
    assert tr_block["tool_use_id"] == "cron_tool_1"
    assert tr_block["content"] == "scheduled ok"
    assert tu_block["id"] == tr_block["tool_use_id"], (
        "cron 会话 tool_use/tool_result 配对关系丢失"
    )


# ---------------------------------------------------------------------------
# SubTask 5.2：验证 react_loop 工具调用后内存与磁盘一致
# ---------------------------------------------------------------------------


def test_original_tool_result_not_summarized_in_history(tmp_path):
    """超长 tool_result 原始内容写入内存与磁盘，无 "[原始结果" 摘要前缀。

    验证 HistoryBuffer 存储的 tool_result 是原始内容：只要原始内容传入
    add_message，内存与磁盘就保留原始内容，不会被替换为
    ``"[原始结果 N chars，已摘要]"`` 摘要格式。tool_result 的 token 控制
    由 Condenser masking 在 LLM 调用前单点负责，HistoryBuffer 不做摘要。

    本测试不直接调用 react_loop（react_loop 需要 mock LLM），而是验证
    HistoryBuffer 存储的 tool_result 是原始的——重点确认无 "[原始结果" 前缀。
    """
    sid = "session-no-summary"
    # 构造 5000 字符的原始 tool_result 内容
    original_long_content = "X" * 5000

    # 第一阶段：buf1 写入一条 user(tool_result) 消息，content 是超长原始字符串
    buf1 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    buf1.add_message(
        sid,
        "assistant",
        make_tool_use_content(
            "tool_long", name="file_read", input_data={"path": "/big"}
        ),
    )
    buf1.add_message(
        sid,
        "user",
        make_tool_result_content(
            "tool_long", content=original_long_content
        ),
    )

    # —— 断言内存中 tool_result.content 是完整原始字符串 ——
    mem_history = buf1.get_history(sid)
    assert len(mem_history) == 2

    mem_tr_msg = mem_history[1]
    assert mem_tr_msg["role"] == "user"
    mem_tr_block = mem_tr_msg["content"][0]
    assert mem_tr_block["type"] == "tool_result"
    assert mem_tr_block["tool_use_id"] == "tool_long"

    # 内容是完整 5000 字符的原始字符串
    assert mem_tr_block["content"] == original_long_content, (
        f"内存中 tool_result.content 应为完整原始字符串（{len(original_long_content)} 字符），"
        f"实际长度 {len(mem_tr_block['content'])}"
    )
    # 不应包含摘要前缀（Task 2 已移除即时压缩）
    assert "[原始结果" not in mem_tr_block["content"], (
        "内存中 tool_result.content 不应含 '[原始结果' 摘要前缀"
    )
    assert "已摘要" not in mem_tr_block["content"], (
        "内存中 tool_result.content 不应含 '已摘要' 摘要标记"
    )

    # —— 断言磁盘中 tool_result.content 也是完整原始字符串 ——
    jsonl_path = tmp_path / "session-no-summary.jsonl"
    disk_lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(disk_lines) == 2

    disk_tr_msg = json.loads(disk_lines[1])
    assert disk_tr_msg["role"] == "user"
    disk_tr_block = disk_tr_msg["content"][0]
    assert disk_tr_block["type"] == "tool_result"
    assert disk_tr_block["tool_use_id"] == "tool_long"
    assert disk_tr_block["content"] == original_long_content, (
        f"磁盘中 tool_result.content 应为完整原始字符串，"
        f"实际长度 {len(disk_tr_block['content'])}"
    )
    assert "[原始结果" not in disk_tr_block["content"], (
        "磁盘中 tool_result.content 不应含 '[原始结果' 摘要前缀"
    )

    # —— 重启 buf2，验证从磁盘恢复后仍是完整原始字符串 ——
    del buf1
    buf2 = HistoryBuffer(max_turns=20, persistence_dir=str(tmp_path))
    restarted_history = buf2.get_history(sid)
    assert len(restarted_history) == 2

    restarted_tr_block = restarted_history[1]["content"][0]
    assert restarted_tr_block["type"] == "tool_result"
    assert restarted_tr_block["tool_use_id"] == "tool_long"
    assert restarted_tr_block["content"] == original_long_content, (
        "重启后 tool_result.content 应仍为完整原始字符串"
    )
    assert "[原始结果" not in restarted_tr_block["content"], (
        "重启后 tool_result.content 不应含 '[原始结果' 摘要前缀"
    )


if __name__ == "__main__":
    import pytest

    # 支持直接运行：python tests/test_history_persistence_integration.py
    raise SystemExit(pytest.main([__file__, "-v"]))
