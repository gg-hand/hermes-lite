"""Task 3: 验证 config_helpers 模块可独立导入且函数行为正确。"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "hermes")


def test_deep_merge_config_recursive():
    """dict + dict 递归合并，入参不被修改。"""
    from hermes.config_helpers import _deep_merge_config
    old = {"a": {"b": 1, "c": 2}, "d": 3}
    new = {"a": {"c": 20, "e": 30}}
    merged = _deep_merge_config(old, new)
    assert merged == {"a": {"b": 1, "c": 20, "e": 30}, "d": 3}
    assert old == {"a": {"b": 1, "c": 2}, "d": 3}
    assert new == {"a": {"c": 20, "e": 30}}


def test_deep_merge_config_new_key_overrides():
    """新增 key 直接写入。"""
    from hermes.config_helpers import _deep_merge_config
    merged = _deep_merge_config({"x": 1}, {"y": 2})
    assert merged == {"x": 1, "y": 2}


def test_validate_config_schema_rejects_non_dict_root():
    """非 dict 根节点拒绝。"""
    from hermes.config_helpers import _validate_config_schema
    with pytest.raises(ValueError, match="配置校验失败"):
        _validate_config_schema([1, 2, 3])


def test_validate_config_schema_rejects_bad_llm_model():
    """llm.main_model 为空字符串拒绝。"""
    from hermes.config_helpers import _validate_config_schema
    with pytest.raises(ValueError, match="main_model"):
        _validate_config_schema({"llm": {"main_model": "", "consolidation_model": "ok"}})


def test_check_needs_restart_detects_llm_change():
    """llm.main_model 变更需重启。"""
    from hermes.config_helpers import _check_needs_restart
    old = {"llm": {"main_model": "a", "consolidation_model": "b"}}
    new = {"llm": {"main_model": "c", "consolidation_model": "b"}}
    assert _check_needs_restart(old, new) is True


def test_check_needs_restart_no_change():
    """无变更不需重启。"""
    from hermes.config_helpers import _check_needs_restart
    old = {"llm": {"main_model": "a"}}
    new = {"llm": {"main_model": "a"}}
    assert _check_needs_restart(old, new) is False


def test_constants_exist():
    """_RESTART_REQUIRED_KEYS 与 _RUNTIME_HOTUPDATE_MAP 常量存在。"""
    from hermes.config_helpers import _RESTART_REQUIRED_KEYS, _RUNTIME_HOTUPDATE_MAP, _MISSING
    assert isinstance(_RESTART_REQUIRED_KEYS, (list, tuple, set))
    assert isinstance(_RUNTIME_HOTUPDATE_MAP, dict)
    assert _MISSING is not None


def test_backup_config_no_exist(tmp_path):
    """配置文件不存在时备份不报错。"""
    from hermes.config_helpers import _backup_config
    _backup_config(str(tmp_path / "nonexistent.yaml"))


def test_atomic_write_config(tmp_path):
    """原子写入配置文件。"""
    from hermes.config_helpers import _atomic_write_config
    import yaml
    cfg_path = str(tmp_path / "test_config.yaml")
    data = {"llm": {"main_model": "test"}}
    _atomic_write_config(cfg_path, data)
    with open(cfg_path, encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    assert loaded == data
