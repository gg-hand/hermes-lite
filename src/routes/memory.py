"""memory 路由。从 server.py 提取。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from orchestrator import Orchestrator

logger = logging.getLogger(__name__)

router = APIRouter()

# 全局组件(由 app.py lifespan 初始化)
orchestrator = None

@router.get("/memories")
def search_memories(
    q: str = Query("", description="搜索关键词，空串返回空列表"),
    top_k: int = Query(20, ge=1, le=200, description="返回条数"),
    type: Optional[str] = Query(None, description="按 metadata.type 过滤"),
):
    """搜索长期记忆（向量检索）。

    调 ``chroma_store.query_memory`` 做向量检索，**浏览不触发强化**
    （reinforce=False，避免浏览也更新 last_accessed / access_count）。
    可选按 ``metadata.type`` 过滤返回结果。

    返回:
        ``{"memories": [{id, content, similarity, metadata}], "total": N}``
    """
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    # 空关键词直接返回空列表（避免无意义检索）
    if not q.strip():
        return {"memories": [], "total": 0}
    try:
        raw = _query_memory_browse(orchestrator.chroma_store, q, top_k)
    except Exception as e:
        logger.exception("检索记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"检索记忆失败: {e}")

    memories = []
    for item in raw:
        meta = item.get("metadata") or {}
        # 可选按 type 过滤（metadata.type 匹配）
        if type is not None and str(meta.get("type", "")) != type:
            continue
        memories.append({
            "id": item.get("id", ""),
            "content": item.get("content", ""),
            "similarity": item.get("similarity", 0.0),
            "metadata": meta,
        })
    return {"memories": memories, "total": len(memories)}

@router.get("/memories/all")
def list_all_memories(
    type: Optional[str] = Query(None, description="按 metadata.type 过滤"),
    limit: int = Query(100, ge=1, le=1000, description="最多返回条数"),
):
    """列出全部长期记忆（不做向量检索，按 metadata.type 过滤 + limit 截断）。

    调 ``chroma_store.get_all_memories()`` 取全量后过滤。适用于「按类型浏览」
    场景（如只看 fact / conversation_turn）。

    返回:
        ``{"memories": [{id, content, metadata}], "total": N}``
    """
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    try:
        raw = orchestrator.chroma_store.get_all_memories()
    except Exception as e:
        logger.exception("列出全部记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"列出记忆失败: {e}")

    memories = []
    for item in raw:
        meta = item.get("metadata") or {}
        if type is not None and str(meta.get("type", "")) != type:
            continue
        memories.append({
            "id": item.get("id", ""),
            "content": item.get("content", ""),
            "metadata": meta,
        })
    # limit 截断
    memories = memories[:limit]
    return {"memories": memories, "total": len(memories)}

@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    """删除单条长期记忆（用户手动删除，立即生效）。

    与 LLM 通过 ``delete_memory`` 工具入队 ``pending_memory_ops``（延迟到下次
    consolidate 时执行）不同，Dashboard 的手动删除应立即生效，故直接调
    ``chroma_store.delete_memory``。

    memory_id 不存在时返回 404。

    返回:
        ``{"status": "ok", "deleted_id": memory_id}``
    """
    if orchestrator is None or orchestrator.chroma_store is None:
        raise HTTPException(status_code=503, detail="ChromaMemoryStore 尚未初始化")
    # 先检查是否存在（get_all_memories 兼容 mock 与真实 chromadb）
    try:
        all_memories = orchestrator.chroma_store.get_all_memories()
        existing_ids = {m.get("id") for m in all_memories}
    except Exception as e:
        logger.exception("检查记忆存在性失败: %s", e)
        raise HTTPException(status_code=500, detail=f"检查记忆失败: {e}")
    if memory_id not in existing_ids:
        raise HTTPException(status_code=404, detail=f"记忆 {memory_id} 不存在")
    try:
        orchestrator.chroma_store.delete_memory(memory_id)
    except Exception as e:
        logger.exception("删除记忆失败: %s", e)
        raise HTTPException(status_code=500, detail=f"删除记忆失败: {e}")
    logger.info("已通过 Dashboard 删除记忆: %s", memory_id)
    return {"status": "ok", "deleted_id": memory_id}

@router.get("/profile")
def get_profile():
    """返回用户画像 memory.md 全文。

    通过 ``orchestrator.memory_md_manager`` 读取 memory.md，文件不存在时
    返回空 content。``updated_at`` 为文件最后修改时间（ISO 格式），
    文件不存在时为空字符串。

    返回:
        ``{"content": "...", "updated_at": "ISO timestamp"}``
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 尚未初始化")
    memory_md_manager = getattr(orchestrator, "memory_md_manager", None)
    if memory_md_manager is None:
        # memory_md_manager 未初始化，返回空内容
        return {"content": "", "updated_at": ""}
    try:
        content = memory_md_manager.read()
    except Exception as e:
        logger.exception("读取 memory.md 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取画像失败: {e}")

    # 取文件最后修改时间作为 updated_at
    updated_at = ""
    try:
        file_path = getattr(memory_md_manager, "file_path", None)
        if file_path is not None:
            from pathlib import Path
            p = Path(file_path)
            if p.exists():
                updated_at = datetime.fromtimestamp(
                    p.stat().st_mtime
                ).isoformat()
    except Exception as e:
        logger.debug("获取 memory.md mtime 失败: %s", e)
    return {"content": content, "updated_at": updated_at}


# ---------- 静态文件服务（Web 前端）----------
