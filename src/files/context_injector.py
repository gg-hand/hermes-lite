"""文件摘要上下文注入器。

在每次对话构建时，将会话中已上传的文件（含处理中状态）摘要注入到上下文
（``messages[0]``），与记忆检索注入拼接，受 token 预算控制。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .upload_manager import UploadManager

logger = logging.getLogger(__name__)


class FileContextInjector:
    """文件摘要上下文注入器。

    从 UploadManager 获取当前会话已上传的文件（done/pending/processing），格式化为注入文本。

    Attributes:
        upload_manager: 上传管理器实例。
        max_files: 最多注入的文件数量。
        max_tokens: 最大注入 token 数（估算）。
    """

    def __init__(
        self,
        upload_manager: "UploadManager",
        max_files: int = 5,
        max_tokens: int = 1000,
    ) -> None:
        """初始化注入器。

        Args:
            upload_manager: 上传管理器实例。
            max_files: 最多注入的文件数量，默认 5。
            max_tokens: 最大注入 token 数（粗略估算：按字符数 / 2），默认 1000。
        """
        self.upload_manager = upload_manager
        self.max_files = max_files
        self.max_tokens = max_tokens

    def get_injection_text(self, session_id: str) -> str:
        """获取当前会话的文件摘要注入文本。

        Args:
            session_id: 会话 ID。

        Returns:
            注入文本。无活跃文件（done/pending/processing）时返回空字符串。
        """
        try:
            files = self.upload_manager.get_session_files(session_id)
        except Exception as e:
            logger.error("获取会话文件列表失败: %s", e)
            return ""

        # 取 etl_status 为 done/pending/processing 的文件（failed/disk_expired 排除）
        # 放宽原 done-only 过滤：让用户上传后第一轮（ETL 处理中）即可被 LLM 感知
        active_files = [
            f for f in files
            if f.get("etl_status") in ("done", "pending", "processing")
        ]
        if not active_files:
            return ""

        # 限制数量（取最新的 max_files 个）
        active_files = active_files[:self.max_files]

        # 统计已处理/处理中数量
        done_count = sum(1 for f in active_files if f.get("etl_status") == "done")
        pending_count = len(active_files) - done_count
        lines = [
            f"📚 已上传文件 {len(active_files)} 个（已处理 {done_count}，处理中 {pending_count}）",
            f"可通过 file_query 搜索已处理文件内容",
            "",
            "## 已上传文件",
        ]
        total_chars = sum(len(l) for l in lines)

        for f in active_files:
            name = f.get("original_name", "unknown")
            file_type = f.get("type", "")
            etl_status = f.get("etl_status", "pending")
            is_image = file_type in (".png", ".jpg", ".jpeg", ".gif")

            if etl_status != "done":
                # 处理中状态：只显示文件名 + 状态标记，不显示摘要/OCR
                type_label = "图片" if is_image else "文件"
                line = f"- {name}（{type_label}，处理中）"
            elif is_image:
                img_text = f.get("img_text", "")
                if img_text:
                    line = f"- {name} ⬤ 图片中的文字：{img_text}"
                else:
                    line = f"- {name}（图片，OCR 未提取到文字）"
            else:
                summary = f.get("summary", "")
                if summary:
                    line = f"- {name}: {summary}"
                else:
                    line = f"- {name}（暂无摘要）"

            # token 预算控制（字符数 / 2 粗略估算 tokens）
            estimated_tokens = (total_chars + len(line)) / 2
            if estimated_tokens > self.max_tokens:
                break

            lines.append(line)
            total_chars += len(line)

        if len(lines) == 4:
            return ""  # 只有标题，没有实际文件（token 预算耗尽）

        return "\n".join(lines)
