"""为缺失的可选依赖（chromadb / sentence_transformers / numpy / uvicorn）提供 mock。

在当前环境未安装这些依赖时，通过 sys.modules 注入 mock 对象，
使 src/ 下依赖它们的模块（主要是 storage/chroma_store.py）仍可被导入与测试。

mock 策略：
- numpy：提供最小化的 _MockArray，支持 .tolist()、__matmul__、__getitem__、__iter__，
  覆盖 chroma_store.py 中用到的 np.array / np.float32 / 向量点积等操作。
- chromadb：提供 PersistentClient 与内存版 Collection，支持 add/get/update/delete/query/count。
- sentence_transformers：提供 SentenceTransformer，encode 按文本关键词返回确定性 embedding，
  便于测试 find_duplicates 的去重逻辑。
- uvicorn：server.py 仅在 __main__ 块中导入，但仍提供 mock 以防被顶层导入。
"""

from __future__ import annotations

import sys
from typing import Any, List


# ---------------------------------------------------------------------------
# numpy 最小化 mock
# ---------------------------------------------------------------------------

class _MockArray:
    """最小化 numpy 数组 mock，支持 chroma_store.py 用到的操作。

    支持：
    - np.array(data, dtype=...) 构造
    - .tolist() 转换为 Python 列表
    - __matmul__ 实现 (n, m) @ (m,) -> (n,) 的矩阵乘法
    - __getitem__ 支持索引访问（2D 访问返回 1D _MockArray）
    - __iter__ 支持迭代（用于 zip 遍历相似度数组）
    - __len__ 支持取长度
    """

    def __init__(self, data: Any) -> None:
        self._data = data

    @property
    def shape(self):
        if isinstance(self._data, list) and self._data:
            if isinstance(self._data[0], list):
                return (len(self._data), len(self._data[0]))
            return (len(self._data),)
        return ()

    def __matmul__(self, other: Any) -> "_MockArray":
        # 支持 (n, m) @ (m,) -> (n,)
        other_data = other._data if isinstance(other, _MockArray) else other
        if isinstance(self._data, list) and self._data:
            if isinstance(self._data[0], list):
                # 2D 矩阵乘 1D 向量
                result = []
                for row in self._data:
                    if isinstance(other_data, list):
                        result.append(
                            sum(r * o for r, o in zip(row, other_data))
                        )
                    else:
                        result.append(sum(r * other_data for r in row))
                return _MockArray(result)
        return _MockArray([])

    def tolist(self):
        return self._data

    def __iter__(self):
        if isinstance(self._data, list):
            return iter(self._data)
        return iter([self._data])

    def __getitem__(self, idx):
        result = self._data[idx]
        if isinstance(result, list):
            return _MockArray(result)
        return result

    def __len__(self):
        if isinstance(self._data, list):
            return len(self._data)
        return 1


class _MockNumpyModule:
    """numpy 模块 mock，提供 array / float32 / ndarray 等被引用的符号。"""

    float32 = "float32"
    ndarray = _MockArray
    float_ = float

    @staticmethod
    def array(data: Any, dtype: Any = None) -> _MockArray:
        if isinstance(data, _MockArray):
            return data
        return _MockArray(data)


# ---------------------------------------------------------------------------
# chromadb 内存版 mock
# ---------------------------------------------------------------------------

def _mock_where_match(metadata: dict, where: dict) -> bool:
    """mock chromadb where 子句匹配（支持 AND 与基础相等）。

    支持的语法：
    - ``{"field": value}``：metadata[field] == value
    - ``{"$and": [clause1, clause2]}``：所有子条件 AND
    - ``{"$or": [clause1, clause2]}``：任一子条件 OR

    不支持的语法直接返回 False（保守拒绝），与真实 chromadb 行为一致。
    """
    if not where:
        return True
    for key, cond in where.items():
        if key == "$and":
            if not all(_mock_where_match(metadata, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_mock_where_match(metadata, c) for c in cond):
                return False
        else:
            if not isinstance(cond, dict):
                # 基础相等：metadata[key] == cond
                if metadata.get(key) != cond:
                    return False
            else:
                # 操作符形式：{"$eq": v} / {"$ne": v} 等
                for op, val in cond.items():
                    cur = metadata.get(key)
                    if op == "$eq" and cur != val:
                        return False
                    if op == "$ne" and cur == val:
                        return False
                    if op == "$in" and cur not in val:
                        return False
                    if op == "$nin" and cur in val:
                        return False
                    # 其他操作符不实现，保守拒绝
                    if op not in ("$eq", "$ne", "$in", "$nin"):
                        return False
    return True


class _MockCollection:
    """内存版 ChromaDB Collection，支持 add/get/update/delete/query/count。

    支持 ``where`` 子句（metadata 过滤），与真实 ChromaDB 接口一致。
    """

    def __init__(self) -> None:
        # 存储：{id: {document, metadata, embedding}}
        self._store: dict = {}

    def count(self) -> int:
        return len(self._store)

    def add(self, ids, documents, metadatas=None, embeddings=None) -> None:
        for i, mid in enumerate(ids):
            self._store[mid] = {
                "document": documents[i],
                "metadata": metadatas[i] if metadatas else {},
                "embedding": embeddings[i] if embeddings else [],
            }

    def get(self, ids=None, include=None, where=None) -> dict:
        # ids 参数：指定要获取的记忆 ID 列表
        if ids is not None:
            result_ids = [mid for mid in ids if mid in self._store]
        elif where:
            result_ids = [mid for mid, d in self._store.items()
                          if _mock_where_match(d["metadata"] or {}, where)]
        else:
            result_ids = list(self._store.keys())
        return {
            "ids": result_ids,
            "documents": [self._store[m]["document"] for m in result_ids],
            "metadatas": [self._store[m]["metadata"] for m in result_ids],
        }

    def update(self, ids, documents, embeddings=None, metadatas=None) -> None:
        for i, mid in enumerate(ids):
            if mid in self._store:
                self._store[mid]["document"] = documents[i]
                if embeddings:
                    self._store[mid]["embedding"] = embeddings[i]
                if metadatas:
                    self._store[mid]["metadata"] = metadatas[i]

    def delete(self, ids) -> None:
        for mid in ids:
            self._store.pop(mid, None)

    def query(self, query_embeddings, n_results, include=None, where=None) -> dict:
        # 计算 query 向量与每条存储向量的 cosine 距离，按距离升序取前 n_results 条。
        # 这样 mock 行为与真实 ChromaDB（cosine space）一致：
        # 相同关键词的文本距离 0，python vs java 距离 1，便于测试去重逻辑。
        # 支持 where 子句按 metadata 过滤（与真实 ChromaDB 接口一致）。
        import math

        # 先按 where 过滤候选集
        if where:
            candidates = [
                (mid, d) for mid, d in self._store.items()
                if _mock_where_match(d["metadata"] or {}, where)
            ]
        else:
            candidates = list(self._store.items())

        n = min(n_results, len(candidates))
        if n == 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        query_emb = query_embeddings[0] if query_embeddings else []
        qn = math.sqrt(sum(x * x for x in query_emb))

        scored = []
        for mid, d in candidates:
            emb = d["embedding"]
            dot = sum(x * y for x, y in zip(query_emb, emb))
            en = math.sqrt(sum(y * y for y in emb))
            sim = dot / (qn * en) if qn > 0 and en > 0 else 0.0
            scored.append((1.0 - sim, mid, d))

        scored.sort(key=lambda x: x[0])
        scored = scored[:n]
        return {
            "ids": [[mid for _, mid, _ in scored]],
            "documents": [[d["document"] for _, _, d in scored]],
            "metadatas": [[d["metadata"] for _, _, d in scored]],
            "distances": [[dist for dist, _, _ in scored]],
        }


class _MockChromaClient:
    """chromadb.PersistentClient 返回的客户端 mock。"""

    def __init__(self) -> None:
        self._collections: dict = {}

    def get_or_create_collection(self, name: str, metadata: dict = None):
        if name not in self._collections:
            self._collections[name] = _MockCollection()
        return self._collections[name]


class _MockEmbeddingFunction:
    """chromadb DefaultEmbeddingFunction mock，按文本关键词返回确定性 embedding。

    与 _MockSentenceTransformer 保持一致的 embedding 规则（2 维向量）：
    - 文本含 "python" → [1.0, 0.0]
    - 文本含 "java"   → [0.0, 1.0]
    - 其他            → [0.5, 0.5]

    接口兼容 chromadb DefaultEmbeddingFunction：接受 list[str]，返回 list[list[float]]。
    """

    def __call__(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return [_MockSentenceTransformer._embed_one(t) for t in texts]


class _MockChromaUtilsModule:
    """chromadb.utils 模块 mock。"""

    embedding_functions = type("obj", (object,), {
        "DefaultEmbeddingFunction": _MockEmbeddingFunction,
    })()


class _MockChromadbModule:
    """chromadb 模块 mock。"""

    utils = _MockChromaUtilsModule

    @staticmethod
    def PersistentClient(path: str = None, **kwargs) -> _MockChromaClient:
        return _MockChromaClient()


# ---------------------------------------------------------------------------
# sentence_transformers mock
# ---------------------------------------------------------------------------

class _MockSentenceTransformer:
    """SentenceTransformer mock，按文本关键词返回确定性 embedding。

    embedding 规则（2 维向量）：
    - 文本含 "python" → [1.0, 0.0]
    - 文本含 "java"   → [0.0, 1.0]
    - 其他            → [0.5, 0.5]

    这样两条都含 "python" 的文本相似度为 1.0（>0.85，判定为重复），
    "python" 与 "java" 相似度为 0.0（<0.85，判定为不重复）。
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self.model_name = model_name

    @staticmethod
    def _embed_one(text: str) -> List[float]:
        lowered = text.lower()
        if "python" in lowered:
            return [1.0, 0.0]
        if "java" in lowered:
            return [0.0, 1.0]
        return [0.5, 0.5]

    def encode(self, texts, normalize_embeddings: bool = False):
        if isinstance(texts, str):
            # 单条文本：返回带 .tolist() 的 _MockArray
            return _MockArray(self._embed_one(texts))
        # 批量：返回嵌套列表，由 np.array 包装
        return [self._embed_one(t) for t in texts]


class _MockSentenceTransformersModule:
    """sentence_transformers 模块 mock。"""

    SentenceTransformer = _MockSentenceTransformer


# ---------------------------------------------------------------------------
# uvicorn mock（server.py 在 __main__ 块中导入）
# ---------------------------------------------------------------------------

class _MockUvicornModule:
    """uvicorn 模块 mock，提供 run 函数占位。"""

    @staticmethod
    def run(*args, **kwargs) -> None:
        pass


# ---------------------------------------------------------------------------
# PyMuPDF (fitz) mock
# ---------------------------------------------------------------------------


class _MockPage:
    """Mock PDF 页。"""

    def get_text(self) -> str:
        return "mock pdf page text"


class _MockPdfDoc:
    """Mock PDF 文档。"""

    def __init__(self, stream=None, filetype=None):
        self._pages = [_MockPage(), _MockPage()]

    def __iter__(self):
        return iter(self._pages)

    def __getitem__(self, idx):
        return self._pages[idx]

    def __len__(self):
        return len(self._pages)

    def close(self):
        pass


def _mock_fitz_open(stream=None, filetype=None):
    return _MockPdfDoc(stream=stream, filetype=filetype)


class _MockPyMuPDFModule:
    """PyMuPDF (fitz) mock。"""

    @staticmethod
    def open(stream=None, filetype=None):
        return _MockPdfDoc(stream=stream, filetype=filetype)


# ---------------------------------------------------------------------------
# python-docx mock
# ---------------------------------------------------------------------------


class _MockPara:
    """Mock 段落。"""

    def __init__(self, text="mock paragraph text"):
        self.text = text


class _MockDocxDoc:
    """Mock DOCX 文档。"""

    def __init__(self, fileobj=None):
        self.paragraphs = [
            _MockPara("第一章 概述"),
            _MockPara("这是一段 mock 文档内容，用于测试 DOCX 解析功能。"),
            _MockPara("包含营收数据和市场分析预测。"),
        ]


class _MockDocxModule:
    """python-docx mock。"""

    @staticmethod
    def Document(fileobj=None):
        return _MockDocxDoc(fileobj=fileobj)


# ---------------------------------------------------------------------------
# pytesseract mock
# ---------------------------------------------------------------------------


def _mock_image_to_string(image, lang="chi_sim+eng"):
    return "mock ocr extracted text from image"


class _MockTesseractModule:
    """pytesseract mock。"""

    @staticmethod
    def image_to_string(image, lang="chi_sim+eng"):
        return _mock_image_to_string(image, lang)


# ---------------------------------------------------------------------------
# Pillow mock
# ---------------------------------------------------------------------------


class _MockImage:
    """Mock PIL Image。"""

    format = "PNG"
    size = (100, 100)

    @staticmethod
    def open(fp):
        return _MockImage()


class _MockImageModule:
    """PIL mock。"""

    Image = _MockImage()
    Image.open = staticmethod(lambda fp: _MockImage())


class _MockPillowModule:
    """Pillow 模块 mock（PIL 命名空间）。"""

    Image = _MockImageModule


# ---------------------------------------------------------------------------
# multipart mock（用于 FastAPI UploadFile 测试）
# ---------------------------------------------------------------------------


class _MockMultipartModule:
    """python-multipart mock（测试中不使用真实 multipart 解析）。"""
    __version__ = "0.0.9"


# ---------------------------------------------------------------------------
# 安装 mock 到 sys.modules
# ---------------------------------------------------------------------------

def install_mocks() -> None:
    """将 mock 模块注入 sys.modules。

    - chromadb：始终强制注入 mock（即使已真实安装），保证测试确定性
      （mock embedding 按关键词生成，不依赖模型权重下载）且避免 Windows
      下 chromadb 临时文件清理问题。同时注入 chromadb.utils 与
      chromadb.utils.embedding_functions 子模块，使
      `from chromadb.utils.embedding_functions import DefaultEmbeddingFunction` 生效。
    - numpy / sentence_transformers / uvicorn：仅在未安装时注入 mock。
    """
    chromadb_mock = _MockChromadbModule()
    sys.modules["chromadb"] = chromadb_mock
    sys.modules["chromadb.utils"] = _MockChromaUtilsModule
    sys.modules["chromadb.utils.embedding_functions"] = type(
        "obj", (object,), {"DefaultEmbeddingFunction": _MockEmbeddingFunction}
    )()
    _maybe_inject("numpy", _MockNumpyModule)
    _maybe_inject("sentence_transformers", _MockSentenceTransformersModule)
    _maybe_inject("uvicorn", _MockUvicornModule)
    # 文件处理 mock（仅未安装时注入）
    _maybe_inject("fitz", _MockPyMuPDFModule)       # PyMuPDF
    _maybe_inject("docx", _MockDocxModule)           # python-docx
    _maybe_inject("pytesseract", _MockTesseractModule)  # pytesseract
    _maybe_inject("PIL", _MockPillowModule)           # Pillow


def _maybe_inject(name: str, mock_factory) -> None:
    """若 name 模块当前不可导入，则将 mock_factory() 注入 sys.modules。"""
    if name in sys.modules:
        # 已加载（真实或 mock），不覆盖
        return
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = mock_factory()


def _force_inject(name: str, mock_factory) -> None:
    """始终将 mock_factory() 注入 sys.modules，覆盖已加载的真实模块。

    用于 chromadb 等需要确定性测试行为的依赖：即使真实包已安装，
    也强制使用 mock 以保证测试结果稳定且不依赖网络/模型权重。
    """
    sys.modules[name] = mock_factory()


# 默认在导入本模块时即安装 mock，便于测试文件直接 from tests._mock_deps import install_mocks
install_mocks()
