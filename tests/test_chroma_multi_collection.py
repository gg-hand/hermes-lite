"""ChromaDB 多 collection 测试（批次 2.6）。

验证：
- get_collection(name) 按名称获取或创建 collection
- add_memory(collection_name=...) 写入指定 collection
- 默认写入 long_term_memory（向后兼容）
- query_memory 跨 collection 搜索并合并结果
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.storage.chroma_store import ChromaMemoryStore


@pytest.fixture
def store(tmp_path):
    """构造 ChromaMemoryStore 实例，patch embedding 避免真实 ONNX 调用。"""
    with patch.object(ChromaMemoryStore, "_embed", return_value=[0.1, 0.2]):
        s = ChromaMemoryStore(persist_path=str(tmp_path / "chroma"))
        yield s


class TestMultiCollection:
    def test_default_collection_is_long_term_memory(self, store):
        """默认 collection 名为 long_term_memory（向后兼容）。"""
        assert store.collection.name == "long_term_memory"
        assert store.COLLECTION_NAME == "long_term_memory"

    def test_get_collection_returns_existing(self, store):
        """get_collection 返回默认 collection 当 name 为默认值。"""
        col = store.get_collection("long_term_memory")
        assert col is store.collection

    def test_get_collection_creates_new(self, store):
        """get_collection 创建并缓存新的 collection。"""
        col = store.get_collection("archive")
        assert col is not None
        assert col is not store.collection
        # 再次获取应返回缓存的同一实例
        col2 = store.get_collection("archive")
        assert col is col2

    def test_add_memory_to_named_collection(self, store):
        """add_memory(collection_name=...) 写入指定 collection。"""
        store.add_memory(
            content="archive memory",
            metadata={"timestamp": "2026-01-01T00:00:00"},
            collection_name="archive",
        )
        store.add_memory(
            content="active memory",
            metadata={"timestamp": "2026-01-01T00:00:00"},
        )
        # 默认 collection 应只有 active memory
        assert store.collection.count() == 1
        # archive collection 应只有 archive memory
        archive_col = store.get_collection("archive")
        assert archive_col.count() == 1

    def test_add_memory_default_collection_backward_compat(self, store):
        """add_memory 不传 collection_name 时写入默认 collection（向后兼容）。"""
        store.add_memory(
            content="default",
            metadata={"timestamp": "2026-01-01T00:00:00"},
        )
        assert store.collection.count() == 1
        # 不应创建其他 collection
        assert len(store._collections) == 1

    def test_query_memory_search_all_collections(self, store):
        """query_memory(search_all_collections=True) 跨所有 collection 搜索并合并。"""
        # 写入默认 collection
        store.add_memory(
            content="python is great",
            metadata={"timestamp": "2026-01-01T00:00:00"},
        )
        # 写入 archive collection
        store.add_memory(
            content="java is also good",
            metadata={"timestamp": "2026-01-01T00:00:00"},
            collection_name="archive",
        )
        # 跨 collection 搜索
        results = store.query_memory(
            query_text="python",
            top_k=10,
            reinforce=False,
            search_all_collections=True,
        )
        # 应返回 2 条结果（来自两个 collection）
        assert len(results) == 2

    def test_query_memory_default_only_searches_default(self, store):
        """query_memory 不传 search_all_collections 时只搜默认 collection（向后兼容）。"""
        store.add_memory(
            content="python is great",
            metadata={"timestamp": "2026-01-01T00:00:00"},
        )
        store.add_memory(
            content="python in archive",
            metadata={"timestamp": "2026-01-01T00:00:00"},
            collection_name="archive",
        )
        # 默认只搜 long_term_memory
        results = store.query_memory(
            query_text="python",
            top_k=10,
            reinforce=False,
        )
        assert len(results) == 1
        assert results[0]["content"] == "python is great"

    def test_delete_old_entries_only_affects_default(self, store):
        """delete_old_entries 默认只清理默认 collection。"""
        # 写入过期的默认 collection 条目
        store.add_memory(
            content="old active",
            metadata={"timestamp": "2020-01-01T00:00:00"},
        )
        # 写入过期的 archive collection 条目
        store.add_memory(
            content="old archive",
            metadata={"timestamp": "2020-01-01T00:00:00"},
            collection_name="archive",
        )
        # 清理默认 collection
        deleted = store.delete_old_entries(days=365)
        assert deleted == 1
        # archive collection 不受影响
        assert store.get_collection("archive").count() == 1

    def test_delete_old_entries_all_collections(self, store):
        """delete_old_entries(all_collections=True) 清理所有 collection。"""
        store.add_memory(
            content="old active",
            metadata={"timestamp": "2020-01-01T00:00:00"},
        )
        store.add_memory(
            content="old archive",
            metadata={"timestamp": "2020-01-01T00:00:00"},
            collection_name="archive",
        )
        deleted = store.delete_old_entries(days=365, all_collections=True)
        assert deleted == 2
        assert store.collection.count() == 0
        assert store.get_collection("archive").count() == 0
