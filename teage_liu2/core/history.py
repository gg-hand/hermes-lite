"""历史存储 SPI 与 SQLite 默认实现(搬自老系统 teage_liu/storage/sqlite_log.py)。

设计:
- :class:`HistoryStore` 是抽象接口 —— 主干只依赖接口,可换内存 / 文件 / 远端实现
- :class:`SQLiteHistoryStore` 是默认实现 —— 表结构兼容老系统
  (sessions / messages 两表 + 迁移逻辑),并存期可无缝指向老库,
  M1 默认使用独立库文件避免格式冲突

角色定位(沿袭老系统):
- SQLite 的 messages 表是会话消息的权威存储
- M1 主干用它做历史落盘与重启恢复;FTS5 全文检索由 /recall 类功能使用

裁剪说明(相对老 SessionLogger):
- 删除:file chunks(FTS 扩展)、delete_old_sessions(TTL 清理,M2 带回)、
  get_messages_since / get_recent_role_messages / session_exists(按需再加)
- 保留:建表迁移逻辑(is_error / title / attachments / message_type / reasoning
  列兼容)、FTS5 unicode61 中文分词建表与回填、log_message 的 FTS 同步
"""

from __future__ import annotations

import abc
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional


class HistoryStore(abc.ABC):
    """历史存储 SPI:主干对话的会话消息权威存储。"""

    @abc.abstractmethod
    def ensure_session(self, session_id: str) -> None:
        """确保会话存在(不存在则创建)。"""

    @abc.abstractmethod
    def log_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        token_count: int = 0,
        is_error: bool = False,
        reasoning: Optional[str] = None,
    ) -> None:
        """记录一条消息,并更新对应会话的 updated_at。"""

    @abc.abstractmethod
    def get_session_messages(
        self, session_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """获取会话全部消息(按时间正序)。"""

    @abc.abstractmethod
    def update_session_title(self, session_id: str, title: str) -> None:
        """更新会话标题。"""

    @abc.abstractmethod
    def get_session_title(self, session_id: str) -> Optional[str]:
        """读取会话标题。"""

    @abc.abstractmethod
    def search_messages(
        self, keyword: str, session_id: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """全文检索消息内容(基于 FTS5,中文按字符分词)。"""

    @abc.abstractmethod
    def close(self) -> None:
        """关闭连接。"""


class SQLiteHistoryStore(HistoryStore):
    """SQLite 实现(表结构兼容老系统 sessions.db)。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        # check_same_thread=False 允许跨线程共享连接
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._lock = threading.Lock()
        self._init_tables()
        self._ensure_fts_table()

    def _init_tables(self) -> None:
        """创建表结构与索引(若不存在),含旧库列迁移。"""
        with self._lock:
            cur = self.conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
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
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
                """
            )
            # 兼容旧库:补列(数据迁移,旧数据按默认值处理)
            _migrate_column(self.conn, "messages", "is_error", "INTEGER DEFAULT 0")
            _migrate_column(self.conn, "sessions", "title", "TEXT")
            _migrate_column(self.conn, "messages", "attachments", "TEXT")
            _migrate_column(self.conn, "messages", "message_type", "TEXT")
            _migrate_column(self.conn, "messages", "reasoning", "TEXT")
            self.conn.commit()

    def _ensure_fts_table(self) -> None:
        """确保 FTS5 全文索引表存在,旧库自动回填。

        分词器用 ``trigram``(SQLite 3.34+):按 3 字符滑动窗口切分,
        正确支持中文子串检索。注意:老系统用 ``unicode61`` 但该分词器
        不按中文单字分词(连续中文整句为一个 token),中文关键词搜不到
        —— 新库修正为 trigram;已存在的旧 FTS 表保持不动(兼容)。
        """
        with self._lock:
            try:
                self.conn.execute("SELECT count(*) FROM messages_fts LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content,
                        session_id UNINDEXED,
                        created_at UNINDEXED,
                        content='messages',
                        content_rowid='id',
                        tokenize='trigram'
                    )
                    """
                )
                self.conn.execute(
                    """
                    INSERT INTO messages_fts(rowid, content, session_id, created_at)
                    SELECT id, content, session_id, created_at FROM messages
                    """
                )
                self.conn.commit()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    def create_session(self, session_id: Optional[str] = None) -> str:
        """创建新会话(指定 ID 或自动生成 UUID)。"""
        if session_id is None:
            session_id = str(uuid.uuid4())
        now = self._now_iso()
        with self._lock:
            self.conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
                (session_id, now, now),
            )
            self.conn.commit()
        return session_id

    def ensure_session(self, session_id: str) -> None:
        """确保会话存在(INSERT OR IGNORE 原子操作)。"""
        now = self._now_iso()
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions (id, created_at, updated_at) "
                "VALUES (?, ?, ?)",
                (session_id, now, now),
            )
            self.conn.commit()

    def log_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        token_count: int = 0,
        is_error: bool = False,
        reasoning: Optional[str] = None,
    ) -> None:
        """记录一条消息,并同步 FTS 表与会话 updated_at。"""
        now = self._now_iso()
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, tool_name, tool_call_id,
                     token_count, is_error, created_at, reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, role, content, tool_name, tool_call_id,
                    token_count, 1 if is_error else 0, now, reasoning,
                ),
            )
            message_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO messages_fts(rowid, content, session_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (message_id, content, session_id, now),
            )
            self.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            self.conn.commit()

    def get_session_messages(
        self, session_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """获取会话全部消息(按时间正序)。"""
        sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC"
        params: list = [session_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def update_session_title(self, session_id: str, title: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)
            )
            self.conn.commit()

    def get_session_title(self, session_id: str) -> Optional[str]:
        cur = self.conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        )
        row = cur.fetchone()
        return row["title"] if row else None

    def search_messages(
        self, keyword: str, session_id: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """全文检索(FTS5 trigram + LIKE 子串匹配)。

        注意(坑):trigram 分词器的 MATCH 查询对 <3 字符的词(如 2 字中文词
        "苹果")返回空——trigram 的官方用法是配合 LIKE/GLOB,由 FTS5 自动
        用 trigram 索引加速;短模式退化为扫描但结果正确。
        LIKE 特殊字符 % _ \\ 需转义,避免通配符注入。
        """
        escaped = (
            keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        sql = (
            "SELECT m.* FROM messages_fts f "
            "JOIN messages m ON m.id = f.rowid "
            "WHERE f.content LIKE ? ESCAPE '\\'"
        )
        params: list = [f"%{escaped}%"]
        if session_id is not None:
            sql += " AND f.session_id = ?"
            params.append(session_id)
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(limit)
        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _migrate_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """兼容旧库:列不存在时补加。"""
    try:
        conn.execute(f"SELECT {column} FROM {table} LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
