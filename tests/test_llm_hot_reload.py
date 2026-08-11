"""LLMClient 精准热重载测试。

验证：
- ``_llm_config_changed``：正确检测影响 LLMClient 的字段变更
- ``_reload_llm_client``：替换所有引用点、失败回滚保留旧实例
- ``_apply_runtime_config``：仅在显式传入 old_config 时触发 LLMClient 重建
- ``unmask_sensitive_config``：existing 无真实值时清空脱敏值
- ``LLMClient``：``consolidation_api_key`` 缺失时回退到 ``main_api_key``

运行方式：
    python -m unittest tests.test_llm_hot_reload -v
"""

from __future__ import annotations

import os
import sys
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

from teage_liu.config_helpers import (  # noqa: E402
    _llm_config_changed,
    _reload_llm_client,
    _apply_runtime_config,
    _LLM_RELOAD_KEYS,
)
from teage_liu.config import (  # noqa: E402
    unmask_sensitive_config,
    is_masked_value,
    mask_api_key,
    SENSITIVE_FIELDS,
)


# ===========================================================================
# 1. _llm_config_changed 单元测试
# ===========================================================================

class TestLLMConfigChanged(unittest.TestCase):
    """验证 _llm_config_changed 的变更检测逻辑。"""

    def test_no_change_returns_false(self):
        """llm 段无关键字段变更时返回 False"""
        old = {"llm": {"main_api_key": "k1", "main_model": "m1", "other": "x"}}
        new = {"llm": {"main_api_key": "k1", "main_model": "m1", "other": "y"}}
        self.assertFalse(_llm_config_changed(old, new))

    def test_main_api_key_changed_returns_true(self):
        """main_api_key 变更时返回 True"""
        old = {"llm": {"main_api_key": "k1", "main_model": "m1"}}
        new = {"llm": {"main_api_key": "k2", "main_model": "m1"}}
        self.assertTrue(_llm_config_changed(old, new))

    def test_main_model_changed_returns_true(self):
        """main_model 变更时返回 True"""
        old = {"llm": {"main_api_key": "k1", "main_model": "m1"}}
        new = {"llm": {"main_api_key": "k1", "main_model": "m2"}}
        self.assertTrue(_llm_config_changed(old, new))

    def test_provider_changed_returns_true(self):
        """provider 变更时返回 True"""
        old = {"llm": {"main_provider": "anthropic"}}
        new = {"llm": {"main_provider": "openai"}}
        self.assertTrue(_llm_config_changed(old, new))

    def test_base_url_changed_returns_true(self):
        """base_url 变更时返回 True"""
        old = {"llm": {"main_base_url": "http://a"}}
        new = {"llm": {"main_base_url": "http://b"}}
        self.assertTrue(_llm_config_changed(old, new))

    def test_consolidation_fields_changed_returns_true(self):
        """consolidation_* 字段变更时返回 True"""
        for key, old_v, new_v in [
            ("consolidation_api_key", "ck1", "ck2"),
            ("consolidation_provider", "anthropic", "openai"),
            ("consolidation_model", "cm1", "cm2"),
            ("consolidation_base_url", "http://a", "http://b"),
        ]:
            with self.subTest(key=key):
                old = {"llm": {key: old_v}}
                new = {"llm": {key: new_v}}
                self.assertTrue(_llm_config_changed(old, new))

    def test_non_reload_key_changed_returns_false(self):
        """非 _LLM_RELOAD_KEYS 字段（如 activity_timeout）变更不触发"""
        old = {"llm": {"activity_timeout": 30, "main_api_key": "k1"}}
        new = {"llm": {"activity_timeout": 60, "main_api_key": "k1"}}
        self.assertFalse(_llm_config_changed(old, new))

    def test_none_old_config_handled_gracefully(self):
        """old_config=None 时不崩溃，视为空配置（new 有 key 则返回 True）。

        实际的 None 守护在 ``_apply_runtime_config`` 中（``old_config is not None``），
        此处仅验证 helper 对 None 入参不抛异常。
        """
        new = {"llm": {"main_api_key": "k1"}}
        # None → {}，与 new 不同返回 True
        self.assertTrue(_llm_config_changed(None, new))
        # 两侧都视为空时返回 False
        self.assertFalse(_llm_config_changed(None, {}))

    def test_empty_llm_section_returns_false(self):
        """两侧 llm 段都缺失或为空时返回 False"""
        self.assertFalse(_llm_config_changed({}, {}))
        self.assertFalse(_llm_config_changed({"llm": {}}, {"llm": {}}))

    def test_old_missing_llm_new_has_key_returns_true(self):
        """old 无 llm 段、new 有 main_api_key 时视为变更"""
        self.assertTrue(_llm_config_changed({}, {"llm": {"main_api_key": "k1"}}))

    def test_reload_keys_covers_critical_fields(self):
        """_LLM_RELOAD_KEYS 覆盖所有影响 LLMClient 实例的关键字段"""
        expected = {
            "main_api_key", "consolidation_api_key",
            "main_provider", "consolidation_provider",
            "main_model", "consolidation_model",
            "main_base_url", "consolidation_base_url",
        }
        self.assertEqual(_LLM_RELOAD_KEYS, expected)


# ===========================================================================
# 2. _reload_llm_client 单元测试
# ===========================================================================

class TestReloadLLMClient(unittest.TestCase):
    """验证 _reload_llm_client 的引用替换与失败回滚。"""

    def setUp(self):
        """构建 mock orchestrator，包含所有 LLMClient 引用点。"""
        self.old_llm = MagicMock(name="old_llm_client")
        # 模拟 count_messages_tokens 方法（供 condenser._token_counter 使用）
        self.old_llm.count_messages_tokens = MagicMock(return_value=10)

        self.new_llm = MagicMock(name="new_llm_client")
        self.new_llm.count_messages_tokens = MagicMock(return_value=20)

        self.orch = MagicMock(name="orchestrator")
        self.orch.llm_client = self.old_llm
        self.orch.metrics = MagicMock()

        self.react_loop = MagicMock()
        self.react_loop.llm_client = self.old_llm
        self.orch.react_loop = self.react_loop

        self.consolidation_engine = MagicMock()
        self.consolidation_engine.llm_client = self.old_llm
        self.orch.consolidation_engine = self.consolidation_engine

        self.memory_retriever = MagicMock()
        self.memory_retriever.llm_client = self.old_llm
        self.orch.memory_retriever = self.memory_retriever

        # condenser 持有 llm_client + _token_counter（LLMSummaryCondenser 形态）
        self.condenser = MagicMock()
        self.condenser.llm_client = self.old_llm
        self.condenser._token_counter = self.old_llm.count_messages_tokens
        self.orch.condenser = self.condenser

        # session_mgr 用私有属性 _llm_client
        self.session_mgr = MagicMock()
        self.session_mgr._llm_client = self.old_llm
        self.orch.session_mgr = self.session_mgr

        self.config = {
            "llm": {
                "main_api_key": "new-key",
                "main_model": "new-model",
                "main_provider": "anthropic",
            }
        }

    def test_successful_reload_replaces_all_references(self):
        """成功重建后所有引用点都指向新 LLMClient"""
        with patch(
            "teage_liu.orchestrator.factories.create_llm_client",
            return_value=self.new_llm,
        ):
            result = _reload_llm_client(self.orch, self.config)

        self.assertTrue(result)
        # 顶层
        self.assertIs(self.orch.llm_client, self.new_llm)
        # react_loop
        self.assertIs(self.react_loop.llm_client, self.new_llm)
        # consolidation_engine
        self.assertIs(self.consolidation_engine.llm_client, self.new_llm)
        # memory_retriever
        self.assertIs(self.memory_retriever.llm_client, self.new_llm)
        # condenser
        self.assertIs(self.condenser.llm_client, self.new_llm)
        self.assertIs(self.condenser._token_counter, self.new_llm.count_messages_tokens)
        # session_mgr
        self.assertIs(self.session_mgr._llm_client, self.new_llm)

    def test_failed_reload_preserves_old_instance(self):
        """create_llm_client 抛异常时保留旧 LLMClient，返回 False"""
        with patch(
            "teage_liu.orchestrator.factories.create_llm_client",
            side_effect=RuntimeError("init failed"),
        ):
            result = _reload_llm_client(self.orch, self.config)

        self.assertFalse(result)
        # 顶层引用未变
        self.assertIs(self.orch.llm_client, self.old_llm)
        self.assertIs(self.react_loop.llm_client, self.old_llm)
        self.assertIs(self.consolidation_engine.llm_client, self.old_llm)
        self.assertIs(self.memory_retriever.llm_client, self.old_llm)
        self.assertIs(self.condenser.llm_client, self.old_llm)
        self.assertIs(self.session_mgr._llm_client, self.old_llm)

    def test_none_return_preserves_old_instance(self):
        """create_llm_client 返回 None（如 API Key 缺失）时保留旧实例"""
        with patch(
            "teage_liu.orchestrator.factories.create_llm_client",
            return_value=None,
        ):
            result = _reload_llm_client(self.orch, self.config)

        self.assertFalse(result)
        self.assertIs(self.orch.llm_client, self.old_llm)
        self.assertIs(self.react_loop.llm_client, self.old_llm)

    def test_missing_optional_components_does_not_crash(self):
        """部分可选组件未装配（None）时不崩溃"""
        # 移除 condenser 和 session_mgr
        self.orch.condenser = None
        self.orch.session_mgr = None
        # react_loop / consolidation_engine / memory_retriever 也设 None
        self.orch.react_loop = None
        self.orch.consolidation_engine = None
        self.orch.memory_retriever = None

        with patch(
            "teage_liu.orchestrator.factories.create_llm_client",
            return_value=self.new_llm,
        ):
            result = _reload_llm_client(self.orch, self.config)

        self.assertTrue(result)
        self.assertIs(self.orch.llm_client, self.new_llm)

    def test_masking_condenser_without_llm_client_skipped(self):
        """MaskingCondenser 无 llm_client 属性时跳过（hasattr 守护）"""
        # 模拟 MaskingCondenser：无 llm_client 属性
        masking_condenser = MagicMock()
        del masking_condenser.llm_client  # 删除属性使 hasattr 返回 False
        self.orch.condenser = masking_condenser

        with patch(
            "teage_liu.orchestrator.factories.create_llm_client",
            return_value=self.new_llm,
        ):
            result = _reload_llm_client(self.orch, self.config)

        self.assertTrue(result)
        # 顶层仍替换成功
        self.assertIs(self.orch.llm_client, self.new_llm)


# ===========================================================================
# 3. _apply_runtime_config LLMClient 集成测试
# ===========================================================================

class TestApplyRuntimeConfigLLMReload(unittest.TestCase):
    """验证 _apply_runtime_config 集成 LLMClient 热重载。"""

    def setUp(self):
        from teage_liu.server import app
        from teage_liu.app import get_orchestrator
        self._app = app
        self._get_orchestrator = get_orchestrator

    def _inject_orchestrator(self, mock_orch):
        self._app.dependency_overrides[self._get_orchestrator] = lambda: mock_orch

    def tearDown(self):
        self._app.dependency_overrides.pop(self._get_orchestrator, None)

    def test_llm_change_triggers_reload(self):
        """llm.main_api_key 变更时触发 LLMClient 重建"""
        old_llm = MagicMock(name="old")
        new_llm = MagicMock(name="new")
        new_llm.count_messages_tokens = MagicMock()

        orch = MagicMock()
        orch.llm_client = old_llm
        orch.react_loop = MagicMock()
        orch.react_loop.llm_client = old_llm
        orch.consolidation_engine = MagicMock()
        orch.consolidation_engine.llm_client = old_llm
        orch.memory_retriever = MagicMock()
        orch.memory_retriever.llm_client = old_llm
        orch.condenser = None
        orch.session_mgr = None
        orch.metrics = None
        self._inject_orchestrator(orch)

        old_config = {"llm": {"main_api_key": "k1", "main_model": "m1"}}
        new_config = {"llm": {"main_api_key": "k2", "main_model": "m1"}}

        with patch(
            "teage_liu.orchestrator.factories.create_llm_client",
            return_value=new_llm,
        ):
            applied = _apply_runtime_config(new_config, old_config=old_config)

        self.assertIn("llm.reload", applied)
        self.assertTrue(applied["llm.reload"])
        self.assertIs(orch.llm_client, new_llm)
        self.assertIs(orch.react_loop.llm_client, new_llm)

    def test_no_llm_change_does_not_trigger_reload(self):
        """llm 段未变更时不触发 LLMClient 重建（不调用 create_llm_client）"""
        orch = MagicMock()
        orch.llm_client = MagicMock()
        self._inject_orchestrator(orch)

        old_config = {"llm": {"main_api_key": "k1"}}
        new_config = {"llm": {"main_api_key": "k1"}}

        with patch(
            "teage_liu.orchestrator.factories.create_llm_client"
        ) as mock_create:
            applied = _apply_runtime_config(new_config, old_config=old_config)

        mock_create.assert_not_called()
        self.assertNotIn("llm.reload", applied)

    def test_old_config_none_does_not_trigger_reload(self):
        """old_config=None 时不触发 LLMClient 重建（向后兼容）"""
        orch = MagicMock()
        orch.llm_client = MagicMock()
        self._inject_orchestrator(orch)

        new_config = {"llm": {"main_api_key": "k1"}}

        with patch(
            "teage_liu.orchestrator.factories.create_llm_client"
        ) as mock_create:
            applied = _apply_runtime_config(new_config, old_config=None)

        mock_create.assert_not_called()
        self.assertNotIn("llm.reload", applied)

    def test_reload_failure_marks_applied_false(self):
        """LLMClient 重建失败时 applied['llm.reload']=False，保留旧实例"""
        old_llm = MagicMock(name="old")
        orch = MagicMock()
        orch.llm_client = old_llm
        orch.react_loop = None
        orch.consolidation_engine = None
        orch.memory_retriever = None
        orch.condenser = None
        orch.session_mgr = None
        orch.metrics = None
        self._inject_orchestrator(orch)

        old_config = {"llm": {"main_api_key": "k1"}}
        new_config = {"llm": {"main_api_key": "k2"}}

        with patch(
            "teage_liu.orchestrator.factories.create_llm_client",
            side_effect=RuntimeError("init failed"),
        ):
            applied = _apply_runtime_config(new_config, old_config=old_config)

        self.assertIn("llm.reload", applied)
        self.assertFalse(applied["llm.reload"])
        # 旧实例保留
        self.assertIs(orch.llm_client, old_llm)


# ===========================================================================
# 4. unmask_sensitive_config 边界条件
# ===========================================================================

class TestUnmaskSensitiveConfig(unittest.TestCase):
    """验证 unmask_sensitive_config 的边界处理。"""

    def test_masked_value_with_existing_real_value_restored(self):
        """existing 有真实值时用真实值替换脱敏值"""
        incoming = {"llm": {"main_api_key": "sk-1****abcd"}}
        existing = {"llm": {"main_api_key": "sk-1234567890abcd"}}
        result = unmask_sensitive_config(incoming, existing)
        self.assertEqual(result["llm"]["main_api_key"], "sk-1234567890abcd")

    def test_masked_value_with_empty_existing_cleared(self):
        """existing 无真实值时清空脱敏值（避免 **** 污染 .env）"""
        incoming = {"llm": {"main_api_key": "sk-1****abcd"}}
        existing = {"llm": {"main_api_key": ""}}
        result = unmask_sensitive_config(incoming, existing)
        self.assertEqual(result["llm"]["main_api_key"], "")

    def test_masked_value_with_missing_existing_cleared(self):
        """existing 中字段不存在时清空脱敏值"""
        incoming = {"llm": {"main_api_key": "sk-1****abcd"}}
        existing = {"llm": {}}
        result = unmask_sensitive_config(incoming, existing)
        self.assertEqual(result["llm"]["main_api_key"], "")

    def test_non_masked_value_preserved(self):
        """非脱敏值（用户实际修改的 API Key）原样保留"""
        incoming = {"llm": {"main_api_key": "sk-new-real-key-12345"}}
        existing = {"llm": {"main_api_key": "sk-old-key"}}
        result = unmask_sensitive_config(incoming, existing)
        self.assertEqual(result["llm"]["main_api_key"], "sk-new-real-key-12345")

    def test_empty_value_preserved(self):
        """空值原样保留（用户主动清空）"""
        incoming = {"llm": {"main_api_key": ""}}
        existing = {"llm": {"main_api_key": "sk-old"}}
        result = unmask_sensitive_config(incoming, existing)
        self.assertEqual(result["llm"]["main_api_key"], "")

    def test_all_sensitive_fields_processed(self):
        """所有 SENSITIVE_FIELDS 中的字段都被处理"""
        # 构造 incoming：所有敏感字段都为脱敏值
        incoming = {}
        for field_path in SENSITIVE_FIELDS:
            parts = field_path.split(".")
            d = incoming
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = "sk-****abcd"

        existing = {}
        for field_path in SENSITIVE_FIELDS:
            parts = field_path.split(".")
            d = existing
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = f"real-value-{field_path}"

        result = unmask_sensitive_config(incoming, existing)
        # 验证所有字段都被还原为真实值
        for field_path in SENSITIVE_FIELDS:
            parts = field_path.split(".")
            v = result
            for p in parts:
                v = v[p]
            self.assertEqual(v, f"real-value-{field_path}")

    def test_is_masked_value_detection(self):
        """is_masked_value 正确识别脱敏值"""
        self.assertTrue(is_masked_value("sk-****abcd"))
        self.assertTrue(is_masked_value("****"))
        self.assertFalse(is_masked_value("sk-real-key"))
        self.assertFalse(is_masked_value(""))
        self.assertFalse(is_masked_value(None))
        self.assertFalse(is_masked_value(123))

    def test_mask_api_key_round_trip(self):
        """mask_api_key → is_masked_value 识别 → unmask 还原"""
        original = "sk-1234567890abcdef"
        masked = mask_api_key(original)
        self.assertTrue(is_masked_value(masked))
        # unmask 时 existing 提供原始值
        incoming = {"llm": {"main_api_key": masked}}
        existing = {"llm": {"main_api_key": original}}
        result = unmask_sensitive_config(incoming, existing)
        self.assertEqual(result["llm"]["main_api_key"], original)


# ===========================================================================
# 5. LLMClient consolidation_api_key 回退逻辑
# ===========================================================================

class TestConsolidationApiKeyFallback(unittest.TestCase):
    """验证 LLMClient 在 consolidation_api_key 缺失时回退到 main_api_key。

    测试中需清除 provider 默认环境变量（如 DEEPSEEK_API_KEY），否则
    ``_resolve_api_key`` 会优先读取环境变量值，掩盖回退逻辑。
    """

    # 所有 provider 默认环境变量名（_PROVIDER_DEFAULT_ENV_KEY 的值集合）
    _PROVIDER_ENV_KEYS = [
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
        "LLM_MAIN_API_KEY", "LLM_CONSOLIDATION_API_KEY",
    ]

    def _clear_provider_env(self):
        """返回 patch.dict 上下文，临时清除所有 provider 环境变量。"""
        env_without_keys = {
            k: v for k, v in os.environ.items()
            if k not in self._PROVIDER_ENV_KEYS
        }
        return patch.dict(os.environ, env_without_keys, clear=True)

    def test_consolidation_falls_back_to_main_api_key(self):
        """consolidation_api_key 未设置时使用 main_api_key"""
        from teage_liu.llm.client import LLMClient
        config = {
            "llm": {
                "main_provider": "deepseek",
                "main_model": "deepseek-chat",
                "main_api_key": "sk-main-key-12345",
                # consolidation_api_key 故意缺失
                "consolidation_provider": "deepseek",
                "consolidation_model": "deepseek-chat",
            }
        }
        with self._clear_provider_env():
            client = LLMClient(config=config)
        self.assertEqual(client.main_api_key, "sk-main-key-12345")
        # 回退后 consolidation_api_key 应等于 main_api_key
        self.assertEqual(client.consolidation_api_key, "sk-main-key-12345")

    def test_consolidation_uses_explicit_value_when_set(self):
        """consolidation_api_key 显式设置时不回退"""
        from teage_liu.llm.client import LLMClient
        config = {
            "llm": {
                "main_provider": "deepseek",
                "main_model": "deepseek-chat",
                "main_api_key": "sk-main-key-12345",
                "consolidation_provider": "deepseek",
                "consolidation_model": "deepseek-chat",
                "consolidation_api_key": "sk-consol-key-67890",
            }
        }
        with self._clear_provider_env():
            client = LLMClient(config=config)
        self.assertEqual(client.main_api_key, "sk-main-key-12345")
        self.assertEqual(client.consolidation_api_key, "sk-consol-key-67890")

    def test_consolidation_no_fallback_when_main_also_empty(self):
        """main_api_key 缺失且无环境变量时 LLMClient 构造抛 ValueError。

        这验证了 ``_resolve_api_key`` 在 config 和 env 都为空时返回空串，
        触发 ``LLMClient.__init__`` 的必填校验（避免静默创建无效实例）。
        """
        from teage_liu.llm.client import LLMClient
        config = {
            "llm": {
                "main_provider": "deepseek",
                "main_model": "deepseek-chat",
                "main_api_key": "",
                "consolidation_provider": "deepseek",
                "consolidation_model": "deepseek-chat",
            }
        }
        with self._clear_provider_env():
            with self.assertRaises(ValueError) as ctx:
                LLMClient(config=config)
        self.assertIn("主对话 LLM API Key 未设置", str(ctx.exception))

    def test_consolidation_uses_provider_env_when_config_empty(self):
        """config 为空时 _resolve_api_key 回退到 provider 环境变量"""
        from teage_liu.llm.client import LLMClient
        config = {
            "llm": {
                "main_provider": "deepseek",
                "main_model": "deepseek-chat",
                "main_api_key": "",  # config 为空
                "consolidation_provider": "deepseek",
                "consolidation_model": "deepseek-chat",
            }
        }
        # 仅设置 DEEPSEEK_API_KEY 环境变量
        env_with_key = {**os.environ, "DEEPSEEK_API_KEY": "sk-from-env-12345"}
        # 清除其他 provider key 避免干扰
        for k in self._PROVIDER_ENV_KEYS:
            if k != "DEEPSEEK_API_KEY":
                env_with_key.pop(k, None)
        with patch.dict(os.environ, env_with_key, clear=True):
            client = LLMClient(config=config)
        self.assertEqual(client.main_api_key, "sk-from-env-12345")
        # consolidation 也通过 env 解析得到（优先于 main_api_key 回退）
        self.assertEqual(client.consolidation_api_key, "sk-from-env-12345")


if __name__ == "__main__":
    unittest.main()
