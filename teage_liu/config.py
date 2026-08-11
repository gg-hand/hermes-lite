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

# 自动加载 .env 文件（如存在），使 ${ENV_VAR} 占位符能解析其中的变量。
# 在 shell 脚本（start.sh/restart.sh）中已通过 source .env 加载，
# 此处作为 Python 层兜底，确保直接通过 python -m uvicorn 启动时也能读取 .env。
# override=True：以 .env 文件为准覆盖已存在的环境变量，确保前端通过 PUT /config
# 写入 .env 的新 API Key 在重启后能正确生效（否则会被系统旧环境变量屏蔽）。
load_dotenv(override=True)

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


# ---------------------------------------------------------------------------
# 敏感字段分离（Task 5）：PUT /config 时将实际值写入 .env，config.yaml 保留占位符
# ---------------------------------------------------------------------------

# 配置路径 → 环境变量名 的映射
SENSITIVE_FIELDS: dict[str, str] = {
    "llm.main_api_key": "LLM_MAIN_API_KEY",
    "llm.consolidation_api_key": "LLM_CONSOLIDATION_API_KEY",
    "security.api_key": "TEAGE_API_KEY",
    "files.ocr.vision_llm.api_key": "FILES_OCR_VISION_LLM_API_KEY",
    "web_search.bing_api_key": "BING_API_KEY",
    "web_search.baidu_api_key": "BAIDU_API_KEY",
}


def _set_nested(config: dict, path: str, value: Any) -> None:
    """设置嵌套配置值（点分隔路径）。"""
    keys = path.split(".")
    for key in keys[:-1]:
        if key not in config or not isinstance(config[key], dict):
            config[key] = {}
        config = config[key]
    config[keys[-1]] = value


def _update_env_file(env_path: str, updates: dict[str, str]) -> None:
    """更新 .env 文件（追加或覆盖对应行），并同步更新 os.environ。

    同步 os.environ 是关键：热重载时 ``load_config`` 解析 ``${ENV_VAR}``
    占位符读取 ``os.environ``，若仅写文件不更新环境变量，热重载后 LLM
    客户端仍用旧值（``load_dotenv`` 只在启动时调用一次）。
    """
    from pathlib import Path
    path = Path(env_path)
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    existing_keys = {line.split("=")[0] for line in lines if "=" in line}
    for key, value in updates.items():
        if key in existing_keys:
            lines = [f"{key}={value}" if line.startswith(f"{key}=") else line
                     for line in lines]
        else:
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 同步更新 os.environ，确保热重载时占位符解析读到新值
    for key, value in updates.items():
        os.environ[key] = value


# ---------------------------------------------------------------------------
# 敏感字段脱敏（Task 23）：GET /config 时对前端脱敏，防止 DevTools 窃取
# ---------------------------------------------------------------------------

# 脱敏掩码标记——用于检测 PUT 请求中是否传来脱敏值
_MASK_SENTINEL = "****"


def mask_api_key(value: str) -> str:
    """将 API Key 脱敏，仅保留首尾若干字符用于辨别是否已设置。

    规则:
    - 空值 → 空字符串
    - 长度 ≤ 8 → 全部掩为 ``****``
    - 正常 Key → ``前缀4字符 + **** + 后缀4字符``（如 ``sk-****abcd``）

    参数:
        value: 原始 API Key。

    返回:
        脱敏后的字符串。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return _MASK_SENTINEL
    return value[:4] + _MASK_SENTINEL + value[-4:]


def is_masked_value(value: Any) -> bool:
    """判断前端提交的值是否为脱敏占位（含 ``****`` 标记）。

    用于 PUT /config 识别前端未修改的脱敏字段，从而保留服务端原始值。
    """
    return isinstance(value, str) and _MASK_SENTINEL in value


def mask_sensitive_config(config: dict) -> dict:
    """对配置字典中的所有敏感字段进行脱敏处理（原地修改并返回）。

    遍历 ``SENSITIVE_FIELDS`` 中定义的路径，对每个非空值调用
    :func:`mask_api_key` 替换为脱敏形态。

    参数:
        config: 原始配置字典（会被原地修改）。

    返回:
        脱敏后的配置字典（与入参为同一对象）。
    """
    for field_path in SENSITIVE_FIELDS:
        actual = _get_nested(config, field_path)
        if actual:
            _set_nested(config, field_path, mask_api_key(str(actual)))
    return config


def unmask_sensitive_config(
    incoming: dict, existing: dict
) -> dict:
    """将前端提交的脱敏敏感字段还原为服务端实际值（原地修改并返回）。

    遍历 ``SENSITIVE_FIELDS``，若 incoming 中的值为脱敏形态（``****``）：
    - existing 中有真实值 → 用真实值替换脱敏值；
    - existing 中无真实值（字段不存在或为空）→ 清空 incoming 中的脱敏值
      （设为 ``""``），避免把无效的脱敏值（含 ``****``）当作真实值写入
      ``.env`` 污染配置。

    参数:
        incoming: 前端 PUT 提交的配置字典（会被原地修改）。
        existing: 当前服务端的实际配置字典。

    返回:
        还原后的 incoming 字典（与入参为同一对象）。
    """
    for field_path in SENSITIVE_FIELDS:
        incoming_val = _get_nested(incoming, field_path)
        if incoming_val and is_masked_value(incoming_val):
            existing_val = _get_nested(existing, field_path)
            if existing_val:
                _set_nested(incoming, field_path, existing_val)
            else:
                # existing 中无真实值，脱敏值本身无效（含 ****），
                # 清空避免写入 .env 污染（write_config 中 actual_value=""
                # 是 falsy 会跳过写入，保留 config.yaml 中的占位符）
                _set_nested(incoming, field_path, "")
    return incoming


def write_config_with_sensitive_separation(
    new_config: dict, config_path: str, env_path: str
) -> None:
    """非敏感字段写 config.yaml，敏感字段实际值写 .env。

    遍历 ``SENSITIVE_FIELDS`` 中的配置路径，若值为实际值（非 ``${...}``
    占位符、非空），则将其写入 .env 文件并在 config.yaml 中替换为
    ``${ENV_VAR}`` 占位符。已是占位符的值保持不变。

    参数:
        new_config: 待写入的配置字典（不会被修改，内部 deepcopy）。
        config_path: config.yaml 输出路径。
        env_path: .env 输出路径。
    """
    from copy import deepcopy
    import yaml as _yaml
    config_to_write = deepcopy(new_config)
    env_updates: dict[str, str] = {}

    for field_path, env_var in SENSITIVE_FIELDS.items():
        actual_value = _get_nested(config_to_write, field_path)
        if actual_value and not str(actual_value).startswith("${"):
            env_updates[env_var] = str(actual_value)
            _set_nested(config_to_write, field_path, f"${{{env_var}}}")

    if env_updates:
        _update_env_file(env_path, env_updates)

    from pathlib import Path
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config_path).write_text(
        _yaml.dump(config_to_write, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


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
