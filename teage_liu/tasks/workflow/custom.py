"""custom 工作流模板（Phase 8 Task 5.8 实装）。

引用 ``cron_tool`` 动态工具系统（Layer 2 能力扩展），从调度项配置读取
cron_tool 名，调用 :func:`cron_tool_loader.execute_tool` 子进程执行，
将工具结果注入 LLM 上下文生成报告。

执行流程（确定性步骤 + LLM 步骤两阶段）：
1. **确定性步骤**：从 ``config.tool_name`` 读取 cron_tool 名，组装 input +
   context，调用 ``cron_tool_loader.execute_tool`` 子进程执行。
2. **LLM 步骤**：单轮调用（``max_loops=1``）让 LLM 基于工具结果生成报告，
   写入 ``report_dir/custom_{tool_name}_{date}.md``。

报告文件名由代码生成（SubTask 2.7 第二层：文件名层）：使用
``current_time.strftime("%Y%m%d")`` 生成日期。

缓存约束：
- :meth:`build_system_prompt` 返回固定 prompt，不含动态变量
- 时间变量只在 LLM 用户输入层替换（通过 :meth:`WorkflowContext.render`）
- cron_tool 子进程执行结果属缓存失效区（注入 messages[0]），不影响
  system_text 稳定性

模块依赖：
- :func:`cron_tool_loader.execute_tool`（子进程执行 + 结构化错误返回）
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

from .base import WorkflowContext, WorkflowResult, WorkflowTemplate

# Phase 8 Task 5.8: cron_tool_loader 懒导入（避免 import 阶段强依赖）
from teage_liu.tasks.cron_tool_loader import (
    DEFAULT_BASE_DIR as _CRON_TOOL_BASE_DIR,
    DEFAULT_TIMEOUT as _CRON_TOOL_DEFAULT_TIMEOUT,
    execute_tool as _execute_cron_tool,
)
logger = logging.getLogger(__name__)


# 固定 system prompt（缓存约束 5：禁含动态变量）
_CUSTOM_SYSTEM_PROMPT = (
    "你是一个自动化任务分析助手。基于给定的 cron_tool 执行结果，"
    "生成简洁的 markdown 报告。报告应包含：\n"
    "1. 工具执行摘要（工具名、执行状态、关键输出）\n"
    "2. 结果分析（基于工具输出推断当前状态或趋势）\n"
    "3. 后续建议（如有需要进一步操作或关注的事项）\n"
    "回答使用中文，控制在 300 字以内。"
)

# 默认 LLM 用户输入模板（含 {tool_result} 占位符，由 execute 填充）
_DEFAULT_USER_INPUT_TEMPLATE = (
    "当前时间: {now}\n"
    "上次执行时间: {last_run_time}\n"
    "调度项: {schedule_id}\n"
    "cron_tool: {tool_name}\n\n"
    "## 工具执行结果\n"
    "{tool_result}\n\n"
    "请基于上述工具执行结果生成 markdown 报告。"
)


def _sanitize_tool_name(name: str) -> str:
    """将工具名转为安全的文件名片段（去除 ``/`` ``\\`` 等）。"""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", name or "tool")
    return safe or "tool"


class CustomTemplate(WorkflowTemplate):
    """自定义工作流模板（cron_tool 引用，Phase 8 Task 5.8 实装）。

    配置字段（``config`` dict）：
    - ``tool_name``（必填）：cron_tool 名称（与 ``cron_tool/{name}/`` 目录名一致）
    - ``input``（可选）：传给 cron_tool 的输入参数 dict，默认 ``{}``
    - ``timeout``（可选）：子进程超时秒数，默认 ``None``（使用 TOOL.md 的
      ``timeout`` 字段或 :data:`DEFAULT_TIMEOUT`）
    - ``base_dir``（可选）：cron_tool 根目录，默认 :data:`DEFAULT_BASE_DIR`
    - ``llm_step``（可选）：是否在 cron_tool 执行后调用 LLM 生成报告，
      默认 ``True``。为 ``False`` 时仅执行 cron_tool，跳过 LLM 步骤
      （适合纯数据采集场景，结果直接写入 metrics_for_injection）
    - ``user_input_template``（可选）：LLM 用户输入模板，含 ``{tool_result}``
      / ``{tool_name}`` / ``{schedule_id}`` / ``{now}`` / ``{last_run_time}``
      占位符。为 ``None`` 时使用 :data:`_DEFAULT_USER_INPUT_TEMPLATE`

    输出：
    - 报告文件：``{report_dir}/custom_{tool_name}_{date}.md``（仅 ``llm_step=True``）
    - ``metrics_for_injection``：``{"cron_tool": tool_name, "执行状态": success/failed, "结果长度": N}``
    - ``tool_calls``：``[{"name": tool_name, "input": input, "result": ..., "is_error": bool}]``
    """

    name = "custom"

    def build_system_prompt(self) -> str:
        """返回固定的 custom system prompt（不含动态变量）。"""
        return _CUSTOM_SYSTEM_PROMPT

    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:
        """执行 custom 工作流模板（cron_tool 子进程执行 + LLM 报告生成）。

        参数:
            config: 模板配置，必含 ``tool_name`` 字段。
            context: 工作流执行上下文。

        返回:
            :class:`WorkflowResult`，含 cron_tool 执行结果与 LLM 报告（如启用）。
        """
        result = WorkflowResult()

        # 1. 校验配置
        tool_name = config.get("tool_name")
        if not tool_name:
            result.add_error("配置缺少 tool_name 字段")
            return result

        if _execute_cron_tool is None:
            result.add_error(
                "cron_tool_loader 模块不可用，无法执行 cron_tool"
            )
            return result

        tool_input = config.get("input") or {}
        if not isinstance(tool_input, dict):
            result.add_error(
                f"input 字段必须为 dict，实际类型: {type(tool_input).__name__}"
            )
            return result

        timeout = config.get("timeout")
        base_dir = config.get("base_dir") or _CRON_TOOL_BASE_DIR
        llm_step = bool(config.get("llm_step", True))
        user_input_template = (
            config.get("user_input_template") or _DEFAULT_USER_INPUT_TEMPLATE
        )

        # 2. 确定性步骤：调用 cron_tool_loader.execute_tool 子进程执行
        #    组装 context dict（注入 session_id / schedule_id / current_time，
        #    供 run.* 脚本通过 stdin JSON 读取）
        cron_context = {
            "session_id": context.session_id,
            "schedule_id": context.schedule_id,
            "current_time": context.current_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        if context.last_run_time is not None:
            cron_context["last_run_time"] = context.last_run_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        try:
            tool_result_text = _execute_cron_tool(
                name=tool_name,
                input=tool_input,
                context=cron_context,
                timeout=timeout,
                base_dir=base_dir,
            )
            is_error = False
        except Exception as exc:
            # execute_tool 正常情况下不抛异常（返回错误字符串），此处兜底
            logger.error(
                "cron_tool %s 子进程执行异常: %s", tool_name, exc
            )
            tool_result_text = f"cron_tool {tool_name} 执行异常: {exc}"
            is_error = True

        # 记录工具调用（供 RunSummary 持久化）
        result.tool_calls.append(
            {
                "name": tool_name,
                "input": tool_input,
                "result": tool_result_text,
                "is_error": is_error,
            }
        )

        if is_error:
            result.add_error(f"cron_tool {tool_name} 执行失败")
            result.metrics_for_injection = {
                "cron_tool": tool_name,
                "执行状态": "failed",
                "结果长度": len(tool_result_text),
            }
            # 执行失败时跳过 LLM 步骤（无有效结果可分析）
            return result

        # 3. 填充 metrics_for_injection（注入下一轮 cron 上下文 messages[0]）
        result.metrics_for_injection = {
            "cron_tool": tool_name,
            "执行状态": "success",
            "结果长度": len(tool_result_text),
        }

        # 4. LLM 步骤（可选）：基于工具结果生成报告
        if not llm_step:
            # 纯数据采集场景，跳过 LLM 调用
            result.assistant_response = ""
            return result

        if context.llm_client is None:
            logger.warning(
                "llm_client 未注入，custom 模板跳过 LLM 报告生成步骤"
            )
            return result

        # 构建 LLM 用户输入（替换 {tool_result} / {tool_name} / {schedule_id}）
        user_input = user_input_template.format(
            tool_result=tool_result_text,
            tool_name=tool_name,
            schedule_id=context.schedule_id,
            now=context.current_time.strftime("%Y-%m-%d %H:%M:%S"),
            last_run_time=(
                context.last_run_time.strftime("%Y-%m-%d %H:%M:%S")
                if context.last_run_time
                else "首次执行"
            ),
            today=context.current_time.strftime("%Y-%m-%d"),
            this_week_start=self._get_week_start_str(context.current_time),
        )
        # 时间变量替换（SubTask 2.7：在用户输入层替换，兼容模板中残留的占位符）
        user_input = context.render(user_input)

        system_prompt = self.build_system_prompt()
        response_text, llm_tool_calls = self._call_llm_single_turn(
            context, user_input, system=system_prompt
        )
        result.assistant_response = response_text
        result.tool_calls.extend(llm_tool_calls)

        # 5. 写报告文件（文件名层：current_time.strftime 生成日期）
        try:
            report_path = self._write_report(
                context, tool_name, tool_result_text, response_text
            )
            result.outputs.append({"path": report_path, "type": "report"})
        except Exception as e:
            result.add_error(f"写报告文件失败: {e}")

        return result

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _get_week_start_str(self, current_time) -> str:
        """返回本周一日期字符串（``%Y-%m-%d``，周一为一周起点）。"""
        from datetime import timedelta

        this_week_start = current_time - timedelta(
            days=current_time.weekday()
        )
        return this_week_start.strftime("%Y-%m-%d")

    def _write_report(
        self,
        context: WorkflowContext,
        tool_name: str,
        tool_result: str,
        llm_response: str,
    ) -> str:
        """写报告文件，返回路径。

        文件名格式：``custom_{tool_name}_{date}.md``（SubTask 2.7 第二层）。
        """
        context.ensure_report_dir()
        safe_name = _sanitize_tool_name(tool_name)
        date_str = context.current_time.strftime("%Y%m%d")
        # D4 修复：追加 run_id[:8] 后缀避免同日多次触发覆盖
        run_id = getattr(context, "run_id", None) or "unknown"
        run_id_suffix = run_id[:8] if isinstance(run_id, str) else "unknown"
        filename = f"custom_{safe_name}_{date_str}_{run_id_suffix}.md"
        report_path = os.path.join(context.report_dir, filename)

        # 报告内容：工具执行摘要 + LLM 分析
        summary_lines = [
            "# cron_tool 执行报告",
            "",
            f"- cron_tool: `{tool_name}`",
            f"- 报告时间: {context.current_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 上次执行: "
            f"{context.last_run_time.strftime('%Y-%m-%d %H:%M:%S') if context.last_run_time else '首次执行'}",
            f"- 调度项: `{context.schedule_id}`",
            "",
            "## 工具执行结果",
            "",
            "```",
            tool_result,
            "```",
            "",
            "## LLM 分析",
            "",
            llm_response or "（LLM 未生成回复）",
            "",
        ]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))
        return report_path
