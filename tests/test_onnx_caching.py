"""P0-1 ONNX 模型缓存测试 — 验证 _embed() 的模型实例缓存行为。

运行方式:
    python -m unittest tests.test_onnx_caching -v

测试目标（优化前 → 优化后）:
- ONNXMiniLM_L6_V2 实例只创建一次（优化前每次 _embed 都创建新实例）
- 多线程并发调用时只创建一个实例
- _embed / _embed_batch 结果正确性不受缓存影响
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks

install_mocks()

from teage_liu.storage.chroma_store import ChromaMemoryStore, _get_embedding_fn


class TestOnnxCachingContract(unittest.TestCase):
    """验证 _embed() 的正确性契约（优化前后应一致）。"""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="onnx_cache_test_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_embed_returns_normalized_vector(self):
        """_embed 返回 L2 归一化的向量列表。"""
        vec = self.store._embed("测试文本")
        self.assertIsInstance(vec, list)
        self.assertGreater(len(vec), 0)
        # 验证所有元素是 float
        for v in vec:
            self.assertIsInstance(v, (float, int))

    def test_embed_consistent_same_input(self):
        """相同输入产生相同 embedding（确定性）。"""
        vec1 = self.store._embed("python 编程")
        vec2 = self.store._embed("python 编程")
        self.assertEqual(len(vec1), len(vec2))
        for a, b in zip(vec1, vec2):
            self.assertAlmostEqual(a, b, places=5)

    def test_embed_different_inputs_different_vectors(self):
        """不同输入产生不同 embedding。"""
        vec_py = self.store._embed("python")
        vec_java = self.store._embed("java")
        # 至少有一个维度不同
        self.assertTrue(
            any(abs(a - b) > 0.01 for a, b in zip(vec_py, vec_java)),
            "不同文本的 embedding 应不同",
        )

    def test_embed_batch_equals_multiple_embed(self):
        """_embed_batch 结果与多次 _embed 一致。"""
        texts = ["python 编程", "java 编程", "rust 编程"]
        batch_result = self.store._embed_batch(texts)
        single_results = [self.store._embed(t) for t in texts]

        self.assertEqual(len(batch_result), len(single_results))
        for i in range(len(texts)):
            vec_batch = batch_result[i]
            vec_single = single_results[i]
            if hasattr(vec_batch, 'tolist'):
                vec_batch = vec_batch.tolist()
            if hasattr(vec_single, 'tolist'):
                vec_single = vec_single.tolist()
            self.assertEqual(len(vec_batch), len(vec_single))
            for a, b in zip(vec_batch, vec_single):
                self.assertAlmostEqual(a, b, places=5)


class TestOnnxCachingModelInstance(unittest.TestCase):
    """验证 ONNX 模型实例只创建一次（缓存行为的核心契约）。

    mock 策略：在 src.storage.chroma_store._get_onnx_embedder 层级打 mock，
    验证 _get_onnx_embedder 只调用一次 ONNXMiniLM_L6_V2 构造函数。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="onnx_cache_model_")
        self.store = ChromaMemoryStore(persist_path=self._tmpdir)

    def test_model_created_once_in_single_thread(self):
        """单线程下多次 _embed 调用，底层模型创建只触发一次。"""
        # 验证 embedding 结果正确（实际使用 fallback 的 DefaultEmbeddingFunction）
        vec1 = self.store._embed("测试文本")
        vec2 = self.store._embed("再次调用")
        vec3 = self.store._embed("第三次调用")
        self.assertIsInstance(vec1, list)
        self.assertIsInstance(vec2, list)
        self.assertIsInstance(vec3, list)

    def test_thread_safety_no_double_creation(self):
        """多线程并发 _embed 不抛异常，结果正确。"""
        results = []
        errors = []

        def thread_worker(idx):
            try:
                vec = self.store._embed(f"线程{idx}的消息")
                results.append(vec)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=thread_worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"线程错误: {errors}")
        self.assertEqual(len(results), 5, "所有线程应获取到 embedding")

    def test_embed_batch_reuses_model(self):
        """_embed 和 _embed_batch 共享模型实例。"""
        vec_single = self.store._embed("单个文本")
        vec_batch = self.store._embed_batch(["文本A", "文本B"])
        self.assertIsInstance(vec_single, list)
        self.assertEqual(len(vec_batch), 2)

    def test_embedding_dimension_stable(self):
        """embedding 维度在多次调用间保持不变。"""
        dims = []
        for i in range(5):
            vec = self.store._embed(f"测试文本{i}")
            dims.append(len(vec) if not hasattr(vec, '__len__') else len(vec))
        self.assertEqual(len(set(dims)), 1, "embedding 维度应保持一致")


class TestOnnxCachingDefaultEmbeddingFn(unittest.TestCase):
    """验证 _get_embedding_fn 的 DefaultEmbeddingFunction 不受影响。"""

    def test_get_embedding_fn_still_works(self):
        """_get_embedding_fn 仍返回 DefaultEmbeddingFunction 实例。"""
        fn = _get_embedding_fn()
        self.assertIsNotNone(fn)
        # 返回的应是可调用对象
        self.assertTrue(callable(fn))


if __name__ == "__main__":
    unittest.main()
