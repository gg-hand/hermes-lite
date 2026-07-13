"""上下文构建:环境信息 + TODO 格式化 + 续接消息。

从 Orchestrator 提取的自包含辅助方法:
- build_environment: 构建运行环境信息段（OS/Shell/Python/CWD）
- format_todo: 将 TodoList dict 格式化为"## 当前计划进度"段
- has_unfinished_steps: 检查 TodoList 是否有未完成步骤
- build_continuation_message: 构造自动续接消息

这些方法不依赖 Orchestrator 实例状态，可独立测试。
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess as _sp
import sys as _sys
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class ContextBuilder:
    """构建 LLM 上下文的辅助方法。

    所有方法均为自包含（不依赖外部状态），可直接实例化使用。
    """

    def build_environment(self) -> str:
        """构造运行环境信息段（跨平台自适应）。

        采集当前进程的运行时环境信息（OS、工作目录、Shell、Python 启动
        命令与路径），用于注入到 messages[0] 顶部（缓存失效区，不污染
        system_text），帮助 LLM 感知运行环境以生成更贴合环境的指令。
        """
        os_name = f"{platform.system()} {platform.release()}"

        if platform.system() == "Windows":
            shell = "PowerShell 7 (pwsh)" if shutil.which("pwsh") else "cmd.exe"
        else:
            shell = "bash" if shutil.which("bash") else "sh"

        try:
            _sp.check_output(
                ["python", "--version"], stderr=_sp.STDOUT, timeout=3
            ).decode().strip()
            python_cmd = "python"
        except (FileNotFoundError, _sp.SubprocessError, OSError):
            python_cmd = "python3"

        python_path = _sys.executable
        cwd = os.getcwd()

        env_lines = [
            "## 运行环境",
            f"- 操作系统: {os_name}",
            f"- 工作目录: {cwd}",
            f"- Shell: {shell}",
            f"- Python 启动命令: {python_cmd}",
            f"- Python 路径: {python_path}",
            f"- 当前日期: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)",
        ]
        if platform.system() == "Windows":
            env_lines.append("- 提示: 跨盘切换目录请用 `cd /d <路径>`（如 cd /d E:\\proj）")
        return "\n".join(env_lines)

    def format_todo(self, todo_dict: Optional[dict]) -> str:
        """将 TodoList dict 格式化为可注入 messages[0] 的"## 当前计划进度"段。

        step 状态映射规则:
        - completed → [x]
        - pending / in_progress / failed → [ ]
        """
        if not todo_dict:
            return ""
        steps = todo_dict.get("steps") or []
        if not steps:
            return ""

        goal = todo_dict.get("goal", "") or ""
        completed_count = sum(1 for s in steps if s.get("status") == "completed")
        total = len(steps)

        step_lines = []
        for s in steps:
            mark = "[x]" if s.get("status") == "completed" else "[ ]"
            content = s.get("content", "") or ""
            step_lines.append(f"{mark} {content}")
        steps_block = "\n".join(step_lines)

        return (
            "## 当前计划进度\n\n"
            f"**目标**: {goal}\n\n"
            f"**总进度**: {completed_count}/{total}\n\n"
            "**步骤**:\n"
            f"{steps_block}\n\n"
            "提醒：每完成一个步骤，必须调用 update_todo 标记为 completed"
        )

    @staticmethod
    def has_unfinished_steps(todo_dict: Optional[dict]) -> bool:
        """检查 TodoList 是否有未完成步骤。"""
        if not todo_dict:
            return False
        steps = todo_dict.get("steps") or []
        if not steps:
            return False
        return any(s.get("status") != "completed" for s in steps)

    def build_continuation_message(self, todo_dict: Optional[dict]) -> str:
        """构造自动续接消息。

        当 react_loop.run 返回 is_complete=False 且 TodoList 有未完成步骤时，
        用此消息作为下一轮 user_input 继续 React 循环。
        """
        if not todo_dict or not todo_dict.get("steps"):
            return (
                "上一轮已达循环上限。请继续完成剩余步骤，无需重复已完成的工作。"
            )
        goal = todo_dict.get("goal", "") or ""
        steps = todo_dict.get("steps") or []
        completed_count = sum(
            1 for s in steps if s.get("status") == "completed"
        )
        total = len(steps)
        unfinished = [
            s.get("content", "")
            for s in steps
            if s.get("status") != "completed"
        ]
        unfinished_block = "\n".join(
            f"- {c}" for c in unfinished if c
        )
        return (
            "上一轮已达循环上限。"
            f"当前进度：目标「{goal}」，已完成 {completed_count}/{total}。"
            f"未完成步骤：\n{unfinished_block}\n"
            "请继续完成剩余步骤，无需重复已完成的工作。"
        )
