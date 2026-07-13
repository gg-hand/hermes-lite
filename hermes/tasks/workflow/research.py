"""research 工作流模板（Phase 8 Task 2.6）。

LLM 自主研究模板，无确定性步骤。

执行流程：
1. LLM 在白名单工具范围内自主调用工具进行研究（``max_loops`` 可配置，
   默认 ``5``）。
2. 通过 ``ReactLoop.run`` 实现多轮工具调用循环，最终输出 markdown 报告。

与其他模板的区别：
- 无确定性步骤（不预填 ``metrics_for_injection``）
- ``max_loops`` 可配置（其他模板固定为 1）
- 通过 ``ReactLoop`` 执行实际工具调用，而非单轮 ``chat_main``

缓存约束：
- :meth:`build_system_prompt` 返回固定 prompt，不含动态变量
- 时间变量只在 LLM 用户输入层替换
- 工具白名单通过 ``config.tool_whitelist`` 指定，请求级过滤不污染全局
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from .base import WorkflowContext, WorkflowResult, WorkflowTemplate

logger = logging.getLogger(__name__)


# 固定 system prompt（缓存约束 5：禁含动态变量）
_RESEARCH_SYSTEM_PROMPT = (
    "你是一个研究助手。基于用户给定的研究主题，自主使用可用工具收集信息并"
    "生成研究报告。研究应包含：\n"
    "1. 主题背景与关键概念\n"
    "2. 主要发现或数据点（基于工具返回结果）\n"
    "3. 结论与建议\n"
    "回答使用中文，结构清晰。工具调用需克制，避免不必要的重复查询。"
)


class ResearchTemplate(WorkflowTemplate):
    """LLM 自主研究工作流模板。

    配置字段（``config`` dict）：
    - ``topic``（必填）：研究主题
    - ``max_loops``（可选）：最大 React 循环次数，默认 ``5``
    - ``tool_whitelist``（可选）：允许的工具名列表。为 ``None`` 时使用
      ``context`` 中 ``tool_registry`` 的全部工具
    - ``react_loop``（可选）：ReactLoop 实例。为 ``None`` 时降级为单轮
      LLM 调用（``max_loops=1`` 语义）

    输出：
    - 报告文件：``{report_dir}/research_{topic}_{date}.md``
    - ``metrics_for_injection``：``{"研究主题": topic, "工具调用数": N}``
    - ``tool_calls``：实际执行的工具调用列表（含 result 字段）
    """

    name = "research"

    def build_system_prompt(self) -> str:
        """返回固定的研究 system prompt（不含动态变量）。"""
        return _RESEARCH_SYSTEM_PROMPT

    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:
        result = WorkflowResult()

        topic = config.get("topic")
        if not topic:
            result.add_error("配置缺少 topic 字段")
            return result

        max_loops = int(config.get("max_loops", 5))
        tool_whitelist = config.get("tool_whitelist")
        react_loop = config.get("react_loop") or getattr(
            context, "react_loop", None
        )

        # 1. 构建用户输入（含时间变量占位符，由 context.render 替换）
        user_input = self._build_llm_input(topic, context)
        user_input = context.render(user_input)

        system_prompt = self.build_system_prompt()

        # 2. 执行 React 循环（若可用）或降级为单轮调用
        if react_loop is not None:
            try:
                # 请求级工具过滤（缓存约束：不修改全局 tool_registry）
                self._apply_tool_whitelist(react_loop, tool_whitelist)
                # react_loop.run 返回四元组 (text, messages, is_complete, termination_reason)
                # workflow 路径不参与自动续接（调度器驱动，单次执行即结束），
                # is_complete 与 termination_reason 仅解包用于 metrics 上报。
                # D7 修复：try asyncio.run + except RuntimeError 兜底
                # run_coroutine_threadsafe，避免在已有事件循环的线程中抛错。
                try:
                    response_text, messages_used, _is_complete, _termination_reason = asyncio.run(
                        react_loop.run(
                            user_input=user_input,
                            history=[],
                            system=system_prompt,
                            session_id=context.session_id,
                        )
                    )
                except RuntimeError as _run_err:
                    # asyncio.run 在已有事件循环的线程中抛 RuntimeError
                    logger.info(
                        "asyncio.run 不可用（已有事件循环），降级到 "
                        "run_coroutine_threadsafe: %s",
                        _run_err,
                    )
                    try:
                        loop = asyncio.get_event_loop_policy().get_event_loop()
                    except Exception as _loop_err:
                        raise RuntimeError(
                            f"获取事件循环失败: {_loop_err}"
                        ) from _loop_err
                    if loop is None or not loop.is_running():
                        raise RuntimeError(
                            f"事件循环不可用，无法执行 react_loop: {_run_err}"
                        )
                    future = asyncio.run_coroutine_threadsafe(
                        react_loop.run(
                            user_input=user_input,
                            history=[],
                            system=system_prompt,
                            session_id=context.session_id,
                        ),
                        loop,
                    )
                    response_text, messages_used, _is_complete, _termination_reason = (
                        future.result()
                    )
                # 反馈监控：上报终止原因（react_loop.metrics 为 None 时跳过）
                _metrics = getattr(react_loop, "metrics", None)
                if _metrics is not None:
                    try:
                        _metrics.observe_termination(_termination_reason)
                    except Exception:
                        pass
                result.assistant_response = response_text
                # 从 messages_used 提取工具调用列表
                result.tool_calls = self._extract_tool_calls(messages_used)
            except Exception as e:
                result.add_error(f"ReactLoop 执行失败: {e}")
                # 降级到单轮调用
                response_text, tool_calls = self._call_llm_single_turn(
                    context, user_input, system=system_prompt
                )
                result.assistant_response = response_text
                result.tool_calls.extend(tool_calls)
        else:
            # 无 react_loop：降级为单轮调用
            response_text, tool_calls = self._call_llm_single_turn(
                context, user_input, system=system_prompt
            )
            result.assistant_response = response_text
            result.tool_calls.extend(tool_calls)

        # 3. 填充 metrics_for_injection
        result.metrics_for_injection = {
            "研究主题": topic,
            "工具调用数": len(result.tool_calls),
            "最大循环数": max_loops,
        }

        # 4. 写报告文件
        try:
            report_path = self._write_report(context, topic, result)
            result.outputs.append({"path": report_path, "type": "report"})
        except Exception as e:
            result.add_error(f"写报告文件失败: {e}")

        return result

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _apply_tool_whitelist(
        self, react_loop: Any, whitelist: Optional[List[str]]
    ) -> None:
        """请求级工具白名单过滤。

        若 ``react_loop.tool_registry`` 支持 ``get_tools_schema``，则
        替换为过滤后的 schema 列表（不修改全局 registry）。

        实际实现：在 react_loop 上设置一个临时 ``_tools_override`` 属性
        （若 ReactLoop 支持），否则静默跳过。
        """
        if not whitelist:
            return
        # ReactLoop 当前不支持 tools_override，此处仅记录日志
        # 真正的请求级过滤由 Task 5 在 Orchestrator cron 路径实现
        logger.debug(
            "research 模板工具白名单过滤由 Task 5 在 Orchestrator 层实装，"
            "当前跳过"
        )

    def _build_llm_input(self, topic: str, context: WorkflowContext) -> str:
        """构建 LLM 用户输入文本（含时间变量占位符）。"""
        lines = [
            f"当前时间: {{now}}",
            f"上次执行时间: {{last_run_time}}",
            f"研究主题: {topic}",
            "",
            "请基于上述主题自主使用工具进行研究，生成 markdown 研究报告。",
        ]
        return "\n".join(lines)

    def _extract_tool_calls(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """从 ReactLoop 返回的 messages 中提取工具调用列表。

        ReactLoop 返回的 messages 含 ``{role: "assistant", content: [{type:
        "tool_use", ...}]}`` 与 ``{role: "user", content: [{type:
        "tool_result", ...}]}`` 两种块。本方法配对提取为
        ``{name, input, result, is_error}`` 字典列表。
        """
        tool_calls: List[Dict[str, Any]] = []
        # tool_use_id → tool_use 块映射，便于与 tool_result 配对
        use_blocks: Dict[str, Dict[str, Any]] = {}
        results: Dict[str, Dict[str, Any]] = {}

        for msg in messages or []:
            content = msg.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    use_id = block.get("id", "")
                    use_blocks[use_id] = block
                elif btype == "tool_result":
                    use_id = block.get("tool_use_id", "")
                    results[use_id] = block

        for use_id, use_block in use_blocks.items():
            result_block = results.get(use_id, {})
            tool_calls.append(
                {
                    "name": use_block.get("name", ""),
                    "input": use_block.get("input", {}) or {},
                    "result": self._extract_result_text(result_block),
                    "is_error": bool(result_block.get("is_error", False)),
                }
            )
        return tool_calls

    def _extract_result_text(self, result_block: Dict[str, Any]) -> str:
        """从 tool_result block 提取纯文本结果。"""
        content = result_block.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for sub in content:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    parts.append(sub.get("text", ""))
            return "".join(parts)
        return str(content) if content else ""

    def _write_report(
        self,
        context: WorkflowContext,
        topic: str,
        result: WorkflowResult,
    ) -> str:
        """写报告文件，返回路径。"""
        context.ensure_report_dir()
        # 主题转为安全文件名片段
        safe_topic = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in topic[:30]
        )
        date_str = context.current_time.strftime("%Y%m%d")
        # D4 修复：追加 run_id[:8] 后缀避免同日多次触发覆盖
        run_id = getattr(context, "run_id", None) or "unknown"
        run_id_suffix = run_id[:8] if isinstance(run_id, str) else "unknown"
        filename = f"research_{safe_topic}_{date_str}_{run_id_suffix}.md"
        report_path = os.path.join(context.report_dir, filename)

        summary_lines = [
            f"# 研究报告",
            f"",
            f"- 研究主题: {topic}",
            f"- 报告时间: {context.current_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 上次执行: "
            f"{context.last_run_time.strftime('%Y-%m-%d %H:%M:%S') if context.last_run_time else '首次执行'}",
            f"- 工具调用数: {len(result.tool_calls)}",
            "",
            "## LLM 研究结果",
            "",
            result.assistant_response or "（LLM 未返回内容）",
            "",
        ]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))
        return report_path
