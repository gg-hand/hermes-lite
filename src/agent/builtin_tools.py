"""内置工具集合。

提供基础工具（文件/命令/HTTP）与 list_tools/call_tool 元工具，以及
plan 模式的 plan_task / update_todo 工具。每个工具提供：name,
description, input_schema, handler。

高频内置工具归 Core Tier（通过 register_core 注册），低频/高风险工具
归 Deferred Tier（通过 register_deferred 注册，按需加载），原因：
1. Core Tier 高频使用：file_read/file_query/bash_exec 等是 Agent 日常操作的基础；
2. Core Tier 字节级稳定，保证 KV cache 100% 命中；
3. Deferred Tier 按需加载，不占缓存 key，LLM 需要时通过 list_tools 发现。

工具清单（Core Tier，由 ``register_builtin_tools`` 注册）：
- file_read: 读取文件内容
- file_write: 写入文件（v2：注入 file_registry 后记录到 created/modified 集合）
- file_listdir: 列出目录内容
- file_delete: 删除文件（仅 file_registry 注入时注册，走 v2 智能豁免）
- file_edit: 精准文本替换
- file_glob: 按通配符查找文件
- file_grep: 搜索文件内容
- web_fetch: 发起 HTTP 请求
- bash_exec: 执行终端命令（30 秒超时）
- tool_list: 搜索并按需加载 Deferred 工具（元工具）
- tool_call: 调用已加载的 Deferred 工具（元工具）

任务管理工具（由 ``register_plan_tools`` 单独注册，需要 ``TodoListRegistry``
实例与 ``get_session_id`` 回调）：
- plan_task: 规划复杂任务的执行步骤并初始化 todo 清单
- update_todo: 更新某个 todo 步骤的状态

记忆管理工具（由 ``register_memory_tools`` 单独注册，Phase 7 Task 3，
需要 ``ChromaMemoryStore`` / ``ConsolidationEngine`` 实例与
``get_session_id`` 回调）：
- search_memory: 检索向量库长期记忆（读取类，不走 confirm）
- delete_memory: 入队删除记忆操作（高危，走 PolicyEngine confirm）
- update_memory: 入队更新记忆操作（高危，走 PolicyEngine confirm）
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys as _sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urlparse

import httpx

# 默认浏览器请求头（避免被目标网站识别为爬虫）
_DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# HTTP 响应体最大字符数（超出截断）
_MAX_HTTP_BODY_CHARS = 50000

# 域名状态持久化路径（存储成功访问过的域名 Cookie/Referer）
_DOMAIN_STATE_PATH = "data/domain_state.json"
# 域名状态文件锁（线程安全）
_domain_state_lock = threading.Lock()

# 可持久化的 header 白名单（不含敏感字段）
_PERSISTABLE_HEADERS = frozenset({"cookie", "referer", "origin", "user-agent"})

# P1 可选依赖：curl_cffi 提供 Chrome TLS 指纹伪装
try:
    from curl_cffi import requests as curl_requests  # noqa: F401
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

# 反爬虫检测关键词（用于工具内部判断质量标签）
_ANTI_CRAWLER_QUALITY_RE = re.compile(
    "|".join(re.escape(kw) for kw in [
        "captcha", "验证码", "人机验证", "安全验证", "access denied",
        "请求被拒绝", "too many requests", "频率限制", "访问频率",
        "triggered our security", "security check", "anti-bot",
        "请完成安全验证", "verify you are human", "are you a robot",
    ]),
    re.IGNORECASE,
)


# 简单 HTML 转纯文本（正则实现，无外部依赖）
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _html_to_plain_text(html: str) -> str:
    """将 HTML 转换为纯文本摘要。

    1. 提取 ``<title>`` 内容作为标题行
    2. 移除 ``<script>/<style>/<noscript>`` 块
    3. 移除其余所有 HTML 标签
    4. 解码 HTML 实体（``&amp;`` / ``&#x27;`` 等）
    5. 压缩连续空白为单个空格

    返回:
        ``[Title: 页面标题]`` + 页面可见文本（压缩后），
        无 title 时仅返回文本。
    """
    if not html:
        return ""

    # 提取 title
    title_match = _TITLE_RE.search(html)
    title = title_match.group(1).strip() if title_match else ""

    # 移除 script / style / noscript 块
    text = _SCRIPT_STYLE_RE.sub("", html)

    # 移除剩余 HTML 标签
    text = _HTML_TAG_RE.sub("", text)

    # 解码 HTML 实体
    import html as _html_mod
    text = _html_mod.unescape(text)

    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()

    if title:
        return f"[Title: {title}]\n{text}"
    return text


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    """从 URL 提取域名（如 ``bilibili.com``）。

    参数:
        url: 完整 URL。

    返回:
        域名部分（小写）或空字符串（解析失败）。
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return host.lower()
    except Exception:
        return ""


def _load_domain_state() -> dict:
    """从 JSON 文件加载域名状态。线程安全，缺失或损坏时返回空 dict。"""
    with _domain_state_lock:
        try:
            if os.path.exists(_DOMAIN_STATE_PATH):
                with open(_DOMAIN_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError, IOError) as e:
            logger.warning("加载域名状态失败: %s", e)
    return {}


def _save_domain_state(state: dict) -> None:
    """持久化域名状态到 JSON 文件。线程安全。"""
    with _domain_state_lock:
        try:
            os.makedirs(os.path.dirname(_DOMAIN_STATE_PATH), exist_ok=True)
            with open(_DOMAIN_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("保存域名状态失败: %s", e)


def _classify_quality(status_code: int, body: str) -> str:
    """根据状态码和 body 返回质量标签。

    返回:
        ``"OK"`` — 正常
        ``"BLOCKED"`` — 反爬虫/风控
        ``"ERROR {code}"`` — 永久错误
        ``"TRANSIENT"`` — 临时错误
    """
    if status_code in (403, 412):
        return "BLOCKED"
    if status_code == 429 or status_code >= 500:
        return "TRANSIENT"
    if status_code in (404, 410):
        return f"ERROR {status_code}"
    if 200 <= status_code < 400:
        # 检查 body 反爬关键词
        if _ANTI_CRAWLER_QUALITY_RE.search(body):
            return "BLOCKED"
        return "OK"
    # 其他状态码
    return f"ERROR {status_code}"


def _build_enhanced_headers(
    url: str, base_headers: dict, domain_state: Optional[dict] = None,
) -> dict:
    """在基础 headers 之上追加从 URL 推导的 Referer/Origin 和域名缓存 Cookie。

    参数:
        url: 请求 URL。
        base_headers: 已有 headers（会被复制）。
        domain_state: 可选的域名状态 dict。

    返回:
        增强后的 headers dict。
    """
    enhanced = dict(base_headers)

    # 收集已有键（大小写不敏感）
    existing_keys = {k.lower() for k in enhanced}

    # 从 URL 推导 Referer 与 Origin
    try:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if "referer" not in existing_keys:
            enhanced["Referer"] = origin + "/"
            existing_keys.add("referer")
        if "origin" not in existing_keys:
            enhanced["Origin"] = origin
            existing_keys.add("origin")
    except Exception:
        pass

    # 注入域名缓存的 Cookie/Referer
    if domain_state:
        domain = _extract_domain(url)
        entry = domain_state.get(domain, {})
        if isinstance(entry, dict):
            cached_cookie = entry.get("cookie")
            if cached_cookie and "cookie" not in existing_keys:
                enhanced["Cookie"] = cached_cookie
                existing_keys.add("cookie")
            cached_referer = entry.get("referer")
            if cached_referer and "referer" not in existing_keys:
                enhanced["Referer"] = cached_referer
                existing_keys.add("referer")

    return enhanced


def _persist_success_headers(url: str, headers: dict, save_state: bool) -> None:
    """将成功的 headers 中可持久化字段存入域名状态。

    参数:
        url: 请求 URL。
        headers: 本次请求使用的完整 headers。
        save_state: 是否持久化。为 False 时跳过。
    """
    if not save_state:
        return
    domain = _extract_domain(url)
    if not domain:
        return

    entry: dict = {}
    for key_lower in _PERSISTABLE_HEADERS:
        # 遍历 headers 查找匹配键（大小写不敏感）
        for k, v in headers.items():
            if k.lower() == key_lower and v:
                entry[key_lower] = v
                break
    if not entry:
        return

    state = _load_domain_state()
    # 合并到已有条目
    existing = state.get(domain, {})
    if isinstance(existing, dict):
        existing.update(entry)
    else:
        existing = entry
    from datetime import datetime
    existing["last_success"] = datetime.now().isoformat()
    state[domain] = existing
    _save_domain_state(state)

# 兼容相对导入与直接运行两种方式（与 orchestrator.py 保持一致）
try:
    from ..tasks.todo_list import TodoListRegistry
except ImportError:  # pragma: no cover - 直接运行模块时回退
    from tasks.todo_list import TodoListRegistry  # type: ignore

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入以避免循环依赖
    from .file_registry import FileOperationRegistry
    from ..files.etl_engine import ETLEngine
    from ..files.upload_manager import UploadManager
    from ..memory.consolidation import ConsolidationEngine
    from ..storage.chroma_store import ChromaMemoryStore

logger = logging.getLogger(__name__)


def read_file(path: str, offset: int = 0, limit: int = 0, max_chars: int = 20000) -> str:
    """读取文件内容。

    参数:
        path: 文件路径。
        offset: 可选，起始行号（从 0 开始）。0 表示从文件开头读取。
        limit: 可选，最多读取的行数。0 表示读取全部行。
        max_chars: 可选，最多返回的字符数。超过时截断并添加提示。
            默认 20000（约 5000 tokens）。

    返回:
        文件内容字符串。读取失败时返回错误信息。
    """
    try:
        p = Path(path)
        if offset > 0 or limit > 0:
            lines = p.read_text(encoding="utf-8").splitlines()
            start = max(0, offset)
            end = start + limit if limit > 0 else len(lines)
            result = "\n".join(lines[start:end])
        else:
            result = p.read_text(encoding="utf-8")
        if max_chars > 0 and len(result) > max_chars:
            result = result[:max_chars] + f"\n...（内容已截断，原始长度 {len(result)} 字符）"
        return result
    except Exception as e:
        return f"读取文件失败: {e}"


def write_file(path: str, content: str) -> str:
    """写入文件（覆盖写入），返回成功信息。

    基础版本，不记录到 file_registry。当 ``register_builtin_tools``
    注入 ``file_registry`` 与 ``get_session_id`` 时，会通过 closure 覆盖
    注册为 v2 版本（执行后调用 ``file_registry.record_write`` 记录新建/
    修改状态）。

    参数:
        path: 文件路径。
        content: 写入内容。

    返回:
        成功信息字符串。
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入文件: {path}（{len(content)} 字符）"
    except Exception as e:
        return f"写入文件失败: {e}"


def delete_file(path: str) -> str:
    """删除文件，返回成功信息。

    基础版本，不走 file_registry 智能豁免。当 ``register_builtin_tools``
    注入 ``file_registry`` 与 ``get_session_id`` 时，会通过 closure 覆盖
    注册为 v2 版本（执行后调用 ``file_registry.remove`` 同步集合状态）。

    参数:
        path: 文件路径。

    返回:
        成功信息字符串。文件不存在返回提示，symlink 拒绝删除。
    """
    try:
        p = Path(path)
        # 先检查 symlink（即使目标不存在也要拒绝，防止 LLM 通过 symlink 操作）
        try:
            if p.is_symlink():
                return f"拒绝删除符号链接: {path}"
        except OSError:
            pass
        if not p.exists():
            return f"文件不存在: {path}"
        p.unlink()
        return f"已删除文件: {path}"
    except Exception as e:
        return f"删除文件失败: {e}"


def list_directory(path: str = ".") -> str:
    """列出目录内容。

    参数:
        path: 目录路径，默认当前目录。

    返回:
        目录内容字符串（每行一项，[DIR]/[FILE] 前缀标识类型）。
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"路径不存在: {path}"
        if not p.is_dir():
            return f"不是目录: {path}"
        # 目录项在前，文件在后，各自按名称排序
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = []
        for entry in entries:
            if entry.is_dir():
                lines.append(f"[DIR]  {entry.name}/")
            else:
                lines.append(f"[FILE] {entry.name}")
        return "\n".join(lines) if lines else "(空目录)"
    except Exception as e:
        return f"列出目录失败: {e}"


# 检测命令中是否含 shell 元字符（管道、重定向、链式执行、变量引用等）
# 若不含，可用 shlex.split() 安全拆分 + shell=False 执行
_SHELL_META_RE = re.compile(r'[|>&;`$(){}!~]|&&|\|\|')


def _has_shell_metachar(command: str) -> bool:
    """检查命令是否含有 shell 元字符。

    检测以下模式：
    - 管道：|
    - 重定向：>  >>
    - 链式执行：&&  ||  ;
    - 变量/命令替换：$  `  ()
    - 通配符/大括号：*  ?  {}  ~

    若不含这些字符，命令可安全使用 shell=False 执行。
    """
    return bool(_SHELL_META_RE.search(command))


# ── 进程追踪（模块级，跨线程可访问）──

_running_proc: Optional[subprocess.Popen] = None
_running_proc_lock = threading.Lock()


def _set_running_proc(proc: Optional[subprocess.Popen]) -> None:
    """设置当前运行中的子进程。线程安全。"""
    global _running_proc
    with _running_proc_lock:
        _running_proc = proc


def _get_and_clear_running_proc() -> Optional[subprocess.Popen]:
    """取出并清空当前运行中的子进程。线程安全。"""
    global _running_proc
    with _running_proc_lock:
        proc = _running_proc
        _running_proc = None
        return proc


def kill_running_process() -> bool:
    """强杀当前运行中的子进程树。跨线程安全，幂等。

    由 StreamManager.force_cancel 或 /chat/cancel（两段式）调用。
    在 FastAPI 线程中执行，可安全访问模块级 _running_proc。

    返回:
        True 表示成功 kill 了一个运行中的进程，False 表示无进程或已退出。
    """
    proc = _get_and_clear_running_proc()
    if proc is None:
        return False
    # 已退出 → 不需要 kill
    if proc.poll() is not None:
        return False
    try:
        if _sys.platform == "win32":
            # 先尝试 taskkill /T 杀进程树
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
            # 如果进程仍然存活，用 proc.kill() 补刀
            if proc.poll() is None:
                proc.kill()
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        # 等待进程完全退出
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        return True
    except Exception:
        # 所有方式都失败，尝试最后手段
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        return False


def execute_command(command: str, timeout: int = 30) -> str:
    """执行终端命令，返回 stdout。

    安全策略：
    - 简单命令（不含 shell 元字符）：使用 shlex.split() 拆分后以
      subprocess.run(..., shell=False) 执行，避免 shell 注入风险。
    - 复杂命令（含管道/重定向/链式执行等）：保留 shell=True 执行，
      但会触发 PolicyEngine 的确认流程（需用户审批）。

    参数:
        command: 要执行的命令字符串。
        timeout: 命令超时秒数，默认 30。

    返回:
        命令输出（stdout）。出错时附上退出码与 stderr。
    """
    # Phase 9+ 中断检查：工具入口处检查 ContextVar
    cancel_event = None
    try:
        from ._cancel_context import current_cancel_event
        cancel_event = current_cancel_event.get()
        if cancel_event is not None and cancel_event.is_set():
            return "[命令已被用户中断]"
    except ImportError:
        pass

    # 规范 4.3 Task 12: Windows + python -c "多行代码" 兼容
    # cmd.exe 不支持多行 -c（换行会被截断），检测到时改写为临时 .py 文件执行。
    # 仅匹配命令级换行（\n 后跟 import/from/def/class/if/for/while 等关键字），
    # 避免误判单行 print('hello\nworld') 这种字符串内的 \n。
    rewritten_command, temp_script_path = _maybe_rewrite_multiline_python_c(command)
    try:
        return _execute_command_inner(rewritten_command, timeout, cancel_event)
    finally:
        # 无论执行成功或失败，都清理临时文件（Task 12.6）
        if temp_script_path is not None:
            try:
                os.unlink(temp_script_path)
            except OSError:
                pass


def _maybe_rewrite_multiline_python_c(command: str):
    """检测 Windows + python -c "多行代码" 模式，命中时改写为临时文件执行。

    返回 (rewritten_command, temp_script_path) 元组：
    - 未命中：返回 (command, None)
    - 命中：返回 ("python <tmp_file>", tmp_file_path)

    检测规则（Task 12.1-12.3）：
    1. 仅在 Windows 平台触发（cmd.exe 不支持多行 -c）
    2. 匹配 ``python -c "..."`` 或 ``python -c '...'`` 模式
    3. 代码内容含**真实换行符**（ASCII 10）。LLM 通过 JSON 传入的命令中，
       ``\\n`` 会被 JSON 解码为真实换行，而 ``print('hello\\nworld')`` 中的
       ``\\n`` 是字面两字符（backslash + n），不会触发本规则。
    """
    if _sys.platform != "win32":
        return command, None

    # 匹配 python -c "代码" 或 python -c '代码'（捕获引号内的完整内容）
    # 使用非贪婪 + 允许换行的 [\s\S] 而非 .
    match = re.match(
        r'^\s*python(?:3|\.exe)?\s+-c\s+(["\'])([\s\S]*?)\1\s*$',
        command,
    )
    if match is None:
        return command, None

    code_content = match.group(2)

    # 检测真实换行符（ASCII 10）。
    # 字面 \n（backslash + n，如 print('hello\nworld')）不会触发，
    # 因为 LLM 在字符串内嵌入换行时用的是字面 \n 而非真实换行。
    if "\n" not in code_content:
        return command, None

    # 命中：写入临时 .py 文件
    import tempfile
    from uuid import uuid4

    tmp_dir = tempfile.gettempdir()
    tmp_filename = f"hermes_exec_{uuid4().hex[:8]}.py"
    tmp_path = os.path.join(tmp_dir, tmp_filename)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(code_content)
    except OSError as e:
        logger.warning("写入临时脚本失败，回退到原命令执行: %s", e)
        return command, None

    logger.info("多行 -c 检测命中，改写为临时文件: %s", tmp_path)
    return f"python {tmp_path}", tmp_path


def _execute_command_inner(
    command: str, timeout: int, cancel_event,
) -> str:
    """实际执行命令的内部函数（被 execute_command 包装）。

    抽取出来是为了让 execute_command 的 try/finally 能确保临时文件被清理。
    """
    # ── 构建 Popen 参数（含进程组/会话隔离） ──
    # Windows 编码兼容：预设 PYTHONIOENCODING=utf-8 防止中文输出
    # 被 cp936/GBK 截断导致 stdout 为空
    _popen_env = None
    if _sys.platform == "win32":
        _popen_env = dict(os.environ, PYTHONIOENCODING="utf-8")

    if not _has_shell_metachar(command):
        args = shlex.split(command, posix=False)
        if not args:
            return "错误：空命令"
        popen_args: dict = {
            "args": args,
            "shell": False,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
    else:
        popen_args = {
            "args": command,
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
    if _popen_env is not None:
        popen_args["env"] = _popen_env

    # 跨平台进程树隔离：force kill 时能杀整个进程树
    if _sys.platform == "win32":
        popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_args["start_new_session"] = True

    try:
        proc = subprocess.Popen(**popen_args)
    except Exception as e:
        return f"命令执行失败: {e}"

    # 暴露进程引用，供外部 force kill
    _set_running_proc(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 超时 → 杀进程树
        _set_running_proc(None)
        try:
            kill_running_process()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        return f"命令执行超时（{timeout} 秒）"
    except Exception as e:
        _set_running_proc(None)
        return f"命令执行失败: {e}"
    finally:
        _set_running_proc(None)

    # force_kill 检测：cancel_event 在 communicate 期间被 set
    interrupted = (
        cancel_event is not None and cancel_event.is_set()
    )

    output = stdout or ""
    if proc.returncode != 0:
        output += f"\n[退出码 {proc.returncode}]\n{stderr or ''}"
        # 退出码 9009（Windows 命令未找到）
        if proc.returncode == 9009 and _sys.platform == "win32":
            output += "\n[提示] 退出码 9009 通常表示命令未找到。请检查命令拼写或使用完整路径。"
    elif output.strip() == "":
        # returncode == 0 但 stdout 为空
        output = "[提示] 命令执行成功但 stdout 为空。可能命令无输出或输出被重定向。"
    if interrupted:
        output = "[命令已被用户中断]\n" + output
    return output


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[dict] = None,
    timeout: int = 30,
    no_cache: bool = False,
    save_state: bool = True,
) -> str:
    """HTTP 请求（升级阶梯版），返回带质量标签的响应文本。

    升级策略：
    1. httpx + 合并头（默认浏览器头 + 域名缓存 + 用户自定义） → Tier 1
    2. 若被反爬（403/412），自动以增强头重试一次 → Tier 2
    3. 若仍被反爬且 curl_cffi 可用，以 Chrome TLS 指纹重试 → Tier 3
    4. 仍失败则返回 ``[BLOCKED]`` 明确信号

    成功时自动将 Cookie/Referer 等白名单头存入域名状态缓存。

    参数:
        url: 请求 URL。
        method: HTTP 方法，默认 GET。
        headers: 可选自定义请求头 dict，覆盖默认头。
        timeout: 超时秒数，默认 30。
        no_cache: 跳过域名状态缓存（不读取已保存的 Cookie/Referer）。
        save_state: 成功后是否将白名单头持久化到域名状态。

    返回:
        首行为质量标签（``[OK]`` / ``[BLOCKED]`` / ``[ERROR nnn]`` /
        ``[TRANSIENT]``），随后是 ``[HTTP nnn]`` 等元数据行，空行后为响应体。
    """
    # Phase 9+ 中断检查
    try:
        from ._cancel_context import current_cancel_event
        ce = current_cancel_event.get()
        if ce is not None and ce.is_set():
            return "[TRANSIENT]\n[HTTP 请求被用户中断]"
    except ImportError:
        pass

    # 合并默认头 + 域名缓存 + 用户自定义
    merged_headers = dict(_DEFAULT_HTTP_HEADERS)
    if not no_cache:
        domain_state = _load_domain_state()
        domain = _extract_domain(url)
        entry = domain_state.get(domain, {}) if domain_state else {}
        if isinstance(entry, dict):
            for key in _PERSISTABLE_HEADERS:
                val = entry.get(key)
                if val and key not in {k.lower() for k in merged_headers}:
                    merged_headers[key.capitalize()] = val
    else:
        domain_state = {}

    if headers:
        merged_headers.update(headers)

    # ── Tier 1: httpx 标准请求 ──
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.request(method, url, headers=merged_headers)
            status_code = response.status_code
            content_type = response.headers.get("content-type", "unknown")
            body = response.text
    except Exception as e:
        return f"[TRANSIENT]\nHTTP 请求失败: {e}"

    quality = _classify_quality(status_code, body)

    # ── Tier 2: 增强头重试（仅反爬虫/风控触发） ──
    if quality == "BLOCKED":
        enhanced = _build_enhanced_headers(url, merged_headers, domain_state)
        # 仅当增强头与原始头不同时才尝试
        if enhanced != merged_headers:
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    resp2 = client.request(method, url, headers=enhanced)
                    sc2 = resp2.status_code
                    ct2 = resp2.headers.get("content-type", "unknown")
                    body2 = resp2.text
                q2 = _classify_quality(sc2, body2)
                if q2 == "OK":
                    quality = "OK"
                    status_code = sc2
                    content_type = ct2
                    body = body2
                    merged_headers = enhanced  # 后续持久化增强头
            except Exception:
                pass  # Tier 2 失败，保持 BLOCKED

        # ── Tier 3: curl_cffi Chrome TLS 指纹 ──
        if quality == "BLOCKED" and _CURL_CFFI_AVAILABLE:
            try:
                cf_headers = _build_enhanced_headers(url, merged_headers, domain_state)
                cf_response = curl_requests.get(
                    url,
                    headers=cf_headers,
                    timeout=timeout,
                    impersonate="chrome120",
                )
                sc3 = cf_response.status_code
                ct3 = cf_response.headers.get("content-type", "unknown")
                body3 = cf_response.text
                q3 = _classify_quality(sc3, body3)
                if q3 == "OK":
                    quality = "OK"
                    status_code = sc3
                    content_type = ct3
                    body = body3
                    merged_headers = cf_headers
            except Exception:
                pass  # Tier 3 失败，保持 BLOCKED

    # ── 持久化成功 headers ──
    if quality == "OK" and save_state:
        _persist_success_headers(url, merged_headers, save_state=True)

    # ── HTML → 纯文本转换（仅 text/html，截断前） ──
    # 注意：反爬分类（_classify_quality）已在 Tier 1-3 对原始 body 完成，
    # 此处清洗不影响反爬判断结果
    if "html" in content_type.lower():
        orig_len = len(body)
        body = _html_to_plain_text(body)
        logger.debug(
            "HTML 响应已转换: 原始 %d chars → 纯文本 %d chars",
            orig_len, len(body),
        )

    # ── 构造输出 ──
    truncated = False
    if len(body) > _MAX_HTTP_BODY_CHARS:
        body = body[:_MAX_HTTP_BODY_CHARS]
        truncated = True

    prefix = f"[{quality}]"
    lines = [prefix, f"[HTTP {status_code}]", f"[URL: {url}]", f"[Type: {content_type}]"]
    result = "\n".join(lines) + "\n\n" + body
    if truncated:
        result += "\n... (响应体已截断)"
    return result
def file_edit(path: str, old_string: str, new_string: str) -> str:
    """在文件中做精准文本替换（替代读+写整个文件）。

    参数:
        path: 文件路径。
        old_string: 要替换的原有文本（必须存在且唯一）。
        new_string: 替换后的新文本。

    返回:
        成功信息或错误提示。
    """
    try:
        p = Path(path)
        content = p.read_text(encoding="utf-8")
        if old_string not in content:
            return f"错误：未找到匹配文本 '{old_string}'"
        if content.count(old_string) > 1:
            return f"错误：'{old_string}' 匹配到多处，请提供更多上下文"
        new_content = content.replace(old_string, new_string)
        p.write_text(new_content, encoding="utf-8")
        return f"已替换（{path}，{len(new_content)} 字符）"
    except Exception as e:
        return f"文件编辑失败: {e}"


def file_glob(pattern: str, max_results: int = 100) -> str:
    """按通配符模式查找文件。

    参数:
        pattern: 通配符模式，如 ``**/*.py``、``src/**/*.ts``。
        max_results: 最大返回条数，默认 100。

    返回:
        匹配文件路径列表（每行一个）。
    """
    try:
        matches = [str(p) for p in Path(".").rglob(pattern) if p.is_file()]
        result = "\n".join(matches[:max_results])
        if len(matches) > max_results:
            result += f"\n... 及另外 {len(matches) - max_results} 个匹配"
        return result or "(无匹配)"
    except Exception as e:
        return f"文件查找失败: {e}"


def file_grep(pattern: str, glob: str = "**/*", max_results: int = 50) -> str:
    """在文件中搜索文本模式，返回匹配行。

    参数:
        pattern: 要搜索的文本（支持 Python str.__contains__ 语义）。
        glob: 文件通配符模式，默认 ``**/*``（所有文件）。
        max_results: 最大返回行数，默认 50。

    返回:
        匹配结果，每行格式 ``文件路径:行号:行内容``。
    """
    try:
        matches = []
        for path_obj in sorted(Path(".").rglob(glob)):
            if not path_obj.is_file():
                continue
            try:
                for i, line in enumerate(path_obj.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern in line:
                        matches.append(f"{path_obj}:{i}:{line.strip()}")
                        if len(matches) >= max_results:
                            return "\n".join(matches) + "\n... (结果已截断)"
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(matches) or "(无匹配)"
    except Exception as e:
        return f"文件搜索失败: {e}"


# ---------------------------------------------------------------------------
# web_search: 百度搜索 API 工具
# ---------------------------------------------------------------------------


def _search_baidu(query: str, top_k: int = 5, api_key: str = "") -> str:
    """通过百度千帆 AppBuilder AI 搜索 API 搜索网页，返回结构化结果。

    API 文档：https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy
    需要配置 ``BAIDU_API_KEY`` 环境变量（AppBuilder API Key 或 BCE IAM Key）。
    免费额度：每日 100 次查询。

    API 使用 messages 格式（类 chat 接口），Bearer Token 认证。
    BCE IAM Key（bce-v3/ALTAK-{ak}/{sk}）可直接作为 Bearer Token 使用。
    """
    if not api_key:
        return "[ERROR] 百度 API Key 未配置"

    # 调用千帆 AI 搜索 API（网页搜索，messages 格式）
    try:
        resp = httpx.post(
            "https://qianfan.baidubce.com/v2/ai_search/web_search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "messages": [{"role": "user", "content": query}],
                "top_n": top_k,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300] if e.response else ""
        return f"[TRANSIENT] 百度搜索 API 请求失败 (HTTP {e.response.status_code}): {body}"
    except Exception as e:
        return f"[TRANSIENT] 百度搜索 API 请求失败: {e}"

    # 解析搜索结果
    # 千帆 AI 搜索 API 返回格式: { "references": [...], ... }
    # 每条 reference: { "id": int, "url": str, "title": str, "date": str, "content": str }
    refs = data.get("references", data.get("search_results", data.get("results", [])))
    if not isinstance(refs, list) or not refs:
        return "[OK]\n未找到相关结果。\n[源: 百度]"

    lines = ["[OK]", "[源: 百度]", f"[查询: {query}]"]
    for i, ref in enumerate(refs[:top_k], 1):
        title = ref.get("title", "") or ""
        url = ref.get("url", ref.get("link", "")) or ""
        content = ref.get("content", ref.get("snippet", ref.get("desc", ""))) or ""
        # 截断超长摘要（单条不超过 300 字）
        if len(content) > 300:
            content = content[:297] + "..."
        lines.append(f"\n{i}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if content:
            lines.append(f"   摘要: {content}")
    return "\n".join(lines)


def web_search(
    query: str,
    top_k: int = 5,
) -> str:
    """通过百度搜索 API 执行网页搜索，返回结构化结果摘要（标题 + URL + 摘要片段）。

    与 ``web_fetch`` 的区别：``web_search`` 通过百度搜索引擎 API 返回精选结果，
    LLM 无需自行猜测 URL；``web_fetch`` 用于获取指定 URL 的完整页面内容。
    搜索公开信息应优先使用此工具。

    需要配置 ``BAIDU_API_KEY`` 环境变量（或 config.yaml 中 web_search.baidu_api_key）。
    免费额度：每日 100 次查询。

    参数:
        query: 搜索关键词（支持中文、英文等自然语言查询）。
        top_k: 返回结果条数，默认 5，最大 10。

    返回:
        首行为 ``[OK]`` / ``[ERROR]`` 质量标签及搜索来源，
        随后为结构化结果列表（标题 + URL + 摘要）。
    """
    if top_k < 1 or top_k > 10:
        top_k = 5

    # 从环境变量或 config 读取百度 API Key
    baidu_key = os.environ.get("BAIDU_API_KEY", "")
    try:
        from ..config import load_config
        cfg = load_config()
        web_cfg = cfg.get("web_search", {}) or {}
        if not baidu_key:
            baidu_key = web_cfg.get("baidu_api_key", "") or ""
    except Exception:
        pass

    if not baidu_key:
        return "[ERROR] BAIDU_API_KEY 未配置。请设置 BAIDU_API_KEY 环境变量或 config.yaml 中 web_search.baidu_api_key。"

    return _search_baidu(query, top_k, baidu_key)


# 工具定义列表：[(name, description, input_schema, handler), ...]
BUILTIN_TOOLS = [
    (
        "file_read",
        "读取指定路径文件的内容并返回文本。读取文件应优先使用此工具，而非通过 bash_exec 执行 cat/type 命令——本工具更安全、无需 shell 权限、自动处理编码。\n\n⚠ 路径边界：Hermes Lite 自身源码（src/、tests/、config.yaml 等）受 PolicyEngine 黑名单保护，调用 file_read 读取这些路径会被直接 deny。如需了解项目实现请询问用户。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径。",
                },
                "offset": {
                    "type": "integer",
                    "description": "可选，起始行号（从 0 开始），0 表示从开头读取。",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多读取的行数，0 表示读取全部行。",
                    "default": 0,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "可选，最多返回的字符数，超过时截断。默认 20000。",
                    "default": 20000,
                },
            },
            "required": ["path"],
        },
        read_file,
    ),
    (
        "file_write",
        "将内容写入指定路径文件（覆盖写入）。自动创建父目录、编码安全、记录操作到审计。✅ 写入代码、配置、文档 ❌ 简单文本拼接（用 echo）",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径。",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容。",
                },
            },
            "required": ["path", "content"],
        },
        write_file,
    ),
    (
        "file_delete",
        "删除指定路径文件。删除文件应优先使用此工具，而非通过 bash_exec 执行 rm——本工具集成审计与策略决策，更安全可控。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的文件路径。",
                },
            },
            "required": ["path"],
        },
        delete_file,
    ),
    (
        "file_listdir",
        "列出指定目录下的文件与子目录。列出目录应优先使用此工具，而非通过 bash_exec 执行 ls/dir——本工具输出格式统一、无 shell 开销。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录路径，默认当前目录。",
                    "default": ".",
                },
            },
            "required": [],
        },
        list_directory,
    ),
    (
        "file_edit",
        "在文件中做精准文本替换（将 old_string 替换为 new_string）。比 file_read+file_write 更安全高效，推荐用于局部修改代码或配置。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径。",
                },
                "old_string": {
                    "type": "string",
                    "description": "要被替换的原有文本（必须存在且唯一）。",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的新文本。",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        file_edit,
    ),
    (
        "file_glob",
        "【文件名搜索】按通配符模式查找文件路径。适合知道文件名但不确定路径的场景，如 ``**/*.py``、``src/**/*.ts``。",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "通配符模式，如 ``**/*.py``。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回条数，默认 100。",
                    "default": 100,
                },
            },
            "required": ["pattern"],
        },
        file_glob,
    ),
    (
        "file_grep",
        "【文本字符串搜索】在文件中搜索精确关键词，返回 文件路径:行号:行内容。适合搜索代码变量名、函数名、特定字符串等精确匹配场景。",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "要搜索的文本（支持子串匹配）。",
                },
                "glob": {
                    "type": "string",
                    "description": "文件通配符，默认 ``**/*``（所有文件）。",
                    "default": "**/*",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回行数，默认 50。",
                    "default": 50,
                },
            },
            "required": ["pattern"],
        },
        file_grep,
    ),
    (
        "web_fetch",
        "发起 HTTP 请求并返回响应文本，优先使用此工具而非 curl/wget。\n"
        "\n"
        "返回格式：首行为 [HTTP {状态码}] + [URL] + [Type] 元数据，空行后为响应体。\n"
        "\n"
        "状态码含义与策略：\n"
        "- 403/412 或响应含「验证码」「人机验证」「access denied」等 = 目标有反爬虫保护，"
        "不要对相同目标用相同参数重试，应添加 Cookie/Referer 等请求头或换用其他方式。\n"
        "- 404/410 = 资源永久不存在，重试无效。\n"
        "- 429/5xx = 临时性错误，可适当重试。\n"
        "- 永久或反爬错误响应末尾会附加 [系统提示] 引导改正策略，请注意阅读。\n\n"
        "⚠ 失败重试上限：同一域名连续失败 2 次后，不要再换 URL 重试，改用 web_search 工具。\n"
        "⚠ 不要自己拼 URL 抓站查实时信息（如价格、新闻）——直接用 web_search。",
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "请求的 URL。",
                },
                "method": {
                    "type": "string",
                    "description": "HTTP 方法，如 GET/POST，默认 GET。",
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "description": "自定义 HTTP 请求头 dict，如 {\"Cookie\": \"...\", \"Referer\": \"...\"}。与默认浏览器头合并，自定义头优先。反爬虫网站可通过此参数添加认证信息。",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 30。",
                    "default": 30,
                },
                "no_cache": {
                    "type": "boolean",
                    "description": "跳过域名状态缓存，不注入已保存的 Cookie/Referer。",
                    "default": False,
                },
                "save_state": {
                    "type": "boolean",
                    "description": "成功后是否将 Cookie/Referer 等存入域名缓存，下次自动注入。默认 True。",
                    "default": True,
                },
            },
            "required": ["url"],
        },
        http_request,
    ),
    (
        "web_search",
        "通过百度搜索 API 执行网页搜索，返回结构化结果摘要（标题 + URL + 摘要片段）。"
        "搜索公开信息应优先使用此工具，而非通过 web_fetch 抓取搜索引擎页面。"
        "需要配置 BAIDU_API_KEY（环境变量或 config.yaml）。免费额度：每日 100 次。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（支持中文、英文等自然语言查询）。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果条数，默认 5，最大 10。",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        web_search,
    ),
]


def register_builtin_tools(
    registry,
    file_registry: Optional["FileOperationRegistry"] = None,
    get_session_id: Callable[[], Optional[str]] = lambda: None,
    consolidation_engine: Optional["ConsolidationEngine"] = None,
    signal_pool=None,
) -> None:
    """将内置工具与元工具注册到 ToolRegistry 实例。

    Core Tier 工具通过 ``register_core()`` 注册（高频，字节级稳定）；
    Deferred Tier 工具通过 ``register_deferred()`` 注册（低频/高风险，按需加载）。

    注册清单：
    - 6 个内置工具（BUILTIN_TOOLS 列表，含基础版 write_file / delete_file）
    - 若注入 ``file_registry``：通过 closure 覆盖注册 write_file v2 版本
      （执行后调用 ``file_registry.record_write`` 记录新建/修改状态），
      并覆盖注册 delete_file v2 版本（执行后调用 ``file_registry.remove``
      同步集合状态）。
    - 若注入 ``consolidation_engine``：注册 update_profile 工具（Core Tier），
      允许 LLM 通过 add/replace/delete 三种操作显式修改用户画像 memory.md。
      add 操作走信号池累积（若注入 signal_pool），达阈值才写入；replace/delete
      直接入 pending 队列，下次 consolidate 时统一合并。为 ``None`` 时不注册。
    - 2 个元工具（list_tools / call_tool，closure 模式访问 registry 实例）

    参数:
        registry: ToolRegistry 实例。
        file_registry: v2 可选注入 ``FileOperationRegistry``。注入后 write_file
            与 delete_file 会通过 closure 覆盖为基础版本，记录文件操作到
            集合供 PolicyEngine 决策。为 ``None`` 时使用 BUILTIN_TOOLS 中的
            基础版本（向后兼容）。
        get_session_id: 一个 callable，调用时返回当前请求的 session_id 字符串
            或 ``None``。用于 write_file / delete_file v2 版本通过 closure 获取
            当前会话 ID。默认返回 ``None``（不记录到 file_registry）。
        consolidation_engine: 可选的 ``ConsolidationEngine`` 实例。注入后注册
            update_profile 工具（Core Tier）。为 ``None`` 时不注册该工具（向后
            兼容，避免在 ConsolidationEngine 不可用的部署中注册无用工具）。
        signal_pool: 可选的 ``SignalPool`` 实例。注入后 update_profile 的 add
            操作走信号池累积（L1 入池，达阈值才入 pending 队列写入画像）；
            为 ``None`` 时 add 操作回退到直接入 pending 队列（向后兼容）。
            replace/delete 不受此参数影响，始终直接入队。
    """
    # 1. 注册内置工具（文件 / 命令 / HTTP / Plan 模式等）为 Core Tier
    for name, description, input_schema, handler in BUILTIN_TOOLS:
        registry.register_core(name, description, input_schema, handler)

    # 1.5 v2 覆盖：若注入 file_registry，用 closure 版本覆盖 write_file / delete_file
    if file_registry is not None:
        _register_write_file_v2(registry, file_registry, get_session_id)
        _register_delete_file_v2(registry, file_registry, get_session_id)

    # 1.6 若注入 consolidation_engine，注册 update_profile 工具（Core Tier）
    if consolidation_engine is not None:
        _register_update_profile(registry, consolidation_engine, signal_pool)

    # 2. 注册元工具 list_tools / call_tool（Core Tier，始终全量注入）
    #    使用 closure 模式，使元工具内部能访问 registry 实例。

    def list_tools(query: str, top_k: int = 5) -> str:
        """搜索并按需加载可用工具，返回匹配工具的完整 schema JSON。

        参数:
            query: ``select:Tool1,Tool2`` 精确加载，或自然语言关键词搜索。
            top_k: 返回匹配工具的最大数量，默认 5。

        返回:
            匹配工具完整 schema 的 JSON 字符串。
        """
        try:
            results = registry.search_and_load(query, top_k=top_k)
            return json.dumps(results, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"list_tools 执行出错: {e}"

    def call_tool(name: str, arguments: dict) -> str:
        """调用一个已通过 list_tools 加载的工具。

        参数:
            name: 工具名称。
            arguments: 工具参数 dict。

        返回:
            工具执行结果字符串。
        """
        try:
            return registry.execute_tool(name, arguments or {})
        except Exception as e:
            return f"call_tool 执行出错: {e}"

    registry.register_core(
        name="tool_list",
        description=(
            "搜索并按需加载可用工具。精确匹配用 select:ToolName1,ToolName2，"
            "或输入自然语言关键词搜索。返回工具的完整 schema。"
            "注:skill__* / mcp__* 已直接注册到 Core Tier,无需通过本工具加载。"
            "本工具仅用于未来动态发现的 Deferred 工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "select:Tool1,Tool2 精确加载，或关键词搜索",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回匹配工具的最大数量，默认 5。",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        handler=list_tools,
    )

    registry.register_core(
        name="tool_call",
        description=(
            "调用一个已通过 list_tools 加载的工具。如果工具未加载,先调用 list_tools。"
            "注:skill__* / mcp__* 可直接调用,无需通过本工具中转。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工具名称"},
                "arguments": {"type": "object", "description": "工具参数"},
            },
            "required": ["name", "arguments"],
        },
        handler=call_tool,
    )


def _register_write_file_v2(
    registry,
    file_registry: "FileOperationRegistry",
    get_session_id: Callable[[], Optional[str]],
) -> None:
    """注册 v2 版本的 write_file 工具（覆盖基础版本）。

    v2 版本在执行写入前通过 ``Path.exists()`` 判断新建/覆盖，执行成功后
    调用 ``file_registry.record_write(session_id, path, is_new)`` 记录到
    created / modified 集合，供 PolicyEngine 决策。

    参数:
        registry: ToolRegistry 实例。
        file_registry: FileOperationRegistry 实例。
        get_session_id: 返回当前 session_id 的 callable。
    """

    def _write_file_v2(path: str, content: str) -> str:
        """v2 版 write_file：执行后记录到 file_registry。"""
        try:
            p = Path(path)
            # stat 判定新建/覆盖（在写入前判断，避免写入后总是 exists=True）
            is_new = not p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            # 记录到 file_registry（仅当 session_id 非空）
            session_id = get_session_id()
            if session_id:
                file_registry.record_write(session_id, path, is_new=is_new)
            return f"已写入文件: {path}（{len(content)} 字符）"
        except Exception as e:
            return f"写入文件失败: {e}"

    registry.register_core(
        name="file_write",
        description=(
            "将内容写入指定路径文件（覆盖写入）。自动创建父目录、编码安全、记录操作到审计。会话内新建/修改的文件将记录"
            "到 file_registry，用于 PolicyEngine 决策（会话内创建的文件后续"
            "修改/删除享有豁免）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径。",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容。",
                },
            },
            "required": ["path", "content"],
        },
        handler=_write_file_v2,
    )


def _register_delete_file_v2(
    registry,
    file_registry: "FileOperationRegistry",
    get_session_id: Callable[[], Optional[str]],
) -> None:
    """注册 v2 版本的 delete_file 工具（覆盖基础版本）。

    v2 版本在执行删除后调用 ``file_registry.remove(session_id, path)`` 从
    created / modified 集合同步移除，保证后续查询状态正确。

    参数:
        registry: ToolRegistry 实例。
        file_registry: FileOperationRegistry 实例。
        get_session_id: 返回当前 session_id 的 callable。
    """

    def _delete_file_v2(path: str) -> str:
        """v2 版 delete_file：执行后从 file_registry 移除。"""
        try:
            p = Path(path)
            # 先检查 symlink（即使目标不存在也要拒绝，与基础版本一致）
            try:
                if p.is_symlink():
                    return f"拒绝删除符号链接: {path}"
            except OSError:
                pass
            if not p.exists():
                return f"文件不存在: {path}"
            p.unlink()
            # 从 file_registry 移除（仅当 session_id 非空）
            session_id = get_session_id()
            if session_id:
                file_registry.remove(session_id, path)
            return f"已删除文件: {path}"
        except Exception as e:
            return f"删除文件失败: {e}"

    registry.register_core(
        name="file_delete",
        description=(
            "删除指定路径文件。会话内 create_file 创建的文件享有豁免（allow），"
            "会话内修改过的文件需 confirm，用户已有文件需 confirm，symlink 拒绝。"
            "删除后从 file_registry 移除。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的文件路径。",
                },
            },
            "required": ["path"],
        },
        handler=_delete_file_v2,
    )


def _register_update_profile(
    registry,
    consolidation_engine: "ConsolidationEngine",
    signal_pool=None,
) -> None:
    """注册 update_profile 工具到 Core Tier。

    工具允许 LLM 通过 add/replace/delete 三种操作显式修改用户画像
    memory.md。采用**延迟合并写入**策略：handler 不立即写 memory.md。

    操作分流（信号池机制）：
    - add：走信号池累积（若注入 signal_pool），相似信号去重 + 计数累加，
      达阈值（7）才入 pending 队列写入画像。即使用户明确说"记住"也需多次
      出现，符合"稳定模式"设计哲学。为 None 时回退到直接入 pending 队列。
    - replace/delete：直接入 pending 队列（用户显式修改，非待观察信号）。

    handler 通过 closure 捕获 ``consolidation_engine`` 与 ``signal_pool``。
    ToolRegistry 调用 handler 时按关键字参数传入 tool_input（``action`` /
    ``section`` / ``content``），由 handler 内部校验后分流。

    三层防线（入池前/入队前校验）：
    1. 长度上限（MAX_PROFILE_CONTENT_LEN = 4000）—— 先校验，避免长文本
       浪费正则匹配开销
    2. 内容黑名单 —— 14 条正则覆盖系统架构/项目描述/一次性上下文
    3. 单会话频次上限（MAX_PROFILE_WRITES_PER_SESSION = 5）—— 仅约束
       add 操作（入信号池），replace/delete 不受限

    参数:
        registry: ToolRegistry 实例。
        consolidation_engine: ConsolidationEngine 实例，提供
            :meth:`enqueue_profile_update` 接口。
        signal_pool: 可选的 ``SignalPool`` 实例。注入后 add 操作走信号池
            累积；为 None 时 add 回退到直接入 pending 队列（向后兼容）。
    """
    # 长度与频次上限常量（4000/5，配合信号池累积机制放宽）
    MAX_PROFILE_CONTENT_LEN = 4000
    MAX_PROFILE_WRITES_PER_SESSION = 5
    # per-session 写入计数器（进程内持久，跨 run 累积）
    # key: session_id, value: write count
    session_write_counts: dict = {}

    def _update_profile(action: str, section: str, content: str = "", target: str = "user") -> str:
        """update_profile 工具 handler（closure 捕获 consolidation_engine/signal_pool）。

        参数:
            action: 操作类型，``"add"`` / ``"replace"`` / ``"delete"`` 之一。
            section: memory.md 中的 section 标题（不含 ``## `` 前缀）。
            content: 新内容（add/replace 时必填，delete 时忽略）。
            target: 信号目标对象，``"user"``（默认，用户画像）或 ``"agent"``
                （Agent 自画像，写入 ``## Agent 自画像`` section）。target=agent
                时按 target 分组计算 Jaccard 相似度，仅与同 target 信号去重。

        返回:
            操作结果字符串。校验失败时返回错误提示（不抛异常，
            与其它工具 handler 一致，保证 ReactLoop 稳定）。
        """
        # target 参数校验
        if target not in ("user", "agent"):
            return f"错误：target 必须是 user 或 agent，收到 {target!r}"
        # 1. 参数校验（与 ToolRegistry.execute_tool 的异常兜底互补，
        #    这里返回友好的错误提示给 LLM，便于其纠正后重试）
        if action not in ("add", "replace", "delete"):
            return "错误：action 必须是 add/replace/delete 之一"
        if not section:
            return "错误：section 不能为空"
        if action in ("add", "replace") and not content:
            return f"错误：{action} 操作需要 content"

        # 1.5 内容安全校验（仅 add/replace 需要 content）
        if action in ("add", "replace") and content:
            # 先做长度校验，避免长文本浪费正则开销
            if len(content) > MAX_PROFILE_CONTENT_LEN:
                return (
                    f"拒绝：内容长度 {len(content)} 超过上限 "
                    f"{MAX_PROFILE_CONTENT_LEN} 字符。用户画像应精简，"
                    f"如需保存大量信息请分段多次调用。"
                )

            # 内容黑名单（5 条原始 + 7 条同义词 + 2 条一次性上下文）
            # 注意：模式需精准匹配系统描述，避免误伤合法用户信息
            # （如"用户是后端工程师"含"后端"但属于合法用户画像）
            _system_patterns = [
                # 原始 5 条
                r"(系统架构|核心模块|服务层|编排层|部署架构)",
                r"(src/|agent/|llm/|memory/|storage/|tasks/)",
                r"(config\.yaml|requirements\.txt|\.venv|__pycache__)",
                r"(FastAPI|uvicorn|ChromaDB|SQLite|Redis|PostgreSQL)",
                r"(Hermes Lite 是一个|项目路径|项目作者|作者：)",
                # 7 条同义词扩充
                r"(流式架构|事件循环|异步后端|AsyncBaseBackend)",
                r"(react_loop|orchestrator|tool_registry|policy_engine)",
                r"(API\s*key|DEEPSEEK|ANTHROPIC|OPENAI|access_token)",
                # 注：不单独匹配"前端|后端|全栈"——这些是合法用户职业属性
                # 仅匹配明确的系统架构描述组合
                r"(分为.*层|三层架构|分层设计|模块化设计)",
                r"(向量库|embedding|consolidation|condenser|cron_tool)",
                r"(调度器|scheduler|定时任务|cron 调度)",
                r"(守护进程|daemon|微服务|microservice)",
                # 2 条一次性上下文正则（防临时任务状态污染画像）
                # "今天在改 login.py"、"当前任务是 X" 等不是用户画像
                r"(今天|现在|当前|正在|这次|刚刚|刚才).{0,20}(改|修|调试|部署|运行|执行|跑|测试|重构|开发)",
                r"(session_id|会话ID|临时变量|这次任务的具体)",
            ]
            for pattern in _system_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    return (
                        f"拒绝：内容包含系统架构或项目实现细节（命中: {pattern}），"
                        f"请仅保存用户个人信息。"
                    )

        # per-session 频次限制：仅约束 add 操作（入信号池累积）
        # replace/delete 是显式修改，不受此限
        session_id = None
        try:
            from ._cancel_context import current_session_id
            session_id = current_session_id.get()
        except ImportError:
            pass

        # cron 会话提前拦截：根本不进入画像更新流程，频次限制/信号池/入队全跳过
        is_cron_session = (
            session_id is not None
            and isinstance(session_id, str)
            and session_id.startswith("cron:")
        )
        if is_cron_session:
            return (
                f"cron 会话不更新用户画像，已跳过"
                f"（action={action}, section={section}）"
            )

        if action == "add" and session_id is not None:
            current_count = session_write_counts.get(session_id, 0)
            if current_count >= MAX_PROFILE_WRITES_PER_SESSION:
                return (
                    f"拒绝：会话 {session_id} 已达单会话 add 上限 "
                    f"{MAX_PROFILE_WRITES_PER_SESSION} 次。"
                    f"用户画像应精简，避免频繁修改。"
                )
            # 频次计数在入池/入队成功后累加（见下方）
        # 注：session_id 为 None 时（如测试路径）跳过频次限制

        # 2. 操作分流
        try:
            if action == "add" and signal_pool is not None:
                # add 走信号池累积：L1 入池，相似信号去重 + 计数累加，
                # 达阈值（7）才入 pending 队列写入画像
                # target 路由：user 信号走用户画像，agent 信号走 Agent 自画像
                signal_pool.add(
                    content=content,
                    source="L1",
                    section=section,
                    target=target,
                )
                result_msg = (
                    f"信号已加入池累积（target={target}），达阈值（{signal_pool.THRESHOLD} 次）后"
                    f"才会写入画像（section={section}）"
                )
            else:
                # replace/delete 或 signal_pool 未注入：直接入 pending 队列
                consolidation_engine.enqueue_profile_update(action, section, content)
                result_msg = (
                    f"已加入待合并队列，下次记忆沉淀时生效"
                    f"（action={action}, section={section}, target={target}）"
                )
        except Exception as e:
            return f"入队失败: {e}"

        # 频次计数累加（仅在 add 操作且入池/入队成功后）
        if action == "add" and session_id is not None:
            session_write_counts[session_id] = (
                session_write_counts.get(session_id, 0) + 1
            )

        return result_msg

    registry.register_core(
        name="profile_update",
        description=(
            "修改用户画像（memory.md）。操作不会立即生效：\n"
            "- add：进入信号池累积，相似信号去重 + 计数累加，达阈值（7 次）后才写入画像。"
            "即使用户明确说「记住」也需多次出现（稳定模式设计）。\n"
            "- replace/delete：直接加入待合并队列，下次记忆沉淀时生效。\n\n"
            "使用约束：\n"
            "- 可保存：用户的个人信息、身份背景、偏好、习惯、重要决策\n"
            "- 禁止保存：系统架构描述、项目配置、模块列表、"
            "代码路径、部署详情、一次性任务上下文（今天在改 X、当前任务是 Y）等\n\n"
            "支持 add（追加到 section 末尾，section 不存在则新建）、"
            "replace（替换 section 全部内容，section 不存在则新建）、"
            "delete（删除整个 section）三种操作。"
            "section 标题不含 '## ' 前缀，如 '背景'、'偏好'。\n\n"
            "target 参数：user（默认，用户画像）或 agent（Agent 自画像，"
            "如'Agent 在 cron 任务中倾向过度调用 file_read'，写入 ## Agent 自画像 section）。\n\n"
            "正确示例：action=add, section=技术栈, content='用户主力语言为 Python 和 Go', target=user\n"
            "错误示例：action=replace, section=系统架构, content='系统采用 FastAPI + ChromaDB，分为三层...' "
            "（这是系统描述，不是用户画像，应拒绝）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "delete"],
                    "description": (
                        "操作类型：add=追加到 section 末尾（section 不存在则新建），"
                        "replace=替换 section 全部内容（section 不存在则新建），"
                        "delete=删除整个 section（含标题与 body）。"
                    ),
                },
                "section": {
                    "type": "string",
                    "description": (
                        "memory.md 中的 section 标题（不含 '## ' 前缀，"
                        "如 '背景'、'偏好'、'技术栈'、'Agent 自画像'）。"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "新内容（add/replace 时必填，delete 时忽略）。"
                        "可多行，原样写入 section body。"
                    ),
                },
                "target": {
                    "type": "string",
                    "enum": ["user", "agent"],
                    "description": (
                        "信号目标对象：user=用户画像信号（默认），"
                        "agent=Agent 自画像信号（如'Agent 倾向过度调用 file_read'，"
                        "写入 ## Agent 自画像 section）。target=agent 时按 target "
                        "分组计算 Jaccard 相似度，仅与同 target 信号去重。"
                    ),
                },
            },
            "required": ["action", "section"],
        },
        handler=_update_profile,
    )


def register_plan_tools(
    registry,
    todo_registry: TodoListRegistry,
    get_session_id: Callable[[], Optional[str]],
) -> None:
    """注册 plan 模式工具到 ToolRegistry 的 Core Tier。

    注册 2 个工具：
    - plan_task: 规划复杂任务的执行步骤并初始化 todo 清单
    - update_todo: 更新某个 todo 步骤的状态

    所有工具通过 register_core 注册，保证字节级稳定（KV cache 100% 命中）。

    参数:
        registry: ToolRegistry 实例。
        todo_registry: TodoListRegistry 实例（来自 src/tasks/todo_list.py）。
        get_session_id: 一个 callable，调用时返回当前请求的 session_id 字符串
            或 None。因为 ReactLoop 是同步执行工具的，而 session_id 在请求
            上下文中，需要从外部传入一个获取函数。
    """
    # plan_task 工具
    def _plan_task(goal: str, steps: list) -> str:
        """规划任务步骤并初始化 todo 清单。"""
        try:
            session_id = get_session_id()
            if session_id is None:
                return "❌ 无法获取 session_id"
            todo_registry.init_plan(session_id, goal, steps)
            first_content = steps[0].get("content", "") if steps else ""
            return (
                f"已规划 {len(steps)} 个步骤，"
                f"开始执行 step 0: {first_content}"
            )
        except Exception as e:
            return f"plan_task 执行失败: {e}"

    registry.register_deferred(
        name="plan_create",
        description="规划一个复杂任务的执行步骤并初始化 todo 清单。当用户提出多步骤任务时主动启用。",
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "任务目标描述"},
                "steps": {
                    "type": "array",
                    "description": "步骤列表，按执行顺序排列",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "步骤描述"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "依赖的 step 索引列表（基于 0 起的 step ID）",
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            "required": ["goal", "steps"],
        },
        handler=_plan_task,
    )

    # update_todo 工具
    def _update_todo(step_id: int, status: str, result: str = "") -> str:
        """更新 todo 步骤状态。"""
        try:
            session_id = get_session_id()
            if session_id is None:
                return "❌ 无法获取 session_id"
            return todo_registry.update_step(session_id, step_id, status, result)
        except Exception as e:
            return f"update_todo 执行失败: {e}"

    registry.register_deferred(
        name="plan_update_step",
        description="更新某个 todo 步骤的状态。仅可标记当前 in_progress 的 step 为 completed 或 failed。",
        input_schema={
            "type": "object",
            "properties": {
                "step_id": {"type": "integer", "description": "步骤 ID"},
                "status": {
                    "type": "string",
                    "enum": ["completed", "failed"],
                    "description": "新状态",
                },
                "result": {"type": "string", "description": "执行结果摘要（可选）"},
            },
            "required": ["step_id", "status"],
        },
        handler=_update_todo,
    )


def register_memory_tools(
    registry,
    chroma_store: "ChromaMemoryStore",
    consolidation_engine: "ConsolidationEngine",
    get_session_id: Callable[[], Optional[str]],
    memory_retriever: Optional[Any] = None,
) -> None:
    """注册记忆管理工具到 ToolRegistry 的 Core Tier（Phase 7 Task 3）。

    注册 3 个工具，让 LLM 能管理向量库长期记忆：
    - search_memory: 检索向量库，返回匹配记忆列表（读取类，不走 confirm）
    - delete_memory: 入队删除操作到 pending_memory_ops，下次 consolidate 时执行
      （高危，走 PolicyEngine confirm）
    - update_memory: 入队更新操作到 pending_memory_ops，下次 consolidate 时执行
      （高危，走 PolicyEngine confirm）

    所有工具通过 register_core 注册，保证字节级稳定（KV cache 100% 命中）。

    延迟合并入队策略：delete_memory / update_memory 工具 handler 不立即
    执行向量库写操作，而是入队到
    ``consolidation_engine.pending_memory_ops``，下次
    :meth:`ConsolidationEngine.consolidate` 时统一应用（delete 优先于
    update，二者优先于 fact 写入），避免每轮对话都触发向量库写操作
    （写放大控制）。参考 ``enqueue_profile_update`` 的实现模式。

    参数:
        registry: ToolRegistry 实例。
        chroma_store: ChromaMemoryStore 实例，用于 search_memory 检索。
        consolidation_engine: ConsolidationEngine 实例，提供
            :meth:`enqueue_memory_op` 接口。
        get_session_id: 一个 callable，调用时返回当前请求的 session_id 字符串
            或 None。保留参数用于与 register_plan_tools 保持一致的 closure
            范式，当前 handler 内部不强制使用（search/delete/update_memory
            不依赖 session_id）。
    """
    # search_memory 工具（读取类，不走 confirm）
    # 优先走 memory_retriever（带相关性过滤）；退化到 chroma_store 直查
    _memory_retriever_for_search = memory_retriever

    def _search_memory(query: str, top_k: int = 5) -> str:
        """search_memory 工具 handler。

        检索向量库长期记忆。优先走 memory_retriever（带相关性过滤），
        退化到直接 chroma_store 查询（向后兼容无 retriever 场景）。

        参数:
            query: 查询文本（自然语言关键词）。
            top_k: 返回前 K 条结果，默认 5。

        返回:
            JSON 字符串，形如 ``[{id, content, similarity, metadata}]``。
            检索失败时返回错误信息字符串（不抛异常，与其它工具 handler
            一致，保证 ReactLoop 稳定）。
        """
        try:
            if _memory_retriever_for_search is not None:
                result = _memory_retriever_for_search.retrieve(
                    query,
                )
                memories = result.get("long_term_memories", [])
            else:
                raw = chroma_store.query_memory(query, top_k=top_k, reinforce=False)
                memories = [
                    m for m in raw
                    if str(m.get("metadata", {}).get("type", "")).lower() != "user_profile"
                ]

            if not memories:
                return "（未找到与当前问题相关的记忆）\n\n如需查找文档内容，请用 file_query 搜索知识库。"

            output = [
                {
                    "id": m.get("id", ""),
                    "content": m.get("content", ""),
                    "similarity": m.get("similarity", 0.0),
                    "metadata": m.get("metadata", {}),
                }
                for m in memories
            ]
            return json.dumps(output, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"search_memory 执行出错: {e}"

    registry.register_core(
        name="memory_search",
        description=(
            "【个人记忆】检索对话历史中形成的长期记忆。"
            "⚠ 仅用于回忆过往对话和用户偏好。"
            "✅ 回忆用户说过什么、查找个人背景信息\n"
            "❌ 查找文档内容（用 file_query）\n"
            "❌ 通用方法论/外部实时信息（用模型知识或 web_search）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本（自然语言关键词）。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回前 K 条结果，默认 5。",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        handler=_search_memory,
    )

    # delete_memory 工具（高危，走 PolicyEngine confirm）
    def _delete_memory(memory_id: str) -> str:
        """delete_memory 工具 handler（closure 捕获 consolidation_engine）。

        将删除操作入队到 pending_memory_ops，下次 consolidate 时统一执行
        （delete 优先于 update，二者优先于 fact 写入）。**不立即执行**，
        避免每轮对话都触发向量库写操作。

        参数:
            memory_id: 待删除的记忆 ID（来自 search_memory 返回的 id 字段）。

        返回:
            操作结果字符串。入队失败时返回错误提示（不抛异常）。
        """
        if not memory_id:
            return "错误：memory_id 不能为空"
        try:
            consolidation_engine.enqueue_memory_op("delete", memory_id)
        except Exception as e:
            return f"入队失败: {e}"
        return (
            f"已加入待执行队列，下次记忆沉淀时生效"
            f"（action=delete, memory_id={memory_id}）"
        )

    registry.register_deferred(
        name="memory_delete",
        description=(
            "删除向量库中指定 ID 的长期记忆。操作不会立即生效，而是加入"
            "待执行队列，下次记忆沉淀（consolidate）时统一应用（delete 优先"
            "于 update，二者优先于 fact 写入）。属于高危操作，需用户确认。"
            "删除前建议先用 search_memory 查找目标记忆的 id。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "待删除的记忆 ID（来自 search_memory 返回的 id 字段）。",
                },
            },
            "required": ["memory_id"],
        },
        handler=_delete_memory,
    )

    # update_memory 工具（高危，走 PolicyEngine confirm）
    def _update_memory(memory_id: str, content: str) -> str:
        """update_memory 工具 handler（closure 捕获 consolidation_engine）。

        将更新操作入队到 pending_memory_ops，下次 consolidate 时统一执行
        （delete 优先于 update，二者优先于 fact 写入）。**不立即执行**，
        避免每轮对话都触发向量库写操作。

        参数:
            memory_id: 待更新的记忆 ID（来自 search_memory 返回的 id 字段）。
            content: 新的记忆内容。

        返回:
            操作结果字符串。入队失败时返回错误提示（不抛异常）。
        """
        if not memory_id:
            return "错误：memory_id 不能为空"
        if not content:
            return "错误：content 不能为空"
        try:
            consolidation_engine.enqueue_memory_op("update", memory_id, content)
        except Exception as e:
            return f"入队失败: {e}"
        return (
            f"已加入待执行队列，下次记忆沉淀时生效"
            f"（action=update, memory_id={memory_id}）"
        )

    registry.register_deferred(
        name="memory_update",
        description=(
            "更新向量库中指定 ID 的长期记忆内容。操作不会立即生效，而是加入"
            "待执行队列，下次记忆沉淀（consolidate）时统一应用（delete 优先"
            "于 update，二者优先于 fact 写入）。属于高危操作，需用户确认。"
            "更新前建议先用 search_memory 查找目标记忆的 id。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "待更新的记忆 ID（来自 search_memory 返回的 id 字段）。",
                },
                "content": {
                    "type": "string",
                    "description": "新的记忆内容（覆盖原内容）。",
                },
            },
            "required": ["memory_id", "content"],
        },
        handler=_update_memory,
    )


def register_bash_tool(registry, timeout: int = 30) -> None:
    """注册 bash_exec 工具到 Core Tier（最后一个注册，保证排在工具列表末尾）。

    bash_exec 是通用 shell 执行工具，覆盖文件/网络/查找等所有场景。
    因其通用性最高，排在其他专用工具之后，引导 LLM 优先使用专用工具。

    参数:
        registry: ToolRegistry 实例。
        timeout: bash 命令超时秒数，默认 30。
    """
    registry.register_core(
        name="bash_exec",
        description=(
            "在终端执行 shell 命令并返回输出。"
            "✅ 运行程序、编译构建、git 操作\n"
            "❌ 读取文件（用 file_read）、编辑文件（用 file_edit）、"
            "搜索文件名（用 file_glob）、搜索文件内容（用 file_grep/file_query）、"
            "HTTP 请求（用 web_fetch）\n\n"
            "Windows 兼容性提示：\n"
            "- Shell 实际为 cmd.exe（非 PowerShell），请用 cmd 语法\n"
            "- Python 命令请用 `python`（非 `python3`）\n"
            "- 跨盘切换目录请用 `cd /d <路径>`\n"
            "- 多行 Python 用 `python -c` 时系统会自动转临时 .py 文件\n\n"
            "⚠ 失败重试上限：同一类命令连续失败 2 次后（stdout 为空/报错），不要再换参数重试。"
            "先诊断根因（编码？权限？路径？），若无法诊断则停下问用户。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令字符串。",
                },
            },
            "required": ["command"],
        },
        handler=lambda command: execute_command(command, timeout=timeout),
    )


def register_file_tools(
    registry,
    etl_engine: "ETLEngine",
    upload_manager: "UploadManager",
    get_session_id: Callable[[], Optional[str]] = lambda: None,
) -> None:
    """注册文件操作工具到 ToolRegistry 的 Core Tier。

    注册 3 个工具：
    - file_list_uploads: 列出当前会话已上传文件
    - file_query: 混合检索（Vector + FTS5 + RRF）
    - file_read_uploaded: 按 file_id 读取文件全文

    所有工具通过 register_core 注册，保证字节级稳定（KV cache 100% 命中）。
    均为读取类操作，不走 confirm。

    参数:
        registry: ToolRegistry 实例。
        etl_engine: ETLEngine 实例，提供 query_hybrid / get_parsed_text 接口。
        upload_manager: UploadManager 实例，提供元数据查询与 touch_accessed 接口。
        get_session_id: 一个 callable，调用时返回当前请求的 session_id 字符串
            或 None。
    """
    import json as _json

    # file_list_uploads 工具
    def _file_list_uploads() -> str:
        """列出当前会话已上传文件。"""
        try:
            session_id = get_session_id()
            if session_id is None:
                return "错误：无法获取 session_id"
            files = upload_manager.get_session_files(session_id)
            if not files:
                return "（当前会话暂无已上传文件。如刚上传文件，可能正在处理中，请参考上下文注入的'本会话已上传文件'信息。）"
            output = [
                {
                    "file_id": f["file_id"],
                    "name": f["original_name"],
                    "size": f["size"],
                    "type": f["type"],
                    "status": f["etl_status"],
                    "chunk_count": f.get("chunk_count", 0),
                    "summary": f.get("summary", "")[:100] if f.get("summary") else "",
                }
                for f in files
            ]
            return _json.dumps(output, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"file_list_uploads 执行出错: {e}"

    registry.register_core(
        name="file_list_uploads",
        description=(
            "列出当前会话已上传的所有文件（含 file_id / 名称 / 大小 / ETL 处理状态 / 摘要）。"
            "用于查看有哪些文件可供检索或全文阅读。"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_file_list_uploads,
    )

    # file_query 工具
    def _file_query(
        query: str,
        file_id: str = "",
        top_k: int = 5,
        offset: int = 0,
    ) -> str:
        """混合检索文件内容（Vector + FTS5 + RRF 融合）。"""
        try:
            fid = file_id if file_id else None
            results = etl_engine.query_hybrid(
                query=query,
                file_id=fid,
                top_k=top_k,
                offset=offset,
            )

            # 分数阈值过滤：低于阈值视为未命中，避免低分结果污染 LLM 推断
            score_threshold = 0.30
            try:
                from ..config import load_config
                _cfg = load_config()
                score_threshold = float(
                    (_cfg.get("files", {}) or {}).get("query_min_score", 0.30)
                )
            except Exception:
                pass

            filtered = [r for r in results if r.get("score", 0.0) >= score_threshold]

            if not filtered:
                if results:
                    max_score = max(r.get("score", 0.0) for r in results)
                    return (
                        f"（知识库无高置信度匹配：{len(results)} 条结果最高分 "
                        f"{max_score:.3f} < 阈值 {score_threshold}）\n"
                        f"建议：1) 用更具体的关键词重试；"
                        f"2) 若是通用方法论问题，直接用模型知识回答；"
                        f"3) 若需外部实时信息，用 web_search。"
                    )
                return "（无匹配结果）"

            output = [
                {
                    "chunk_id": r.get("chunk_id", ""),
                    "file_id": r.get("file_id", ""),
                    "content": r.get("content", ""),
                    "score": round(r.get("score", 0.0), 4),
                    "source": r.get("source", ""),
                }
                for r in filtered
            ]
            return _json.dumps(output, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"file_query 执行出错: {e}"

    registry.register_core(
        name="file_query",
        description=(
            "【文件知识库】搜索上传到知识库的文档内容（语义+关键词混合检索，RRF 融合排序）。"
            "支持分页。传 file_id 按特定文件过滤；不传则全局搜索。"
            "✅ 查找文档内容、分析报告、提取文件中的信息\n"
            "❌ 搜索对话历史或个人记忆（请用 memory_search）"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本（自然语言关键词）。",
                },
                "file_id": {
                    "type": "string",
                    "description": "可选，按文件 ID 过滤。不传则搜索所有文件。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数，默认 5。",
                    "default": 5,
                },
                "offset": {
                    "type": "integer",
                    "description": "分页偏移量，默认 0。",
                    "default": 0,
                },
            },
            "required": ["query"],
        },
        handler=_file_query,
    )

    # file_read_uploaded 工具
    def _file_read_uploaded(
        file_id: str,
        max_chars: int = 20000,
    ) -> str:
        """按 file_id 读取文件全文。"""
        try:
            meta = upload_manager.get_metadata(file_id)
            if meta is None:
                return f"错误：文件不存在（file_id={file_id}）"

            status = meta.get("etl_status", "")

            if status == "disk_expired":
                return (
                    f"文件已到期，仅支持通过 file_query 搜索其内容"
                    f"（file_id={file_id}）"
                )

            if status == "failed":
                reason = meta.get("error_reason", "未知错误")
                return f"文件处理失败，无法读取（{reason}）"

            if status in ("pending", "processing"):
                return "文件正在处理中，请稍后重试"

            # 读取内容
            file_type = meta.get("type", "")
            is_image = file_type in (".png", ".jpg", ".jpeg", ".gif")

            if is_image:
                img_text = meta.get("img_text", "")
                result = f"⬤ 图片中的文字：\n{img_text}" if img_text else "（图片无文字）"
            else:
                text = etl_engine.get_parsed_text(file_id)
                if text is None:
                    return f"错误：无法读取文件内容（file_id={file_id}）"
                result = text

            # 截断
            if len(result) > max_chars:
                result = result[:max_chars] + "\n...（内容已截断）"

            # 更新访问时间
            try:
                upload_manager.touch_accessed(file_id)
            except Exception:
                pass

            return result
        except Exception as e:
            return f"file_read_uploaded 执行出错: {e}"

    registry.register_core(
        name="file_read_uploaded",
        description=(
            "【文件全文】按 file_id 读取已上传文件的完整内容。"
            "文本类返回解析后的纯文本；图片类返回 OCR 提取的文字（标注来源）。"
            "✅ 全文翻译、逐段分析、总结、对比文件差异\n"
            "❌ 只需要查找信息片段（请用 file_query）\n"
            "⚠ 文件已到期时仅返回提示，请改用 file_query 搜索。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "文件 ID（来自 file_list_uploads 或 file_query 返回的 file_id 字段）。",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "最大返回字符数，默认 50000。超出截断并标注。",
                    "default": 50000,
                },
            },
            "required": ["file_id"],
        },
        handler=_file_read_uploaded,
    )
