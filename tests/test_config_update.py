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

# Task 11: 配置辅助函数直接从 config_helpers 导入（server.py 不再 re-export）
from src.config_helpers import (  # noqa: E402
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
        """每个测试用例使用独立的临时 config.yaml，并 patch CONFIG_PATH。

        Task 11: orchestrator 通过 app.dependency_overrides[get_orchestrator]
        注入 None（不再 patch src.server.orchestrator 全局变量）。
        """
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(_INITIAL_CONFIG_YAML)
        # CONFIG_PATH 仍保留在 server.py，继续用 patch
        self._patches = [
            patch("src.server.CONFIG_PATH", self.config_path),
        ]
        for p in self._patches:
            p.start()
        # 通过 DI override 注入 orchestrator=None，使 _apply_runtime_config 早退
        from src.server import app  # noqa: E402
        from app import get_orchestrator  # noqa: E402
        self._app = app
        self._get_orchestrator = get_orchestrator
        app.dependency_overrides[get_orchestrator] = lambda: None

    def tearDown(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        # 清理 DI overrides
        self._app.dependency_overrides.pop(self._get_orchestrator, None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_client(self):
        """构建 TestClient（patch 已在 setUp 中启动，整个测试方法期间生效）。"""
        from fastapi.testclient import TestClient
        return TestClient(self._app)

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

    def setUp(self):
        """注入 mock orchestrator via app.dependency_overrides。"""
        from src.server import app  # noqa: E402
        from app import get_orchestrator  # noqa: E402
        self._app = app
        self._get_orchestrator = get_orchestrator

    def _inject_orchestrator(self, mock_orch):
        """通过 DI override 注入 mock orchestrator。"""
        self._app.dependency_overrides[self._get_orchestrator] = lambda: mock_orch

    def tearDown(self):
        self._app.dependency_overrides.pop(self._get_orchestrator, None)

    def test_apply_runtime_config_does_not_misjudge_empty_dict(self):
        """val: {} 不被误判为缺失"""
        # 用 mock orchestrator
        mock_orch = MagicMock()
        mock_orch.consolidation_engine = MagicMock()
        mock_orch.consolidation_engine.threshold = 5
        self._inject_orchestrator(mock_orch)
        # val 走到 int({}) 时会抛 TypeError，标记 applied=False
        applied = _apply_runtime_config({"memory": {"consolidation_threshold": {}}})
        # 应该返回 dict，且该项标记为 False（int({}) 抛错）
        self.assertIn("memory.consolidation_threshold", applied)
        self.assertFalse(applied["memory.consolidation_threshold"])


# ===========================================================================
# 5. 防护开关热更新（security.enabled + guardrails.*.enabled）
# ===========================================================================

class TestGuardrailSwitchHotUpdate(unittest.TestCase):
    """验证防护开关通过 _RUNTIME_HOTUPDATE_MAP 热更新即时生效。"""

    def setUp(self):
        """注入 mock orchestrator via app.dependency_overrides。"""
        from src.server import app  # noqa: E402
        from app import get_orchestrator  # noqa: E402
        self._app = app
        self._get_orchestrator = get_orchestrator

    def _inject_orchestrator(self, mock_orch):
        """通过 DI override 注入 mock orchestrator。"""
        self._app.dependency_overrides[self._get_orchestrator] = lambda: mock_orch

    def tearDown(self):
        self._app.dependency_overrides.pop(self._get_orchestrator, None)

    def _build_mock_orchestrator(self):
        """构造 mock orchestrator，policy_engine / guardrail_engine 非 None。"""
        mock_orch = MagicMock()
        mock_orch.policy_engine = MagicMock()
        mock_orch.guardrail_engine = MagicMock()
        return mock_orch

    def test_security_enabled_hot_update_calls_setattr(self):
        """security.enabled=True 通过 setattr 写入 policy_engine.enabled。"""
        mock_orch = self._build_mock_orchestrator()
        self._inject_orchestrator(mock_orch)
        applied = _apply_runtime_config({"security": {"enabled": False}})
        self.assertTrue(applied.get("security.enabled"))
        # setattr(mock, "enabled", False) → mock.enabled = False
        self.assertEqual(mock_orch.policy_engine.enabled, False)

    def test_guardrails_input_scan_enabled_hot_update(self):
        """guardrails.input_scan.enabled 通过 setattr 写入 guardrail_engine。"""
        mock_orch = self._build_mock_orchestrator()
        self._inject_orchestrator(mock_orch)
        applied = _apply_runtime_config(
            {"guardrails": {"input_scan": {"enabled": False}}}
        )
        self.assertTrue(applied.get("guardrails.input_scan.enabled"))
        self.assertEqual(mock_orch.guardrail_engine.input_scan_enabled, False)

    def test_guardrails_sanitizer_enabled_hot_update(self):
        """guardrails.sanitizer.enabled 通过 setattr 写入 guardrail_engine。"""
        mock_orch = self._build_mock_orchestrator()
        self._inject_orchestrator(mock_orch)
        applied = _apply_runtime_config(
            {"guardrails": {"sanitizer": {"enabled": False}}}
        )
        self.assertTrue(applied.get("guardrails.sanitizer.enabled"))
        self.assertEqual(mock_orch.guardrail_engine.sanitizer_enabled, False)

    def test_guardrails_output_filter_enabled_hot_update(self):
        """guardrails.output_filter.enabled 通过 setattr 写入 guardrail_engine。"""
        mock_orch = self._build_mock_orchestrator()
        self._inject_orchestrator(mock_orch)
        applied = _apply_runtime_config(
            {"guardrails": {"output_filter": {"enabled": False}}}
        )
        self.assertTrue(applied.get("guardrails.output_filter.enabled"))
        self.assertEqual(mock_orch.guardrail_engine.output_filter_enabled, False)

    def test_policy_engine_none_marks_false(self):
        """policy_engine 为 None 时 security.enabled 标记 False 不崩溃。"""
        mock_orch = MagicMock()
        mock_orch.policy_engine = None  # 初始化失败降级
        self._inject_orchestrator(mock_orch)
        applied = _apply_runtime_config({"security": {"enabled": False}})
        self.assertFalse(applied.get("security.enabled"))

    def test_guardrail_engine_none_marks_false(self):
        """guardrail_engine 为 None 时 guardrails.*.enabled 标记 False 不崩溃。"""
        mock_orch = MagicMock()
        mock_orch.guardrail_engine = None
        self._inject_orchestrator(mock_orch)
        applied = _apply_runtime_config(
            {"guardrails": {"input_scan": {"enabled": False}}}
        )
        self.assertFalse(applied.get("guardrails.input_scan.enabled"))


# ===========================================================================
# 6. HIL 关闭专项：resolve_all 唤醒 pending 审批
# ===========================================================================

class TestHilDisableResolveAll(unittest.TestCase):
    """验证关闭 HIL 时 _apply_runtime_config 调用 approval_manager.resolve_all。"""

    def setUp(self):
        """通过 app.dependency_overrides 注入 mock 组件。"""
        from src.server import app  # noqa: E402
        from app import (  # noqa: E402
            get_orchestrator,
            get_approval_manager,
            get_audit_logger,
        )
        self._app = app
        self._get_orchestrator = get_orchestrator
        self._get_approval_manager = get_approval_manager
        self._get_audit_logger = get_audit_logger

    def _inject(self, mock_orch, mock_approval=None, mock_audit=None):
        """注入 orchestrator / approval_manager / audit_logger。"""
        self._app.dependency_overrides[self._get_orchestrator] = lambda: mock_orch
        self._app.dependency_overrides[self._get_approval_manager] = lambda: mock_approval
        self._app.dependency_overrides[self._get_audit_logger] = lambda: mock_audit

    def tearDown(self):
        self._app.dependency_overrides.pop(self._get_orchestrator, None)
        self._app.dependency_overrides.pop(self._get_approval_manager, None)
        self._app.dependency_overrides.pop(self._get_audit_logger, None)

    def test_disable_hil_calls_resolve_all(self):
        """security.enabled 翻转为 False 时调用 approval_manager.resolve_all('deny')。"""
        mock_orch = MagicMock()
        mock_orch.policy_engine = MagicMock()
        mock_orch.guardrail_engine = MagicMock()
        mock_approval = MagicMock()
        mock_approval.resolve_all.return_value = 2
        self._inject(mock_orch, mock_approval, None)
        _apply_runtime_config({"security": {"enabled": False}})
        mock_approval.resolve_all.assert_called_once_with(
            "deny", "HIL 已关闭，审批自动拒绝"
        )

    def test_enable_hil_does_not_call_resolve_all(self):
        """security.enabled 翻转为 True 时不调用 resolve_all。"""
        mock_orch = MagicMock()
        mock_orch.policy_engine = MagicMock()
        mock_orch.guardrail_engine = MagicMock()
        mock_approval = MagicMock()
        self._inject(mock_orch, mock_approval, None)
        _apply_runtime_config({"security": {"enabled": True}})
        mock_approval.resolve_all.assert_not_called()

    def test_disable_hil_with_zero_pending_no_audit(self):
        """resolve_all 返回 0（无 pending）时不记审计日志。"""
        mock_orch = MagicMock()
        mock_orch.policy_engine = MagicMock()
        mock_orch.guardrail_engine = MagicMock()
        mock_approval = MagicMock()
        mock_approval.resolve_all.return_value = 0
        mock_audit = MagicMock()
        self._inject(mock_orch, mock_approval, mock_audit)
        _apply_runtime_config({"security": {"enabled": False}})
        mock_approval.resolve_all.assert_called_once()
        mock_audit.log_guardrail_decision.assert_not_called()

    def test_disable_hil_with_pending_logs_audit(self):
        """resolve_all 返回 >0 时记审计日志（layer=policy_switch, risk=high）。"""
        mock_orch = MagicMock()
        mock_orch.policy_engine = MagicMock()
        mock_orch.guardrail_engine = MagicMock()
        mock_approval = MagicMock()
        mock_approval.resolve_all.return_value = 3
        mock_audit = MagicMock()
        self._inject(mock_orch, mock_approval, mock_audit)
        _apply_runtime_config({"security": {"enabled": False}})
        mock_audit.log_guardrail_decision.assert_called_once()
        call_kwargs = mock_audit.log_guardrail_decision.call_args
        self.assertEqual(call_kwargs.kwargs.get("layer"), "policy_switch")
        self.assertEqual(call_kwargs.kwargs.get("action"), "disable")
        self.assertEqual(call_kwargs.kwargs.get("risk_level"), "high")
        self.assertEqual(call_kwargs.kwargs.get("session_id"), "system")

    def test_disable_hil_approval_manager_none_no_crash(self):
        """approval_manager 为 None 时不崩溃（降级场景）。"""
        mock_orch = MagicMock()
        mock_orch.policy_engine = MagicMock()
        mock_orch.guardrail_engine = MagicMock()
        self._inject(mock_orch, None, None)
        # 不应抛异常
        applied = _apply_runtime_config({"security": {"enabled": False}})
        self.assertTrue(applied.get("security.enabled"))


# ===========================================================================
# 7. _check_needs_restart 防护开关判定
# ===========================================================================

class TestCheckNeedsRestartGuardrails(unittest.TestCase):
    """验证 enabled 字段变更不触发重启，结构性字段变更触发重启。"""

    def test_security_enabled_change_no_restart(self):
        """security.enabled 翻转不再触发重启（已改为热更新）。"""
        old = {"security": {"enabled": True, "rules": []}}
        new = {"security": {"enabled": False, "rules": []}}
        self.assertFalse(_check_needs_restart(old, new))

    def test_security_rules_change_still_needs_restart(self):
        """security.rules 变更仍需重启（规则需重建 PolicyEngine）。"""
        old = {"security": {"enabled": True, "rules": []}}
        new = {"security": {"enabled": True, "rules": [{"tool": "x", "risk": "deny"}]}}
        self.assertTrue(_check_needs_restart(old, new))

    def test_guardrails_enabled_change_no_restart(self):
        """guardrails.*.enabled 翻转不再触发重启（已改为热更新）。"""
        old = {
            "guardrails": {
                "input_scan": {"enabled": True, "action": "warn"},
                "sanitizer": {"enabled": True, "trusted_tools": []},
                "output_filter": {"enabled": True},
            }
        }
        new = {
            "guardrails": {
                "input_scan": {"enabled": False, "action": "warn"},
                "sanitizer": {"enabled": False, "trusted_tools": []},
                "output_filter": {"enabled": False},
            }
        }
        self.assertFalse(_check_needs_restart(old, new))

    def test_guardrails_action_change_needs_restart(self):
        """guardrails.input_scan.action 变更需重启（InjectionGuard 行为模式重建）。"""
        old = {"guardrails": {"input_scan": {"enabled": True, "action": "warn"}}}
        new = {"guardrails": {"input_scan": {"enabled": True, "action": "block"}}}
        self.assertTrue(_check_needs_restart(old, new))

    def test_guardrails_trusted_tools_change_needs_restart(self):
        """guardrails.sanitizer.trusted_tools 变更需重启。"""
        old = {"guardrails": {"sanitizer": {"enabled": True, "trusted_tools": ["a"]}}}
        new = {"guardrails": {"sanitizer": {"enabled": True, "trusted_tools": ["b"]}}}
        self.assertTrue(_check_needs_restart(old, new))

    def test_guardrails_max_output_length_change_needs_restart(self):
        """guardrails.sanitizer.max_output_length 变更需重启。"""
        old = {"guardrails": {"sanitizer": {"enabled": True, "max_output_length": 20000}}}
        new = {"guardrails": {"sanitizer": {"enabled": True, "max_output_length": 10000}}}
        self.assertTrue(_check_needs_restart(old, new))

    def test_guardrails_enable_bank_card_change_needs_restart(self):
        """guardrails.output_filter.enable_bank_card 变更需重启。"""
        old = {"guardrails": {"output_filter": {"enabled": True, "enable_bank_card": True}}}
        new = {"guardrails": {"output_filter": {"enabled": True, "enable_bank_card": False}}}
        self.assertTrue(_check_needs_restart(old, new))


if __name__ == "__main__":
    unittest.main()
