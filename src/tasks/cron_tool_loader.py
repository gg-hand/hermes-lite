"""cron_tool 加载器与子进程执行器（Phase 8 Task 5.2）。

本模块实现 cron_tool 的「加载 TOOL.md → 子进程执行 run.*」链路，是 Layer 2
能力扩展层的核心运行时。

核心 API：
- :func:`load_tool`：解析 ``cron_tool/{name}/TOOL.md`` 的 frontmatter，返回
  :class:`CronToolMeta`（含 name/version/description/input_schema/timeout 等）。
- :func:`execute_tool`：通过 ``subprocess.run`` 调用 ``cron_tool/{name}/run.*``，
  stdin 传 JSON（input + context），stdout 收 JSON（result/error）。

设计要点：
- **子进程执行（非 import）**：语言无关，崩溃隔离。run.* 脚本崩溃不影响主进程，
  返回结构化错误 JSON（含 ``error_type`` / ``stderr``）。
- **按扩展名识别解释器**：``run.py`` → python，``run.sh`` → bash，
  ``run.js`` → node。其他扩展名返回错误。
- **超时终止**：使用 ``subprocess.run`` 的 ``timeout`` 参数，超时抛
  ``TimeoutExpired``，捕获后返回结构化超时错误。
- **TOOL.md 解析**：复用 yaml frontmatter 解析风格（与 skills/SKILL.md 一致），
  frontmatter 字段：``name / version / description / author / input_schema /
  timeout``。
- **不依赖全局对象**：纯函数式 API，``load_tool`` / ``execute_tool`` 均接受
  ``base_dir`` 参数（默认 ``cron_tool``），便于测试隔离。

缓存约束：
- 本模块不修改全局 ToolRegistry，cron_tool 仅在 cron 调度会话内可见
  （由 :class:`CronToolRegistry` 单独管理，SubTask 5.3）。
- 用户会话的 tools schema 字节级稳定（缓存硬约束 1）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - yaml 为项目硬依赖
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: cron_tool 根目录名（相对项目根）
DEFAULT_BASE_DIR = "cron_tool"

#: 默认子进程超时秒数（TOOL.md 的 timeout 字段可覆盖）
DEFAULT_TIMEOUT = 30

#: 支持的 run.* 扩展名 → 解释器映射
#: 顺序优先：python（项目运行时已有）> bash > node
_RUN_INTERPRETERS: Dict[str, list] = {
    ".py": [sys.executable or "python"],
    ".sh": ["bash"],
    ".js": ["node"],
}

#: TOOL.md 必填 frontmatter 字段
_REQUIRED_FRONTMATTER_FIELDS = ("name", "version", "description", "input_schema")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class CronToolMeta:
    """cron_tool 元数据（由 TOOL.md frontmatter 解析得到）。

    属性:
        name: 工具名（唯一标识，与目录名一致）。
        version: 语义化版本（如 ``1.0.0``）。
        description: 工具描述（展示给 LLM）。
        author: 作者（``llm-generated`` 或用户名）。
        input_schema: Anthropic tool use 格式的输入 JSON Schema。
        timeout: 子进程超时秒数。``None`` 时使用 :data:`DEFAULT_TIMEOUT`。
        tool_dir: 工具目录绝对路径（``cron_tool/{name}/``）。
        run_script: run.* 脚本的绝对路径（``{tool_dir}/run.*``）。
        run_interpreter: 解释器命令列表（如 ``["python"]``）。
    """

    name: str
    version: str
    description: str
    author: str
    input_schema: Dict[str, Any]
    timeout: Optional[int] = None
    tool_dir: str = ""
    run_script: str = ""
    run_interpreter: list = field(default_factory=list)

    def get_timeout(self, default: int = DEFAULT_TIMEOUT) -> int:
        """返回生效的超时秒数。

        ``timeout`` 为 ``None`` 或非正数时回退到 ``default``。
        """
        if self.timeout is None or self.timeout <= 0:
            return default
        return int(self.timeout)

    def to_schema(self) -> Dict[str, Any]:
        """返回 Anthropic tool use 格式的 schema dict。

        供 :class:`CronToolRegistry.get_tools_schema` 使用。
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class CronToolError(Exception):
    """cron_tool 加载或执行错误的基类。"""


class CronToolNotFoundError(CronToolError):
    """指定 cron_tool 不存在（目录或 TOOL.md 缺失）。"""


class CronToolParseError(CronToolError):
    """TOOL.md frontmatter 解析失败或必填字段缺失。"""


class CronToolRunScriptNotFoundError(CronToolError):
    """工具目录下未找到 run.* 脚本。"""


# ---------------------------------------------------------------------------
# 加载 TOOL.md
# ---------------------------------------------------------------------------


def load_tool(
    name: str, base_dir: str = DEFAULT_BASE_DIR
) -> CronToolMeta:
    """解析 ``cron_tool/{name}/TOOL.md``，返回 :class:`CronToolMeta`。

    解析流程：
    1. 校验 ``name``（非空、不含路径分隔符，防止路径穿越）。
    2. 定位工具目录 ``{base_dir}/{name}/``，不存在抛
       :class:`CronToolNotFoundError`。
    3. 读取 ``TOOL.md``，解析 yaml frontmatter（``---`` 分隔）。
    4. 校验必填字段（name/version/description/input_schema）。
    5. 定位 ``run.*`` 脚本并识别解释器。

    参数:
        name: cron_tool 名称（与目录名一致）。
        base_dir: cron_tool 根目录，默认 :data:`DEFAULT_BASE_DIR`。
            支持相对路径（相对当前工作目录）与绝对路径。

    返回:
        :class:`CronToolMeta` 实例。

    Raises:
        CronToolNotFoundError: 工具目录不存在。
        CronToolParseError: TOOL.md 缺失或 frontmatter 解析失败 / 必填字段缺失。
        CronToolRunScriptNotFoundError: 工具目录下未找到 run.* 脚本。
    """
    # 1. 校验 name（防路径穿越）
    _validate_tool_name(name)

    # 2. 定位工具目录
    tool_dir = Path(base_dir) / name
    if not tool_dir.is_dir():
        raise CronToolNotFoundError(
            f"cron_tool '{name}' 目录不存在: {tool_dir}"
        )

    tool_md_path = tool_dir / "TOOL.md"
    if not tool_md_path.is_file():
        raise CronToolParseError(
            f"cron_tool '{name}' 缺少 TOOL.md: {tool_md_path}"
        )

    # 3. 解析 frontmatter
    frontmatter = _parse_frontmatter(tool_md_path, name)

    # 4. 校验必填字段
    for field_name in _REQUIRED_FRONTMATTER_FIELDS:
        if field_name not in frontmatter:
            raise CronToolParseError(
                f"cron_tool '{name}' 的 TOOL.md frontmatter 缺少必填字段: "
                f"{field_name}"
            )

    input_schema = frontmatter.get("input_schema")
    if not isinstance(input_schema, dict):
        raise CronToolParseError(
            f"cron_tool '{name}' 的 input_schema 必须为 dict"
        )

    # 5. 定位 run.* 脚本与解释器
    run_script, run_interpreter = _find_run_script(tool_dir, name)

    return CronToolMeta(
        name=str(frontmatter["name"]),
        version=str(frontmatter.get("version", "")),
        description=str(frontmatter.get("description", "")),
        author=str(frontmatter.get("author", "")),
        input_schema=input_schema,
        timeout=_parse_timeout(frontmatter.get("timeout")),
        tool_dir=str(tool_dir.resolve()),
        run_script=str(run_script.resolve()),
        run_interpreter=list(run_interpreter),
    )


def _validate_tool_name(name: str) -> None:
    """校验工具名合法性（非空、无路径分隔符、无 ``.`` 前缀防穿越）。"""
    if not name or not isinstance(name, str):
        raise CronToolParseError("cron_tool 名称不能为空")
    # 防路径穿越：禁止 / \ 与 .. 前缀
    if "/" in name or "\\" in name or name.startswith("."):
        raise CronToolParseError(
            f"cron_tool 名称含非法字符: {name!r}（禁止 / \\ 与 . 前缀）"
        )


def _parse_frontmatter(tool_md_path: Path, name: str) -> Dict[str, Any]:
    """解析 TOOL.md 的 yaml frontmatter。

    frontmatter 由首尾的 ``---`` 分隔，如::

        ---
        name: foo
        version: 1.0.0
        ---
        # markdown 正文

    参数:
        tool_md_path: TOOL.md 文件路径。
        name: 工具名（用于错误信息）。

    返回:
        frontmatter dict。

    Raises:
        CronToolParseError: 文件读取失败、frontmatter 格式非法或 yaml 解析失败。
    """
    try:
        content = tool_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CronToolParseError(
            f"读取 cron_tool '{name}' 的 TOOL.md 失败: {exc}"
        ) from exc

    if not content.startswith("---"):
        raise CronToolParseError(
            f"cron_tool '{name}' 的 TOOL.md 必须以 '---' frontmatter 开头"
        )

    # 分割 frontmatter 与正文
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise CronToolParseError(
            f"cron_tool '{name}' 的 TOOL.md frontmatter 格式非法（缺少闭合 '---'）"
        )

    frontmatter_text = parts[1]
    if yaml is None:
        raise CronToolParseError(
            "yaml 模块不可用，无法解析 TOOL.md frontmatter"
        )

    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise CronToolParseError(
            f"cron_tool '{name}' 的 TOOL.md frontmatter yaml 解析失败: {exc}"
        ) from exc

    if not isinstance(frontmatter, dict):
        raise CronToolParseError(
            f"cron_tool '{name}' 的 TOOL.md frontmatter 必须为 dict"
        )
    return frontmatter


def _parse_timeout(timeout: Any) -> Optional[int]:
    """解析 timeout 字段为 int（None 或非正数时返回 None，由 get_timeout 回退）。"""
    if timeout is None:
        return None
    try:
        val = int(timeout)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _find_run_script(tool_dir: Path, name: str) -> tuple:
    """在工具目录下查找 run.* 脚本，返回 (脚本路径, 解释器命令列表)。

    按 :data:`_RUN_INTERPRETERS` 的扩展名顺序查找，找到第一个即返回。

    Raises:
        CronToolRunScriptNotFoundError: 未找到任何 run.* 脚本。
    """
    for ext, interpreter in _RUN_INTERPRETERS.items():
        candidate = tool_dir / f"run{ext}"
        if candidate.is_file():
            return candidate, list(interpreter)
    raise CronToolRunScriptNotFoundError(
        f"cron_tool '{name}' 目录下未找到 run.py / run.sh / run.js: {tool_dir}"
    )


# ---------------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------------


def execute_tool(
    name: str,
    input: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
    base_dir: str = DEFAULT_BASE_DIR,
    meta: Optional[CronToolMeta] = None,
) -> str:
    """通过子进程执行 ``cron_tool/{name}/run.*``，返回结果字符串。

    执行流程：
    1. 加载 TOOL.md（若 ``meta`` 为 ``None`` 则调 :func:`load_tool`）。
    2. 确定超时：参数 ``timeout`` > meta.timeout > :data:`DEFAULT_TIMEOUT`。
    3. 构造 stdin payload：``{"input": {...}, "context": {...}}``。
    4. ``subprocess.run`` 调用解释器 + run.* 脚本，stdin 传 payload。
    5. 解析 stdout JSON：
       - 含 ``result`` → 返回 ``str(result)``
       - 含 ``error`` → 返回结构化错误 JSON 字符串
       - 非 JSON 或缺字段 → 视为执行错误，返回结构化错误 JSON
    6. 异常处理：
       - ``TimeoutExpired`` → 超时错误 JSON（含 ``error_type="timeout"``）
       - 其他异常 → 崩溃错误 JSON（含 ``error_type`` / ``stderr``）

    子进程崩溃不影响主进程（崩溃隔离硬约束）。所有错误均以字符串形式返回，
    不抛异常（保证 ReactLoop 稳定）。

    参数:
        name: cron_tool 名称。
        input: 工具入参 dict（与 TOOL.md 的 input_schema 对应）。
        context: 可选上下文 dict（含 session_id / schedule_id / current_time
            等）。为 ``None`` 时传空 dict。
        timeout: 子进程超时秒数。为 ``None`` 时使用 meta.timeout 或
            :data:`DEFAULT_TIMEOUT`。
        base_dir: cron_tool 根目录，默认 :data:`DEFAULT_BASE_DIR`。
        meta: 预加载的 :class:`CronToolMeta`（避免重复解析 TOOL.md）。
            为 ``None`` 时调 :func:`load_tool`。

    返回:
        结果字符串。成功时为 ``str(result)``（result 来自子进程 stdout JSON），
        失败时为结构化错误 JSON 字符串，形如::

            {"error": "...", "error_type": "timeout|crash|parse_error|...",
             "stderr": "...", "returncode": N}
    """
    # 1. 加载 meta（如未预加载）
    try:
        if meta is None:
            meta = load_tool(name, base_dir=base_dir)
    except CronToolError as exc:
        return _format_error(
            error_type="load_error",
            message=f"加载 cron_tool '{name}' 失败: {exc}",
        )

    # 2. 确定超时
    effective_timeout = (
        timeout if (timeout is not None and timeout > 0) else meta.get_timeout()
    )

    # 3. 构造 stdin payload
    payload = {"input": input or {}, "context": context or {}}
    try:
        stdin_text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return _format_error(
            error_type="input_serialize_error",
            message=f"序列化输入 JSON 失败: {exc}",
        )

    # 4. 子进程执行
    cmd = list(meta.run_interpreter) + [meta.run_script]
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            cwd=meta.tool_dir,
        )
    except subprocess.TimeoutExpired:
        return _format_error(
            error_type="timeout",
            message=(
                f"cron_tool '{name}' 执行超时（{effective_timeout}s），"
                f"子进程已终止"
            ),
            timeout=effective_timeout,
        )
    except FileNotFoundError as exc:
        return _format_error(
            error_type="interpreter_not_found",
            message=(
                f"cron_tool '{name}' 解释器不存在: "
                f"{meta.run_interpreter} ({exc})"
            ),
            stderr=str(exc),
        )
    except Exception as exc:
        return _format_error(
            error_type="subprocess_error",
            message=f"cron_tool '{name}' 子进程启动失败: {exc}",
            stderr=str(exc),
        )

    # 5. 解析 stdout
    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""

    # 子进程非零退出 + 无 stdout → 视为崩溃
    if proc.returncode != 0 and not stdout_text.strip():
        return _format_error(
            error_type="crash",
            message=(
                f"cron_tool '{name}' 子进程崩溃（returncode={proc.returncode}）"
            ),
            stderr=stderr_text,
            returncode=proc.returncode,
        )

    # 尝试解析 stdout JSON
    try:
        result_obj = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        return _format_error(
            error_type="output_parse_error",
            message=(
                f"cron_tool '{name}' stdout 非 JSON: {exc}"
            ),
            stdout=stdout_text,
            stderr=stderr_text,
            returncode=proc.returncode,
        )

    if not isinstance(result_obj, dict):
        return _format_error(
            error_type="output_format_error",
            message=(
                f"cron_tool '{name}' stdout JSON 必须为 dict，实际: "
                f"{type(result_obj).__name__}"
            ),
            stdout=stdout_text,
            returncode=proc.returncode,
        )

    # 含 error 字段 → 子进程主动报告错误
    if "error" in result_obj:
        return _format_error(
            error_type=result_obj.get("error_type", "tool_error"),
            message=str(result_obj.get("error", "")),
            stderr=stderr_text,
            returncode=proc.returncode,
        )

    # 含 result 字段 → 成功
    if "result" in result_obj:
        return str(result_obj["result"])

    # 缺 result 与 error → 格式错误
    return _format_error(
        error_type="output_format_error",
        message=(
            f"cron_tool '{name}' stdout JSON 缺少 'result' 或 'error' 字段"
        ),
        stdout=stdout_text,
        returncode=proc.returncode,
    )


def _format_error(
    error_type: str,
    message: str,
    **extra: Any,
) -> str:
    """构造结构化错误 JSON 字符串。

    参数:
        error_type: 错误类型简码（``timeout`` / ``crash`` / ``parse_error``
            等）。
        message: 人类可读错误信息。
        **extra: 附加字段（如 ``stderr`` / ``returncode`` / ``stdout``）。

    返回:
        JSON 字符串，形如 ``{"error": "...", "error_type": "...", ...}``。
        保证返回的 JSON 可被 :func:`json.loads` 解析。
    """
    payload: Dict[str, Any] = {
        "error": message,
        "error_type": error_type,
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        # 极端情况：extra 含不可序列化对象，降级为字符串
        return json.dumps(
            {"error": message, "error_type": error_type},
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# 工具列表辅助
# ---------------------------------------------------------------------------


def list_tools(base_dir: str = DEFAULT_BASE_DIR) -> list:
    """列出 ``base_dir`` 下所有已激活的 cron_tool 名称。

    跳过 ``.pending`` 目录与隐藏目录。返回的名称按字典序排序。

    参数:
        base_dir: cron_tool 根目录。

    返回:
        工具名列表。目录不存在时返回空列表。
    """
    root = Path(base_dir)
    if not root.is_dir():
        return []
    names = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        # 必须含 TOOL.md 才算合法工具
        if (entry / "TOOL.md").is_file():
            names.append(entry.name)
    return sorted(names)


def list_pending_tools(base_dir: str = DEFAULT_BASE_DIR) -> list:
    """列出 ``base_dir/.pending`` 下所有待审查的 cron_tool 名称。

    参数:
        base_dir: cron_tool 根目录。

    返回:
        待审查工具名列表。``.pending`` 目录不存在时返回空列表。
    """
    pending_dir = Path(base_dir) / ".pending"
    if not pending_dir.is_dir():
        return []
    names = []
    for entry in pending_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if (entry / "TOOL.md").is_file():
            names.append(entry.name)
    return sorted(names)
