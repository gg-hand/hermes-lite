"""配置加载工具。

读取 YAML 配置文件并自动解析 ${ENV_VAR} 形式的占位符为对应环境变量值。

Phase 9 优化：添加基于文件修改时间的缓存，避免在 /metrics、/health 等
高频端点上重复读盘解析。缓存通过文件 mtime 自动失效，也支持显式清除。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

# 自动加载 .env 文件（如存在），使 ${ENV_VAR} 占位符能解析其中的变量
# 在 shell 脚本（start.sh/restart.sh）中已通过 source .env 加载，
# 此处作为 Python 层兜底，确保直接通过 python -m uvicorn 启动时也能读取 .env
load_dotenv()

# 匹配 ${ENV_VAR} 形式的占位符
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_value(value: Any) -> Any:
    """递归解析配置项中的 ${ENV_VAR} 占位符。

    - 字符串类型：若整体匹配单个占位符且环境变量存在，则返回环境变量原值
      （保留类型可能性，例如纯数字字符串仍以字符串返回）；
      若环境变量不存在则返回空字符串。
    - 其他类型（int/list/dict 等）递归处理其子元素。
    """
    if isinstance(value, str):
        # 整体匹配单个占位符：直接返回环境变量值（缺失时为空串）
        full_match = _ENV_VAR_PATTERN.fullmatch(value)
        if full_match:
            env_name = full_match.group(1)
            return os.environ.get(env_name, "")

        # 部分匹配：逐个替换占位符
        def _sub(match: re.Match) -> str:
            env_name = match.group(1)
            return os.environ.get(env_name, "")

        return _ENV_VAR_PATTERN.sub(_sub, value)

    if isinstance(value, dict):
        return {k: _resolve_value(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_resolve_value(item) for item in value]

    return value


# ---------------------------------------------------------------------------
# 配置缓存（Phase 9）：基于文件 mtime，避免高频读盘解析
# ---------------------------------------------------------------------------
_config_cache: Optional[dict] = None
_config_cache_mtime: float = 0.0
_config_cache_path: Optional[str] = None


# ---------------------------------------------------------------------------
# 环境变量校验
# ---------------------------------------------------------------------------

# 关键 API Key 配置路径——缺失时阻止启动
_CRITICAL_API_KEY_PATHS = [
    "llm.main_api_key",
]

# 重要但可降级的 API Key 配置路径——缺失时仅警告
_WARN_API_KEY_PATHS = [
    "llm.consolidation_api_key",
    "security.api_key",
]


def _get_nested(config: dict, path: str) -> Any:
    """从嵌套字典中按点分隔路径取值。"""
    parts = path.split(".")
    current: Any = config
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def validate_required_env_vars(config: dict) -> None:
    """校验配置中所有 API Key 字段是否已解析为有效值。

    在 ``${ENV_VAR}`` 占位符被 ``_resolve_value`` 解析后调用。
    若占位符对应的环境变量不存在，解析结果为空字符串。

    对 ``_CRITICAL_API_KEY_PATHS`` 中的字段：为空时抛出
    ``ValueError``，阻止服务启动。
    对 ``_WARN_API_KEY_PATHS`` 中的字段：为空时仅记录 warning。

    参数:
        config: 已解析的配置字典。

    异常:
        ValueError: 关键 API Key 缺失时抛出。
    """
    missing_critical = []
    for path in _CRITICAL_API_KEY_PATHS:
        value = _get_nested(config, path)
        if not value:
            missing_critical.append(path)

    if missing_critical:
        raise ValueError(
            f"关键 API Key 未配置（环境变量缺失）：{', '.join(missing_critical)}。\n"
            "请设置对应的环境变量或在 config.yaml 中使用 ${VAR_NAME} 占位符。"
        )

    for path in _WARN_API_KEY_PATHS:
        value = _get_nested(config, path)
        if not value:
            import logging
            logging.getLogger(__name__).warning(
                "API Key 未配置（可选）: %s。若不使用对应功能可忽略此警告。", path
            )


def clear_config_cache() -> None:
    """显式清除配置缓存（由 PUT /config 端点调用）。"""
    global _config_cache, _config_cache_mtime, _config_cache_path
    _config_cache = None
    _config_cache_mtime = 0.0
    _config_cache_path = None


# ---------------------------------------------------------------------------
# LLM 超时配置默认值（spec: async-llm-backend）
# ---------------------------------------------------------------------------
# per-token 活跃超时：LLM 在此秒数内未返回任何 token 则视为卡死并中断
_LLM_ACTIVITY_TIMEOUT_DEFAULT: float = 60.0
# 流式总超时：兜底整个流式调用（含工具执行时间）的最大时长
_LLM_STREAM_TOTAL_TIMEOUT_DEFAULT: float = 300.0


def get_llm_timeouts(config: dict) -> tuple[float, float]:
    """从配置字典读取 LLM 超时配置，缺失时返回默认值（向后兼容旧 config）。

    读取 ``llm.activity_timeout`` 与 ``llm.stream_total_timeout`` 两个数值字段
    （单位秒，int 或 float 均可）。字段缺失或 ``llm`` 段不存在时使用默认值
    (60.0, 300.0)，保证旧配置文件无需修改即可加载。

    本函数只做读取与类型归一化（统一转 float），不做范围校验；非法类型
    （如字符串）会抛出 ``ValueError``/``TypeError``，交由调用方处理。

    参数:
        config: 已由 :func:`load_config` 解析的配置字典。

    返回:
        ``(activity_timeout, stream_total_timeout)`` 元组，单位秒。
    """
    llm_cfg = config.get("llm") or {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    activity_timeout = float(
        llm_cfg.get("activity_timeout", _LLM_ACTIVITY_TIMEOUT_DEFAULT)
    )
    stream_total_timeout = float(
        llm_cfg.get("stream_total_timeout", _LLM_STREAM_TOTAL_TIMEOUT_DEFAULT)
    )
    return activity_timeout, stream_total_timeout


def load_config(config_path: str = "config.yaml") -> dict:
    """读取 YAML 配置文件并返回解析后的 dict。

    自动解析配置中所有 ${ENV_VAR} 形式的占位符为环境变量值。

    参数:
        config_path: 配置文件路径，可为相对路径或绝对路径。
                     相对路径基于当前工作目录解析。

    返回:
        解析后的配置字典。

    异常:
        FileNotFoundError: 配置文件不存在。
        yaml.YAMLError: 配置文件格式错误。
    """
    # Phase 9 缓存检查：基于文件 mtime 自动失效
    global _config_cache, _config_cache_mtime, _config_cache_path

    config_file = Path(config_path)
    if not config_file.is_absolute():
        config_file = Path.cwd() / config_file

    try:
        current_mtime = config_file.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    # 缓存命中条件：同一文件路径且 mtime 未变
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
        raise ValueError(f"配置文件根节点必须是字典，实际类型: {type(raw_config).__name__}")

    resolved = _resolve_value(raw_config)

    # 更新缓存
    _config_cache = resolved
    _config_cache_mtime = current_mtime
    _config_cache_path = str(config_file)

    return resolved
