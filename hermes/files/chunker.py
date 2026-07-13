"""段落感知文档分块器。

将长文本按 chunk_size 切分为有重叠的块，优先在段落边界处切分。
"""

from __future__ import annotations

import re
from typing import List


class DocumentChunker:
    """段落感知文档分块器。

    切分策略：
    1. 先按段落（``\\n\\n``）分割
    2. 每个段落若超过 chunk_size，强制在 chunk_size 处切分
    3. 合并短段落直到接近 chunk_size
    4. 相邻块之间保留 overlap 字符的重叠

    Attributes:
        chunk_size: 每块最大字符数。
        chunk_overlap: 相邻块之间的重叠字符数。
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64) -> None:
        """初始化分块器。

        Args:
            chunk_size: 每块最大字符数，默认 512。
            chunk_overlap: 相邻块之间的重叠字符数，默认 64。
                若 overlap >= chunk_size，自动 clamp 为 chunk_size // 4。

        Raises:
            ValueError: chunk_size <= 0 时抛出。
        """
        if chunk_size <= 0:
            raise ValueError(f"chunk_size 必须为正数，实际: {chunk_size}")
        if chunk_overlap < 0:
            chunk_overlap = 0
        if chunk_overlap >= chunk_size:
            chunk_overlap = max(0, chunk_size // 4)

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk(self, text: str) -> List[str]:
        """将文本切分为重叠的块。

        Args:
            text: 待切分的纯文本。

        Returns:
            字符串列表，每项为一个分块。空文本返回空列表。
        """
        if not text or not text.strip():
            return []

        # 1. 按段落分割
        paragraphs = re.split(r'\n\s*\n', text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        if not paragraphs:
            return []

        # 2. 构建分块
        chunks: List[str] = []
        current_chunk = ""

        for para in paragraphs:
            # 如果段落本身超过 chunk_size，先切分长段落
            if len(para) > self.chunk_size:
                # 先保存当前积攒的 chunk
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""

                # 强制切分长段落
                long_chunks = self._split_long_paragraph(para)
                chunks.extend(long_chunks)
                continue

            # 尝试合并到当前块
            if current_chunk:
                candidate = current_chunk + "\n\n" + para
            else:
                candidate = para

            if len(candidate) <= self.chunk_size:
                current_chunk = candidate
            else:
                # 当前块已满，保存并开始新块
                chunks.append(current_chunk)
                current_chunk = para

        # 保存最后一个块
        if current_chunk:
            chunks.append(current_chunk)

        # 3. 为相邻块添加 overlap
        if self.chunk_overlap > 0 and len(chunks) > 1:
            chunks = self._add_overlap(chunks)

        return chunks

    def _split_long_paragraph(self, paragraph: str) -> List[str]:
        """将超长段落强制切分为 chunk_size 大小的块。

        Args:
            paragraph: 超长段落文本。

        Returns:
            分块列表。
        """
        chunks = []
        start = 0
        while start < len(paragraph):
            end = min(start + self.chunk_size, len(paragraph))
            chunks.append(paragraph[start:end])
            next_start = end - self.chunk_overlap if self.chunk_overlap > 0 else end
            # 确保 start 始终严格前进，避免 infinite loop
            if next_start <= start:
                next_start = end
            start = next_start
        return chunks

    def _add_overlap(self, chunks: List[str]) -> List[str]:
        """为相邻块之间添加重叠文本。

        每块的尾部 overlap 字符作为下一块的前缀。
        只修改除第一块外的块，在前面追加前一块的尾部重叠文本。

        Args:
            chunks: 无重叠的分块列表。

        Returns:
            有重叠的分块列表。
        """
        if self.chunk_overlap <= 0:
            return chunks

        result = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = chunks[i - 1]
            current = chunks[i]
            if len(prev) > self.chunk_overlap:
                overlap_text = prev[-self.chunk_overlap:]
                result.append(overlap_text + "\n...\n" + current)
            else:
                result.append(current)
        return result
