"""会话级状态容器(E7,计划 §4.3):core 提供的 SessionStore(~20 行)。

枝干实例全局共享、必须无状态或只读共享;可变状态只能放
``ctx.extra``(对话态)/ ``SessionStore``(会话态,内存,重启即失)。
"""

from __future__ import annotations

from typing import Any, Dict


class SessionStore:
    """session_id → dict 懒加载容器;drop 由调用方负责,core 不自动清理。"""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}

    def get(self, session_id: str) -> Dict[str, Any]:
        """获取会话状态 dict(懒创建)。"""
        return self._data.setdefault(session_id, {})

    def drop(self, session_id: str) -> None:
        """删除会话状态(调用方负责清理时机)。"""
        self._data.pop(session_id, None)

    def clear(self) -> None:
        """清空全部(关闭时)。"""
        self._data.clear()
