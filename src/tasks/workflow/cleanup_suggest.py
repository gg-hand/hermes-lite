"""cleanup_suggest 工作流模板（Phase 8 Task 2.5）。

查询低价值记忆并 LLM 生成清理建议。

执行流程：
1. **确定性步骤**：通过 ``context.chroma_store.list_memories`` 查询
   importance 低于阈值的记忆条目（cron namespace 隔离）。
2. **LLM 步骤**：单轮调用（``max_loops=1``）让 LLM 基于低价值记忆列表
   生成清理建议（删除候选 / 合并候选 / 保留但降权），输出 markdown 报告。

缓存约束：
- :meth:`build_system_prompt` 返回固定 prompt，不含动态变量
- 时间变量只在 LLM 用户输入层替换
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .base import WorkflowContext, WorkflowResult, WorkflowTemplate

logger = logging.getLogger(__name__)


# 固定 system prompt（缓存约束 5：禁含动态变量）
_CLEANUP_SUGGEST_SYSTEM_PROMPT = (
    "你是一个记忆管理助手。基于给定的低价值记忆条目，生成清理建议。"
    "建议应分类为：\n"
    "1. **删除候选**：明显过时或无价值的记忆\n"
    "2. **合并候选**：内容重复或可合并的记忆\n"
    "3. **保留降权**：仍有价值但重要性低的记忆\n"
    "对每条建议给出简短理由。回答使用中文，控制在 400 字以内。"
)


class CleanupSuggestTemplate(WorkflowTemplate):
    """记忆清理建议工作流模板。

    配置字段（``config`` dict）：
    - ``importance_threshold``（可选）：importance 阈值，默认 ``0.3``，
      查询 importance < 阈值的记忆
    - ``max_memories``（可选）：最多查询记忆数，默认 ``50``
    - ``namespace``（可选）：记忆命名空间，默认 ``"cron"``（cron 调度专用）
    - ``cron_id``（可选）：cron 命名空间下的调度项 ID。为 ``None`` 时
      取 ``context.schedule_id``

    输出：
    - 报告文件：``{report_dir}/cleanup_{cron_id}_{date}.md``
    - ``metrics_for_injection``：``{"低价值记忆数": N, "阈值": threshold}``
    """

    name = "cleanup_suggest"

    def build_system_prompt(self) -> str:
        """返回固定的清理建议 system prompt（不含动态变量）。"""
        return _CLEANUP_SUGGEST_SYSTEM_PROMPT

    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:
        result = WorkflowResult()

        importance_threshold = float(config.get("importance_threshold", 0.3))
        max_memories = int(config.get("max_memories", 50))
        namespace = config.get("namespace", "cron")
        cron_id = config.get("cron_id") or context.schedule_id

        # 1. 确定性步骤：查询低价值记忆
        if context.chroma_store is None:
            result.add_error("chroma_store 未配置，无法查询记忆")
            return result

        try:
            low_value_memories = self._query_low_value_memories(
                context.chroma_store,
                namespace,
                cron_id,
                importance_threshold,
                max_memories,
            )
        except Exception as e:
            result.add_error(f"查询低价值记忆失败: {e}")
            return result

        # 2. 填充 metrics_for_injection
        result.metrics_for_injection = {
            "低价值记忆数": len(low_value_memories),
            "importance阈值": importance_threshold,
            "命名空间": namespace,
        }

        if not low_value_memories:
            result.assistant_response = "（无低价值记忆可清理）"
            return result

        # 3. LLM 步骤：单轮调用生成清理建议
        user_input = self._build_llm_input(
            cron_id, low_value_memories, importance_threshold, context
        )
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
                context, cron_id, low_value_memories, response_text
            )
            result.outputs.append({"path": report_path, "type": "report"})
        except Exception as e:
            result.add_error(f"写报告文件失败: {e}")

        return result

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _query_low_value_memories(
        self,
        chroma_store: Any,
        namespace: str,
        cron_id: str,
        threshold: float,
        max_memories: int,
    ) -> List[Dict[str, Any]]:
        """查询 importance < threshold 的记忆条目。

        使用 ``chroma_store.list_memories`` 全量读取后 Python 过滤
        （chromadb 不支持数值范围 where 查询）。返回前 ``max_memories`` 条。
        """
        list_memories = getattr(chroma_store, "list_memories", None)
        if not callable(list_memories):
            return []

        # list_memories 签名：(namespace=None, cron_id=None) -> [{id, content, metadata}]
        all_memories = list_memories(namespace=namespace, cron_id=cron_id) or []
        low_value: List[Dict[str, Any]] = []
        for mem in all_memories:
            meta = mem.get("metadata", {}) or {}
            importance = float(meta.get("importance", 0.5))
            if importance < threshold:
                low_value.append(
                    {
                        "id": mem.get("id", ""),
                        "content": mem.get("content", ""),
                        "importance": importance,
                        "type": meta.get("type", "fact"),
                        "timestamp": meta.get("timestamp", ""),
                    }
                )
            if len(low_value) >= max_memories:
                break
        return low_value

    def _build_llm_input(
        self,
        cron_id: str,
        memories: List[Dict[str, Any]],
        threshold: float,
        context: WorkflowContext,
    ) -> str:
        """构建 LLM 用户输入文本（含时间变量占位符）。"""
        max_show = 30
        shown = memories[:max_show]
        lines = [
            f"当前时间: {{now}}",
            f"上次执行时间: {{last_run_time}}",
            f"调度项 ID: {cron_id}",
            f"低价值记忆数: {len(memories)}（importance < {threshold}）",
            f"展示前 {len(shown)} 条：",
            "",
            "## 低价值记忆列表",
        ]
        for idx, mem in enumerate(shown, 1):
            lines.append(
                f"{idx}. [importance={mem['importance']:.2f}] "
                f"{mem['content']}"
            )
        lines.append("")
        lines.append("请基于上述低价值记忆生成分类清理建议。")
        return "\n".join(lines)

    def _write_report(
        self,
        context: WorkflowContext,
        cron_id: str,
        memories: List[Dict[str, Any]],
        llm_response: str,
    ) -> str:
        """写报告文件，返回路径。"""
        context.ensure_report_dir()
        # 文件名层：current_time.strftime 生成日期
        date_str = context.current_time.strftime("%Y%m%d")
        # D4 修复：追加 run_id[:8] 后缀避免同日多次触发覆盖
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in cron_id)
        run_id = getattr(context, "run_id", None) or "unknown"
        run_id_suffix = run_id[:8] if isinstance(run_id, str) else "unknown"
        filename = f"cleanup_{safe_id}_{date_str}_{run_id_suffix}.md"
        report_path = os.path.join(context.report_dir, filename)

        summary_lines = [
            f"# 记忆清理建议报告",
            f"",
            f"- 调度项 ID: `{cron_id}`",
            f"- 报告时间: {context.current_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 上次执行: "
            f"{context.last_run_time.strftime('%Y-%m-%d %H:%M:%S') if context.last_run_time else '首次执行'}",
            f"- 低价值记忆数: {len(memories)}",
            "",
            "## LLM 清理建议",
            "",
            llm_response or "（LLM 未返回内容）",
            "",
        ]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))
        return report_path
