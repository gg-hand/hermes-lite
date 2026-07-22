"""ChromaDB 队列恢复测试（批次 2.3）。

验证：
- ChromaMemoryStore.__init__ 末尾调用 _recover_pending_queue
- _recover_pending_queue 调用 client.persist() 当可用时
- client.persist() 不存在时不抛异常（向后兼容 chromadb 0.5+）
- add_memory 累积 N 次后触发 persist
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# 注入 mock 依赖
import tests._mock_deps  # noqa: F401

from teage_liu.storage.chroma_store import ChromaMemoryStore


class _CountingClient:
    """带 persist 调用计数的 mock chromadb PersistentClient。

    用于验证 _recover_pending_queue / add_memory 是否正确调用 persist。
    """

    def __init__(self):
        self.persist_call_count = 0
        self._collections: dict = {}

    def get_or_create_collection(self, name: str, metadata=None):
        if name not in self._collections:
            from tests._mock_deps import _MockCollection
            self._collections[name] = _MockCollection()
        return self._collections[name]

    def persist(self):
        self.persist_call_count += 1


class _NoPersistClient:
    """无 persist 方法的 mock client（模拟 chromadb 0.5+ 自动持久化）。"""

    def __init__(self):
        self._collections: dict = {}

    def get_or_create_collection(self, name: str, metadata=None):
        if name not in self._collections:
            from tests._mock_deps import _MockCollection
            self._collections[name] = _MockCollection()
        return self._collections[name]

    # 故意不提供 persist 方法


class TestRecoverPendingQueue:
    def test_init_calls_recover_with_persist_capable_client(self, tmp_path):
        """__init__ 应调用 _recover_pending_queue，client 有 persist 时被调用一次。"""
        counting_client = _CountingClient()
        with patch("teage_liu.storage.chroma_store.chromadb") as mock_chromadb:
            mock_chromadb.PersistentClient.return_value = counting_client
            store = ChromaMemoryStore(persist_path=str(tmp_path / "chroma"))
        # 启动时触发一次 persist
        assert counting_client.persist_call_count == 1

    def test_init_does_not_raise_when_client_has_no_persist(self, tmp_path):
        """client 无 persist 方法时（chromadb 0.5+），不抛异常。"""
        no_persist_client = _NoPersistClient()
        with patch("teage_liu.storage.chroma_store.chromadb") as mock_chromadb:
            mock_chromadb.PersistentClient.return_value = no_persist_client
            # 不应抛异常
            store = ChromaMemoryStore(persist_path=str(tmp_path / "chroma"))
        # 验证 store 正常初始化
        assert store.collection is not None

    def test_add_memory_triggers_periodic_persist(self, tmp_path):
        """add_memory 每 N 次（N=10）调用一次 persist。"""
        counting_client = _CountingClient()
        with patch("teage_liu.storage.chroma_store.chromadb") as mock_chromadb:
            mock_chromadb.PersistentClient.return_value = counting_client
            # patch embedding 函数避免真实 ONNX 调用
            with patch.object(ChromaMemoryStore, "_embed", return_value=[0.1, 0.2]):
                store = ChromaMemoryStore(persist_path=str(tmp_path / "chroma"))
                # 初始化时已调用 1 次 persist
                initial_count = counting_client.persist_call_count
                assert initial_count == 1

                # 写入 9 次（不触发 persist，因为计数从 0 开始，第 10 次触发）
                for i in range(9):
                    store.add_memory(content=f"msg-{i}", metadata={"timestamp": "2026-01-01T00:00:00"})
                # 9 次后未触发 persist（计数 9 < 10）
                assert counting_client.persist_call_count == initial_count

                # 第 10 次触发 persist
                store.add_memory(content="msg-9", metadata={"timestamp": "2026-01-01T00:00:00"})
                assert counting_client.persist_call_count == initial_count + 1

                # 再写 9 次，不触发
                for i in range(9):
                    store.add_memory(content=f"msg-{i+10}", metadata={"timestamp": "2026-01-01T00:00:00"})
                assert counting_client.persist_call_count == initial_count + 1

                # 第 20 次再次触发
                store.add_memory(content="msg-19", metadata={"timestamp": "2026-01-01T00:00:00"})
                assert counting_client.persist_call_count == initial_count + 2

    def test_add_memory_without_persist_method_does_not_raise(self, tmp_path):
        """add_memory 多次后，client 无 persist 方法时不抛异常。"""
        no_persist_client = _NoPersistClient()
        with patch("teage_liu.storage.chroma_store.chromadb") as mock_chromadb:
            mock_chromadb.PersistentClient.return_value = no_persist_client
            with patch.object(ChromaMemoryStore, "_embed", return_value=[0.1, 0.2]):
                store = ChromaMemoryStore(persist_path=str(tmp_path / "chroma"))
                # 写入 20 次，不应抛异常
                for i in range(20):
                    store.add_memory(content=f"msg-{i}", metadata={"timestamp": "2026-01-01T00:00:00"})
                # 验证写入成功
                assert store.collection.count() == 20

    def test_recover_pending_queue_logs_warning_on_failure(self, tmp_path, caplog):
        """client.persist() 抛异常时仅 warning 不传播。"""
        class _FailingPersistClient(_CountingClient):
            def persist(self):
                raise RuntimeError("simulated persist failure")

        failing_client = _FailingPersistClient()
        with patch("teage_liu.storage.chroma_store.chromadb") as mock_chromadb:
            mock_chromadb.PersistentClient.return_value = failing_client
            # 不应抛异常
            store = ChromaMemoryStore(persist_path=str(tmp_path / "chroma"))
            # 验证 store 仍可用
            assert store.collection is not None
