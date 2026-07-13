"""directory_watch 工作流模板（Phase 8 Task 2.2）。

监控指定目录的文件变更并 LLM 分析趋势。

执行流程（确定性步骤 + LLM 步骤两阶段）：
1. **确定性步骤**：扫描 ``watch_path`` 下的文件列表（``path + mtime + size``），
   与上次快照（存 ``data/schedules/{id}/snapshot.json``）对比生成差异 JSON，
   将新快照写回磁盘。
2. **LLM 步骤**：单轮调用（``max_loops=1``）让 LLM 基于差异分析趋势，输出
   markdown 报告，写入 ``report_dir/watch_{path}_{date}.md``。

报告文件名由代码生成（SubTask 2.7 第二层：文件名层）：使用
``current_time.strftime("%Y%m%d")`` 生成日期，``watch_path`` 取末尾目录名。

缓存约束：
- :meth:`build_system_prompt` 返回固定 prompt，不含动态变量
- 时间变量只在 LLM 用户输入层替换（通过 :meth:`WorkflowContext.render`）
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import WorkflowContext, WorkflowResult, WorkflowTemplate

logger = logging.getLogger(__name__)


# 固定 system prompt（缓存约束 5：禁含动态变量）
_DIRECTORY_WATCH_SYSTEM_PROMPT = (
    "你是一个目录监控分析助手。基于给定的文件变更差异，分析目录内容的演化趋势，"
    "并生成简洁的 markdown 报告。报告应包含：\n"
    "1. 变更摘要（新增/修改/删除文件数）\n"
    "2. 趋势分析（基于文件名与扩展名推断可能的开发/工作活动）\n"
    "3. 异常提示（如有可疑的大规模删除或非工作时间变更）\n"
    "回答使用中文，控制在 300 字以内。"
)


def _sanitize_path_name(path: str) -> str:
    """将路径转为安全的文件名片段（去除 ``/`` ``\\`` ``:`` 等）。"""
    # 取末尾目录名，再清理非法字符
    tail = os.path.basename(path.rstrip("/\\")) or "root"
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", tail)
    return safe or "root"


class DirectoryWatchTemplate(WorkflowTemplate):
    """目录监控工作流模板。

    配置字段（``config`` dict）：
    - ``watch_path``（必填）：监控目录路径
    - ``pattern``（可选）：glob 模式，默认 ``**/*`` 递归全部文件
    - ``max_files``（可选）：最多扫描文件数，默认 ``1000``，避免巨型目录
    - ``ignore_dirs``（可选）：忽略的目录名列表，默认 ``[".git", "__pycache__", "node_modules"]``

    输出：
    - 报告文件：``{report_dir}/watch_{path}_{date}.md``
    - 快照文件：``data/schedules/{schedule_id}/snapshot.json``
    - ``metrics_for_injection``：``{"扫描文件数": N, "新增": N, "修改": N, "删除": N}``
    """

    name = "directory_watch"

    def build_system_prompt(self) -> str:
        """返回固定的目录监控 system prompt（不含动态变量）。"""
        return _DIRECTORY_WATCH_SYSTEM_PROMPT

    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:
        result = WorkflowResult()

        watch_path = config.get("watch_path")
        if not watch_path:
            result.add_error("配置缺少 watch_path 字段")
            return result

        pattern = config.get("pattern", "**/*")
        max_files = int(config.get("max_files", 1000))
        ignore_dirs = config.get(
            "ignore_dirs", [".git", "__pycache__", "node_modules"]
        )

        # 1. 确定性步骤：扫描目录
        try:
            current_snapshot = self._scan_directory(
                watch_path, pattern, max_files, ignore_dirs
            )
        except Exception as e:
            result.add_error(f"扫描目录失败: {e}")
            return result

        # 2. 确定性步骤：与上次快照对比
        snapshot_path = self._get_snapshot_path(context.schedule_id)
        previous_snapshot = self._load_snapshot(snapshot_path)
        diff = self._compute_diff(previous_snapshot, current_snapshot)

        # 3. 写回新快照
        try:
            self._save_snapshot(snapshot_path, current_snapshot)
            result.outputs.append(
                {"path": snapshot_path, "type": "snapshot"}
            )
        except Exception as e:
            result.add_error(f"保存快照失败: {e}")

        # 4. 填充 metrics_for_injection
        result.metrics_for_injection = {
            "扫描文件数": len(current_snapshot),
            "新增文件": len(diff["added"]),
            "修改文件": len(diff["modified"]),
            "删除文件": len(diff["removed"]),
            "监控路径": watch_path,
        }

        # 5. LLM 步骤：单轮调用分析趋势
        user_input = self._build_llm_input(watch_path, diff, context)
        # 时间变量替换（SubTask 2.7：在用户输入层替换）
        user_input = context.render(user_input)

        system_prompt = self.build_system_prompt()
        response_text, tool_calls = self._call_llm_single_turn(
            context, user_input, system=system_prompt
        )
        result.assistant_response = response_text
        result.tool_calls.extend(tool_calls)

        # 6. 写报告文件（文件名层：current_time.strftime 生成日期）
        try:
            report_path = self._write_report(
                context, watch_path, diff, response_text
            )
            result.outputs.append({"path": report_path, "type": "report"})
        except Exception as e:
            result.add_error(f"写报告文件失败: {e}")

        return result

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------
    def _scan_directory(
        self,
        watch_path: str,
        pattern: str,
        max_files: int,
        ignore_dirs: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """扫描目录，返回 ``{relative_path: {mtime, size}}`` 字典。"""
        if not os.path.isdir(watch_path):
            raise FileNotFoundError(f"watch_path 不是目录: {watch_path}")

        snapshot: Dict[str, Dict[str, Any]] = {}
        base = Path(watch_path)
        for idx, path in enumerate(base.glob(pattern)):
            if idx >= max_files:
                break
            if not path.is_file():
                continue
            # 过滤忽略目录
            parts = path.relative_to(base).parts
            if any(part in ignore_dirs for part in parts[:-1]):
                continue
            try:
                stat = path.stat()
                rel = str(path.relative_to(base)).replace("\\", "/")
                snapshot[rel] = {
                    "mtime": int(stat.st_mtime),
                    "size": stat.st_size,
                }
            except OSError:
                continue
        return snapshot

    def _get_snapshot_path(self, schedule_id: str) -> str:
        """返回快照文件路径 ``data/schedules/{id}/snapshot.json``。"""
        return os.path.join(
            "data", "schedules", schedule_id, "snapshot.json"
        )

    def _load_snapshot(self, snapshot_path: str) -> Dict[str, Dict[str, Any]]:
        """加载上次快照。文件不存在时返回空 dict。"""
        if not os.path.exists(snapshot_path):
            return {}
        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            logger.warning("加载快照失败，视为空快照: %s", snapshot_path)
            return {}

    def _save_snapshot(
        self, snapshot_path: str, snapshot: Dict[str, Dict[str, Any]]
    ) -> None:
        """保存快照到磁盘（原子写入）。"""
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        tmp_path = snapshot_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, snapshot_path)

    def _compute_diff(
        self,
        previous: Dict[str, Dict[str, Any]],
        current: Dict[str, Dict[str, Any]],
    ) -> Dict[str, List[Any]]:
        """对比两次快照，返回 ``{"added": [...], "modified": [...], "removed": [...]}``。"""
        prev_keys = set(previous.keys())
        curr_keys = set(current.keys())

        added = sorted(curr_keys - prev_keys)
        removed = sorted(prev_keys - curr_keys)
        modified: List[str] = []
        for key in sorted(prev_keys & curr_keys):
            if previous[key] != current[key]:
                modified.append(key)

        return {
            "added": [{"path": k, **current[k]} for k in added],
            "modified": [
                {"path": k, "before": previous[k], "after": current[k]}
                for k in modified
            ],
            "removed": [{"path": k, **previous[k]} for k in removed],
        }

    def _build_llm_input(
        self,
        watch_path: str,
        diff: Dict[str, List[Any]],
        context: WorkflowContext,
    ) -> str:
        """构建 LLM 用户输入文本（含时间变量占位符，由上层替换）。"""
        # 截断过长的差异列表，避免 token 超限
        max_show = 50
        added = diff["added"][:max_show]
        modified = diff["modified"][:max_show]
        removed = diff["removed"][:max_show]

        lines = [
            f"当前时间: {{now}}",
            f"上次执行时间: {{last_run_time}}",
            f"监控目录: {watch_path}",
            "",
            f"## 文件变更差异",
            f"### 新增文件（共 {len(diff['added'])} 个，展示前 {len(added)} 个）",
            json.dumps(added, ensure_ascii=False, indent=2),
            f"### 修改文件（共 {len(diff['modified'])} 个，展示前 {len(modified)} 个）",
            json.dumps(modified, ensure_ascii=False, indent=2),
            f"### 删除文件（共 {len(diff['removed'])} 个，展示前 {len(removed)} 个）",
            json.dumps(removed, ensure_ascii=False, indent=2),
            "",
            "请基于上述差异分析目录内容的演化趋势，生成 markdown 报告。",
        ]
        return "\n".join(lines)

    def _write_report(
        self,
        context: WorkflowContext,
        watch_path: str,
        diff: Dict[str, List[Any]],
        llm_response: str,
    ) -> str:
        """写报告文件，返回路径。

        文件名格式：``watch_{path}_{date}.md``（SubTask 2.7 第二层）。
        """
        context.ensure_report_dir()
        path_name = _sanitize_path_name(watch_path)
        date_str = context.current_time.strftime("%Y%m%d")
        # D4 修复：追加 run_id[:8] 后缀避免同日多次触发覆盖
        run_id = getattr(context, "run_id", None) or "unknown"
        run_id_suffix = run_id[:8] if isinstance(run_id, str) else "unknown"
        filename = f"watch_{path_name}_{date_str}_{run_id_suffix}.md"
        report_path = os.path.join(context.report_dir, filename)

        # 报告内容：变更摘要 + LLM 分析
        summary_lines = [
            f"# 目录监控报告",
            f"",
            f"- 监控路径: `{watch_path}`",
            f"- 报告时间: {context.current_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 上次执行: "
            f"{context.last_run_time.strftime('%Y-%m-%d %H:%M:%S') if context.last_run_time else '首次执行'}",
            f"- 新增文件: {len(diff['added'])}",
            f"- 修改文件: {len(diff['modified'])}",
            f"- 删除文件: {len(diff['removed'])}",
            "",
            "## LLM 趋势分析",
            "",
            llm_response or "（LLM 未返回内容）",
            "",
        ]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))
        return report_path
