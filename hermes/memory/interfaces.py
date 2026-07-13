"""存储后端抽象接口层（Protocol）。

定义长期记忆存储与会话日志存储的核心方法签名，实现依赖倒置。
Orchestrator 依赖 Protocol 而非具体类，未来替换存储后端（如 Qdrant /
pgvector / Redis）只需实现 Protocol 即可，无需修改业务代码。

当前实现：
- MemoryStoreProtocol → ChromaMemoryStore
- SessionStoreProtocol → SessionLogger

扩展指南：
1. 新后端只需实现 Protocol 中的所有方法签名
2. 加 @runtime_checkable 装饰的 Protocol 可用 isinstance 校验
3. Protocol 是结构化类型，不强制继承
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class MemoryStoreProtocol(Protocol):
    """长期记忆存储的抽象接口。

    任何实现此 Protocol 的类都可作为长期记忆后端。
    当前实现：ChromaMemoryStore（基于 ChromaDB + HNSW 索引）。
    未来可替换：QdrantMemoryStore / PGVectorStore / RedisMemoryStore。
    """

    def add_memory(
        self,
        content: str,
        metadata: Optional[dict] = None,
        memory_id: Optional[str] = None,
    ) -> str: ...

    def query_memory(self, query_text: str, top_k: int = 5) -> list: ...

    def find_duplicates(
        self, new_content: str, threshold: float = 0.85,
    ) -> list: ...

    def update_memory(
        self, memory_id: str, content: str, metadata: Optional[dict] = None,
    ) -> None: ...

    def delete_memory(self, memory_id: str) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class SessionStoreProtocol(Protocol):
    """会话日志存储的抽象接口。

    任何实现此 Protocol 的类都可作为会话日志后端。
    当前实现：SessionLogger（基于 SQLite）。
    未来可替换：PostgresSessionLogger / RedisSessionLogger。
    """

    def create_session(self, session_id: Optional[str] = None) -> str: ...

    def log_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        token_count: int = 0,
        is_error: bool = False,
    ) -> None: ...

    def get_session_messages(
        self, session_id: str, limit: Optional[int] = None,
    ) -> list: ...

    def list_sessions(self) -> list: ...

    def delete_session(self, session_id: str) -> bool: ...

    def close(self) -> None: ...
