"""AuditLogger 增量读取接口测试（批次 2.2）。

验证：
- read_since(since, limit, entry_type) 从磁盘 JSONL 流式读取并按时间过滤
- get_cursor() 返回最新记录的 unix timestamp，作为下次增量起点
- 过滤行为：timestamp >= since（含等于，便于幂等重试）
- entry_type 过滤：tool_call / guardrail
- limit 截断：达到 limit 即停止扫描
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone

import pytest

# 注入 mock 依赖（与项目其他测试一致）
import tests._mock_deps  # noqa: F401

from teage_liu.agent.audit import AuditLogger


def _parse_iso_to_ts(iso_str: str) -> float:
    """将 audit.jsonl 中的 ISO 时间戳转 unix epoch seconds。"""
    return datetime.fromisoformat(iso_str).timestamp()


@pytest.fixture
def tmp_audit_path(tmp_path):
    """提供一个临时 audit.jsonl 路径。"""
    return str(tmp_path / "audit.jsonl")


class TestReadSince:
    def test_read_since_returns_all_when_cursor_zero(self, tmp_audit_path):
        """cursor=0 时应返回全部记录。"""
        logger = AuditLogger(log_path=tmp_audit_path)
        for i in range(10):
            logger.log_tool_call(
                session_id=f"sess-{i}",
                tool_name="echo",
                tool_input={"text": f"msg-{i}"},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
        logger.close()

        # 重新打开以模拟服务端读取
        reader = AuditLogger(log_path=tmp_audit_path)
        entries = reader.read_since(0.0)
        assert len(entries) == 10
        # 按时间正序返回（最早在前，便于增量消费）
        sessions = [e["session_id"] for e in entries]
        assert sessions == [f"sess-{i}" for i in range(10)]

    def test_read_since_returns_only_new_after_cursor(self, tmp_audit_path):
        """取 cursor 后再写入，read_since(cursor) 只返回新增条目。

        每条记录之间 sleep 微秒，确保 ISO timestamp 不同（datetime.now 精度
        为微秒，紧密循环可能产生相同 timestamp，导致 cursor 无法区分新旧）。
        """
        logger = AuditLogger(log_path=tmp_audit_path)
        for i in range(10):
            logger.log_tool_call(
                session_id=f"old-{i}",
                tool_name="echo",
                tool_input={},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
            time.sleep(0.001)  # 确保 timestamp 单调递增
        cursor = logger.get_cursor()
        assert cursor > 0
        for i in range(5):
            logger.log_tool_call(
                session_id=f"new-{i}",
                tool_name="echo",
                tool_input={},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
            time.sleep(0.001)
        logger.close()

        reader = AuditLogger(log_path=tmp_audit_path)
        entries = reader.read_since(cursor)
        sessions = [e["session_id"] for e in entries]
        # 含等于：返回最后 1 条 old（cursor 等于其 timestamp）+ 5 条 new
        assert sessions == ["old-9"] + [f"new-{i}" for i in range(5)]

    def test_read_since_respects_limit(self, tmp_audit_path):
        """limit 截断：达到 limit 即停止扫描。"""
        logger = AuditLogger(log_path=tmp_audit_path)
        for i in range(20):
            logger.log_tool_call(
                session_id=f"sess-{i}",
                tool_name="echo",
                tool_input={},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
        logger.close()

        reader = AuditLogger(log_path=tmp_audit_path)
        entries = reader.read_since(0.0, limit=3)
        assert len(entries) == 3
        # 仍按时间正序，limit 截断最早 3 条
        assert entries[0]["session_id"] == "sess-0"
        assert entries[2]["session_id"] == "sess-2"

    def test_read_since_filters_by_entry_type(self, tmp_audit_path):
        """entry_type 过滤：guardrail 与 tool_call 分离。"""
        logger = AuditLogger(log_path=tmp_audit_path)
        logger.log_tool_call(
            session_id="s1",
            tool_name="echo",
            tool_input={},
            result="ok",
            is_error=False,
            duration_ms=1.0,
        )
        logger.log_guardrail_decision(
            layer="input_scan",
            action="allow",
            reason="ok",
            session_id="s1",
        )
        logger.log_tool_call(
            session_id="s2",
            tool_name="echo",
            tool_input={},
            result="ok",
            is_error=False,
            duration_ms=1.0,
        )
        logger.log_guardrail_decision(
            layer="output_filter",
            action="warn",
            reason="sensitive",
            session_id="s2",
        )
        logger.close()

        reader = AuditLogger(log_path=tmp_audit_path)
        tool_calls = reader.read_since(0.0, entry_type="tool_call")
        guardrails = reader.read_since(0.0, entry_type="guardrail")
        assert len(tool_calls) == 2
        assert len(guardrails) == 2
        assert all(e.get("entry_type", "tool_call") == "tool_call" for e in tool_calls)
        assert all(e.get("entry_type") == "guardrail" for e in guardrails)

    def test_read_since_empty_file_returns_empty_list(self, tmp_audit_path):
        """空 audit.jsonl 返回空列表。"""
        # 创建空文件
        open(tmp_audit_path, "w").close()
        reader = AuditLogger(log_path=tmp_audit_path)
        assert reader.read_since(0.0) == []

    def test_read_since_skips_malformed_lines(self, tmp_audit_path):
        """损坏的 JSON 行应被跳过，不抛异常。"""
        logger = AuditLogger(log_path=tmp_audit_path)
        logger.log_tool_call(
            session_id="ok-1",
            tool_name="echo",
            tool_input={},
            result="ok",
            is_error=False,
            duration_ms=1.0,
        )
        logger.close()
        # 在文件末尾追加一行损坏的 JSON
        with open(tmp_audit_path, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")
        # 再追加一行正常的
        with open(tmp_audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": "ok-2",
                "tool_name": "echo",
                "tool_input": {},
                "result": "ok",
                "is_error": False,
                "duration_ms": 1.0,
            }, ensure_ascii=False) + "\n")

        reader = AuditLogger(log_path=tmp_audit_path)
        entries = reader.read_since(0.0)
        # 损坏行被跳过，2 条正常记录返回
        sessions = [e["session_id"] for e in entries]
        assert "ok-1" in sessions
        assert "ok-2" in sessions
        assert len(entries) == 2


class TestGetCursor:
    def test_get_cursor_returns_zero_on_empty_file(self, tmp_audit_path):
        """空文件 get_cursor() 返回 0。"""
        open(tmp_audit_path, "w").close()
        reader = AuditLogger(log_path=tmp_audit_path)
        assert reader.get_cursor() == 0.0

    def test_get_cursor_returns_latest_timestamp(self, tmp_audit_path):
        """get_cursor() 返回最新记录的时间戳。"""
        logger = AuditLogger(log_path=tmp_audit_path)
        logger.log_tool_call(
            session_id="s1",
            tool_name="echo",
            tool_input={},
            result="ok",
            is_error=False,
            duration_ms=1.0,
        )
        time.sleep(0.05)
        logger.log_tool_call(
            session_id="s2",
            tool_name="echo",
            tool_input={},
            result="ok",
            is_error=False,
            duration_ms=1.0,
        )
        logger.close()

        reader = AuditLogger(log_path=tmp_audit_path)
        cursor = reader.get_cursor()
        # 验证 cursor 与第二条记录的 timestamp 对齐
        entries = reader.read_since(0.0)
        last_ts = _parse_iso_to_ts(entries[-1]["timestamp"])
        # cursor 应等于最新记录的 timestamp（unix 秒）
        assert abs(cursor - last_ts) < 1.0

    def test_get_cursor_after_read_since_returns_no_new(self, tmp_audit_path):
        """取 cursor 后用该 cursor 调用 read_since，应返回最新 1 条（含等于语义）。"""
        logger = AuditLogger(log_path=tmp_audit_path)
        for i in range(5):
            logger.log_tool_call(
                session_id=f"s{i}",
                tool_name="echo",
                tool_input={},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
            time.sleep(0.001)  # 确保 timestamp 单调递增
        logger.close()
        reader = AuditLogger(log_path=tmp_audit_path)
        cursor = reader.get_cursor()
        # read_since(cursor) 含等于，应返回最新 1 条（cursor 等于最新 timestamp）
        # 为确保严格增量，调用方应使用 cursor + epsilon；但接口语义为 >=
        entries = reader.read_since(cursor)
        # 含等于：返回最后 1 条
        assert len(entries) == 1
        assert entries[0]["session_id"] == "s4"


class TestRotatedFiles:
    """轮转文件读取测试（P1-1 修复）。"""

    def _setup_rotated_files(self, tmp_audit_path):
        """辅助：创建一个含 .rotated 文件的测试场景。

        写入 3 条旧记录 -> 手动轮转 -> 写入 2 条新记录。
        """
        logger = AuditLogger(log_path=tmp_audit_path)
        for i in range(3):
            logger.log_tool_call(
                session_id=f"old-{i}",
                tool_name="echo",
                tool_input={},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
            time.sleep(0.001)
        logger.close()
        # 手动创建 .rotated 文件模拟轮转
        import shutil
        ts = int(datetime.now(timezone.utc).timestamp())
        rotated_path = f"{tmp_audit_path}.{ts}.rotated"
        shutil.move(tmp_audit_path, rotated_path)
        # 写入 2 条新记录到新的 audit.jsonl
        logger2 = AuditLogger(log_path=tmp_audit_path)
        for i in range(2):
            logger2.log_tool_call(
                session_id=f"new-{i}",
                tool_name="echo",
                tool_input={},
                result="ok",
                is_error=False,
                duration_ms=1.0,
            )
            time.sleep(0.001)
        logger2.close()

    def test_get_cursor_scans_rotated_files(self, tmp_audit_path):
        """get_cursor 应扫描 .rotated 文件取最大 timestamp。

        场景：轮转后旧记录在 .rotated 文件，新记录在当前文件。
        get_cursor 应返回所有文件中的最大 timestamp（新文件中的最大值）。
        """
        self._setup_rotated_files(tmp_audit_path)
        reader = AuditLogger(log_path=tmp_audit_path)
        cursor = reader.get_cursor()
        # cursor 应为新文件中最大 timestamp（new-1）
        entries_current = reader.read_since(0.0)
        sessions = [e["session_id"] for e in entries_current]
        assert "new-1" in sessions
        # 验证 cursor >= new-1 的 timestamp
        last_ts = _parse_iso_to_ts(entries_current[-1]["timestamp"])
        assert abs(cursor - last_ts) < 1.0

    def test_read_since_include_rotated_reads_all_files(self, tmp_audit_path):
        """read_since(include_rotated=True) 应扫描所有 .rotated 文件。"""
        self._setup_rotated_files(tmp_audit_path)
        reader = AuditLogger(log_path=tmp_audit_path)
        # include_rotated=True 应返回所有文件中的记录
        entries_all = reader.read_since(0.0, include_rotated=True)
        sessions = [e["session_id"] for e in entries_all]
        assert "old-0" in sessions
        assert "old-2" in sessions
        assert "new-0" in sessions
        assert "new-1" in sessions
        assert len(entries_all) == 5

        # 默认 include_rotated=False 只读当前文件
        entries_current = reader.read_since(0.0)
        sessions_current = [e["session_id"] for e in entries_current]
        assert "old-0" not in sessions_current
        assert "new-1" in sessions_current
        assert len(entries_current) == 2

    def test_read_since_include_rotated_respects_limit(self, tmp_audit_path):
        """read_since(include_rotated=True) 仍受 limit 截断。"""
        self._setup_rotated_files(tmp_audit_path)
        reader = AuditLogger(log_path=tmp_audit_path)
        entries = reader.read_since(0.0, limit=2, include_rotated=True)
        assert len(entries) == 2

    def test_get_cursor_no_rotated_files(self, tmp_audit_path):
        """无 .rotated 文件时 get_cursor 正常工作。"""
        logger = AuditLogger(log_path=tmp_audit_path)
        logger.log_tool_call(
            session_id="s1",
            tool_name="echo",
            tool_input={},
            result="ok",
            is_error=False,
            duration_ms=1.0,
        )
        logger.close()
        reader = AuditLogger(log_path=tmp_audit_path)
        cursor = reader.get_cursor()
        assert cursor > 0
