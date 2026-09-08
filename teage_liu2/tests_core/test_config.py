"""配置严格校验专项测试(F1/F2,计划 §6.7)。

core_config_from:类型 + 范围 + **未知键拒绝**(防 typo 静默失效,
老系统踩过的坑);失败抛 ValueError = 启动失败。
"""

from __future__ import annotations

import pytest

from teage_liu2.core.config import CoreConfig, core_config_from


def test_defaults_when_core_missing():
    """验收:core 段缺失 → 全部默认值。"""
    cfg = core_config_from({})
    assert isinstance(cfg, CoreConfig)
    assert cfg.mode == "loop"
    assert cfg.max_loops == 50
    assert cfg.system_prompt is None
    assert cfg.hook_timeout == 5.0
    assert cfg.history_window_messages == 100
    assert cfg.injection_budget_chars is None
    assert cfg.branches == {}


def test_values_filled():
    """验收:合法值正确填充。"""
    cfg = core_config_from({"core": {
        "mode": "bare",
        "max_loops": 10,
        "system_prompt": "助手",
        "hook_timeout": 3.0,
        "history_window_messages": 200,
        "branches": {"guardrails": {"enabled": True}},
    }})
    assert cfg.mode == "bare"
    assert cfg.max_loops == 10
    assert cfg.system_prompt == "助手"
    assert cfg.hook_timeout == 3.0
    assert cfg.history_window_messages == 200
    assert cfg.branches == {"guardrails": {"enabled": True}}


def test_unknown_key_rejected():
    """验收:未知键拒绝(可读错误列未知键)—— 防 typo 静默失效。"""
    with pytest.raises(ValueError, match="modex"):
        core_config_from({"core": {"modex": "loop"}})


def test_unknown_mode_rejected():
    """验收:mode 未知值拒绝(与 E6 形态校验一致)。"""
    with pytest.raises(ValueError, match="mode"):
        core_config_from({"core": {"mode": "loopx"}})


def test_type_rejected():
    """验收:类型错误拒绝。"""
    with pytest.raises(ValueError, match="max_loops"):
        core_config_from({"core": {"max_loops": "五十"}})
    with pytest.raises(ValueError, match="hook_timeout"):
        core_config_from({"core": {"hook_timeout": "5s"}})
    with pytest.raises(ValueError, match="system_prompt"):
        core_config_from({"core": {"system_prompt": 123}})


def test_range_rejected():
    """验收:范围错误拒绝。"""
    with pytest.raises(ValueError, match="max_loops"):
        core_config_from({"core": {"max_loops": 0}})
    with pytest.raises(ValueError, match="history_window_messages"):
        core_config_from({"core": {"history_window_messages": -1}})
    with pytest.raises(ValueError, match="hook_timeout"):
        core_config_from({"core": {"hook_timeout": 0}})


def test_injection_budget_validation():
    """验收:分层预算 —— 合法层名通过,未知层名拒绝。"""
    cfg = core_config_from({"core": {"injection_budget_chars": {"PREFIX": 3000}}})
    assert cfg.injection_budget_chars == {"PREFIX": 3000}
    with pytest.raises(ValueError, match="WEIRD"):
        core_config_from({"core": {"injection_budget_chars": {"WEIRD": 100}}})


def test_non_dict_cfg_rejected():
    """验收(P3-3):配置根非映射 → ValueError(而非 AttributeError 崩溃)。"""
    with pytest.raises(ValueError, match="配置根"):
        core_config_from(["not", "dict"])
    assert core_config_from(None).mode == "loop"  # None 兼容默认值


def test_branches_must_be_dict():
    """验收:branches 段类型校验。"""
    with pytest.raises(ValueError, match="branches"):
        core_config_from({"core": {"branches": ["guardrails"]}})

def test_extensions_root_default_and_validation():
    """验收(2026-09-08 扩展目录树):extensions_root 默认值 + 类型校验。"""
    assert core_config_from({}).extensions_root == "data2/extensions"
    assert core_config_from(
        {"core": {"extensions_root": "d:/my_extensions"}}
    ).extensions_root == "d:/my_extensions"
    with pytest.raises(ValueError, match="extensions_root"):
        core_config_from({"core": {"extensions_root": ""}})
    with pytest.raises(ValueError, match="extensions_root"):
        core_config_from({"core": {"extensions_root": 123}})
