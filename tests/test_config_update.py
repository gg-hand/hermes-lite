"""配置更新接口与辅助函数测试。

覆盖：
- ``_deep_merge_config``：递归合并、保留旧 key、标量覆盖、list 覆盖、不修改入参
- ``PUT /config``：部分更新保留其他段、原子写入临时文件清理、备份创建、
  Schema 校验（顶层类型错误 / 必填字段缺失）、并发安全、响应字段名
- ``_check_needs_restart``：旧值/新值非 dict 时的鲁棒性
- ``_apply_runtime_config``：空 dict 不被哨兵误判为缺失

运行方式：
    python -m unittest tests.test_config_update -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# 路径与 mock 依赖初始化（必须在导入任何 src 模块之前完成）
# ---------------------------------------------------------------------------

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.server import (  # noqa: E402
    _deep_merge_config,
    _atomic_write_config,
    _backup_config,
    _validate_config_schema,
    _check_needs_restart,
    _apply_runtime_config,
    _MISSING,
)

# 初始完整配置 YAML，供 PUT /config 端到端测试使用
_INITIAL_CONFIG_YAML = """\
llm:
  main_provider: anthropic
  main_model: claude-3-5-sonnet-20241022
  main_api_key: test-key
  consolidation_provider: anthropic
  consolidation_model: claude-3-5-sonnet-20241022
  consolidation_api_key: test-key
  max_context_tokens: 200000
  context_threshold: 0.8
memory:
  chroma_path: data/chroma
  memory_md_path: data/memory.md
  consolidation_threshold: 5
  dedup_similarity_threshold: 0.85
  retrieval_top_k: 5
  history_max_turns: 20
server:
  host: 0.0.0.0
  port: 8000
storage:
  sqlite_path: data/sessions.db
  session_ttl_days: 30
  cleanup_interval_hours: 24
tools:
  max_react_loops: 10
  defer_loading_threshold: 20
monitoring:
  enabled: true
skills:
  hermes: []
  mcp: []
"""


# ===========================================================================
# 1. _deep_merge_config 单元测试
# ===========================================================================

class TestDeepMerge(unittest.TestCase):
    """验证 _deep_merge_config 的递归合并、保留旧 key、覆盖与不可变性。"""

    def test_deep_merge_dict_recursive(self):
        """嵌套 dict 递归合并"""
        old = {"a": 1, "b": {"x": 1, "y": 2}}
        new = {"b": {"y": 3, "z": 4}, "c": 5}
        result = _deep_merge_config(old, new)
        self.assertEqual(result, {"a": 1, "b": {"x": 1, "y": 3, "z": 4}, "c": 5})

    def test_deep_merge_preserves_missing_keys(self):
        """未在新配置出现的旧 key 保留"""
        old = {"a": 1, "b": 2, "c": 3}
        new = {"a": 10}
        result = _deep_merge_config(old, new)
        self.assertEqual(result, {"a": 10, "b": 2, "c": 3})

    def test_deep_merge_overwrites_scalar(self):
        """标量值用新值覆盖"""
        old = {"a": 1, "b": "old"}
        new = {"a": 2, "b": "new"}
        result = _deep_merge_config(old, new)
        self.assertEqual(result, {"a": 2, "b": "new"})

    def test_deep_merge_overwrites_list(self):
        """list 用新值覆盖（不拼接）"""
        old = {"items": [1, 2, 3]}
        new = {"items": [4, 5]}
        result = _deep_merge_config(old, new)
        self.assertEqual(result, {"items": [4, 5]})

    def test_deep_merge_does_not_mutate_inputs(self):
        """不修改入参 dict"""
        old = {"a": {"x": 1}}
        new = {"a": {"y": 2}}
        _deep_merge_config(old, new)
        self.assertEqual(old, {"a": {"x": 1}})
        self.assertEqual(new, {"a": {"y": 2}})


# ===========================================================================
# 2. PUT /config 端到端测试（FastAPI TestClient + tmp_path）
# ===========================================================================

class TestPutConfigEndpoint(unittest.TestCase):
    """验证 PUT /config 接口的部分更新、原子写入、备份、Schema 校验与并发安全。"""

    def setUp(self):
        """每个测试用例使用独立的临时 config.yaml，并 patch CONFIG_PATH 与 orchestrator。"""
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(_INITIAL_CONFIG_YAML)
        # 用 patch.start/stop 保证整个测试方法执行期间 patch 持续生效
        self._patches = [
            patch("src.server.CONFIG_PATH", self.config_path),
            patch("src.server.orchestrator", None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_client(self):
        """构建 TestClient（patch 已在 setUp 中启动，整个测试方法期间生效）。"""
        from src.server import app
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_put_config_partial_update_preserves_other_sections(self):
        """部分配置不丢失其他段"""
        client = self._get_client()
        # 仅修改 memory.consolidation_threshold
        resp = client.put("/config", json={"config": {"memory": {"consolidation_threshold": 15}}})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "saved")
        # 验证 config.yaml 仍含全部段
        import yaml
        with open(self.config_path, "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        for seg in ("llm", "memory", "server", "storage", "tools", "monitoring", "skills"):
            self.assertIn(seg, saved, f"段 {seg} 不应丢失")
        # 验证 consolidation_threshold 已更新
        self.assertEqual(saved["memory"]["consolidation_threshold"], 15)
        # 验证 memory 其他子项保留
        self.assertEqual(saved["memory"]["retrieval_top_k"], 5)
        # 验证 llm 段完整保留
        self.assertEqual(saved["llm"]["main_model"], "claude-3-5-sonnet-20241022")

    def test_put_config_atomic_write_temp_file_cleanup(self):
        """临时文件被清理（成功写入后 .tmp 不存在）"""
        client = self._get_client()
        resp = client.put("/config", json={"config": {"memory": {"consolidation_threshold": 20}}})
        self.assertEqual(resp.status_code, 200)
        tmp_file = self.config_path + ".tmp"
        self.assertFalse(os.path.exists(tmp_file), "临时文件应被清理")

    def test_put_config_creates_backup(self):
        """`.bak` 文件创建且内容为旧配置"""
        client = self._get_client()
        # 先读旧配置内容
        with open(self.config_path, "r", encoding="utf-8") as f:
            old_content = f.read()
        # PUT 修改
        resp = client.put("/config", json={"config": {"memory": {"consolidation_threshold": 25}}})
        self.assertEqual(resp.status_code, 200)
        # 验证 .bak 存在且内容为旧配置
        bak_file = self.config_path + ".bak"
        self.assertTrue(os.path.exists(bak_file), ".bak 文件应存在")
        with open(bak_file, "r", encoding="utf-8") as f:
            bak_content = f.read()
        self.assertEqual(bak_content, old_content, ".bak 内容应为旧配置")

    def test_put_config_schema_validation_invalid_top_type(self):
        """顶层段类型错误返回 400"""
        client = self._get_client()
        # llm 段为字符串（应为 dict）
        resp = client.put("/config", json={"config": {"llm": "invalid"}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("配置校验失败", resp.json()["detail"])
        self.assertIn("llm 必须是字典", resp.json()["detail"])
        # 验证 config.yaml 未被修改
        import yaml
        with open(self.config_path, "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        self.assertIsInstance(saved["llm"], dict, "config.yaml 不应被修改")

    def test_put_config_schema_validation_missing_llm_required(self):
        """llm 必填字段为空返回 400（merge 后 llm.main_model 必填且不能为空）"""
        client = self._get_client()
        # 用空字符串覆盖 main_model（merge 后 main_model 为空，触发必填校验）
        resp = client.put("/config", json={"config": {"llm": {"main_model": ""}}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("配置校验失败", resp.json()["detail"])
        self.assertIn("llm.main_model", resp.json()["detail"])

    def test_put_config_concurrent_safe(self):
        """并发 PUT 不冲突（用线程模拟 2 个并发 PUT）"""
        results = []

        def put_request(value):
            try:
                # 每个线程独立创建 TestClient，避免跨线程共享连接
                client = self._get_client()
                resp = client.put(
                    "/config",
                    json={"config": {"memory": {"consolidation_threshold": value}}},
                )
                results.append((value, resp.status_code, resp.json().get("status")))
            except Exception as e:
                results.append((value, "error", str(e)))

        threads = [
            threading.Thread(target=put_request, args=(15,)),
            threading.Thread(target=put_request, args=(25,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 验证两个请求都成功
        self.assertEqual(len(results), 2)
        for v, code, status in results:
            self.assertEqual(code, 200, f"值 {v} 的请求应成功")
            self.assertEqual(status, "saved")
        # 验证最终 config.yaml 中 consolidation_threshold 是 15 或 25 之一（最后一个写入的）
        import yaml
        with open(self.config_path, "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        self.assertIn(saved["memory"]["consolidation_threshold"], (15, 25))

    def test_put_config_response_field_needs_restart(self):
        """响应字段名为 needs_restart（验证字段存在且类型为 bool）"""
        client = self._get_client()
        resp = client.put("/config", json={"config": {"memory": {"consolidation_threshold": 30}}})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("needs_restart", data)
        self.assertIsInstance(data["needs_restart"], bool)
        # 不应有 restart_required 字段
        self.assertNotIn("restart_required", data)


# ===========================================================================
# 3. _check_needs_restart 鲁棒性
# ===========================================================================

class TestCheckNeedsRestartRobustness(unittest.TestCase):
    """验证 _check_needs_restart 在旧值/新值非 dict 时不崩溃。"""

    def test_check_needs_restart_handles_non_dict_old_value(self):
        """旧值非 dict 时不崩溃"""
        # old 中 llm 是字符串，new 中 llm 是 dict
        old = {"llm": "old_value"}
        new = {"llm": {"main_provider": "anthropic"}}
        # 不应抛 AttributeError，应返回 True（值已变化）
        result = _check_needs_restart(old, new)
        self.assertTrue(result)

    def test_check_needs_restart_handles_non_dict_new_value(self):
        """新值非 dict 时不崩溃"""
        old = {"llm": {"main_provider": "anthropic"}}
        new = {"llm": "new_value"}
        result = _check_needs_restart(old, new)
        self.assertTrue(result)


# ===========================================================================
# 4. _apply_runtime_config 哨兵处理
# ===========================================================================

class TestApplyRuntimeConfigSentinel(unittest.TestCase):
    """验证 _apply_runtime_config 不将空 dict 误判为缺失。"""

    def test_apply_runtime_config_does_not_misjudge_empty_dict(self):
        """val: {} 不被误判为缺失"""
        # 用 mock orchestrator
        mock_orch = MagicMock()
        mock_orch.consolidation_engine = MagicMock()
        mock_orch.consolidation_engine.threshold = 5
        # val 走到 int({}) 时会抛 TypeError，标记 applied=False
        with patch("src.server.orchestrator", mock_orch):
            applied = _apply_runtime_config({"memory": {"consolidation_threshold": {}}})
        # 应该返回 dict，且该项标记为 False（int({}) 抛错）
        self.assertIn("memory.consolidation_threshold", applied)
        self.assertFalse(applied["memory.consolidation_threshold"])


if __name__ == "__main__":
    unittest.main()
