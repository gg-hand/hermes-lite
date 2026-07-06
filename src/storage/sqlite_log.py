"""基于 SQLite 的会话日志持久化。"""

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class SessionLogger:
    """会话日志记录器，负责将会话与消息持久化到 SQLite。"""

    def __init__(self, db_path: str):
        """初始化日志记录器。

        Args:
            db_path: SQLite 数据库文件路径，如 data/sessions.db
        """
        self.db_path = db_path
        # 确保数据库所在目录存在（sqlite3 不会自动创建父目录）
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # check_same_thread=False 允许跨线程共享连接（服务端多线程场景）
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        # 使用 Row 工厂，便于按列名访问结果
        self.conn.row_factory = sqlite3.Row
        # 开启 WAL 模式，提升并发读写性能
        self.conn.execute("PRAGMA journal_mode=WAL;")
        # 写操作互斥锁，保证线程安全
        self._lock = threading.Lock()

        self._init_tables()
        # 确保 FTS5 全文索引表存在（旧库自动回填）
        self._ensure_fts_table()

    def _init_tables(self):
        """创建表结构与索引（若不存在）。"""
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
            # 兼容旧库：若 is_error 列不存在则补加（数据迁移无所谓时旧数据按 0 处理）
            try:
                self.conn.execute("SELECT is_error FROM messages LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(
                    "ALTER TABLE messages ADD COLUMN is_error INTEGER DEFAULT 0"
                )
            # 兼容旧库：若 sessions 表无 title 列则补加（旧数据按 NULL 处理）
            try:
                self.conn.execute("SELECT title FROM sessions LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
            # 兼容旧库：若 messages 表无 attachments 列则补加（文件上传消息附件 JSON）
            try:
                self.conn.execute("SELECT attachments FROM messages LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(
                    "ALTER TABLE messages ADD COLUMN attachments TEXT"
                )
            # 兼容旧库：若 messages 表无 message_type 列则补加（消息类型标记）
            try:
                self.conn.execute("SELECT message_type FROM messages LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(
                    "ALTER TABLE messages ADD COLUMN message_type TEXT"
                )
            self.conn.commit()

    def _ensure_fts_table(self):
        """确保 FTS5 全文索引表存在，旧库自动回填。

        采用手动同步而非触发器，避免旧库迁移时触发器漏触发问题。
        ``unicode61`` 是 SQLite 内置分词器，对中文按字符分词（每个汉字
        作为独立 token），无需外部依赖。

        - 新库：messages 表为空，回填 0 条，等同于仅建表。
        - 旧库：messages 已有数据，回填后立即可检索。
        """
        with self._lock:
            try:
                self.conn.execute("SELECT count(*) FROM messages_fts LIMIT 1")
            except sqlite3.OperationalError:
                # FTS 表不存在，创建并回填
                self.conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content,
                        session_id UNINDEXED,
                        created_at UNINDEXED,
                        content='messages',
                        content_rowid='id',
                        tokenize='unicode61'
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
        """返回当前时间的 ISO 格式字符串。"""
        return datetime.now().isoformat()

    def create_session(self, session_id: str = None) -> str:
        """创建新会话。

        Args:
            session_id: 指定会话 ID，为 None 时自动生成 UUID。

        Returns:
            会话 ID。
        """
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
        """确保会话存在，不存在则创建（原子操作，O(1)）。

        Phase 9: 使用 INSERT OR IGNORE 替代先 SELECT 再 INSERT 模式。
        """
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
        tool_name: str = None,
        tool_call_id: str = None,
        token_count: int = 0,
        is_error: bool = False,
        attachments: str = None,
        message_type: str = None,
    ):
        """记录一条消息，并更新对应会话的 updated_at。

        Args:
            session_id: 所属会话 ID
            role: 消息角色（user / assistant / tool）
            content: 消息内容
            tool_name: 工具名称（role=tool 时使用）
            tool_call_id: 工具调用 ID
            token_count: token 数量
            is_error: 工具调用是否出错（仅 tool_result 有意义，存为 0/1）
            attachments: 附件 JSON 字符串（如文件上传消息的附件元数据）
            message_type: 消息类型标记（如 'file_upload'）
        """
        now = self._now_iso()
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, tool_name, tool_call_id,
                     token_count, is_error, created_at, attachments, message_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, role, content, tool_name, tool_call_id,
                    token_count, 1 if is_error else 0, now,
                    attachments, message_type,
                ),
            )
            message_id = cur.lastrowid
            # 同步到 FTS 表（手动同步，避免触发器在旧库迁移时漏触发）
            self.conn.execute(
                "INSERT INTO messages_fts(rowid, content, session_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (message_id, content, session_id, now),
            )
            # 同步更新会话的最后更新时间
            self.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            self.conn.commit()

    def get_session_messages(self, session_id: str, limit: int = None):
        """获取指定会话的全部消息（按时间正序）。

        Args:
            session_id: 会话 ID
            limit: 最多返回的条数，None 表示不限制

        Returns:
            dict 列表，每条消息为一个字典。
        """
        sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC"
        params = [session_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def search_messages(
        self, keyword: str, session_id: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """全文检索消息内容。

        基于 FTS5 的 MATCH 操作进行检索，``unicode61`` 分词器对中文按字符
        分词，因此中英文关键词均可命中。结果按 ``created_at`` 降序排列，
        即最新匹配在前。

        Args:
            keyword: 搜索关键词。内部会用双引号包裹为 FTS5 phrase 查询，
                防止特殊字符（如 ``*`` ``OR``）干扰；关键词中的双引号会
                按 FTS5 转义规则双写。
            session_id: 可选，按会话过滤。为 None 时跨所有会话检索。
            limit: 返回条数上限，默认 20。

        Returns:
            匹配的消息列表，每项含 id/session_id/role/content/created_at。
            无匹配时返回空列表。
        """
        # FTS5 phrase 查询：关键词用双引号包裹，内部双引号双写转义
        escaped = keyword.replace('"', '""')
        match_query = f'"{escaped}"'
        sql = (
            "SELECT m.id, m.session_id, m.role, m.content, m.created_at "
            "FROM messages_fts f "
            "JOIN messages m ON f.rowid = m.id "
            "WHERE messages_fts MATCH ?"
        )
        params: List[Any] = [match_query]
        if session_id:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        # created_at 降序为主，id 降序为辅（Windows 时钟分辨率较低，
        # 快速写入可能出现相同 created_at，用 id 作为确定性 tiebreaker）
        sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
        params.append(limit)

        cur = self.conn.execute(sql, params)
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "session_id": r[1],
                "role": r[2],
                "content": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def get_recent_messages(self, session_id: str, n: int = 20):
        """获取指定会话最近 N 条消息（按时间正序返回，便于直接拼入上下文）。

        Args:
            session_id: 会话 ID
            n: 取最近 N 条

        Returns:
            dict 列表，按时间正序排列。
        """
        # 子查询取最近 N 条（倒序），外层再正序排列
        sql = """
            SELECT * FROM (
                SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
        """
        cur = self.conn.execute(sql, (session_id, n))
        return [dict(row) for row in cur.fetchall()]

    def get_recent_role_messages(
        self, session_id: str, role: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """获取指定会话中某角色的最近 N 条消息（按时间倒序返回）。

        Phase 8 Task 1.8：供调度执行历史端点使用，按 ``session_id="cron:{id}"``
        取最近 N 条 assistant 回复。SQL 用 WHERE role = ? 过滤后 ORDER BY id
        DESC LIMIT ?，直接利用 ``idx_messages_session`` 索引。

        Args:
            session_id: 会话 ID（如 ``cron:{schedule_id}``）。
            role: 消息角色（如 ``"assistant"``）。
            limit: 返回条数上限，默认 10。

        Returns:
            dict 列表，每项含 id/session_id/role/content/tool_name/created_at，
            按 id 倒序排列（最新在前）。
        """
        sql = (
            "SELECT id, session_id, role, content, tool_name, created_at "
            "FROM messages WHERE session_id = ? AND role = ? "
            "ORDER BY id DESC LIMIT ?"
        )
        cur = self.conn.execute(sql, (session_id, role, limit))
        return [dict(row) for row in cur.fetchall()]

    def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在（O(1) 索引查询）。

        Phase 9 优化：替代 list_sessions() + Python 侧 any() 的 O(n) 方式。

        Args:
            session_id: 会话 ID。

        Returns:
            True 表示会话已存在。
        """
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(1) FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            return row is not None and row[0] > 0

    def list_sessions(self):
        """列出所有会话（按最后更新时间倒序）。

        Returns:
            dict 列表，每个会话为一个字典。
        """
        cur = self.conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_messages_since(self, session_id: str, since: str):
        """获取指定会话中创建时间晚于 since 的消息（供 consolidation 读取）。

        Args:
            session_id: 会话 ID
            since: ISO 格式时间戳，返回此时间之后的消息

        Returns:
            dict 列表，按时间正序排列。
        """
        # ISO 格式字符串可按字典序正确比较时间先后
        sql = (
            "SELECT * FROM messages WHERE session_id = ? AND created_at > ? "
            "ORDER BY id ASC"
        )
        cur = self.conn.execute(sql, (session_id, since))
        return [dict(row) for row in cur.fetchall()]

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话及其所有消息。

        Args:
            session_id: 要删除的会话 ID。

        Returns:
            True 表示会话存在并已删除，False 表示会话不存在。
        """
        with self._lock:
            cur = self.conn.cursor()
            # 先检查会话是否存在
            cur.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            )
            if cur.fetchone() is None:
                return False
            # 删除消息（外键关联）再删除会话
            self.conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            self.conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            self.conn.commit()
            return True

    def delete_old_sessions(self, ttl_days: Optional[int] = None) -> int:
        """删除超过 TTL 的旧会话及其消息。

        以会话的 ``updated_at`` 为准，删除所有最后更新时间早于
        ``now - ttl_days`` 的会话，并级联删除其关联消息。

        Args:
            ttl_days: 保留天数；为 None 或 <=0 时直接返回 0，不做任何清理。

        Returns:
            删除的会话数量。
        """
        if ttl_days is None or ttl_days <= 0:
            return 0

        cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat()
        with self._lock:
            cur = self.conn.cursor()
            # 先查符合条件的会话 ID
            cur.execute(
                "SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)
            )
            rows = cur.fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            # 删除关联消息
            self.conn.execute(
                f"DELETE FROM messages WHERE session_id IN ({placeholders})", ids
            )
            # 删除会话
            self.conn.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})", ids
            )
            self.conn.commit()
            return len(ids)

    def update_session_title(self, session_id: str, title: str) -> None:
        """更新会话标题。

        Args:
            session_id: 会话 ID。
            title: 标题文本，长度上限 100 字符（超出截断）。
        """
        if title is None:
            return
        title = title.strip()[:100]
        if not title:
            return
        with self._lock:
            self.conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
            self.conn.commit()

    def get_session_title(self, session_id: str) -> Optional[str]:
        """获取会话标题。

        Args:
            session_id: 会话 ID。

        Returns:
            标题字符串；会话不存在或未设置标题时返回 None。
        """
        with self._lock:
            cur = self.conn.execute(
                "SELECT title FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            return row["title"] if row is not None else None

    # ------------------------------------------------------------------
    # 文件分块 FTS5 全文索引（文件 ETL 管道使用）
    # ------------------------------------------------------------------

    def _ensure_file_chunks_fts(self) -> None:
        """确保 file_chunks_fts 虚拟表存在。"""
        with self._lock:
            try:
                self.conn.execute(
                    "SELECT count(*) FROM file_chunks_fts LIMIT 1"
                )
            except sqlite3.OperationalError:
                self.conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_fts USING fts5(
                        content, chunk_id, file_id, original_name,
                        tokenize='unicode61'
                    )
                    """
                )
                self.conn.commit()

    def insert_file_chunk(
        self, chunk_id: str, file_id: str, content: str, original_name: str
    ) -> None:
        """插入一条文件分块到 FTS5 索引。

        Args:
            chunk_id: 分块唯一 ID。
            file_id: 所属文件 ID。
            content: 分块文本内容。
            original_name: 原始文件名（用于检索结果显示）。
        """
        self._ensure_file_chunks_fts()
        with self._lock:
            self.conn.execute(
                "INSERT INTO file_chunks_fts "
                "(chunk_id, file_id, content, original_name) "
                "VALUES (?, ?, ?, ?)",
                (chunk_id, file_id, content, original_name),
            )
            self.conn.commit()

    def search_file_chunks(
        self,
        query: str,
        file_id: Optional[str] = None,
        top_k: int = 10,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """全文检索文件分块。

        Args:
            query: 搜索关键词（FTS5 phrase 查询）。
            file_id: 可选，按文件 ID 过滤。
            top_k: 返回条数上限。
            offset: 分页偏移量。

        Returns:
            匹配的分块列表，每项含 chunk_id / file_id / content / original_name。
        """
        self._ensure_file_chunks_fts()
        escaped = query.replace('"', '""')
        match_query = f'"{escaped}"'
        params: List[Any] = [match_query]
        sql = (
            "SELECT chunk_id, file_id, content, original_name "
            "FROM file_chunks_fts WHERE file_chunks_fts MATCH ?"
        )
        if file_id:
            sql += " AND file_id = ?"
            params.append(file_id)
        sql += " ORDER BY rank LIMIT ? OFFSET ?"
        params.extend([top_k, offset])

        with self._lock:
            cur = self.conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def delete_file_chunks(self, file_id: str) -> int:
        """删除指定文件的所有分块（仅供管理端点使用）。

        Args:
            file_id: 文件 ID。

        Returns:
            删除的分块数量。
        """
        self._ensure_file_chunks_fts()
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM file_chunks_fts WHERE file_id = ?", (file_id,)
            )
            self.conn.commit()
            deleted = cur.rowcount
            if deleted > 0:
                logger = logging.getLogger(__name__)
                logger.info(
                    "已删除 %d 条文件分块 FTS 索引: file_id=%s", deleted, file_id
                )
            return deleted

    def close(self):
        """关闭数据库连接。"""
        with self._lock:
            self.conn.close()
