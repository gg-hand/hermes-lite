"""协作消息内容净化：剥离 LLM 思考性开头段落与工具元语言污染。

设计原则：
- 纯函数：无 IO、无状态，便于单测与跨进程复用。
- 段落级剥离：从前往后剥离命中"思考性前缀"或"工具元语言"的开头段落，
  遇到第一个实质段落即停止，保留其后全部内容（含中间出现的污染段落不误伤）。
- 句级剥离：对保留的段落按句分割，剔除整句为工具元语言的句子。
- 空结果兜底：若净化后无任何剩余内容（全为污染），返回占位符避免空消息，
  同时不让工具元语言原文污染协作流（P3-1：原"保留原文"会让 100% 元语言消息
  原样写入协作流，违反验收 (c) 无元语言要求）。

覆盖中英文工具元语言：
  我来调用 / send_remote_message / 工具调用被拦截 / tool_list 未返回 /
  shell 命令 / bash 环境 / 调用工具 / 工具列表查询 等。
"""
from __future__ import annotations

import re

# 净化后无任何剩余内容（原内容全为工具元语言/思考性污染）时的占位符。
# 不返回原文以避免元语言污染协作流（P3-1）；不返回空串以避免空消息。
SANITIZED_PLACEHOLDER = "[本条消息经净化：原内容为工具元语言，已剥离]"

# -----------------------------------------------------------------------------
# 思考性开头前缀（段落级匹配，case-insensitive）
# 沿用 worker_adapter._META_LANGUAGE_PREFIX_RE 语义，确保迁移后行为一致。
# -----------------------------------------------------------------------------
_META_LANGUAGE_PREFIX_RE = re.compile(
    r"^(?:"
    r"i[''\u2019]ll|i will|i'm going to|i'd|"
    r"let me|let's|"
    r"since|actually|looking at|looking to|looking into|based on|"
    r"after reviewing|after checking|"
    r"让我|我来|我看看|我查看|我来查|我来答|"
    r"我没有看到|我注意到|我作为|"
    r"根据|既然|实际上|"
    r"查看工具|查询工具|工具列表查询|工具列表|"
    r"工具未|工具不|工具没|工具 schema|"
    r"分析一下|首先让我|首先我|"
    r"the collaboration|the tool|the context|"
    r"所有的 collab|按照轮次"
    r")",
    re.IGNORECASE,
)

# -----------------------------------------------------------------------------
# 工具元语言关键词（段落级或句级命中即剥离）
# 覆盖"我来调用/send_remote_message/工具调用被拦截/tool_list 未返回/
# shell 命令/bash 环境"等中英文工具元语言。
# -----------------------------------------------------------------------------
_TOOL_META_LANGUAGE_RE = re.compile(
    r"(?:"
    # 中文工具元语言
    r"我来调用|我来使用工具|我调用工具|调用工具|"
    r"工具调用|工具列表查询|查询工具|查看工具|"
    r"工具调用被拦截|工具未返回|工具列表未返回|"
    r"shell\s*命令|bash\s*环境|命令行环境|"
    r"工具返回|工具结果|工具响应|"
    r"协作工具 schema|协作工具|工具 schema|"
    r"让我尝试|让我直接|让我回复|让我写|"
    r"回复本消息|协作黑板|写作协作|"
    # 英文工具元语言
    r"send_remote_message|"
    r"i[''\u2019]ll call|i will call|let me call|"
    r"calling the tool|calling tool|"
    r"tool_list returned no|tool_list did not return|tool_list not returned|"
    r"tool call (?:was )?blocked|tool invocation blocked|"
    r"shell command|bash environment|"
    r"tool list|toolset|function list|available tools|available functions|"
    r"tool isn't|tool doesn't|tool list query|"
    r"let me try|let me respond|let me make|let me check|"
    r"let me search|let me use|"
    r"reply directly|communicate via|communicate with|"
    r"auto-written|collaboration\.md|collaboration context|"
    r"i see the available|i notice that|i don't see|"
    r"looking at my available|based on the context"
    r")",
    re.IGNORECASE,
)


def _strip_leading_meta_paragraphs(text: str) -> str:
    """按空行切分段落，从前往后剥离命中思考性前缀或工具元语言的开头段落。

    遇到第一个非污染段落即停止，保留其后全部内容。
    全部段落都被剥离时返回空串（由调用方兜底）。
    """
    paragraphs = re.split(r"\n[ \t]*\n", text)
    first_kept = 0
    for i, p in enumerate(paragraphs):
        stripped = p.strip()
        if not stripped:
            # 空段落：不推进 first_kept，保留原结构（但开头空段落后被跳过）
            continue
        if (_META_LANGUAGE_PREFIX_RE.match(stripped)
                or _TOOL_META_LANGUAGE_RE.search(stripped)):
            first_kept = i + 1
        else:
            break
    if first_kept >= len(paragraphs):
        return ""
    return "\n\n".join(paragraphs[first_kept:])


# 句末分隔符（中文。！？ + 英文 .!?）：零宽 lookbehind，保留标号在前一句末，
# 不消耗任何分隔符，重组时用 "" join 即可保留原始空白/换行结构。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？.!?])")


def _strip_tool_meta_sentences(text: str) -> str:
    """段落内按句剥离含工具元语言的句子，保留段落结构与剩余句子。

    句子按中英文句末标号（。！？.!?）零宽分割，整句命中工具元语言即丢弃。
    整段都被剥空时丢弃该段（同时丢弃其后紧跟的段落分隔符）。
    """
    if not text:
        return ""
    paragraphs = re.split(r"(\n[ \t]*\n)", text)  # 保留分隔符用于重组
    out_parts: list[str] = []
    for part in paragraphs:
        if re.fullmatch(r"\n[ \t]*\n", part):
            out_parts.append(part)
            continue
        sentences = _SENTENCE_SPLIT_RE.split(part)
        kept = [s for s in sentences
                if s.strip() and not _TOOL_META_LANGUAGE_RE.search(s)]
        if not kept:
            # 整段都是工具元语言 → 丢弃该段（同时丢弃其后紧跟的分隔符）
            continue
        out_parts.append("".join(kept))
    result = "".join(out_parts)
    # 清理首尾多余空白与多余空行
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def _normalize_horizontal_rules(content: str) -> str:
    """将裸 ``---`` 行替换为 ``-``，避免与 frontmatter 块分隔符冲突。

    协作消息以 ``---`` 分隔多个 YAML frontmatter 块（``_parse_frontmatter_blocks``
    用 ``content.split("---\\n")`` 朴素切分）。LLM 生成的 markdown 常含裸 ``---``
    （水平分割线 / 文档开头分隔线），会被误当作块分隔符，导致该消息块解析被
    破坏（seq/timestamp/message_id 字段被切走，消息读不出来——曾导致主会话协作
    响应"写入成功但等待超时"）。

    修复：把内容中恰好为 ``---`` 的行替换为 ``-``（水平线是纯装饰，替换不损失
    语义）。对 request/response/consensus 等含正文的协作类型统一生效。

    Args:
        content: 原始文本。

    Returns:
        无裸 ``---`` 行的文本。
    """
    lines = content.split("\n")
    out = []
    for ln in lines:
        if ln.strip() == "---":
            out.append("-")
        else:
            out.append(ln)
    return "\n".join(out)


def sanitize_collab_content(content: str) -> str:
    """净化协作消息内容：剥离工具元语言（句级）+ 思考性开头段落（段落级）。

    处理顺序（关键）：先句级后段落级。原因：LLM 常把工具元语言与实质答案
    写在同一段落（如"我来调用工具。答案是42。"），若先段落级会因前缀命中
    "我来"而整段剥离，丢失实质答案。先句级精准剔除工具元语言句子，剩余
    实质内容再交段落级剥离开头思考性段落。

    Args:
        content: 原始 LLM 回复文本。

    Returns:
        净化后文本。空文本返回空串；纯空白输入原样返回；全为污染内容时
        返回占位符 SANITIZED_PLACEHOLDER（避免空消息，同时不污染协作流）。
    """
    if not content:
        return ""
    # 纯空白输入：无实质内容可净化，原样返回（与空文本区分，避免空白变占位符）。
    if not content.strip():
        return content
    # 1. 句级剥离（先剔除所有段落内的工具元语言句子）
    sentence_cleaned = _strip_tool_meta_sentences(content)
    if not sentence_cleaned.strip():
        # 原内容有实质文本但全部为工具元语言 → 返回占位符（P3-1：不再保留原文）
        return SANITIZED_PLACEHOLDER
    # 2. 段落级剥离（再剥离开头思考性/工具元语言段落）
    paragraph_cleaned = _strip_leading_meta_paragraphs(sentence_cleaned)
    if not paragraph_cleaned:
        # 句级有保留但段落级剥光（全为思考性开头）→ 返回占位符
        return SANITIZED_PLACEHOLDER
    # 3. 规范化裸 --- 行（防 frontmatter 块分隔符冲突，见 _normalize_horizontal_rules）
    return _normalize_horizontal_rules(paragraph_cleaned)
