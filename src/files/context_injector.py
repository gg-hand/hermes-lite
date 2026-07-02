"""文件摘要上下文注入器。

在每次对话构建时，将会话中已上传且处理完成的文件摘要注入到上下文
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

    从 UploadManager 获取当前会话已处理完成的文件，格式化为注入文本。

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
            注入文本。无已完成文件时返回空字符串。
        """
        try:
            files = self.upload_manager.get_session_files(session_id)
        except Exception as e:
            logger.error("获取会话文件列表失败: %s", e)
            return ""

        # 仅取 etl_status='done' 的文件
        done_files = [f for f in files if f.get("etl_status") == "done"]
        if not done_files:
            return ""

        # 限制数量（取最新的 max_files 个）
        done_files = done_files[:self.max_files]

        # 计算知识库总览（跨会话全局）
        total_chunks = sum(f.get("chunk_count", 0) for f in done_files)
        lines = [
            f"📚 知识库共 {len(done_files)} 个文件，",
            f"可通过 file_query 搜索其内容（全局，不限会话）",
        ]
        total_chars = sum(len(l) for l in lines)
        lines.append("")

        lines.append("## 已上传文件")
        total_chars += len(lines[-1])

        for f in done_files:
            name = f.get("original_name", "unknown")
            file_type = f.get("type", "")
            is_image = file_type in (".png", ".jpg", ".jpeg", ".gif")

            if is_image:
                img_text = f.get("img_text", "")
                line = f"- {name} ⬤ 图片中的文字：{img_text}"
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

        if len(lines) == 1:
            return ""  # 只有标题，没有实际文件

        return "\n".join(lines)
