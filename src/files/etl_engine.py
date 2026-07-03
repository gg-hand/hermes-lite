"""ETL 管道编排引擎。

协调文件解析 → 分块 → ChromaDB 向量写入 → FTS5 全文索引 → LLM 摘要生成
的完整流水线。同时提供混合检索（Vector + FTS5 + RRF 融合）与全链路删除能力。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .parser import ParseError

if TYPE_CHECKING:
    from ..storage.chroma_store import ChromaMemoryStore
    from ..storage.sqlite_log import SessionLogger
    from ..llm.client import LLMClient
    from .upload_manager import UploadManager
    from .parser import WaterfallParser
    from .chunker import DocumentChunker

logger = logging.getLogger(__name__)

# RRF 融合常数
RRF_K = 60


class ETLEngine:
    """ETL 管道编排器 + 混合检索引擎。

    编排文件从原始字节到可检索知识块的完整转换流程。
    对文本类文件执行 parse → chunk → embed → index；
    对图片类文件执行 OCR → 存储文字。
    """

    def __init__(
        self,
        upload_manager: "UploadManager",
        chroma_store: "ChromaMemoryStore",
        session_logger: "SessionLogger",
        parser: "WaterfallParser",
        chunker: "DocumentChunker",
        llm_client: Optional["LLMClient"] = None,
        config: Optional[dict] = None,
    ) -> None:
        """初始化 ETL 引擎。

        Args:
            upload_manager: 上传管理器，用于读写文件与元数据。
            chroma_store: ChromaDB 向量库，用于写入 chunk 向量。
            session_logger: 会话日志器，用于读写 FTS5 索引。
            parser: 瀑布式文件解析器。
            chunker: 文档分块器。
            llm_client: LLM 客户端（用于摘要生成）。为 None 时摘要功能跳过。
            config: ``config.files`` 配置子段。为 None 时使用默认值。
        """
        self.upload_manager = upload_manager
        self.chroma_store = chroma_store
        self.session_logger = session_logger
        self.parser = parser
        self.chunker = chunker
        self.llm_client = llm_client
        self.config = config or {}

        self.chroma_namespace = self.config.get("chroma_namespace", "file")
        self.max_summary_chars = 200
        self.etl_max_queue = self.config.get("etl_max_queue", 100)

        # 简易入队计数（用于超限退化为同步）
        self._queue_size = 0
        self._queue_lock = __import__('threading').Lock()

    # ------------------------------------------------------------------
    # ETL 处理
    # ------------------------------------------------------------------

    def process_file(self, file_id: str, session_id: str) -> dict:
        """对单个文件执行完整 ETL 流水线。

        流程：
        - 文本类：读原始内容 → parse → 写缓存 txt → chunk → ChromaDB + FTS5 → 摘要
        - 图片类：读原始内容 → OCR → 写 img_text → done

        任意步骤失败 → 标记 failed + 记录错误原因。

        Args:
            file_id: 文件 ID。
            session_id: 上传会话 ID。

        Returns:
            ETL 结果摘要::

                {"status": "done|failed", "chunk_count": int, "error": str|None}
        """
        meta = self.upload_manager.get_metadata(file_id)
        if meta is None:
            return {"status": "failed", "chunk_count": 0, "error": "文件元数据不存在"}

        file_type = meta.get("type", "")
        is_image = file_type in (".png", ".jpg", ".jpeg", ".gif")

        # 标记处理中
        self.upload_manager.update_etl_status(file_id, "processing")

        try:
            if is_image:
                return self._process_image(file_id, meta)
            else:
                return self._process_text(file_id, meta)
        except Exception as e:
            error_msg = f"ETL 异常: {e}"
            logger.exception("ETL 处理异常: file_id=%s", file_id)
            self.upload_manager.update_error(file_id, error_msg)
            return {"status": "failed", "chunk_count": 0, "error": error_msg}

    def _process_text(self, file_id: str, meta: dict) -> dict:
        """处理文本类文件。"""
        # 读取原始内容
        content = self.upload_manager.read_content(file_id)
        if content is None:
            error_msg = "无法读取文件内容（磁盘文件可能已丢失）"
            self.upload_manager.update_error(file_id, error_msg)
            return {"status": "failed", "chunk_count": 0, "error": error_msg}

        # 解析
        try:
            parsed_text = self.parser.parse(content, meta.get("original_name", ""))
        except Exception as e:
            error_msg = f"解析失败: {e}"
            self.upload_manager.update_error(file_id, error_msg)
            return {"status": "failed", "chunk_count": 0, "error": error_msg}

        # 写解析缓存
        self._write_parsed_cache(file_id, parsed_text)

        # 分块
        chunks = self.chunker.chunk(parsed_text)
        if not chunks:
            error_msg = "分块结果为空"
            self.upload_manager.update_error(file_id, error_msg)
            return {"status": "failed", "chunk_count": 0, "error": error_msg}

        # ChromaDB + FTS5 双写
        chunk_count = 0
        chroma_errors = 0
        fts_errors = 0
        original_name = meta.get("original_name", "")

        for i, chunk_text in enumerate(chunks):
            chunk_id = f"{file_id}_chunk_{i}"

            # 块级去重
            try:
                dups = self.chroma_store.find_duplicates(
                    chunk_text,
                    threshold=0.85,
                    namespace=self.chroma_namespace,
                )
            except Exception as e:
                logger.warning("块级去重查询失败: %s (chunk=%s)", e, chunk_id)
                dups = []

            if dups:
                logger.debug("跳过重复块: %s (相似度=%.2f)", chunk_id, dups[0].get("similarity", 0))
                continue

            # ChromaDB 写入
            try:
                self.chroma_store.add_memory(
                    content=chunk_text,
                    metadata={
                        "file_id": file_id,
                        "chunk_index": i,
                        "original_name": original_name,
                        "type": "file_chunk",
                        "importance": 0.5,
                        "session_id": meta.get("session_id", ""),
                    },
                    memory_id=chunk_id,
                    namespace=self.chroma_namespace,
                )
            except Exception as e:
                logger.warning("ChromaDB 写入失败: %s (chunk=%s)", e, chunk_id)
                chroma_errors += 1
                continue

            # FTS5 写入（ChromaDB 成功后）
            try:
                self.session_logger.insert_file_chunk(
                    chunk_id=chunk_id,
                    file_id=file_id,
                    content=chunk_text,
                    original_name=original_name,
                )
            except Exception as e:
                logger.warning("FTS5 写入失败: %s (chunk=%s)", e, chunk_id)
                fts_errors += 1
                # 不回滚 ChromaDB（无分布式事务能力），继续处理

            chunk_count += 1

        if chroma_errors > 0 and chunk_count == 0:
            error_msg = f"ChromaDB 写入全部失败（{chroma_errors} 个错误）"
            self.upload_manager.update_error(file_id, error_msg)
            return {"status": "failed", "chunk_count": 0, "error": error_msg}

        # LLM 摘要（best-effort）
        summary = self._generate_summary(parsed_text)

        # 更新状态
        self.upload_manager.update_etl_status(
            file_id, "done", summary=summary, chunk_count=chunk_count
        )

        if fts_errors > 0:
            logger.warning(
                "ETL 完成但有 FTS5 错误: file_id=%s, chunks=%d, fts_errors=%d",
                file_id, chunk_count, fts_errors,
            )

        return {"status": "done", "chunk_count": chunk_count, "error": None}

    def _process_image(self, file_id: str, meta: dict) -> dict:
        """处理图片类文件（仅 OCR）。

        图片无文字（OCR 结果为空）被视为正常完成而非错误，
        img_text 保持空字符串，etl_status 标记为 done。
        """
        content = self.upload_manager.read_content(file_id)
        if content is None:
            error_msg = "无法读取图片文件"
            self.upload_manager.update_error(file_id, error_msg)
            return {"status": "failed", "chunk_count": 0, "error": error_msg}

        try:
            ocr_text = self.parser.parse(content, meta.get("original_name", ""))
        except ParseError as e:
            # 图片无文字是正常情况，标记为 done 但保留空 img_text
            logger.info("图片未识别到文字: file_id=%s, %s", file_id, e)
            ocr_text = ""
        except Exception as e:
            error_msg = f"OCR 失败: {e}"
            self.upload_manager.update_error(file_id, error_msg)
            return {"status": "failed", "chunk_count": 0, "error": error_msg}

        self.upload_manager.update_img_text(file_id, ocr_text)
        self.upload_manager.update_etl_status(file_id, "done", summary="", chunk_count=0)

        return {"status": "done", "chunk_count": 0, "error": None}

    def _write_parsed_cache(self, file_id: str, text: str) -> None:
        """将解析后的文本写入磁盘缓存。

        Args:
            file_id: 文件 ID。
            text: 解析后的纯文本。
        """
        cache_path = self._get_cache_path(file_id)
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            logger.warning("写解析缓存失败: %s -> %s", cache_path, e)

    def _get_cache_path(self, file_id: str) -> str:
        """获取解析缓存文件路径。

        使用 ``{file_id}.parsed`` 后缀，避免与原始文件的 ``{file_id}.ext``
        碰撞（当 ext == '.txt' 时两者相同）。

        Args:
            file_id: 文件 ID。

        Returns:
            ``data/uploads/{file_id}.parsed``
        """
        upload_dir = self.config.get("upload_dir", "data/uploads")
        return os.path.join(upload_dir, f"{file_id}.parsed")

    # ------------------------------------------------------------------
    # LLM 摘要
    # ------------------------------------------------------------------

    def _generate_summary(self, text: str) -> str:
        """生成文件摘要（best-effort）。

        Args:
            text: 解析后的全文。

        Returns:
            摘要文本（≤200 字）。LLM 不可用时返回空前缀摘要。
        """
        if self.llm_client is None:
            return self._simple_summary(text)

        # 截取前 8000 字符作为摘要输入
        sample = text[:8000]
        prompt = (
            f"请为以下文档内容生成一段不超过 {self.max_summary_chars} 字的摘要：\n\n"
            f"```\n{sample}\n```\n\n摘要："
        )

        try:
            response = self.llm_client.chat([{"role": "user", "content": prompt}])
            if hasattr(response, "content"):
                result = response.content
                if isinstance(result, list):
                    result = "".join(
                        b.get("text", "") for b in result if isinstance(b, dict)
                    )
            elif isinstance(response, dict):
                result = response.get("content", "")
            else:
                result = str(response)

            summary = result.strip() if result else ""
            if len(summary) > self.max_summary_chars:
                summary = summary[:self.max_summary_chars]
            return summary
        except Exception as e:
            logger.warning("LLM 摘要生成失败，使用简单摘要: %s", e)
            return self._simple_summary(text)

    @staticmethod
    def _simple_summary(text: str) -> str:
        """简单摘要：取文本前 200 字符。"""
        cleaned = text.strip().replace("\n", " ")
        if len(cleaned) <= 200:
            return cleaned
        return cleaned[:197] + "..."

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------

    def enqueue(self, file_id: str) -> bool:
        """将文件加入 ETL 处理队列。

        超限时退化为同步处理。

        Args:
            file_id: 文件 ID。

        Returns:
            True 表示已入队（异步处理），False 表示已同步处理（队列超限）。
        """
        with self._queue_lock:
            if self._queue_size >= self.etl_max_queue:
                logger.warning("ETL 队列已满 (%d)，退化为同步处理: %s", self.etl_max_queue, file_id)
                return False
            self._queue_size += 1
        # 异步处理由调用方（server.py）通过 asyncio 实现
        return True

    def dequeue(self) -> None:
        """标记队列中一个任务完成。"""
        with self._queue_lock:
            self._queue_size = max(0, self._queue_size - 1)

    # ------------------------------------------------------------------
    # 混合检索
    # ------------------------------------------------------------------

    def query_hybrid(
        self,
        query: str,
        file_id: Optional[str] = None,
        top_k: int = 5,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """混合检索：ChromaDB 向量 + FTS5 全文 + RRF 融合。

        两个引擎各取 ``top_k + offset`` 条候选，RRF 融合排名后切片。

        Args:
            query: 查询文本。
            file_id: 可选，按文件 ID 过滤。
            top_k: 返回条数。
            offset: 分页偏移量。

        Returns:
            融合排序后的结果列表，每项含 content / score / chunk_id / file_id。
        """
        fetch_count = top_k + offset

        # Vector 检索
        try:
            vector_results = self.chroma_store.query_memory(
                query_text=query,
                top_k=fetch_count,
                reinforce=False,
                namespace=self.chroma_namespace,
            )
        except Exception as e:
            logger.warning("向量检索失败: %s", e)
            vector_results = []

        # FTS5 检索
        try:
            fts_results = self.session_logger.search_file_chunks(
                query=query,
                file_id=file_id,
                top_k=fetch_count,
                offset=0,
            )
        except Exception as e:
            logger.warning("FTS5 检索失败: %s", e)
            fts_results = []

        # RRF 融合
        fused = self._rrf_fuse(vector_results, fts_results, file_id)

        # 分页切片
        return fused[offset:offset + top_k]

    def _rrf_fuse(
        self,
        vector_results: List[dict],
        fts_results: List[dict],
        filter_file_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """RRF 融合排序。

        score = 1/(K + rank_vector) + 1/(K + rank_fts)

        Args:
            vector_results: ChromaDB 检索结果。
            fts_results: FTS5 检索结果。
            filter_file_id: 可选，按 file_id 过滤。

        Returns:
            按 RRF score 降序排列的结果列表。
        """
        scores: Dict[str, float] = {}
        contents: Dict[str, str] = {}
        meta: Dict[str, dict] = {}

        # 向量排名（排名从 1 开始）
        for rank, item in enumerate(vector_results, start=1):
            chunk_id = item.get("id", "")
            f_id = item.get("metadata", {}).get("file_id", "")
            if filter_file_id and f_id != filter_file_id:
                continue
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            contents[chunk_id] = item.get("content", "")
            meta[chunk_id] = {
                "source": "vector",
                "chunk_id": chunk_id,
                "file_id": f_id,
                "similarity": item.get("similarity", 0.0),
                **item.get("metadata", {}),
            }

        # FTS5 排名
        for rank, item in enumerate(fts_results, start=1):
            chunk_id = item.get("chunk_id", "")
            f_id = item.get("file_id", "")
            if filter_file_id and f_id != filter_file_id:
                continue
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            if chunk_id not in contents:
                contents[chunk_id] = item.get("content", "")
            if chunk_id not in meta:
                meta[chunk_id] = {
                    "source": "fts",
                    "chunk_id": chunk_id,
                    "file_id": f_id,
                    "similarity": 0.0,
                }

        # 排序
        sorted_ids = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
        return [
            {
                "content": contents[cid],
                "score": scores[cid],
                **meta[cid],
            }
            for cid in sorted_ids
        ]

    # ------------------------------------------------------------------
    # 解析文本读取
    # ------------------------------------------------------------------

    def get_parsed_text(self, file_id: str) -> Optional[str]:
        """获取文件的解析后全文。

        - 文本类：读 ``data/uploads/{file_id}.txt`` 缓存
        - 图片类：读 SQLite img_text
        - 缓存缺失：重新解析并写回缓存（容错）

        Args:
            file_id: 文件 ID。

        Returns:
            解析后的纯文本，完全失败时返回 None。
        """
        meta = self.upload_manager.get_metadata(file_id)
        if meta is None:
            return None

        file_type = meta.get("type", "")
        is_image = file_type in (".png", ".jpg", ".jpeg", ".gif")

        # 图片类：直接返回 img_text
        if is_image:
            img_text = meta.get("img_text", "")
            return img_text if img_text else None

        # 文本类：读缓存
        cache_path = self._get_cache_path(file_id)
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, IOError):
            logger.info("解析缓存缺失，尝试重解析: %s", file_id)

        # 缓存缺失 → 重解析
        content = self.upload_manager.read_content(file_id)
        if content is None:
            return None

        try:
            parsed_text = self.parser.parse(content, meta.get("original_name", ""))
            self._write_parsed_cache(file_id, parsed_text)
            return parsed_text
        except Exception as e:
            logger.error("重解析失败: %s -> %s", file_id, e)
            return None

    # ------------------------------------------------------------------
    # 全链路删除
    # ------------------------------------------------------------------

    def delete_file_knowledge(self, file_id: str) -> dict:
        """全链路删除文件知识（仅供管理端点使用）。

        删除：磁盘文件 + 磁盘缓存 + ChromaDB 块 + FTS5 索引 + SQLite 元数据。

        Args:
            file_id: 文件 ID。

        Returns:
            删除结果摘要::

                {"deleted": bool, "details": {...}}
        """
        result = {
            "deleted": False,
            "details": {
                "disk": False,
                "cache": False,
                "chromadb": False,
                "fts5": False,
                "sqlite": False,
            },
        }

        meta = self.upload_manager.get_metadata(file_id)
        if meta is None:
            return result

        # 1. 磁盘原始文件
        saved_path = meta.get("saved_path")
        if saved_path:
            try:
                os.remove(saved_path)
                result["details"]["disk"] = True
            except OSError as e:
                logger.warning("删除磁盘文件失败: %s -> %s", saved_path, e)

        # 2. 磁盘缓存文件
        cache_path = self._get_cache_path(file_id)
        try:
            os.remove(cache_path)
            result["details"]["cache"] = True
        except OSError:
            pass  # 缓存可能不存在

        # 3. ChromaDB 块
        try:
            # 获取文件的所有 chunk 并逐条删除
            all_memories = self.chroma_store.get_all_memories(
                namespace=self.chroma_namespace
            )
            for mem in all_memories:
                m_id = mem.get("id", "")
                m_meta = mem.get("metadata", {})
                if isinstance(m_meta, dict) and m_meta.get("file_id") == file_id:
                    try:
                        self.chroma_store.delete_memory(m_id)
                    except Exception as e:
                        logger.warning("ChromaDB 删除块失败: %s -> %s", m_id, e)
            result["details"]["chromadb"] = True
        except Exception as e:
            logger.warning("ChromaDB 清理失败: %s", e)

        # 4. FTS5 索引
        try:
            self.session_logger.delete_file_chunks(file_id)
            result["details"]["fts5"] = True
        except Exception as e:
            logger.warning("FTS5 清理失败: %s", e)

        # 5. SQLite 元数据
        try:
            self.upload_manager.delete_record(file_id)
            result["details"]["sqlite"] = True
        except Exception as e:
            logger.warning("SQLite 删除记录失败: %s", e)

        result["deleted"] = any(result["details"].values())
        logger.info("全链路删除完成: file_id=%s, details=%s", file_id, result["details"])
        return result
