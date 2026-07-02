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
_MAX_HTTP_BODY_CHARS = 10000

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


def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """读取文件内容。

    参数:
        path: 文件路径。
        offset: 可选，起始行号（从 0 开始）。0 表示从文件开头读取。
        limit: 可选，最多读取的行数。0 表示读取全部行。

    返回:
        文件内容字符串。读取失败时返回错误信息。
    """
    try:
        p = Path(path)
        if offset > 0 or limit > 0:
            lines = p.read_text(encoding="utf-8").splitlines()
            start = max(0, offset)
            end = start + limit if limit > 0 else len(lines)
            return "\n".join(lines[start:end])
        return p.read_text(encoding="utf-8")
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

    # ── 构建 Popen 参数（含进程组/会话隔离） ──
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
        }
    else:
        popen_args = {
            "args": command,
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }

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





# 工具定义列表：[(name, description, input_schema, handler), ...]
BUILTIN_TOOLS = [
    (
        "file_read",
        "读取指定路径文件的内容并返回文本。读取文件应优先使用此工具，而非通过 bash_exec 执行 cat/type 命令——本工具更安全、无需 shell 权限、自动处理编码。",
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
        "- 永久或反爬错误响应末尾会附加 [系统提示] 引导改正策略，请注意阅读。",
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
]


def register_builtin_tools(
    registry,
    file_registry: Optional["FileOperationRegistry"] = None,
    get_session_id: Callable[[], Optional[str]] = lambda: None,
    consolidation_engine: Optional["ConsolidationEngine"] = None,
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
      采用延迟合并写入策略——handler 仅将操作入队到
      ``consolidation_engine.pending_profile_updates``，下次 consolidate 时
      统一合并到 memory.md，避免每轮缓存失效。为 ``None`` 时不注册（向后兼容）。
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
        _register_update_profile(registry, consolidation_engine)

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
            "调用一个已通过 list_tools 加载的工具。如果工具未加载，先调用 list_tools。"
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
) -> None:
    """注册 update_profile 工具到 Core Tier。

    工具允许 LLM 通过 add/replace/delete 三种操作显式修改用户画像
    memory.md。采用**延迟合并写入**策略：handler 不立即写 memory.md，
    而是将操作入队到 ``consolidation_engine.pending_profile_updates``，
    下次 :meth:`ConsolidationEngine.consolidate` 时统一合并到 memory.md，
    避免每轮对话都让 system prompt 的缓存命中区失效。

    handler 通过 closure 捕获 ``consolidation_engine`` 实例。ToolRegistry
    调用 handler 时按关键字参数传入 tool_input（``action`` / ``section``
    / ``content``），由 handler 内部校验后入队。

    参数:
        registry: ToolRegistry 实例。
        consolidation_engine: ConsolidationEngine 实例，提供
            :meth:`enqueue_profile_update` 接口。
    """

    def _update_profile(action: str, section: str, content: str = "") -> str:
        """update_profile 工具 handler（closure 捕获 consolidation_engine）。

        参数:
            action: 操作类型，``"add"`` / ``"replace"`` / ``"delete"`` 之一。
            section: memory.md 中的 section 标题（不含 ``## `` 前缀）。
            content: 新内容（add/replace 时必填，delete 时忽略）。

        返回:
            操作结果字符串。校验失败时返回错误提示（不抛异常，
            与其它工具 handler 一致，保证 ReactLoop 稳定）。
        """
        # 1. 参数校验（与 ToolRegistry.execute_tool 的异常兜底互补，
        #    这里返回友好的错误提示给 LLM，便于其纠正后重试）
        if action not in ("add", "replace", "delete"):
            return "错误：action 必须是 add/replace/delete 之一"
        if not section:
            return "错误：section 不能为空"
        if action in ("add", "replace") and not content:
            return f"错误：{action} 操作需要 content"

        # 2. 入队（不立即写 memory.md，下次 consolidate 时统一合并）
        try:
            consolidation_engine.enqueue_profile_update(action, section, content)
        except Exception as e:
            return f"入队失败: {e}"

        return (
            f"已加入待合并队列，下次记忆沉淀时生效"
            f"（action={action}, section={section}）"
        )

    registry.register_core(
        name="profile_update",
        description=(
            "修改用户画像（memory.md）。操作不会立即生效，而是加入待合并队列，"
            "下次记忆沉淀时统一写入，避免每轮缓存失效。"
            "支持 add（追加到 section 末尾，section 不存在则新建）、"
            "replace（替换 section 全部内容，section 不存在则新建）、"
            "delete（删除整个 section）三种操作。"
            "section 标题不含 '## ' 前缀，如 '背景'、'偏好'。"
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
                        "如 '背景'、'偏好'、'技术栈'）。"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "新内容（add/replace 时必填，delete 时忽略）。"
                        "可多行，原样写入 section body。"
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
            "❌ 查找文档内容、分析报告、搜索信息（请先用 file_query）\n"
            "❌ 如果你不确定信息在哪，先试 file_query（知识库比记忆更完整）"
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
            "HTTP 请求（用 web_fetch）"
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
                return "（当前会话无已上传文件）"
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
            if not results:
                return "（无匹配结果）"
            output = [
                {
                    "chunk_id": r.get("chunk_id", ""),
                    "file_id": r.get("file_id", ""),
                    "content": r.get("content", ""),
                    "score": round(r.get("score", 0.0), 4),
                    "source": r.get("source", ""),
                }
                for r in results
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
        max_chars: int = 50000,
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
