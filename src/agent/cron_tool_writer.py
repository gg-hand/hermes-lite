"""write_cron_tool 工具（Phase 8 Task 5.4）。

LLM 调用此工具生成新的 cron_tool，写入 ``cron_tool/.pending/{name}/`` 等待
用户审查。**不直接激活**——用户必须在前端审查卡片上点击「激活」后，工具才
从 ``.pending/`` 移到 ``cron_tool/{name}/`` 并注册到
:class:`CronToolRegistry`（SubTask 5.5）。

设计要点：
- **HIL 硬约束**：write_cron_tool 写入 ``.pending/``，不直接注册到 registry，
  防止 LLM 生成恶意工具自动激活。
- **工具名校验**：与 ``cron_tool_loader._validate_tool_name`` 一致（非空、
  无路径分隔符、无 ``.`` 前缀）。
- **内容校验**：TOOL.md 必须含合法 frontmatter（``---`` 分隔 + yaml），
  run.* 必须非空。校验通过才写入磁盘。
- **覆盖保护**：同名工具已在 ``.pending/`` 或已激活时返回错误，避免误覆盖。
- **触发审查卡片**：返回特殊标记 ``pending_review: true``，前端据此渲染
  审查卡片（SubTask 5.5）。

模块依赖：
- :mod:`src.tasks.cron_tool_loader`（``_validate_tool_name`` / ``list_tools``
  / ``list_pending_tools``）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

# 兼容相对导入与直接运行两种方式
try:
    from ..tasks.cron_tool_loader import (
        DEFAULT_BASE_DIR,
        _validate_tool_name,
        list_pending_tools,
        list_tools,
    )
except ImportError:  # pragma: no cover
    from tasks.cron_tool_loader import (  # type: ignore
        DEFAULT_BASE_DIR,
        _validate_tool_name,
        list_pending_tools,
        list_tools,
    )

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入以避免循环依赖
    from .tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


# 支持的 run.* 扩展名（与 cron_tool_loader._RUN_INTERPRETERS 一致）
_SUPPORTED_RUN_EXTS = (".py", ".sh", ".js")


def register_write_cron_tool(
    registry: "ToolRegistry",
    base_dir: str = DEFAULT_BASE_DIR,
) -> None:
    """注册 ``write_cron_tool`` 工具到 ``ToolRegistry``（用户会话可用）。

    LLM 通过此工具生成 cron_tool，写入 ``.pending/`` 等待用户审查。注册到
    全局 ToolRegistry 是合理的——这是「用户会话工具」（让用户对话中让 LLM
    生成工具），不污染 cron 执行会话的 tools schema（缓存约束 1）。

    参数:
        registry: ToolRegistry 实例（用户会话的 registry）。
        base_dir: cron_tool 根目录，默认 :data:`DEFAULT_BASE_DIR`。
    """

    def _write_cron_tool(
        tool_name: str,
        tool_md: str,
        run_script: str,
        run_ext: str = ".py",
        llm_explanation: str = "",
    ) -> str:
        """write_cron_tool 工具 handler。

        流程：
        1. 校验 ``tool_name`` 合法性（防路径穿越）。
        2. 校验 ``run_ext`` 在 ``.py / .sh / .js`` 之内。
        3. 校验 ``tool_md`` 含合法 frontmatter。
        4. 校验 ``run_script`` 非空。
        5. 覆盖保护：检查 ``.pending/{name}/`` 与已激活目录是否已存在。
        6. 写入 ``.pending/{name}/TOOL.md`` 与 ``.pending/{name}/run{ext}``。
        7. 返回成功 JSON，含 ``pending_review: true`` 标记触发前端审查卡片。

        参数:
            tool_name: 工具名（与目录名一致，需合法）。
            tool_md: TOOL.md 全文（含 frontmatter）。
            run_script: run.* 脚本内容。
            run_ext: 脚本扩展名，``.py`` / ``.sh`` / ``.js``，默认 ``.py``。
            llm_explanation: LLM 说明（展示在审查卡片上）。

        返回:
            JSON 字符串。成功形如::

                {"status": "pending_review", "tool_name": "...",
                 "pending_review": true,
                 "message": "工具已写入 .pending/，等待用户审查激活"}

            失败为错误信息字符串（含「错误」前缀）。
        """
        import json

        try:
            # 1. 校验 tool_name
            try:
                _validate_tool_name(tool_name)
            except Exception as exc:
                return f"错误：tool_name 非法: {exc}"

            # 2. 校验 run_ext
            if run_ext not in _SUPPORTED_RUN_EXTS:
                return (
                    f"错误：run_ext 必须为 "
                    f"{list(_SUPPORTED_RUN_EXTS)} 之一，实际: {run_ext!r}"
                )

            # 3. 校验 tool_md 含 frontmatter
            md_text = tool_md or ""
            if not md_text.startswith("---"):
                return "错误：tool_md 必须以 '---' frontmatter 开头"
            parts = md_text.split("---", 2)
            if len(parts) < 3:
                return "错误：tool_md frontmatter 格式非法（缺少闭合 '---'）"
            # 验证 yaml 可解析（不强制校验字段，留给 load_tool 在激活时校验）
            try:
                import yaml

                frontmatter = yaml.safe_load(parts[1]) or {}
            except Exception as exc:
                return f"错误：tool_md frontmatter yaml 解析失败: {exc}"
            if not isinstance(frontmatter, dict):
                return "错误：tool_md frontmatter 必须为 dict"
            if frontmatter.get("name") != tool_name:
                return (
                    f"错误：tool_md frontmatter.name ({frontmatter.get('name')!r})"
                    f" 必须与 tool_name ({tool_name!r}) 一致"
                )

            # 4. 校验 run_script 非空
            if not (run_script or "").strip():
                return "错误：run_script 不能为空"

            # 5. 覆盖保护
            pending_path = Path(base_dir) / ".pending" / tool_name
            active_path = Path(base_dir) / tool_name
            if pending_path.exists():
                return (
                    f"错误：待审查工具 {tool_name} 已存在（{pending_path}），"
                    f"请先激活或拒绝后再重新生成"
                )
            if active_path.exists():
                return (
                    f"错误：已激活工具 {tool_name} 已存在（{active_path}），"
                    f"如需更新请使用「重新加载」功能"
                )

            # 6. 写入磁盘（原子性：先建目录再写文件）
            pending_path.mkdir(parents=True, exist_ok=False)
            tool_md_path = pending_path / "TOOL.md"
            run_script_path = pending_path / f"run{run_ext}"
            try:
                tool_md_path.write_text(md_text, encoding="utf-8")
                run_script_path.write_text(run_script, encoding="utf-8")
            except OSError as exc:
                # 写入失败时清理已创建的目录
                try:
                    if pending_path.exists():
                        for f in pending_path.iterdir():
                            f.unlink()
                        pending_path.rmdir()
                except OSError:
                    pass
                return f"错误：写入 .pending/{tool_name}/ 失败: {exc}"

            # 7. 返回成功（含 pending_review 标记触发前端审查卡片）
            return json.dumps(
                {
                    "status": "pending_review",
                    "tool_name": tool_name,
                    "pending_review": True,
                    "run_script": f"run{run_ext}",
                    "llm_explanation": llm_explanation or "",
                    "message": (
                        f"工具已写入 .pending/{tool_name}/，"
                        f"等待用户在「调度」Tab 审查卡片上激活或拒绝"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            return f"write_cron_tool 执行出错: {exc}"

    registry.register_core(
        name="cron_tool_create",
        description=(
            "生成一个新的 cron_tool（Layer 2 能力扩展），写入 .pending/ 等待"
            "用户审查激活。**不会直接激活**——用户需在前端「调度」Tab 的审查"
            "卡片上点击「激活」后，工具才会注册到 cron_tool_registry 并可被"
            "cron 调度会话使用。\n\n"
            "## TOOL.md 模板\n\n"
            "```markdown\n"
            "---\n"
            "name: <工具名，与 tool_name 一致>\n"
            "version: 1.0.0\n"
            "description: <一句话描述工具功能>\n"
            "author: hermes-lite\n"
            "timeout: 30\n"
            "input_schema:\n"
            "  type: object\n"
            "  properties:\n"
            "    <参数名>:\n"
            "      type: string\n"
            "      description: <参数说明>\n"
            "  required: [<必填参数名>]\n"
            "---\n\n"
            "# <工具名>\n\n"
            "<工具功能详细说明>\n"
            "```\n\n"
            "## run.py 模板\n\n"
            "```python\n"
            "from __future__ import annotations\n"
            "import json, sys\n\n"
            "def main() -> None:\n"
            "    raw = sys.stdin.read()\n"
            "    try:\n"
            "        payload = json.loads(raw) if raw.strip() else {}\n"
            "    except json.JSONDecodeError as exc:\n"
            "        print(json.dumps({\"error\": f\"输入 JSON 解析失败: {exc}\"}))\n"
            "        sys.exit(1)\n"
            "    tool_input = payload.get(\"input\") or {}\n"
            "    # TODO: 实现工具逻辑\n"
            "    result = \"...\"\n"
            "    print(json.dumps({\"result\": result}, ensure_ascii=False))\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
            "```\n\n"
            "## 接口契约\n\n"
            "- stdin 输入：JSON 字符串 {input: {...}, context: {...}}\n"
            "- stdout 输出：JSON 字符串 {result: '...'} 或 {error: '...'}\n"
            "- 超时：由 frontmatter 的 timeout 字段配置（秒）\n"
            "- input_schema：JSON Schema 格式，定义工具入参结构\n\n"
            "## 示例：echo_text 工具\n\n"
            "TOOL.md frontmatter 的 input_schema 定义 text 参数，run.py 读取 "
            "input.text 并回显到 result。参考完整示例见 cron_tool/echo_text/。\n\n"
            "参数：tool_name（工具名）；tool_md（TOOL.md 全文）；"
            "run_script（run.* 脚本内容）；run_ext（.py/.sh/.js，默认 .py）；"
            "llm_explanation（生成理由，展示在审查卡片上）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": (
                        "工具名（与目录名一致）。禁止 / \\ 与 . 前缀，"
                        "防路径穿越。"
                    ),
                },
                "tool_md": {
                    "type": "string",
                    "description": (
                        "TOOL.md 全文。必须以 '---' frontmatter 开头，"
                        "含字段：name（与 tool_name 一致）/ version / "
                        "description / author / timeout / input_schema。"
                    ),
                },
                "run_script": {
                    "type": "string",
                    "description": (
                        "run.* 脚本内容。从 stdin 读 JSON "
                        "{input: {...}, context: {...}}，stdout 写 JSON "
                        "{result: '...'} 或 {error: '...'}。"
                    ),
                },
                "run_ext": {
                    "type": "string",
                    "enum": [".py", ".sh", ".js"],
                    "description": "脚本扩展名，默认 .py",
                    "default": ".py",
                },
                "llm_explanation": {
                    "type": "string",
                    "description": "向用户说明的生成理由（展示在审查卡片上）",
                },
            },
            "required": ["tool_name", "tool_md", "run_script"],
        },
        handler=_write_cron_tool,
    )
