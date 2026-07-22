"""会话级文件操作记录（FileOperationRegistry）。

按 ``session_id`` 维护两个集合：
- ``created``：LLM 本次会话新建的文件
- ``modified``：LLM 本次会话修改过的用户文件

用于权限决策（``PolicyEngine`` 据此决定 ``write_file`` / ``delete_file``
是放行还是需用户确认）。

设计要点：
- 纯内存对象，不持久化：随会话生灭，不写入任何文件。
- 会话隔离：按 ``session_id`` 维护独立集合，互不干扰；``session_id``
  不存在时查询一律返回 ``False``。
- 路径规范化：所有 ``path`` 参数接受 ``str`` 或 ``Path``，内部用
  ``Path(path).resolve()`` 统一为绝对路径后存储/比较，避免相对路径与
  绝对路径、``..`` / ``.`` 等差异导致误判。
- symlink 防御：查询时先 ``path.is_symlink()`` 检测，是 symlink 直接
  返回 ``False``（视为用户文件，不享受豁免）。``record_write`` 不做
  symlink 检查（记录但查询时返回 ``False``，简单实现）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Set, Union

PathLike = Union[str, Path]


class FileOperationRegistry:
    """会话级文件操作记录，用于权限决策。

    维护两个集合（按 ``session_id`` 隔离）：
    - ``created``：LLM 本次会话新建的文件
    - ``modified``：LLM 本次会话修改过的用户文件

    纯内存对象，不持久化。服务重启或会话销毁后集合数据丢失，不写入
    任何文件。
    """

    def __init__(self) -> None:
        """初始化注册表，内部 dict 存储 session_id → {created, modified} 映射。"""
        self._sessions: Dict[str, Dict[str, Set[Path]]] = {}

    def record_write(
        self, session_id: str, path: PathLike, is_new: bool
    ) -> None:
        """记录一次文件写入操作。

        ``path`` 经 ``Path(path).resolve()`` 规范化为绝对路径后加入对应
        集合：``is_new=True`` 加入 ``created``，``is_new=False`` 加入
        ``modified``。``session_id`` 不存在时自动创建对应集合。

        参数:
            session_id: 会话 ID。
            path: 文件路径（``str`` 或 ``Path``），内部 resolve 规范化。
            is_new: ``True`` 表示新建文件，加入 ``created`` 集合；
                ``False`` 表示修改已有文件，加入 ``modified`` 集合。
        """
        resolved = Path(path).resolve()
        buckets = self._sessions.setdefault(
            session_id, {"created": set(), "modified": set()}
        )
        key = "created" if is_new else "modified"
        buckets[key].add(resolved)

    def is_created(self, session_id: str, path: PathLike) -> bool:
        """查询 ``path`` 是否在指定会话的 ``created`` 集合中。

        symlink 防御：``path`` 为符号链接时直接返回 ``False``（视为用户
        文件，不享受豁免）。``session_id`` 不存在时返回 ``False``。

        参数:
            session_id: 会话 ID。
            path: 文件路径（``str`` 或 ``Path``），先 ``is_symlink()`` 检测，
                再 ``resolve()`` 规范化后比较。

        返回:
            在 ``created`` 集合中返回 ``True``，否则 ``False``。
        """
        return self._query(session_id, path, "created")

    def is_modified(self, session_id: str, path: PathLike) -> bool:
        """查询 ``path`` 是否在指定会话的 ``modified`` 集合中。

        symlink 防御：``path`` 为符号链接时直接返回 ``False``（视为用户
        文件，不享受豁免）。``session_id`` 不存在时返回 ``False``。

        参数:
            session_id: 会话 ID。
            path: 文件路径（``str`` 或 ``Path``），先 ``is_symlink()`` 检测，
                再 ``resolve()`` 规范化后比较。

        返回:
            在 ``modified`` 集合中返回 ``True``，否则 ``False``。
        """
        return self._query(session_id, path, "modified")

    def remove(self, session_id: str, path: PathLike) -> None:
        """从指定会话的集合中移除 ``path``。

        删除文件后调用，从 ``created`` 与 ``modified`` 两个集合中同时移除
        （文件可能存在于任一集合）。``session_id`` 不存在时为空操作。

        参数:
            session_id: 会话 ID。
            path: 文件路径（``str`` 或 ``Path``），内部 resolve 规范化后比较。
        """
        buckets = self._sessions.get(session_id)
        if buckets is None:
            return
        resolved = Path(path).resolve()
        buckets["created"].discard(resolved)
        buckets["modified"].discard(resolved)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _query(
        self, session_id: str, path: PathLike, key: str
    ) -> bool:
        """查询 ``path`` 是否在指定会话的指定集合中。

        symlink 防御：先 ``Path(path).is_symlink()`` 检测，是 symlink 返回
        ``False``（视为用户文件，不享受豁免）。``session_id`` 不存在返回
        ``False``。

        参数:
            session_id: 会话 ID。
            path: 文件路径。
            key: 集合名，``"created"`` 或 ``"modified"``。

        返回:
            命中返回 ``True``，否则 ``False``。
        """
        p = Path(path)
        # symlink 防御：视为用户文件，不享受豁免
        if p.is_symlink():
            return False
        buckets = self._sessions.get(session_id)
        if buckets is None:
            return False
        resolved = p.resolve()
        return resolved in buckets[key]
