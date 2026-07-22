"""文件操作工具：read_file / write_file / delete_file / list_directory /
file_edit / file_glob / file_grep，以及面向 ToolRegistry 的注册函数
register_file_tools（含 file_list_uploads / file_query / file_read_uploaded
三个 Core Tier 工具）和 v2 版本的 write_file / delete_file 注册器（与
FileOperationRegistry 联动，记录会话内文件操作用于 PolicyEngine 决策）。

本模块从 builtin_tools.py 迁移而来，函数体保持原样未做改动。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from ..file_registry import FileOperationRegistry
    from ...files.etl_engine import ETLEngine
    from ...files.upload_manager import UploadManager

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
                from ...config import load_config
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


__all__ = [
    "read_file",
    "write_file",
    "delete_file",
    "list_directory",
    "file_edit",
    "file_glob",
    "file_grep",
    "register_file_tools",
]
