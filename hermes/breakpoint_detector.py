"""自然断点检测器。

用于优雅中断模式：用户发送新消息时，不立即中断 LLM 输出，
而是等待自然断点（句子结束 / 代码块闭合 / 段落结束）再中断。

评分规则：
- 句子结束（。！？.!?）   +40
- 段落结束（\n\n）        +30
- 代码块闭合（```）       +50
- 列表中项结束             +15
- 代码块内部（奇数个 ```）-50 （严重负分）
- 词中间（无句号结尾）     -20
"""

from __future__ import annotations

import re
from typing import Optional


class BreakpointDetector:
    """对累积流式文本评分，判断是否到达合适的自然断点。

    用法：
        detector = BreakpointDetector(threshold=40)
        text = ""
        for token in stream:
            text += token
            if detector.should_break(text):
                print("到达断点，可以中断")
                break
    """

    # 代码块 fence 检测：行首的 ```
    _CODE_FENCE_RE = re.compile(r'^```', re.MULTILINE)

    # 中英文句子结束标点
    _SENTENCE_END_RE = re.compile(r'[。！？.!?]\s*$')

    # 列表项标记：行首的 - * 或数字+.)
    _LIST_ITEM_RE = re.compile(r'(?:^|\n)\s*[-*\d]+[.)]\s', re.MULTILINE)

    def __init__(self, threshold: int = 40) -> None:
        """

        参数:
            threshold: 评分达到此值触发断点。默认 40。
                       可通过 config.yaml interrupt.breakpoint_threshold 配置。
        """
        self.threshold = threshold

    def score(self, text: str) -> int:
        """对文本末尾评分，越高越适合作为断点。

        仅检查最后 200 字符以保证性能。
        """
        if not text:
            return 0

        tail = text[-200:]
        score = 0

        # ── 正面信号 ──

        # 代码块闭合：行末的 ```
        if re.search(r'```\s*$', tail):
            score += 50

        # 段落结束：末尾双换行
        if tail.rstrip().endswith('\n\n'):
            score += 30

        # 句子结束标点在最后 60 字符内
        last_60 = tail[-60:]
        if self._SENTENCE_END_RE.search(last_60):
            score += 40

        # 列表项结束
        if self._LIST_ITEM_RE.search(tail[-120:]):
            score += 15

        # ── 负面信号 ──

        # 代码块内部（奇数个 fence）
        # 注意：如果末尾是 ``` 闭合，这个 fence 不参与奇偶判定（代码块已结束）
        fences = self._CODE_FENCE_RE.findall(text)
        fences_count = len(fences)
        # 如果末尾有闭合 ```，从计数中排除它
        if fences_count > 0 and re.search(r'```\s*$', tail):
            fences_count -= 1
        if fences_count % 2 == 1:
            score -= 50

        # 词中间结尾：末尾是中文字符或字母，且附近无句号
        if re.search(r'[a-zA-Z一-鿿]\s*$', tail):
            # 最后 80 字符内无句子结束标点 → 在词中间
            if not re.search(r'[。！？.!?]', tail[-80:]):
                score -= 20

        # 逗号或冒号结尾（句子未完成）
        if re.search(r'[，、,:]\s*$', tail):
            score -= 10

        return score

    def should_break(self, text: str) -> bool:
        """返回 True 表示文本已到达可中断的断点。"""
        return self.score(text) >= self.threshold
