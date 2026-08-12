"""配置加载(搬自老系统 teage_liu/config.py,裁剪掉热更新边界等后置项)。

保留:
- ${ENV_VAR} 占位符递归解析
- 基于文件 mtime 的缓存(高频端点不重复读盘)
- 敏感字段分离(实际值写 .env,config.yaml 保留占位符)+ GET 脱敏 / PUT 还原
- 关键 API Key 启动校验(llm.main_api_key 缺失阻止启动)
- LLM 超时默认值(activity_timeout / stream_total_timeout)
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

# .env 兜底加载(override=True 保证 PUT /config 写入 .env 的新 Key 重启后生效)
load_dotenv(override=True)

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_value(value: Any) -> Any:
    """递归解析配置项中的 ${ENV_VAR} 占位符。

    - 字符串整体匹配单个占位符:返回环境变量原值(缺失时为空串)
    - 部分匹配:逐个替换
    - dict/list 递归处理
    """
    if isinstance(value, str):
        full_match = _ENV_VAR_PATTERN.fullmatch(value)
        if full_match:
            env_name = full_match.group(1)
            return os.environ.get(env_name, "")
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    if isinstance(value, dict):
        return {k: _resolve_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 配置缓存:基于文件 mtime 自动失效
# ---------------------------------------------------------------------------
_config_cache: Optional[dict] = None
_config_cache_mtime: float = 0.0
_config_cache_path: Optional[str] = None


def clear_config_cache() -> None:
    """显式清除配置缓存(由 PUT /config 端点调用)。"""
    global _config_cache, _config_cache_mtime, _config_cache_path
    _config_cache = None
    _config_cache_mtime = 0.0
    _config_cache_path = None


# ---------------------------------------------------------------------------
# 敏感字段分离:PUT /config 时将实际值写入 .env,config.yaml 保留占位符
# ---------------------------------------------------------------------------
SENSITIVE_FIELDS: dict[str, str] = {
    "llm.main_api_key": "LLM_MAIN_API_KEY",
    "llm.consolidation_api_key": "LLM_CONSOLIDATION_API_KEY",
    "security.api_key": "TEAGE_API_KEY",
    "files.ocr.vision_llm.api_key": "FILES_OCR_VISION_LLM_API_KEY",
    "web_search.bing_api_key": "BING_API_KEY",
    "web_search.baidu_api_key": "BAIDU_API_KEY",
}

_MASK_SENTINEL = "****"


def _get_nested(config: dict, path: str) -> Any:
    """按点分隔路径取值。"""
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_nested(config: dict, path: str, value: Any) -> None:
    """按点分隔路径设值。"""
    keys = path.split(".")
    for key in keys[:-1]:
        if key not in config or not isinstance(config[key], dict):
            config[key] = {}
        config = config[key]
    config[keys[-1]] = value


def _update_env_file(env_path: str, updates: dict[str, str]) -> None:
    """更新 .env 文件(追加或覆盖对应行),并同步 os.environ。

    同步 os.environ 是关键:热重载时 load_config 解析占位符读取 os.environ,
    若仅写文件不更新环境变量,热重载后仍用旧值。
    """
    path = Path(env_path)
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    existing_keys = {line.split("=")[0] for line in lines if "=" in line}
    for key, value in updates.items():
        if key in existing_keys:
            lines = [
                f"{key}={value}" if line.startswith(f"{key}=") else line
                for line in lines
            ]
        else:
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value


def mask_api_key(value: str) -> str:
    """脱敏:仅保留首尾若干字符用于辨别是否已设置。"""
    if not value:
        return ""
    if len(value) <= 8:
        return _MASK_SENTINEL
    return value[:4] + _MASK_SENTINEL + value[-4:]


def is_masked_value(value: Any) -> bool:
    """判断是否为脱敏占位(含 **** 标记)。"""
    return isinstance(value, str) and _MASK_SENTINEL in value


def mask_sensitive_config(config: dict) -> dict:
    """对配置中所有敏感字段脱敏(原地修改并返回)。"""
    for field_path in SENSITIVE_FIELDS:
        actual = _get_nested(config, field_path)
        if actual:
            _set_nested(config, field_path, mask_api_key(str(actual)))
    return config


def unmask_sensitive_config(incoming: dict, existing: dict) -> dict:
    """将前端提交的脱敏敏感字段还原为服务端实际值(原地修改并返回)。

    现有值不存在时清空脱敏值,避免把无效的 '****' 写入 .env 污染配置。
    """
    for field_path in SENSITIVE_FIELDS:
        incoming_val = _get_nested(incoming, field_path)
        if incoming_val and is_masked_value(incoming_val):
            existing_val = _get_nested(existing, field_path)
            if existing_val:
                _set_nested(incoming, field_path, existing_val)
            else:
                _set_nested(incoming, field_path, "")
    return incoming


def write_config_with_sensitive_separation(
    new_config: dict, config_path: str, env_path: str
) -> None:
    """非敏感字段写 config.yaml,敏感字段实际值写 .env。"""
    from copy import deepcopy

    config_to_write = deepcopy(new_config)
    env_updates: dict[str, str] = {}
    for field_path, env_var in SENSITIVE_FIELDS.items():
        actual_value = _get_nested(config_to_write, field_path)
        if actual_value and not str(actual_value).startswith("${"):
            env_updates[env_var] = str(actual_value)
            _set_nested(config_to_write, field_path, f"${{{env_var}}}")
    if env_updates:
        _update_env_file(env_path, env_updates)
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config_path).write_text(
        yaml.dump(config_to_write, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 启动校验
# ---------------------------------------------------------------------------
_CRITICAL_API_KEY_PATHS = ["llm.main_api_key"]


def validate_required_env_vars(config: dict) -> None:
    """关键 API Key 缺失时抛 ValueError 阻止启动;可选缺失仅告警。"""
    import logging

    logger = logging.getLogger(__name__)
    missing = [p for p in _CRITICAL_API_KEY_PATHS if not _get_nested(config, p)]
    if missing:
        raise ValueError(
            f"关键 API Key 未配置(环境变量缺失):{', '.join(missing)}。\n"
            "请设置对应的环境变量或在 config.yaml 中使用 ${VAR_NAME} 占位符。"
        )
    for path in ("llm.consolidation_api_key", "security.api_key"):
        if not _get_nested(config, path):
            logger.warning("API Key 未配置(可选): %s。若不使用对应功能可忽略。", path)


# ---------------------------------------------------------------------------
# LLM 超时默认值
# ---------------------------------------------------------------------------
_LLM_ACTIVITY_TIMEOUT_DEFAULT: float = 60.0
_LLM_STREAM_TOTAL_TIMEOUT_DEFAULT: float = 300.0


def get_llm_timeouts(config: dict) -> tuple[float, float]:
    """读取 llm.activity_timeout / llm.stream_total_timeout,缺失时返回默认值。"""
    llm_cfg = config.get("llm") or {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    activity_timeout = float(llm_cfg.get("activity_timeout", _LLM_ACTIVITY_TIMEOUT_DEFAULT))
    stream_total_timeout = float(
        llm_cfg.get("stream_total_timeout", _LLM_STREAM_TOTAL_TIMEOUT_DEFAULT)
    )
    return activity_timeout, stream_total_timeout


def load_config(config_path: str = "config.yaml") -> dict:
    """读取 YAML 配置文件并返回解析后的 dict(mtime 缓存)。

    异常:
        FileNotFoundError: 配置文件不存在
        yaml.YAMLError: 配置文件格式错误
    """
    global _config_cache, _config_cache_mtime, _config_cache_path

    config_file = Path(config_path)
    if not config_file.is_absolute():
        config_file = Path.cwd() / config_file

    try:
        current_mtime = config_file.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    if (
        _config_cache is not None
        and _config_cache_path == str(config_file)
        and current_mtime > 0
        and current_mtime == _config_cache_mtime
    ):
        return _config_cache

    if not config_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_file}")

    with config_file.open("r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError(f"配置文件根节点必须是字典,实际类型: {type(raw_config).__name__}")

    resolved = _resolve_value(raw_config)
    _config_cache = resolved
    _config_cache_mtime = current_mtime
    _config_cache_path = str(config_file)
    return resolved
