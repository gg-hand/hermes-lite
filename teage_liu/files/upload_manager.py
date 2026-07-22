"""文件上传管理器，基于 SQLite 持久化上传记录。

提供文件校验、全局 SHA256 去重、元数据 CRUD、TTL 追踪等功能。
复用 ``sessions.db`` 数据库连接，在 ``uploaded_files`` 表中存储所有上传记录。
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 图片类扩展名（需要 OCR 支持）
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif"})

# 默认允许的扩展名
_DEFAULT_ALLOWED_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".gif",
})


class UploadManager:
    """上传文件管理器，提供校验、存储与元数据管理。

    通过 ``threading.Lock`` 保护 SQLite 写操作，保证多线程并发安全。
    SHA256 去重使用 ``BEGIN IMMEDIATE`` 事务序列化并发请求。
    """

    def __init__(
        self,
        db_path: str,
        upload_dir: str = "data/uploads",
        max_upload_size_mb: float = 50,
        max_files_per_session: int = 50,
        allowed_extensions: Optional[List[str]] = None,
        ocr_enabled: bool = False,
        get_file_count: Optional[Callable[[str], int]] = None,
    ) -> None:
        """初始化上传管理器。

        Args:
            db_path: SQLite 数据库路径（复用 sessions.db）。
            upload_dir: 原始文件存储目录。
            max_upload_size_mb: 单文件最大上传大小（MB）。
            max_files_per_session: 每个会话的最大文件数。
            allowed_extensions: 允许的文件扩展名列表。为 None 时使用默认列表。
            ocr_enabled: 是否启用 OCR（影响图片上传校验）。
            get_file_count: 可选，一个返回指定会话文件总数的 callable。
                为 None 时使用内置的 SQL 查询。
        """
        self.db_path = db_path
        self.upload_dir = upload_dir
        self.max_upload_size_mb = max_upload_size_mb
        self.max_files_per_session = max_files_per_session
        self.allowed_extensions = (
            frozenset(allowed_extensions)
            if allowed_extensions
            else _DEFAULT_ALLOWED_EXTENSIONS
        )
        self.ocr_enabled = ocr_enabled
        self._get_file_count = get_file_count

        # 确保上传目录存在
        os.makedirs(self.upload_dir, exist_ok=True)

        # 连接 SQLite（复用 sessions.db）
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")

        # 线程安全锁
        self._lock = threading.Lock()

        self._init_table()

    def _init_table(self) -> None:
        """创建 uploaded_files 与 session_file_association 表及索引（若不存在）。"""
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS uploaded_files (
                    file_id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    saved_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    etl_status TEXT DEFAULT 'pending',
                    error_reason TEXT,
                    summary TEXT DEFAULT '',
                    img_text TEXT DEFAULT '',
                    chunk_count INTEGER DEFAULT 0,
                    uploaded_at TEXT NOT NULL,
                    last_accessed TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_files_session
                    ON uploaded_files(session_id);
                CREATE INDEX IF NOT EXISTS idx_files_hash
                    ON uploaded_files(content_hash);
                CREATE INDEX IF NOT EXISTS idx_files_accessed
                    ON uploaded_files(last_accessed);

                CREATE TABLE IF NOT EXISTS session_file_association (
                    session_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    associated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, file_id)
                );
                CREATE INDEX IF NOT EXISTS idx_assoc_session
                    ON session_file_association(session_id);
                """
            )
            self.conn.commit()
        self._backfill_associations()

    def _backfill_associations(self) -> None:
        """为旧数据回填 session_file_association 关联记录。

        仅在关联表为空时执行（首次升级），幂等安全。
        """
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM session_file_association"
            )
            if cur.fetchone()[0] > 0:
                return
            self.conn.execute(
                "INSERT OR IGNORE INTO session_file_association "
                "(session_id, file_id, associated_at) "
                "SELECT session_id, file_id, uploaded_at FROM uploaded_files"
            )
            self.conn.commit()

    @staticmethod
    def _now_iso() -> str:
        """返回当前时间的 ISO 格式字符串。"""
        return datetime.now().isoformat()

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def validate(self, filename: str, content: bytes) -> Optional[str]:
        """校验上传文件是否合法。

        Args:
            filename: 原始文件名。
            content: 文件内容字节。

        Returns:
            None 表示校验通过，否则返回错误消息字符串。
        """
        # 空文件
        if not content:
            return "文件内容为空"

        # 扩展名
        ext = os.path.splitext(filename)[1].lower()
        if not ext:
            return "无法识别文件类型（缺少扩展名）"
        if ext not in self.allowed_extensions:
            return f"不支持的文件类型: {ext}"

        # 图片类需要 OCR 支持
        if ext in _IMAGE_EXTENSIONS and not self.ocr_enabled:
            return "当前未开启图片文字识别（OCR）"

        # 大小限制
        max_bytes = int(self.max_upload_size_mb * 1024 * 1024)
        if len(content) > max_bytes:
            return f"文件大小超过限制 {self.max_upload_size_mb}MB"

        return None

    def _check_session_limit(self, session_id: str) -> Optional[str]:
        """检查会话文件数是否超限。

        Args:
            session_id: 会话 ID。

        Returns:
            None 表示未超限，否则返回错误消息。
        """
        if self._get_file_count is not None:
            count = self._get_file_count(session_id)
        else:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM session_file_association WHERE session_id = ?",
                (session_id,),
            )
            count = cur.fetchone()[0]
        if count >= self.max_files_per_session:
            return f"当前会话文件数已达上限 {self.max_files_per_session}"
        return None

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------

    def save(
        self, filename: str, content: bytes, session_id: str
    ) -> Tuple[Optional[str], Optional[bool]]:
        """保存上传文件到磁盘与 SQLite，含全局 SHA256 去重。

        使用 ``BEGIN IMMEDIATE`` 事务序列化并发请求：
        第一个请求 INSERT 成功，第二个请求 SELECT 命中已存在的 hash 后返回。

        Args:
            filename: 原始文件名。
            content: 文件内容字节。
            session_id: 上传会话 ID。

        Returns:
            ``(file_id, is_dup)`` 元组。
            - 正常新增：``(file_id, None)``
            - 重复文件：``(existing_file_id, True)``
            - 失败：``(None, None)``
        """
        # 校验
        error = self.validate(filename, content)
        if error is not None:
            logger.warning("上传校验失败: %s (session=%s)", error, session_id)
            return None, None

        # 会话文件数检查
        limit_error = self._check_session_limit(session_id)
        if limit_error is not None:
            logger.warning("上传限制: %s (session=%s)", limit_error, session_id)
            return None, None

        content_hash = hashlib.sha256(content).hexdigest()
        ext = os.path.splitext(filename)[1].lower()
        now = self._now_iso()

        with self._lock:
            # BEGIN IMMEDIATE 序列化并发，保证 hash 检测 + INSERT 原子性
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # 全局去重（含 disk_expired 状态）
                cur = self.conn.execute(
                    "SELECT file_id, etl_status FROM uploaded_files "
                    "WHERE content_hash = ?",
                    (content_hash,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    # 去重命中：为当前会话建立关联（即使文件物理记录属于其他会话）
                    self.conn.execute(
                        "INSERT OR IGNORE INTO session_file_association "
                        "(session_id, file_id, associated_at) VALUES (?, ?, ?)",
                        (session_id, existing["file_id"], now),
                    )
                    self.conn.commit()
                    logger.info(
                        "文件去重命中: %s (hash=%s...) (existing_file_id=%s, status=%s)",
                        filename, content_hash[:12], existing["file_id"],
                        existing["etl_status"],
                    )
                    return existing["file_id"], True

                # 新增记录
                file_id = str(uuid.uuid4())
                saved_path = os.path.join(
                    self.upload_dir, f"{file_id}{ext}"
                )

                # 写磁盘
                try:
                    with open(saved_path, "wb") as f:
                        f.write(content)
                except OSError as e:
                    self.conn.rollback()
                    logger.error("写入磁盘失败: %s", e)
                    return None, None

                # 写 SQLite
                self.conn.execute(
                    """
                    INSERT INTO uploaded_files
                        (file_id, original_name, saved_path, content_hash,
                         size, type, session_id, etl_status,
                         uploaded_at, last_accessed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        file_id, filename, saved_path, content_hash,
                        len(content), ext, session_id, now, now,
                    ),
                )
                # 建立会话-文件关联
                self.conn.execute(
                    "INSERT OR IGNORE INTO session_file_association "
                    "(session_id, file_id, associated_at) VALUES (?, ?, ?)",
                    (session_id, file_id, now),
                )
                self.conn.commit()
                logger.info(
                    "文件已保存: %s -> %s (file_id=%s, session=%s, size=%d)",
                    filename, saved_path, file_id, session_id, len(content),
                )
                return file_id, None
            except Exception:
                self.conn.rollback()
                logger.exception("保存文件失败: %s", filename)
                return None, None

    # ------------------------------------------------------------------
    # 元数据查询
    # ------------------------------------------------------------------

    def get_metadata(self, file_id: str) -> Optional[Dict[str, Any]]:
        """获取单个文件的元数据。

        Args:
            file_id: 文件 ID。

        Returns:
            文件元数据 dict，不存在时返回 None。
        """
        cur = self.conn.execute(
            "SELECT * FROM uploaded_files WHERE file_id = ?", (file_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_session_files(self, session_id: str) -> List[Dict[str, Any]]:
        """获取指定会话关联的所有上传文件（按上传时间倒序）。

        通过 session_file_association 关联表查询，包含本会话上传的文件
        以及跨会话去重命中后关联到本会话的文件。同名文件标记版本号
        (version_seq) 和是否最新 (is_latest)。

        Args:
            session_id: 会话 ID。

        Returns:
            文件元数据列表，每项额外含 version_seq / is_latest 字段。
        """
        cur = self.conn.execute(
            "SELECT uf.* FROM uploaded_files uf "
            "JOIN session_file_association sfa ON uf.file_id = sfa.file_id "
            "WHERE sfa.session_id = ? "
            "ORDER BY uf.uploaded_at ASC",
            (session_id,),
        )
        files = [dict(row) for row in cur.fetchall()]

        # 版本标记：按 original_name 分组，组内按 uploaded_at ASC（SQL 已保证）
        # 最新版本 is_latest=True，version_seq 从 1 递增
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for f in files:
            groups.setdefault(f.get("original_name", ""), []).append(f)
        for group in groups.values():
            total = len(group)
            for i, f in enumerate(group):
                f["version_seq"] = i + 1
                f["is_latest"] = (i == total - 1)

        # 重新按 uploaded_at DESC 排序返回（保持原有返回顺序约定）
        files.sort(key=lambda f: f.get("uploaded_at", ""), reverse=True)
        return files

    def list_all(self) -> List[Dict[str, Any]]:
        """列出所有上传文件（按上传时间倒序）。

        Returns:
            文件元数据列表。
        """
        cur = self.conn.execute(
            "SELECT * FROM uploaded_files ORDER BY uploaded_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------

    def update_etl_status(
        self,
        file_id: str,
        status: str,
        summary: str = "",
        chunk_count: int = 0,
    ) -> None:
        """更新文件的 ETL 状态。

        Args:
            file_id: 文件 ID。
            status: 新状态（'pending' / 'processing' / 'done' / 'failed'）。
            summary: LLM 生成的摘要文本。
            chunk_count: 分块数量。
        """
        with self._lock:
            self.conn.execute(
                """
                UPDATE uploaded_files
                SET etl_status = ?, summary = ?, chunk_count = ?
                WHERE file_id = ?
                """,
                (status, summary, chunk_count, file_id),
            )
            self.conn.commit()

    def update_img_text(self, file_id: str, text: str) -> None:
        """更新图片文件的 OCR 文字。

        Args:
            file_id: 文件 ID。
            text: OCR 提取的文字。
        """
        with self._lock:
            self.conn.execute(
                "UPDATE uploaded_files SET img_text = ? WHERE file_id = ?",
                (text, file_id),
            )
            self.conn.commit()

    def update_error(self, file_id: str, reason: str) -> None:
        """记录文件处理错误。

        Args:
            file_id: 文件 ID。
            reason: 错误原因描述。
        """
        with self._lock:
            self.conn.execute(
                "UPDATE uploaded_files SET error_reason = ?, etl_status = 'failed' "
                "WHERE file_id = ?",
                (reason, file_id),
            )
            self.conn.commit()

    def touch_accessed(self, file_id: str) -> None:
        """更新文件的最后访问时间（延长 TTL）。

        Args:
            file_id: 文件 ID。
        """
        now = self._now_iso()
        with self._lock:
            self.conn.execute(
                "UPDATE uploaded_files SET last_accessed = ? WHERE file_id = ?",
                (now, file_id),
            )
            self.conn.commit()

    # ------------------------------------------------------------------
    # TTL 清理
    # ------------------------------------------------------------------

    def get_expired(self, ttl_days: int) -> List[str]:
        """获取超过 TTL 且非 processing 状态的文件 ID 列表。

        Args:
            ttl_days: 保留天数。

        Returns:
            过期文件 ID 列表。
        """
        cur = self.conn.execute(
            "SELECT file_id FROM uploaded_files "
            "WHERE julianday('now') - julianday(last_accessed) > ? "
            "AND etl_status != 'processing' "
            "AND etl_status != 'disk_expired'",
            (ttl_days,),
        )
        return [row["file_id"] for row in cur.fetchall()]

    def mark_disk_expired(self, file_id: str) -> None:
        """标记文件磁盘已过期（删除 saved_path）。

        Args:
            file_id: 文件 ID。
        """
        with self._lock:
            self.conn.execute(
                "UPDATE uploaded_files "
                "SET etl_status = 'disk_expired', saved_path = '' "
                "WHERE file_id = ?",
                (file_id,),
            )
            self.conn.commit()

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete_record(self, file_id: str) -> bool:
        """从 SQLite 中删除文件记录及其所有会话关联（仅供管理端点使用）。

        Args:
            file_id: 文件 ID。

        Returns:
            True 表示成功删除，False 表示记录不存在。
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM uploaded_files WHERE file_id = ?", (file_id,)
            )
            # 同步清理 session_file_association 中的关联记录
            self.conn.execute(
                "DELETE FROM session_file_association WHERE file_id = ?",
                (file_id,),
            )
            self.conn.commit()
            deleted = cur.rowcount > 0
            if deleted:
                logger.info("已删除文件记录: %s", file_id)
            return deleted

    def associate_file(self, session_id: str, file_id: str) -> None:
        """关联文件到会话（幂等）。

        用于跨会话引用场景：LLM 可通过 file_attach 工具把已有文件
        关联到当前会话，使其出现在 file_list_uploads / 文件面板 /
        FileContextInjector 注入中。

        Args:
            session_id: 会话 ID。
            file_id: 文件 ID。
        """
        now = self._now_iso()
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO session_file_association "
                "(session_id, file_id, associated_at) VALUES (?, ?, ?)",
                (session_id, file_id, now),
            )
            self.conn.commit()

    def cleanup_session(self, session_id: str) -> int:
        """清理会话的所有文件关联（删除会话时调用）。

        注意：仅清理 session_file_association 中的关联记录，不删除
        uploaded_files 中的文件物理记录（文件可能被其他会话引用）。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的关联记录数。
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM session_file_association WHERE session_id = ?",
                (session_id,),
            )
            self.conn.commit()
            return cur.rowcount

    def read_content(self, file_id: str) -> Optional[bytes]:
        """读取文件的原始内容。

        Args:
            file_id: 文件 ID。

        Returns:
            文件 bytes，文件不存在或磁盘丢失返回 None。
        """
        meta = self.get_metadata(file_id)
        if meta is None:
            return None
        saved_path = meta.get("saved_path")
        if not saved_path or saved_path == "":
            return None
        try:
            with open(saved_path, "rb") as f:
                return f.read()
        except (OSError, IOError) as e:
            logger.warning("读取文件失败: %s -> %s", saved_path, e)
            return None

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self.conn.close()
