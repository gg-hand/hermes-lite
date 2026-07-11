"""基于 ChromaDB 的长期记忆向量库。

使用 ChromaDB 内置的 DefaultEmbeddingFunction（ONNX 版 all-MiniLM-L6-v2）
在本地生成 embedding，通过 PersistentClient 持久化存储，
支持记忆的增删改查与去重检测。

优势：无需安装 sentence-transformers / torch，ONNX runtime 已随 chromadb 安装，
模型权重（~80MB）首次使用时自动下载到本地缓存。
"""
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# embedding 函数单例管理
# ---------------------------------------------------------------------------
# 全局 embedding 函数实例与锁，避免在多线程下重复创建
_embedding_fn = None
_embedding_fn_lock = threading.Lock()


def _get_embedding_fn():
    """获取 chromadb DefaultEmbeddingFunction 单例。

    使用双重检查锁定（double-checked locking）确保线程安全，
    全局只创建一次。DefaultEmbeddingFunction 内部使用 ONNX 版
    all-MiniLM-L6-v2 模型，首次调用时自动下载权重到本地缓存。

    Returns:
        chromadb EmbeddingFunction 实例。

    Raises:
        RuntimeError: 模型加载失败时抛出，附带友好提示。
    """
    global _embedding_fn
    if _embedding_fn is None:
        with _embedding_fn_lock:
            if _embedding_fn is None:
                try:
                    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
                    _embedding_fn = DefaultEmbeddingFunction()
                except Exception as e:
                    raise RuntimeError(
                        "加载 chromadb DefaultEmbeddingFunction 失败："
                        f"{e}\n"
                        "请确认已安装 chromadb（pip install chromadb），"
                        "并具备网络连接以首次下载 all-MiniLM-L6-v2 ONNX 模型权重。"
                    ) from e
    return _embedding_fn


# ---------------------------------------------------------------------------
# ONNX 模型单例（绕过 DefaultEmbeddingFunction 的每次调用创建新实例问题）
# DefaultEmbeddingFunction.__call__ 每次调用都创建新的 ONNXMiniLM_L6_V2()
# 实例，导致 onnxruntime.InferenceSession 被反复重建。
# _get_onnx_embedder 在模块级缓存一个永久实例，使 session 只创建一次。
# ---------------------------------------------------------------------------
_onnx_embedder = None
_onnx_embedder_lock = threading.Lock()


def _get_onnx_embedder():
    """获取 ONNX 嵌入模型单例（线程安全）。

    与 _get_embedding_fn() 使用相同的双重检查锁定模式。
    如果 ONNX 模型加载失败（如 chromadb 未安装），回退到
    DefaultEmbeddingFunction。

    桌面端打包时，_onnx_model 目录随安装包分发，此处将
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH 指向本地路径，避免首次启动联网下载。

    Returns:
        可调用的嵌入函数，接受 list[str] 返回 list[list[float]]。
    """
    global _onnx_embedder
    if _onnx_embedder is None:
        with _onnx_embedder_lock:
            if _onnx_embedder is None:
                try:
                    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
                        ONNXMiniLM_L6_V2,
                    )
                    _hermes_root = os.environ.get("HERMES_ROOT")
                    if _hermes_root:
                        _local_model = os.path.join(_hermes_root, "_onnx_model")
                        if os.path.isdir(os.path.join(_local_model, "onnx")):
                            ONNXMiniLM_L6_V2.DOWNLOAD_PATH = type(
                                ONNXMiniLM_L6_V2.DOWNLOAD_PATH
                            )(_local_model)
                            logger.info(
                                "使用本地 ONNX 模型: %s", _local_model
                            )
                    _onnx_embedder = ONNXMiniLM_L6_V2()
                except Exception:
                    # 回退到 DefaultEmbeddingFunction（测试环境或 chromadb 版本不兼容时）
                    _onnx_embedder = _get_embedding_fn()
    return _onnx_embedder


def _normalize(vec) -> np.ndarray:
    """L2 归一化向量，便于直接用点积计算余弦相似度。"""
    v = np.array(vec, dtype=np.float32)
    norm = np.linalg.norm(v)
    if norm > 0:
        v = v / norm
    return v


def _normalize_batch(vecs) -> np.ndarray:
    """L2 归一化批量向量，每行归一化。"""
    m = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def _normalize_metadata(metadata: Optional[dict]) -> Optional[dict]:
    """规范化 metadata，确保所有值都是 ChromaDB 支持的类型。

    ChromaDB 的 metadata 值仅支持 str/int/float/bool，
    其他类型会被统一转成字符串。

    Args:
        metadata: 原始 metadata。

    Returns:
        规范化后的 metadata，输入为 None 时返回 None。
    """
    if not metadata:
        return None
    return {
        k: (v if isinstance(v, (str, int, float, bool)) else str(v))
        for k, v in metadata.items()
    }


class ChromaMemoryStore:
    """基于 ChromaDB 的长期记忆向量库。

    集合名固定为 long_term_memory，使用 cosine 距离度量。
    通过 chromadb 内置 ONNX embedding 函数在本地生成 embedding，
    无需调用外部 API，也不依赖 sentence-transformers / torch。
    """

    COLLECTION_NAME = "long_term_memory"

    def __init__(self, persist_path: str = "data/chroma"):
        """初始化 ChromaDB 持久化客户端与记忆集合。

        Args:
            persist_path: ChromaDB 数据持久化路径，对应 config.yaml 中的
                memory.chroma_path，例如 data/chroma。
        """
        # 确保持久化目录存在
        self.persist_path = persist_path
        os.makedirs(self.persist_path, exist_ok=True)

        # 创建 PersistentClient，数据落盘到 persist_path
        import chromadb
        self.client = chromadb.PersistentClient(path=self.persist_path)

        # 获取或创建集合，使用 cosine 距离度量
        self.collection = self.client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

        # embedding 函数为懒加载：首次 add_memory / query_memory 时才创建，
        # 避免初始化阶段下载模型权重。

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _embed(self, text: str) -> list:
        """生成单条文本的 embedding 向量（已 L2 归一化）。

        Args:
            text: 待编码文本。

        Returns:
            归一化后的 embedding 向量（list[float]）。
        """
        model = _get_onnx_embedder()
        raw = model([text])[0]
        return _normalize(raw).tolist()

    def _embed_batch(self, texts: list) -> np.ndarray:
        """批量生成 embedding 向量。

        Args:
            texts: 文本列表。

        Returns:
            shape 为 (n, dim) 的 numpy 数组，每行已 L2 归一化。
        """
        model = _get_onnx_embedder()
        raw = model(texts)
        return _normalize_batch(raw)

    # ------------------------------------------------------------------
    # 记忆 CRUD
    # ------------------------------------------------------------------
    def add_memory(
        self,
        content: str,
        metadata: Optional[dict] = None,
        memory_id: Optional[str] = None,
        namespace: str = "user",
        cron_id: Optional[str] = None,
    ) -> str:
        """添加记忆到向量库。

        Args:
            content: 记忆内容文本。
            metadata: 元数据，建议包含：
                - timestamp: ISO 格式时间戳
                - session_id: 来源会话 ID
                - importance: 0-1 权重
                - type: fact 类型
                - last_accessed: ISO 格式最后访问时间（缺省取 timestamp）
                - access_count: 访问次数（缺省 0）
            memory_id: 自定义记忆 ID，未提供则自动生成 UUID。
            namespace: Phase 8 隔离层。``"user"``（默认）写入用户会话命名空间，
                ``"cron"`` 写入 cron 调度命名空间（需配合 ``cron_id``）。
                写入时注入 ``metadata.namespace`` 字段，便于检索时按命名空间过滤。
            cron_id: 当 ``namespace="cron"`` 时必填，标识具体调度项 ID。
                写入时注入 ``metadata.cron_id`` 字段，使不同调度项之间记忆互不可见。

        Returns:
            memory_id: 添加的记忆 ID。
        """
        if memory_id is None:
            memory_id = str(uuid.uuid4())

        # 补全 metadata 默认字段
        meta = dict(metadata or {})
        meta.setdefault("timestamp", datetime.now().isoformat())
        meta.setdefault("session_id", "")
        meta.setdefault("importance", 0.5)
        meta.setdefault("type", "fact")
        # Phase 7 Task 1: 三因子强化所需字段
        # last_accessed 缺省取 timestamp（写入时间），access_count 缺省 0
        meta.setdefault("last_accessed", meta["timestamp"])
        meta.setdefault("access_count", 0)
        # Phase 8 Task 1: 隔离层 — 命名空间与 cron_id
        # 始终注入 namespace 字段（默认 "user"），便于检索时按命名空间过滤；
        # cron_id 仅在 namespace="cron" 时注入（user 命名空间保持 '' 兼容旧查询）。
        meta["namespace"] = namespace or "user"
        if namespace == "cron":
            # cron_id 显式注入；为 None 时退化为空字符串（仍属于 cron 命名空间）
            meta["cron_id"] = cron_id or ""
        else:
            # user 命名空间不写 cron_id，保持 metadata 干净
            meta.setdefault("cron_id", "")
        meta = _normalize_metadata(meta)

        # 生成 embedding
        embedding = self._embed(content)

        # 写入集合
        self.collection.add(
            ids=[memory_id],
            documents=[content],
            metadatas=[meta] if meta is not None else None,
            embeddings=[embedding],
        )
        return memory_id

    def query_memory(
        self,
        query_text: str,
        top_k: int = 5,
        reinforce: bool = True,
        namespace: Optional[str] = "user",
        cron_id: Optional[str] = None,
    ) -> list:
        """向量检索相似记忆。

        Args:
            query_text: 查询文本。
            top_k: 返回前 K 条结果。
            reinforce: 是否在命中后强化记忆（更新 last_accessed / access_count）。
                默认 True，适合 Agent 正常检索路径；Dashboard 浏览场景应
                设为 False，避免浏览也触发强化。reinforce 失败不影响检索
                结果返回（try/except 兜底）。
            namespace: Phase 8 隔离层。``"user"``（默认）只检索用户命名空间条目
                （含旧数据：缺失 namespace 字段视为 user）；``"cron"`` 只检索
                cron 命名空间条目且要求 ``cron_id`` 匹配；``None`` 表示不按命名空间
                过滤（管理员视图，跨命名空间检索）。
            cron_id: 当 ``namespace="cron"`` 时必填，用于匹配 ``metadata.cron_id``。
                其他命名空间忽略此参数。

        Returns:
            list of dict，每个元素形如：
            {id, content, metadata, distance, similarity}
            其中 similarity = 1 - distance。
        """
        if top_k <= 0:
            return []

        # 集合为空时直接返回，避免 ChromaDB 报错
        if self.collection.count() == 0:
            return []

        query_embedding = self._embed(query_text)

        # 实际可用条数受集合大小限制
        n_results = min(top_k, self.collection.count())

        # Phase 8 Task 1: 按 namespace + cron_id 构造 where 子句
        # - namespace="user"：匹配 namespace="user" 或缺失 namespace 字段的旧数据
        #   （chromadb 不支持字段存在性查询，故先无 where 查询再 Python 过滤）
        # - namespace="cron"：精确匹配 namespace="cron" AND cron_id=<id>
        # - namespace=None：不构造 where，跨命名空间检索
        where = self._build_namespace_where(namespace, cron_id)

        # user 命名空间需要兼容旧数据（缺失 namespace 字段视为 user），
        # 此处先用宽松 where 查询，再在 Python 侧做最终过滤；
        # cron / None 命名空间下 chromadb where 已精确过滤，无需 Python 二次过滤。
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
            where=where,
        )

        # query 返回的结构是 {documents: [[...]], metadatas: [[...]], distances: [[...]]}
        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        output = []
        for mid, doc, meta, dist in zip(ids, documents, metadatas, distances):
            meta_dict = meta or {}
            # user 命名空间下兼容旧数据：缺失 namespace 字段视为 user
            if namespace == "user":
                if meta_dict.get("namespace", "user") != "user":
                    continue
            elif namespace == "cron":
                # 严格匹配 namespace=cron AND cron_id（防御 chromadb where 未生效）
                if meta_dict.get("namespace") != "cron":
                    continue
                if cron_id is not None and meta_dict.get("cron_id") != cron_id:
                    continue
            similarity = 1.0 - dist
            output.append({
                "id": mid,
                "content": doc,
                "metadata": meta_dict,
                "distance": dist,
                "similarity": similarity,
            })

        # Phase 7 Task 1: 命中后强化记忆（更新 last_accessed / access_count）
        # reinforce=False 时跳过（Dashboard 搜索等浏览场景）
        # 失败不影响检索结果返回
        if reinforce:
            for item in output:
                memory_id = item.get("id")
                if not memory_id:
                    continue
                try:
                    self.reinforce(memory_id)
                except Exception as e:
                    logger.warning(
                        "reinforce 记忆 %s 失败，不影响检索结果: %s",
                        memory_id,
                        e,
                    )

        return output

    @staticmethod
    def _build_namespace_where(
        namespace: Optional[str], cron_id: Optional[str]
    ) -> Optional[dict]:
        """构造 chromadb where 子句，用于按 namespace + cron_id 过滤。

        策略：
        - ``namespace=None``：返回 ``None``（不构造 where，跨命名空间检索）
        - ``namespace="user"``：返回 ``None``（在 Python 侧兼容旧数据，
          缺失 namespace 字段视为 user；chromadb 不支持字段存在性查询）
        - ``namespace="cron"``：返回 ``{"$and": [{"namespace": "cron"}, {"cron_id": <id>}]}``
          （精确匹配，cron 命名空间下 cron_id 总是显式写入；多条件用 ``$and`` 包装）
        - 其他值：返回 ``{"namespace": namespace}``

        Args:
            namespace: 命名空间标识（``"user"`` / ``"cron"`` / ``None`` / 其他）。
            cron_id: cron 命名空间下的调度项 ID。

        Returns:
            chromadb where 子句 dict，``None`` 表示不构造 where。
        """
        if namespace is None:
            return None
        if namespace == "user":
            # user 命名空间需兼容旧数据（缺失 namespace 视为 user），
            # chromadb 不支持字段存在性查询，故在 Python 侧过滤。
            return None
        if namespace == "cron":
            if cron_id is not None:
                # ChromaDB 顶层 where 只接受单操作符，多条件需用 $and 包装
                return {"$and": [{"namespace": "cron"}, {"cron_id": cron_id}]}
            return {"namespace": "cron"}
        # 其他自定义命名空间：精确匹配
        return {"namespace": namespace}

    def reinforce(self, memory_id: str) -> None:
        """强化记忆：更新 last_accessed 与 access_count。

        命中检索时调用，使常用记忆的 decayed_importance 排名更靠前。
        读取当前 metadata，更新 last_accessed = now / access_count += 1，
        用 collection.update 写回。

        memory_id 不存在时记录 warning 并静默返回（不抛异常，保证检索
        主流程稳定）。

        优化说明（Phase 9）：原实现通过 get_all_memories() 加载全量集合后
        在 Python 侧线性搜索（O(N)），现改用 collection.get(ids=[memory_id])
        直接 ID 查询（O(1)）。若 collection.get 返回空或 mock 不支持 ids
        参数，回退到 get_all_memories 以兼容测试环境。

        Args:
            memory_id: 待强化的记忆 ID。
        """
        metadata = None
        content = ""

        # 优先使用 O(1) 的 ID 直查
        try:
            result = self.collection.get(
                ids=[memory_id], include=["metadatas", "documents"]
            )
            # 确认返回的目标 ID 匹配（兼容 mock：mock 的 get 忽略 ids 参数返回全部）
            if (
                result
                and result.get("ids")
                and memory_id in result["ids"]
            ):
                idx = result["ids"].index(memory_id)
                raw_meta = (
                    result.get("metadatas", [None])[idx]
                    if result.get("metadatas") and len(result["metadatas"]) > idx
                    else {}
                )
                metadata = dict(raw_meta) if isinstance(raw_meta, dict) else {}
                doc_list = result.get("documents", [])
                content = doc_list[idx] if doc_list and len(doc_list) > idx else ""
        except Exception:
            metadata = None  # 触发降级

        # 降级路径：ID 直查失败时回退到 get_all_memories（兼容测试用 mock）
        if metadata is None:
            try:
                all_memories = self.get_all_memories()
            except Exception as e:
                logger.warning(
                    "reinforce 读取记忆列表失败 (memory_id=%s): %s", memory_id, e
                )
                return

            target = None
            for m in all_memories:
                if m.get("id") == memory_id:
                    target = m
                    break

            if target is None:
                logger.warning(
                    "reinforce 失败：memory_id 不存在: %s", memory_id
                )
                return

            old_meta = target.get("metadata") or {}
            if not isinstance(old_meta, dict):
                old_meta = {}
            metadata = dict(old_meta)
            content = target.get("content", "")

        old_count = metadata.get("access_count", 0)
        try:
            old_count = int(old_count)
        except (TypeError, ValueError):
            old_count = 0
        metadata["last_accessed"] = datetime.now().isoformat()
        metadata["access_count"] = old_count + 1
        metadata = _normalize_metadata(metadata)

        try:
            self.collection.update(
                ids=[memory_id],
                documents=[content],
                metadatas=[metadata] if metadata is not None else None,
            )
        except Exception as e:
            logger.warning(
                "reinforce 写回失败 (memory_id=%s): %s", memory_id, e
            )

    def get_all_memories(
        self,
        namespace: Optional[str] = None,
        cron_id: Optional[str] = None,
    ) -> list:
        """返回所有记忆，用于去重检测与管理员视图。

        Args:
            namespace: Phase 8 隔离层。``"user"`` 只返回用户命名空间条目（含
                旧数据：缺失 namespace 字段视为 user）；``"cron"`` 只返回 cron
                命名空间条目且要求 ``cron_id`` 匹配；``None``（默认）返回全部，
                不按命名空间过滤（管理员视图）。
            cron_id: 当 ``namespace="cron"`` 时必填，用于匹配 ``metadata.cron_id``。

        Returns:
            list of dict，每个元素形如：{id, content, metadata}
        """
        if self.collection.count() == 0:
            return []

        # cron 命名空间可用 chromadb where 加速过滤；user / None 需 Python 过滤
        where = None
        if namespace == "cron":
            if cron_id is not None:
                # ChromaDB 顶层 where 只接受单操作符，多条件需用 $and 包装
                where = {"$and": [{"namespace": "cron"}, {"cron_id": cron_id}]}
            else:
                where = {"namespace": "cron"}

        results = self.collection.get(
            include=["documents", "metadatas"], where=where
        )

        ids = results.get("ids", [])
        documents = results.get("documents", [])
        metadatas = results.get("metadatas", [])

        output = []
        for mid, doc, meta in zip(ids, documents, metadatas):
            meta_dict = meta or {}
            # user 命名空间下兼容旧数据：缺失 namespace 字段视为 user
            if namespace == "user":
                if meta_dict.get("namespace", "user") != "user":
                    continue
            elif namespace == "cron":
                # 防御性二次校验（chromadb where 已过滤，此处兜底）
                if meta_dict.get("namespace") != "cron":
                    continue
                if cron_id is not None and meta_dict.get("cron_id") != cron_id:
                    continue
            output.append({
                "id": mid,
                "content": doc,
                "metadata": meta_dict,
            })
        return output

    def update_memory(
        self,
        memory_id: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """更新指定记忆的内容与元数据。

        Args:
            memory_id: 记忆 ID。
            content: 新的记忆内容。
            metadata: 新的元数据（可选）。
        """
        # 内容变化需要重新生成 embedding
        embedding = self._embed(content)

        meta = _normalize_metadata(metadata)

        self.collection.update(
            ids=[memory_id],
            documents=[content],
            embeddings=[embedding],
            metadatas=[meta] if meta is not None else None,
        )

    def delete_memory(self, memory_id: str) -> None:
        """删除指定记忆。

        Args:
            memory_id: 记忆 ID。
        """
        self.collection.delete(ids=[memory_id])

    # ------------------------------------------------------------------
    # 去重检测
    # ------------------------------------------------------------------
    def find_duplicates(
        self,
        new_content: str,
        threshold: float = 0.85,
        namespace: Optional[str] = "user",
        cron_id: Optional[str] = None,
    ) -> list:
        """查找与 new_content 相似的已有记忆（去重检测）。

        使用 ChromaDB 的 ANN 近似检索代替全量扫描。
        ChromaDB 内部使用 HNSW 索引，O(log n) 而非 O(n)。

        Args:
            new_content: 待检测的新内容。
            threshold: 相似度阈值，cosine similarity > threshold 视为重复。
            namespace: Phase 8 隔离层。``"user"``（默认）只在用户命名空间内去重
                （含旧数据：缺失 namespace 字段视为 user）；``"cron"`` 只在 cron
                命名空间且 ``cron_id`` 匹配的条目内去重，使惊讶门控范围限定在
                同一调度项内；``None`` 跨命名空间去重（管理员视图）。
            cron_id: 当 ``namespace="cron"`` 时必填，用于匹配 ``metadata.cron_id``。

        Returns:
            按相似度降序排列的重复记忆列表，每项含：
            {"id": str, "content": str, "metadata": dict, "similarity": float}
        """
        # 空库直接返回
        if self.collection.count() == 0:
            return []

        query_embedding = self._embed(new_content)

        # cosine 距离 = 1 - cosine_similarity
        # 目标 similarity > threshold → distance < 1 - threshold
        max_distance = 1.0 - threshold

        # Phase 8 Task 1: 按 namespace + cron_id 构造 where 子句
        where = self._build_namespace_where(namespace, cron_id)

        # 用 ChromaDB 的 HNSW 索引做近似检索
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(50, self.collection.count()),  # Phase 9: cap 50，HNSW ef_search=40 下更多是填充
            include=["documents", "metadatas", "distances"],
            where=where,
        )

        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        ids = results.get("ids", [[]])[0]

        duplicates = []
        for mid, doc, meta, dist in zip(ids, documents, metadatas, distances):
            meta_dict = meta or {}
            # user 命名空间下兼容旧数据：缺失 namespace 字段视为 user
            if namespace == "user":
                if meta_dict.get("namespace", "user") != "user":
                    continue
            elif namespace == "cron":
                # 防御性二次校验
                if meta_dict.get("namespace") != "cron":
                    continue
                if cron_id is not None and meta_dict.get("cron_id") != cron_id:
                    continue
            if dist <= max_distance:  # distance 越小越相似
                duplicates.append({
                    "id": mid,
                    "content": doc,
                    "metadata": meta_dict,
                    "similarity": 1.0 - dist,
                })

        duplicates.sort(key=lambda x: x["similarity"], reverse=True)
        return duplicates

    # ------------------------------------------------------------------
    # 资源释放
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭连接，释放资源。

        ChromaDB 的 PersistentClient 没有显式 close 接口，
        数据在每次操作后已自动落盘，这里仅释放引用。
        """
        self.collection = None
        self.client = None
