"""P2-5 会话存在性检查优化测试 — 验证 session_exists 与 _ensure_session。

运行方式:
    python -m unittest tests.test_session_logger_ext -v

测试目标:
- session_exists 返回正确结果
- 大量会话下性能验证
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from src.storage.sqlite_log import SessionLogger


class TestSessionExistsContract(unittest.TestCase):
    """验证 session_exists 方法的行为。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="session_logger_test_")
        self._db_path = os.path.join(self._tmpdir, "sessions.db")
        self.logger = SessionLogger(self._db_path)

    def test_session_exists_after_creation(self):
        """创建后 session_exists 返回 True。"""
        self.logger.create_session("test_session_1")
        self.assertTrue(self.logger.session_exists("test_session_1"))

    def test_session_not_exists(self):
        """未创建的 session 返回 False。"""
        self.assertFalse(self.logger.session_exists("nonexistent_session"))

    def test_session_exists_multiple_sessions(self):
        """多个 session 中正确识别。"""
        self.logger.create_session("session_a")
        self.logger.create_session("session_b")
        self.logger.create_session("session_c")

        self.assertTrue(self.logger.session_exists("session_b"))
        self.assertFalse(self.logger.session_exists("session_d"))

    def test_session_exists_after_delete(self):
        """删除后 session_exists 返回 False。"""
        self.logger.create_session("to_delete")
        self.assertTrue(self.logger.session_exists("to_delete"))
        self.logger.delete_session("to_delete")
        self.assertFalse(self.logger.session_exists("to_delete"))

    def test_session_exists_empty_session_id(self):
        """空字符串 session_id 不抛异常。"""
        try:
            result = self.logger.session_exists("")
            self.assertIsInstance(result, bool)
        except Exception as e:
            self.fail(f"session_exists('') 抛异常: {e}")

    def test_session_exists_special_chars(self):
        """含特殊字符的 session_id 正确工作。"""
        special_id = "user:abc-123_def@host"
        self.logger.create_session(special_id)
        self.assertTrue(self.logger.session_exists(special_id))


class TestSessionExistsPerformance(unittest.TestCase):
    """大量会话下 session_exists 性能。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="session_perf_")
        self._db_path = os.path.join(self._tmpdir, "sessions.db")
        self.logger = SessionLogger(self._db_path)

        # 创建大量会话
        for i in range(100):
            self.logger.create_session(f"perf_session_{i}")

    def test_session_exists_is_fast(self):
        """session_exists 在 100 个会话中应 < 10ms。"""
        t0 = time.perf_counter()
        for i in range(10):
            self.logger.session_exists(f"perf_session_{i * 10}")
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self.assertLess(elapsed_ms, 10,
                        f"10 次 session_exists 应 < 10ms，实际 {elapsed_ms:.1f}ms")


class TestEnsureSessionAlternative(unittest.TestCase):
    """验证 _ensure_session 的替代实现（INSERT OR IGNORE）。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="ensure_session_")
        self._db_path = os.path.join(self._tmpdir, "sessions.db")
        self.logger = SessionLogger(self._db_path)

    def test_insert_or_ignore_duplicate(self):
        """INSERT OR IGNORE 重复 session 不抛异常。"""
        self.logger.create_session("unique_session")
        # 直接执行 INSERT OR IGNORE（模拟优化后的 _ensure_session）
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, created_at, updated_at) VALUES (?, datetime('now'), datetime('now'))",
                ("unique_session",),
            )
            conn.commit()
        except Exception as e:
            self.fail(f"INSERT OR IGNORE 抛异常: {e}")
        finally:
            conn.close()

    def test_create_session_then_ensure(self):
        """创建 session 后再确认存在性。"""
        self.logger.create_session("my_session")
        self.assertTrue(self.logger.session_exists("my_session"))


if __name__ == "__main__":
    unittest.main()
