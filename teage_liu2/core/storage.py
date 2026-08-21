"""存储平台:D1 开放落盘平台(计划 §1 定案)。

三件套:
- :class:`StorageProvider` —— 通用持久化通道(扩展接口):枝干可经
  ``write(kind, doc)`` 落盘任意信息,自主选择 kind 命名空间
- :class:`SQLiteStorageProvider` —— 唯一实现:单库多 kind,每 kind 一张
  doc 表(doc JSON + created_at),kind 白名单校验防表注入
- :class:`MessageStore` —— 消息级落盘契约(主干最小实现,由
  ``core/history.SQLiteHistoryStore`` 实现):messages 专表承载
  content_blocks(JSON,LLM 重建)/ token_count / reasoning / message_type

设计原则(定案):core 提供极高自由度的落盘接口,子系统自主选择——经核心
通道落盘,或自持存储。core 只保持最基本的稳定高效的最小实现。
与历史库共用同一 SQLite 文件(WAL 模式多连接安全,写串行)。
"""

from __future__ import annotations

import abc
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# kind 白名单:防表注入(仅小写字母/数字/点/下划线)
_KIND_PATTERN_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_.")


def _validate_kind(kind: str) -> None:
    """校验 kind 命名空间,非法名抛 ValueError(防表注入)。"""
    if not kind or not isinstance(kind, str):
        raise ValueError(f"非法 kind: {kind!r}(必须匹配 ^[a-z0-9_.]+$)")
    if any(c not in _KIND_PATTERN_CHARS for c in kind):
        raise ValueError(
            f"非法 kind: {kind!r}(仅允许小写字母/数字/点/下划线)"
        )


class StorageProvider(abc.ABC):
    """通用持久化通道:子系统按 kind 命名空间落盘任意信息。"""

    @abc.abstractmethod
    def write(
        self, kind: str, doc: Union[dict, List[dict]]
    ) -> Union[str, List[str]]:
        """写入一个文档(单条)或多个文档(批量,§18.4),返回 doc_id。

        - 单条:返回 doc_id(str)
        - 批量(docs[]):返回 doc_id 列表(list[str]),同事务原子提交
        kind 命名空间示例:"messages" / "audit" / "memory.facts" / "collab"。
        """

    @abc.abstractmethod
    def read(self, kind: str, doc_id: str) -> Optional[dict]:
        """按 doc_id 读取文档;不存在返回 None。"""

    @abc.abstractmethod
    def query(
        self, kind: str, limit: Optional[int] = None, **filters
    ) -> List[dict]:
        """查询 kind 下文档(按写入序);filters 为 doc 顶层字段精确匹配。

        limit(§18.4):防全量加载,正整数,缺失 = 全量。
        """

    @abc.abstractmethod
    def delete(self, kind: str, doc_id: str) -> None:
        """删除文档。"""

    @abc.abstractmethod
    def close(self) -> None:
        """关闭连接。"""


class SQLiteStorageProvider(StorageProvider):
    """SQLite 实现:单库多 kind,每 kind 一张 doc 表(doc JSON + created_at)。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        # check_same_thread=False 允许跨线程共享连接(WAL + 锁)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._lock = threading.Lock()

    @staticmethod
    def _table_for(kind: str) -> str:
        """kind → 表名(白名单校验 + 点转下划线,防注入)。"""
        _validate_kind(kind)
        return f"doc_{kind.replace('.', '_')}"

    def _ensure_table(self, kind: str) -> str:
        table = self._table_for(kind)
        with self._lock:
            self.conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    doc_id TEXT PRIMARY KEY,
                    doc TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self.conn.commit()
        return table

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    def write(
        self, kind: str, doc: Union[dict, List[dict]]
    ) -> Union[str, List[str]]:
        """写入单条(返回 str)或批量(返回 List[str],同事务原子提交)。"""
        if isinstance(doc, list):
            if not doc:
                raise ValueError("批量写入 docs 不能为空列表")
            table = self._ensure_table(kind)
            now = self._now_iso()
            doc_ids: List[str] = []
            with self._lock:
                for d in doc:
                    doc_id = str(uuid.uuid4().hex)
                    self.conn.execute(
                        f"INSERT INTO {table} (doc_id, doc, created_at) "
                        "VALUES (?, ?, ?)",
                        (doc_id, json.dumps(d, ensure_ascii=False), now),
                    )
                    doc_ids.append(doc_id)
                self.conn.commit()
            return doc_ids
        table = self._ensure_table(kind)
        doc_id = str(uuid.uuid4().hex)
        with self._lock:
            self.conn.execute(
                f"INSERT INTO {table} (doc_id, doc, created_at) VALUES (?, ?, ?)",
                (doc_id, json.dumps(doc, ensure_ascii=False), self._now_iso()),
            )
            self.conn.commit()
        return doc_id

    def read(self, kind: str, doc_id: str) -> Optional[dict]:
        table = self._table_for(kind)
        with self._lock:
            cur = self.conn.execute(
                f"SELECT doc FROM {table} WHERE doc_id = ?", (doc_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row["doc"])

    def query(
        self, kind: str, limit: Optional[int] = None, **filters
    ) -> List[dict]:
        table = self._table_for(kind)
        with self._lock:
            # rowid = 插入序:created_at 同毫秒时顺序稳定(防 flaky)
            if limit is not None:
                if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                    raise ValueError(f"limit 必须是正整数,实际 {limit!r}")
                cur = self.conn.execute(
                    f"SELECT doc FROM {table} ORDER BY rowid ASC LIMIT ?",
                    (int(limit),),
                )
            else:
                cur = self.conn.execute(
                    f"SELECT doc FROM {table} ORDER BY rowid ASC"
                )
            rows = cur.fetchall()
        docs = [json.loads(r["doc"]) for r in rows]
        if filters:
            docs = [
                d for d in docs
                if all(d.get(k) == v for k, v in filters.items())
            ]
        return docs

    def delete(self, kind: str, doc_id: str) -> None:
        table = self._table_for(kind)
        with self._lock:
            self.conn.execute(
                f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,)
            )
            self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception as e:
            logger.error("关闭存储平台连接失败: %s", e)


class MessageStore(abc.ABC):
    """消息级落盘契约:主干最小实现(实现:``core/history.SQLiteHistoryStore``)。

    在 HistoryStore 基础上扩展消息级字段 —— content_blocks(JSON,LLM 重建,
    工具配对结构)/ token_count / reasoning / message_type(D3 一并接线)。
    旧行(content_blocks 为 NULL)回退纯文本 content。
    """

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
        content_blocks: Optional[List[dict]] = None,
        message_type: Optional[str] = None,
    ) -> None:
        """记录一条消息(消息级):content 纯文本保 FTS,content_blocks 供 LLM 重建。"""

    @abc.abstractmethod
    def get_session_messages(
        self, session_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """获取会话全部消息(按时间正序),含 content_blocks 等消息级字段。"""
