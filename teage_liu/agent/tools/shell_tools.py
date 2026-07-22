"""Shell 工具：execute_command, kill_running_process, register_bash_tool。

提供终端命令执行能力，包括：
- 简单命令（无 shell 元字符）使用 shell=False 安全执行
- 复杂命令（含管道/重定向/链式执行）保留 shell=True 执行
- 跨线程进程追踪与强制终止（force kill）
- Windows + python -c 多行代码兼容（自动改写为临时 .py 文件）
- 中断检查（ContextVar 协作式取消）
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import sys as _sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_SHELL_META_RE = re.compile(r'[|>&;`$(){}!~]|&&|\|\|')

# 工具输出截断阈值（字符数），与 file_read 的 max_chars 对齐
# 避免大量命令输出（如 dir /s、cat 大日志）注入 LLM 上下文导致 token 预算耗尽
_MAX_OUTPUT_CHARS = int(os.environ.get("TEAGE_MAX_OUTPUT_CHARS", "20000"))


def _truncate_output(text: str) -> str:
    """对命令输出做长度截断。

    超过 _MAX_OUTPUT_CHARS 的输出截断为前 N 字符 + 原文长度提示。
    提示格式: ``...[truncated, original {N} chars]``

    Args:
        text: 原始输出文本（stdout / stderr 拼接后的最终输出）。

    Returns:
        截断后的文本。若未超阈值则原样返回。
    """
    if not isinstance(text, str):
        return text
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n...[truncated, original {len(text)} chars]"


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
        from .._cancel_context import current_cancel_event
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
    tmp_filename = f"teage_exec_{uuid4().hex[:8]}.py"
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
    return _truncate_output(output)


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


__all__ = [
    "execute_command",
    "kill_running_process",
    "register_bash_tool",
    "_has_shell_metachar",
    "_set_running_proc",
    "_get_and_clear_running_proc",
    "_maybe_rewrite_multiline_python_c",
    "_execute_command_inner",
]
