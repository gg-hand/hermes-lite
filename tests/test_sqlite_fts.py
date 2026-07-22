"""SessionLogger FTS5 全文检索单元测试（P2: Recall Memory）。

验证基于 SQLite FTS5 的 search_messages 行为，覆盖以下场景：
1. 插入后可检索（基础功能）
2. 跨会话检索（session_id=None 时返回所有会话匹配）
3. session_id 过滤（仅返回指定会话的匹配）
4. limit 生效（限制返回条数）
5. 中文关键词命中（unicode61 按字符分词）
6. 无匹配返回空列表
7. 旧库兼容（先无 FTS 表写入数据，再触发 _ensure_fts_table 回填）

运行方式：
    python -m unittest tests.test_sqlite_fts -v
    python tests/test_sqlite_fts.py

测试策略：
- 每个测试用例使用 tempfile.TemporaryDirectory 创建独立的 SQLite 文件，
  避免污染项目数据，且各用例之间相互隔离。
- 直接对 SessionLogger 实例操作，不依赖 FastAPI / Orchestrator。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.storage.sqlite_log import SessionLogger  # noqa: E402


def _make_logger(tmpdir: str, name: str = "test.db") -> SessionLogger:
    """在临时目录下创建 SessionLogger 实例。"""
    db_path = os.path.join(tmpdir, name)
    return SessionLogger(db_path)


class TestSearchMessages(unittest.TestCase):
    """验证 search_messages 的核心行为。"""

    def test_insert_then_search(self):
        """插入后可检索：log_message 写入 '我们讨论了 Redis 缓存方案'，
        search_messages('Redis') 应命中该条。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid = logger.create_session("sess-1")
                logger.log_message(sid, "user", "我们讨论了 Redis 缓存方案")
                logger.log_message(sid, "assistant", "其它无关内容")

                results = logger.search_messages("Redis")
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["session_id"], "sess-1")
                self.assertIn("Redis", results[0]["content"])
                self.assertEqual(results[0]["role"], "user")
                # 字段完整性
                for key in ("id", "session_id", "role", "content", "created_at"):
                    self.assertIn(key, results[0])
            finally:
                logger.close()

    def test_cross_session_search(self):
        """跨会话检索：两个会话都有 'Redis' 的消息，session_id=None 时
        应返回两个会话的匹配结果。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid_a = logger.create_session("sess-A")
                sid_b = logger.create_session("sess-B")
                logger.log_message(sid_a, "user", "Redis 方案 A")
                logger.log_message(sid_b, "user", "Redis 方案 B")
                logger.log_message(sid_a, "assistant", "无关消息")

                results = logger.search_messages("Redis")
                self.assertEqual(len(results), 2)
                session_ids = {r["session_id"] for r in results}
                self.assertEqual(session_ids, {"sess-A", "sess-B"})
            finally:
                logger.close()

    def test_session_id_filter(self):
        """session_id 过滤：指定 session_id 只返回该会话的匹配。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid_a = logger.create_session("sess-A")
                sid_b = logger.create_session("sess-B")
                logger.log_message(sid_a, "user", "Redis 方案 A")
                logger.log_message(sid_b, "user", "Redis 方案 B")

                results = logger.search_messages("Redis", session_id="sess-A")
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["session_id"], "sess-A")

                # sess-B 的过滤同样只返回 B 的匹配
                results_b = logger.search_messages("Redis", session_id="sess-B")
                self.assertEqual(len(results_b), 1)
                self.assertEqual(results_b[0]["session_id"], "sess-B")
            finally:
                logger.close()

    def test_limit(self):
        """limit 生效：插入 30 条匹配消息，limit=10 只返回 10 条。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid = logger.create_session("sess-limit")
                for i in range(30):
                    logger.log_message(sid, "user", f"Redis 消息编号 {i}")

                results = logger.search_messages("Redis", limit=10)
                self.assertEqual(len(results), 10)
                # 应按 created_at 降序（最新在前），即编号 29 在第一条
                self.assertIn("29", results[0]["content"])
            finally:
                logger.close()

    def test_chinese_keyword(self):
        """中文关键词命中：插入 '用户喜欢 Python 编程'，
        搜索 'Python' 命中。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid = logger.create_session("sess-cn")
                logger.log_message(sid, "user", "用户喜欢 Python 编程")
                logger.log_message(sid, "assistant", "今天天气不错")

                # 英文关键词命中中文句子
                results = logger.search_messages("Python")
                self.assertEqual(len(results), 1)
                self.assertIn("Python", results[0]["content"])

                # 中文单字命中（unicode61 按字符分词）
                results_cn = logger.search_messages("编程")
                self.assertEqual(len(results_cn), 1)
                self.assertIn("编程", results_cn[0]["content"])
            finally:
                logger.close()

    def test_no_match_returns_empty(self):
        """无匹配返回空列表：搜索不存在的关键词返回空列表。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid = logger.create_session("sess-empty")
                logger.log_message(sid, "user", "Redis 缓存方案")

                results = logger.search_messages("不存在的关键词XYZ")
                self.assertEqual(results, [])
                self.assertEqual(len(results), 0)
            finally:
                logger.close()


class TestFtsBackfill(unittest.TestCase):
    """验证旧库兼容：FTS 表不存在时自动创建并回填。"""

    def test_backfill_old_db(self):
        """旧库兼容：先用裸 SQLite 写入 messages 数据（不创建 FTS 表），
        再实例化 SessionLogger 触发 _ensure_fts_table，验证能回填并检索。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "old.db")
            # 模拟旧库：手动建表 + 写入数据，但不创建 FTS 表
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_name TEXT,
                    tool_call_id TEXT,
                    token_count INTEGER DEFAULT 0,
                    is_error INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )
            conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at) "
                "VALUES (?, ?, ?)",
                ("sess-old", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("sess-old", "user", "旧库的 Redis 笔记", "2026-01-01T00:00:01"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("sess-old", "assistant", "另一条无关消息", "2026-01-01T00:00:02"),
            )
            conn.commit()
            conn.close()

            # 实例化 SessionLogger，应触发 _ensure_fts_table 自动回填
            logger = SessionLogger(db_path)
            try:
                # 验证 FTS 表已创建并回填：能检索到旧库数据
                results = logger.search_messages("Redis")
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["session_id"], "sess-old")
                self.assertIn("Redis", results[0]["content"])

                # 验证新写入的消息也能正常检索（同步链路完整）
                logger.log_message("sess-old", "user", "新增的 Redis 内容")
                results_new = logger.search_messages("Redis")
                self.assertEqual(len(results_new), 2)
            finally:
                logger.close()

    def test_ensure_fts_table_idempotent(self):
        """_ensure_fts_table 幂等：已存在 FTS 表时重复调用不应报错，
        也不应重复回填。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid = logger.create_session("sess-idem")
                logger.log_message(sid, "user", "Redis 测试")

                # 再次调用 _ensure_fts_table：FTS 表已存在，应直接 SELECT 成功
                logger._ensure_fts_table()

                # 结果仍只有 1 条，未重复回填
                results = logger.search_messages("Redis")
                self.assertEqual(len(results), 1)
            finally:
                logger.close()


class TestSearchOrdering(unittest.TestCase):
    """验证 search_messages 的排序与字段。"""

    def test_results_ordered_by_created_at_desc(self):
        """结果按 created_at 降序排列（最新在前）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = _make_logger(tmpdir)
            try:
                sid = logger.create_session("sess-order")
                # 依次写入 3 条匹配消息，created_at 递增
                logger.log_message(sid, "user", "Redis 早期")
                logger.log_message(sid, "user", "Redis 中期")
                logger.log_message(sid, "user", "Redis 晚期")

                results = logger.search_messages("Redis", limit=10)
                self.assertEqual(len(results), 3)
                # 最新（最后写入的）应排第一
                self.assertIn("晚期", results[0]["content"])
                self.assertIn("早期", results[2]["content"])
            finally:
                logger.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
