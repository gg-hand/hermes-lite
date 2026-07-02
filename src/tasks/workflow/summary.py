"""summary 工作流模板（Phase 8 Task 2.3）。

总结指定会话历史并 LLM 提炼要点。

执行流程：
1. **确定性步骤**：从 ``session_logger`` 读取指定 ``session_id`` 的会话历史
   （纯文本对话消息，过滤工具调用记录）。
2. **LLM 步骤**：单轮调用（``max_loops=1``）让 LLM 总结提炼，输出 markdown
   摘要，写入 ``report_dir/summary_{session_id}_{date}.md``。

缓存约束：
- :meth:`build_system_prompt` 返回固定 prompt，不含动态变量
- 时间变量只在 LLM 用户输入层替换
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from .base import WorkflowContext, WorkflowResult, WorkflowTemplate

logger = logging.getLogger(__name__)


# 固定 system prompt（缓存约束 5：禁含动态变量）
_SUMMARY_SYSTEM_PROMPT = (
    "你是一个会话总结助手。基于给定的对话历史，提炼关键信息与决策点，"
    "生成简洁的 markdown 摘要。摘要应包含：\n"
    "1. 主要话题（1-3 个）\n"
    "2. 关键决策与结论\n"
    "3. 待办事项或后续行动（如有）\n"
    "回答使用中文，控制在 500 字以内。"
)


def _sanitize_session_id(session_id: str) -> str:
    """将 session_id 转为安全的文件名片段。"""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", session_id)
    return safe or "session"


class SummaryTemplate(WorkflowTemplate):
    """会话总结工作流模板。

    配置字段（``config`` dict）：
    - ``session_id``（必填）：待总结的会话 ID
    - ``max_messages``（可选）：最多读取消息数，默认 ``100``
    - ``session_logger``（可选）：SessionLogger 实例。为 ``None`` 时尝试从
      ``context`` 的 ``session_logger`` 属性获取（CronScheduler 在调用时
      可注入）。

    输出：
    - 报告文件：``{report_dir}/summary_{session_id}_{date}.md``
    - ``metrics_for_injection``：``{"消息数": N, "会话ID": session_id}``
    """

    name = "summary"

    def build_system_prompt(self) -> str:
        """返回固定的总结 system prompt（不含动态变量）。"""
        return _SUMMARY_SYSTEM_PROMPT

    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:
        result = WorkflowResult()

        target_session_id = config.get("session_id")
        if not target_session_id:
            result.add_error("配置缺少 session_id 字段")
            return result

        max_messages = int(config.get("max_messages", 100))
        session_logger = config.get("session_logger") or getattr(
            context, "session_logger", None
        )

        # 1. 确定性步骤：读取会话历史
        messages: List[Dict[str, Any]] = []
        if session_logger is not None:
            try:
                rows = self._read_session_messages(
                    session_logger, target_session_id, max_messages
                )
                messages = rows
            except Exception as e:
                result.add_error(f"读取会话历史失败: {e}")
                return result
        else:
            result.add_error("session_logger 未配置，无法读取会话历史")
            return result

        if not messages:
            result.metrics_for_injection = {
                "消息数": 0,
                "会话ID": target_session_id,
            }
            result.assistant_response = "（无会话历史可总结）"
            return result

        # 2. 填充 metrics_for_injection
        result.metrics_for_injection = {
            "消息数": len(messages),
            "会话ID": target_session_id,
        }

        # 3. LLM 步骤：单轮调用总结
        user_input = self._build_llm_input(target_session_id, messages, context)
        user_input = context.render(user_input)

        system_prompt = self.build_system_prompt()
        response_text, tool_calls = self._call_llm_single_turn(
            context, user_input, system=system_prompt
        )
        result.assistant_response = response_text
        result.tool_calls.extend(tool_calls)

        # 4. 写报告文件
        try:
            report_path = self._write_report(
                context, target_session_id, messages, response_text
            )
            result.outputs.append({"path": report_path, "type": "report"})
        except Exception as e:
            result.add_error(f"写报告文件失败: {e}")

        return result

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _read_session_messages(
        self, session_logger: Any, session_id: str, max_messages: int
    ) -> List[Dict[str, Any]]:
        """从 session_logger 读取指定会话的纯文本对话消息。

        过滤掉 ``tool_name`` 非空的消息（工具调用记录），仅保留 user /
        assistant 文本对话。
        """
        # 优先使用 get_session_messages（按时间正序）
        get_session_messages = getattr(session_logger, "get_session_messages", None)
        if callable(get_session_messages):
            rows = get_session_messages(session_id, limit=max_messages)
        else:
            get_recent = getattr(session_logger, "get_recent_messages", None)
            if not callable(get_recent):
                return []
            rows = get_recent(session_id, n=max_messages)

        # 过滤工具消息 + 仅保留 role/content
        messages: List[Dict[str, Any]] = []
        for row in rows or []:
            if row.get("tool_name"):
                continue
            role = row.get("role", "user")
            content = row.get("content", "")
            if not content:
                continue
            messages.append({"role": role, "content": content})
        return messages

    def _build_llm_input(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        context: WorkflowContext,
    ) -> str:
        """构建 LLM 用户输入文本（含时间变量占位符）。"""
        # 截断过长的历史，避免 token 超限
        max_show = 50
        shown = messages[:max_show]
        lines = [
            f"当前时间: {{now}}",
            f"上次执行时间: {{last_run_time}}",
            f"待总结会话 ID: {session_id}",
            f"消息总数: {len(messages)}（展示前 {len(shown)} 条）",
            "",
            "## 对话历史",
        ]
        for msg in shown:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(f"**{role}**: {content}")
            lines.append("")
        lines.append("请基于上述对话历史生成 markdown 摘要。")
        return "\n".join(lines)

    def _write_report(
        self,
        context: WorkflowContext,
        session_id: str,
        messages: List[Dict[str, Any]],
        llm_response: str,
    ) -> str:
        """写报告文件，返回路径。"""
        context.ensure_report_dir()
        safe_id = _sanitize_session_id(session_id)
        date_str = context.current_time.strftime("%Y%m%d")
        filename = f"summary_{safe_id}_{date_str}.md"
        report_path = os.path.join(context.report_dir, filename)

        summary_lines = [
            f"# 会话总结报告",
            f"",
            f"- 会话 ID: `{session_id}`",
            f"- 报告时间: {context.current_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 上次执行: "
            f"{context.last_run_time.strftime('%Y-%m-%d %H:%M:%S') if context.last_run_time else '首次执行'}",
            f"- 消息数: {len(messages)}",
            "",
            "## LLM 摘要",
            "",
            llm_response or "（LLM 未返回内容）",
            "",
        ]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))
        return report_path
