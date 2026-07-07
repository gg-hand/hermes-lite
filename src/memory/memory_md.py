"""用户画像 memory.md 管理。

异步写入、检索返回相关摘要，按段落组织 Markdown 文件。
由 ConsolidationEngine 在沉淀 user_profile 类事实时触发写入。
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 类别关键词映射：按优先级顺序匹配，命中第一个即归类
# 用于将 user_profile 事实内容归类到对应 Markdown 段落
_CATEGORY_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("基本信息", ["职业", "名字", "年龄", "城市", "位置", "地区", "母语", "籍贯", "语言"]),
    (
        "技术栈",
        [
            "框架", "技术栈", "技术", "python", "typescript", "javascript",
            "java", "golang", "rust", "react", "vue", "node",
        ],
    ),
    ("工作习惯", ["习惯", "偏好", "倾向", "风格", "方式", "工作流", "代码"]),
    ("兴趣爱好", ["爱好", "兴趣", "业余", "周末", "游戏", "电影", "音乐", "运动"]),
]


def _categorize(content: str) -> str:
    """根据内容关键词归类到对应类别。

    参数:
        content: 事实内容文本。

    返回:
        类别名称，未匹配则返回 "其他"。
    """
    text = content.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw.lower() in text:
                return category
    return "其他"


def _extract_keywords(text: str) -> set:
    """从文本中提取关键词集合（用于去重匹配）。

    简单按非字母数字汉字字符切分，过滤掉过短的词（长度 < 2）。

    参数:
        text: 原始文本。

    返回:
        关键词集合（小写）。
    """
    # \w 在 Python3 默认匹配 Unicode 字符（含汉字），此处显式补充汉字范围以增强可读性
    tokens = re.split(r"[^\w\u4e00-\u9fa5]+", text)
    return {t.lower() for t in tokens if len(t) >= 2}


class MemoryMdManager:
    """用户画像 memory.md 文件管理器。

    异步写入、按类别组织段落、关键词检索摘要。
    由 ConsolidationEngine 调用 async_write 触发写入，
    也可通过 read / get_summary 同步读取供上下文拼接使用。

    文件操作通过 threading.Lock 互斥，防止并发写入冲突。
    """

    def __init__(
        self,
        file_path: str = "data/memory.md",
        max_tokens: int = 3000,
    ) -> None:
        """初始化 memory.md 管理器。

        参数:
            file_path: memory.md 文件路径，默认 "data/memory.md"。
            max_tokens: 最大 token 数（字符数/3 粗估），默认 3000。
        """
        self.file_path = Path(file_path)
        self.max_tokens = max_tokens
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def read(self) -> str:
        """读取整个 memory.md 内容。

        文件不存在时返回空字符串。

        返回:
            memory.md 文件的完整文本。
        """
        with self._lock:
            return self._read_unchecked()

    def read_system_profile(self) -> str:
        """读取用于 system_text 的画像文本（排除 Agent 自画像和沟通偏好段）。

        这两段从 system_text 移出注入 messages[0] 动态区，避免 memory.md
        异步更新时破坏 system_text 缓存稳定性。其余段（用户画像主体）保留
        在 system_text 中享受前缀缓存。

        返回:
            排除 Agent 自画像和沟通偏好段后的画像文本。文件不存在时返回空字符串。
        """
        full_text = self.read()
        if not full_text:
            return ""
        for section_title in ("Agent 自画像", "沟通偏好"):
            full_text = self._remove_section_from_text(full_text, section_title)
        # 清理末尾多余空行
        return full_text.rstrip()

    def read_section_body(self, section_title: str) -> str:
        """读取指定 section 的 body 文本（不含 ``## 标题`` 行）。

        用于 context_manager 注入 messages[0] 动态区（如 Agent 自画像、
        沟通偏好）。

        参数:
            section_title: section 标题（不含 ``## `` 前缀）。

        返回:
            section body 文本。section 不存在时返回空字符串。
        """
        full_text = self.read()
        if not full_text:
            return ""
        lines = full_text.split("\n")
        start, end = self._find_section_range(lines, section_title)
        if start < 0:
            return ""
        body_lines = lines[start + 1:end]
        return "\n".join(body_lines).strip()

    @staticmethod
    def _remove_section_from_text(text: str, section_title: str) -> str:
        """从文本中移除指定 section（含标题行与 body）。

        移除后清理残留的连续空行（最多清一行），避免段间出现双空行。

        参数:
            text: 原始文本。
            section_title: section 标题（不含 ``## `` 前缀）。

        返回:
            移除指定 section 后的文本。section 不存在时原样返回。
        """
        lines = text.split("\n")
        start, end = MemoryMdManager._find_section_range(lines, section_title)
        if start < 0:
            return text
        del lines[start:end]
        if start < len(lines) and lines[start].strip() == "":
            lines.pop(start)
        return "\n".join(lines)

    def write(self, facts: List[Dict[str, Any]]) -> None:
        """同步写入 facts 到 memory.md（覆盖式更新）。

        只处理 type == "user_profile" 的 facts，按内容分类组织成 Markdown 段落。
        与现有内容做关键词去重合并，超出 max_tokens 时按重要性从低到高删除。

        参数:
            facts: 事实列表，每个元素形如
                   {"content": "...", "type": "user_profile", "importance": 0.8}。
        """
        with self._lock:
            # 1. 过滤并标准化 user_profile 类事实
            new_items = self._extract_items(facts)
            if not new_items:
                logger.debug("无可写入的 user_profile 事实，跳过 memory.md 写入")
                return

            # 2. 读取并解析现有内容
            existing_items = self._parse_existing()

            # 3. 关键词去重合并
            merged = self._merge_items(existing_items, new_items)

            # 4. 超限时按重要性从低到高裁剪
            merged = self._trim_by_importance(merged)

            # 5. 渲染并写回文件
            self._write_file(merged)

    def async_write(self, facts: List[Dict[str, Any]]) -> None:
        """异步写入 facts（使用守护线程，不阻塞主流程）。

        内部调用 write()，由 ConsolidationEngine 在沉淀时触发。
        若写入抛出异常，仅记录日志不影响主流程。

        参数:
            facts: 事实列表。
        """
        def _run() -> None:
            try:
                self.write(facts)
            except Exception as e:
                logger.error("异步写入 memory.md 失败: %s", e)

        thread = threading.Thread(
            target=_run, daemon=True, name="memory-md-async-writer"
        )
        thread.start()

    # 画像总长度硬上限（防止画像膨胀失控）
    MAX_PROFILE_TOTAL_CHARS = 8000

    # 存储层分段独立计数上限（三段总和保持 ≤ MAX_PROFILE_TOTAL_CHARS）
    # 用户画像段：含基本信息/技术栈/工作习惯/兴趣爱好/其他/沉淀笔记等所有
    #   非 Agent、非沟通偏好的 section（含 H1 标题与文件头部空行）
    MAX_USER_PROFILE_CHARS = 5000
    # Agent 自画像段：仅含 `## Agent 自画像` section
    MAX_AGENT_PROFILE_CHARS = 2000
    # 沟通偏好段：仅含 `## 沟通偏好` section
    MAX_COMMUNICATION_CHARS = 1000

    # section → segment 映射（其余 section 均归入 user 段）
    _AGENT_SECTIONS = frozenset({"Agent 自画像"})
    _COMMUNICATION_SECTIONS = frozenset({"沟通偏好"})

    def apply_profile_updates(self, updates: List[Dict[str, Any]]) -> None:
        """应用一批画像更新操作（add/replace/delete）到 memory.md。

        由 ConsolidationEngine 在 consolidate() 时统一调用，将 pending 队列中
        LLM 通过 update_profile 工具请求的修改批量合并到 memory.md。
        与 ``write()``（按 fact 关键词去重合并）不同，本方法按 section
        （``## 标题``）粒度直接增删改，保留 LLM 显式给出的内容形态。

        操作语义:
            - add: 在指定 section 末尾追加 content；section 不存在则新建。
              **第二道去重防线**：若 section 已有内容与待 add 的 content
              关键词高度重叠（交集非空），跳过本次 add，防止 pending 队列中
              多条相似 add 重复写入。
            - replace: 替换指定 section 的全部 body；section 不存在则新建。
            - delete: 删除指定 section（含标题行与 body）；section 不存在则跳过。

        **分段硬上限**：写文件前按段独立检查长度——用户画像段 ≤5000、
        Agent 自画像段 ≤2000、沟通偏好段 ≤1000。某段超限时仅拒绝该段的
        add 操作（replace/delete 不受影响，仍正常应用），其他段的 add 正常执行。
        分段检查后再做总长度兜底（≤8000），防止分段遗漏导致总长度失控。
        超限时不抛异常，仅记录警告日志，避免阻断 consolidate。

        参数:
            updates: 更新操作列表，每项含:
                - action: "add" | "replace" | "delete"
                - section: section 标题（如 "背景"、"偏好"），不含 ``## `` 前缀
                - content: 新内容（add/replace 时必填，delete 时忽略）
        """
        with self._lock:
            current_text = self._read_unchecked()
            # 第二道去重防线：过滤掉 section 已有相似内容的 add 操作
            deduped_updates = self._dedupe_add_updates(current_text, updates)
            new_text = self._apply_updates_to_text(current_text, deduped_updates)

            # 分段硬上限检查：按段拒绝超限的 add 操作
            segment_limits = {
                "user": self.MAX_USER_PROFILE_CHARS,
                "agent": self.MAX_AGENT_PROFILE_CHARS,
                "communication": self.MAX_COMMUNICATION_CHARS,
            }
            segment_lengths = self._compute_segment_lengths(new_text)
            exceeded_segments = {
                seg
                for seg, length in segment_lengths.items()
                if length > segment_limits[seg]
            }
            for seg in exceeded_segments:
                logger.warning(
                    "画像段 '%s' 长度 %d 超过上限 %d，拒绝该段 add 操作",
                    seg, segment_lengths[seg], segment_limits[seg],
                )

            if exceeded_segments:
                safe_updates = [
                    u for u in deduped_updates
                    if u.get("action") != "add"
                    or self._categorize_section(str(u.get("section", "")))
                    not in exceeded_segments
                ]
                new_text = self._apply_updates_to_text(current_text, safe_updates)

            # 总长度兜底检查（分段检查的 fallback，防止分段遗漏）
            if len(new_text) > self.MAX_PROFILE_TOTAL_CHARS:
                logger.warning(
                    "画像总长度 %d 超过上限 %d，拒绝全部 add 操作",
                    len(new_text), self.MAX_PROFILE_TOTAL_CHARS,
                )
                safe_updates = [
                    u for u in deduped_updates if u.get("action") != "add"
                ]
                new_text = self._apply_updates_to_text(current_text, safe_updates)
            self._write_raw_text(new_text)

    @classmethod
    def _categorize_section(cls, section_title: str) -> str:
        """将 section 标题归类到对应段（user/agent/communication）。

        参数:
            section_title: section 标题（不含 ``## `` 前缀）。

        返回:
            段标识符：``"agent"`` / ``"communication"`` / ``"user"``。
        """
        if section_title in cls._AGENT_SECTIONS:
            return "agent"
        if section_title in cls._COMMUNICATION_SECTIONS:
            return "communication"
        return "user"

    def _compute_segment_lengths(self, text: str) -> Dict[str, int]:
        """计算文本中各段的字符长度。

        按 ``## section`` 边界切分文本，累加各 section（含标题行）字符数到
        对应段。H1 标题（``# 用户画像``）与首段前的空行归入 user 段。

        参数:
            text: memory.md 全文。

        返回:
            ``{"user": int, "agent": int, "communication": int}`` 字典。
        """
        if not text:
            return {"user": 0, "agent": 0, "communication": 0}

        lines = text.split("\n")
        segments = {"user": 0, "agent": 0, "communication": 0}
        current_segment = "user"
        current_lines: List[str] = []

        def flush() -> None:
            if current_lines:
                segments[current_segment] += len("\n".join(current_lines))

        for line in lines:
            if line.startswith("## "):
                flush()
                current_lines = [line]
                section_title = line[3:].strip()
                current_segment = self._categorize_section(section_title)
            else:
                current_lines.append(line)
        flush()
        return segments

    def _dedupe_add_updates(
        self, current_text: str, updates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """第二道去重防线：过滤 section 已有相似内容的 add 操作。

        信号池的入池前查重是第一道防线（Jaccard ≥0.7 跳过入池），
        但 pending 队列中可能积累多条相似 add（不同信号达阈值触发）。
        本方法在 apply 前再过滤一次：若 add 的 content 关键词与目标 section
        现有内容有关键词交集，跳过本次 add。

        参数:
            current_text: 当前 memory.md 全文。
            updates: 待应用的更新操作列表。

        返回:
            过滤后的更新操作列表（add 操作可能被移除）。
        """
        if not current_text:
            return list(updates)

        lines = current_text.split("\n") if current_text else []
        deduped: List[Dict[str, Any]] = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            action = str(update.get("action", "")).strip()
            if action != "add":
                deduped.append(update)
                continue
            section = str(update.get("section", "")).strip()
            content = str(update.get("content", ""))
            if not section or not content:
                deduped.append(update)
                continue
            # 提取目标 section 现有内容
            start, end = self._find_section_range(lines, section)
            if start < 0:
                # section 不存在，必新建，无需去重
                deduped.append(update)
                continue
            existing_body = "\n".join(lines[start + 1:end])
            if not existing_body.strip():
                # section 存在但 body 为空，无需去重
                deduped.append(update)
                continue
            # 关键词交集去重（与 _merge_items 一致的策略）
            existing_kw = _extract_keywords(existing_body)
            new_kw = _extract_keywords(content)
            if existing_kw & new_kw:
                logger.info(
                    "apply 去重：add '%s' 与 section '%s' 现有内容关键词重叠，跳过",
                    content[:50], section,
                )
                continue
            deduped.append(update)
        return deduped

    @staticmethod
    def _find_section_range(
        lines: List[str], section_title: str
    ) -> Tuple[int, int]:
        """查找 section 在行列表中的范围 ``[start, end)``。

        section 由 ``## {section_title}`` 行起始，至下一个 ``## `` 行（不含）
        或文件末尾结束。匹配规则：标题行去除前后空白后必须等于
        ``## {section_title}``（精确匹配，避免 ``背景`` 误匹配 ``背景信息``）。

        参数:
            lines: memory.md 按行拆分后的列表。
            section_title: section 标题（不含 ``## `` 前缀）。

        返回:
            ``(start, end)`` 元组。未找到时返回 ``(-1, -1)``。
            ``start`` 为 ``## 标题`` 行索引，``end`` 为 section body 末尾
            （exclusive，指向下一个 ``## `` 行或 ``len(lines)``）。
        """
        header = f"## {section_title}"
        start = -1
        for i, line in enumerate(lines):
            if line.strip() == header:
                start = i
                break
        if start < 0:
            return -1, -1
        # body 延伸至下一个 ## 标题或文件末尾
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        return start, end

    def _apply_updates_to_text(
        self, text: str, updates: List[Dict[str, Any]]
    ) -> str:
        """对 memory.md 全文应用一批更新操作，返回新文本。

        参数:
            text: 原始 memory.md 文本。
            updates: 更新操作列表（与 :meth:`apply_profile_updates` 一致）。

        返回:
          应用所有操作后的新文本。
        """
        lines: List[str] = text.split("\n") if text else []

        for update in updates:
            if not isinstance(update, dict):
                continue
            action = str(update.get("action", "")).strip()
            section = str(update.get("section", "")).strip()
            content = update.get("content", "")
            if not section:
                logger.warning("跳过缺少 section 的画像更新: %s", update)
                continue
            if action == "add":
                lines = self._add_to_section(lines, section, str(content))
            elif action == "replace":
                lines = self._replace_section(lines, section, str(content))
            elif action == "delete":
                lines = self._delete_section(lines, section)
            else:
                logger.warning("跳过未知 action 的画像更新: %s", action)

        return "\n".join(lines)

    def _add_to_section(
        self, lines: List[str], section: str, content: str
    ) -> List[str]:
        """在指定 section 末尾追加 content；section 不存在则新建。

        参数:
            lines: 当前行列表。
            section: section 标题（不含 ``## `` 前缀）。
            content: 待追加的内容（可多行）。

        返回:
            修改后的行列表（原列表变更并返回）。
        """
        start, end = self._find_section_range(lines, section)
        if start < 0:
            # section 不存在：在文件末尾新建一个 section
            # 去除末尾空行，保持文件结构整洁
            while lines and lines[-1].strip() == "":
                lines.pop()
            if lines:
                lines.append("")  # 与上一个 section 之间空一行
            lines.append(f"## {section}")
            for ln in content.split("\n"):
                lines.append(ln)
            return lines
        # section 存在：在 end 之前（即下一个 ## 之前或文件末尾）插入 content
        new_lines = content.split("\n")
        for offset, ln in enumerate(new_lines):
            lines.insert(end + offset, ln)
        return lines

    def _replace_section(
        self, lines: List[str], section: str, content: str
    ) -> List[str]:
        """替换指定 section 的全部 body；section 不存在则新建。

        保留 ``## 标题`` 行不变，删除原 body 并插入新 content。

        参数:
            lines: 当前行列表。
            section: section 标题（不含 ``## `` 前缀）。
            content: 新的 section body（可多行）。

        返回:
            修改后的行列表（原列表变更并返回）。
        """
        start, end = self._find_section_range(lines, section)
        if start < 0:
            # section 不存在：在文件末尾新建一个 section
            while lines and lines[-1].strip() == "":
                lines.pop()
            if lines:
                lines.append("")
            lines.append(f"## {section}")
            for ln in content.split("\n"):
                lines.append(ln)
            return lines
        # section 存在：删除原 body（start+1 ~ end），再插入新 content
        del lines[start + 1:end]
        new_body = content.split("\n")
        for offset, ln in enumerate(new_body):
            lines.insert(start + 1 + offset, ln)
        return lines

    def _delete_section(
        self, lines: List[str], section: str
    ) -> List[str]:
        """删除指定 section（含标题行与 body）；section 不存在则跳过。

        删除 section 后，若原位置残留连续空行，清理其中紧跟的一行空行，
        避免多个 section 之间出现双空行。

        参数:
            lines: 当前行列表。
            section: section 标题（不含 ``## `` 前缀）。

        返回:
            修改后的行列表（原列表变更并返回）。
        """
        start, end = self._find_section_range(lines, section)
        if start < 0:
            return lines
        del lines[start:end]
        # 清理残留的空行（最多清一行，避免过度清理）
        if start < len(lines) and lines[start].strip() == "":
            lines.pop(start)
        return lines

    def _backup_before_write(self) -> Optional[str]:
        """写入前备份当前 memory.md（规范 9.3.1）。

        备份到 ``data/memory_backups/memory_YYYYMMDD_HHMMSS.md``，
        保留最近 5 个版本，超出时删除最旧的。

        返回:
            备份文件路径字符串，备份失败或源文件不存在时返回 None。
        """
        try:
            if not self.file_path.exists():
                return None
            backup_dir = self.file_path.parent / "memory_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"memory_{timestamp}.md"
            shutil.copy2(str(self.file_path), str(backup_path))
            # 清理旧备份，仅保留最近 5 个
            backups = sorted(backup_dir.glob("memory_*.md"))
            if len(backups) > 5:
                for old in backups[:-5]:
                    old.unlink(missing_ok=True)
            logger.debug("memory.md 已备份到 %s", backup_path)
            return str(backup_path)
        except Exception as e:
            logger.warning("memory.md 备份失败（不阻塞写入）: %s", e)
            return None

    def _write_raw_text(self, text: str) -> None:
        """直接写入原始文本到 memory.md 文件（不加锁，调用方需自行持锁）。

        与 :meth:`_write_file` 不同，本方法不做条目解析与渲染，
        直接将 ``text`` 覆盖写入文件，用于 :meth:`apply_profile_updates`
        这种按 section 粒度的修改。

        写入前自动备份到 ``memory_backups/`` 目录（规范 9.3.1），
        备份失败不阻塞写入。

        参数:
            text: 待写入的完整文本。
        """
        self._backup_before_write()
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(text, encoding="utf-8")
            logger.debug("memory.md 已写入（raw text，%d 字符）", len(text))
        except OSError as e:
            logger.error("写入 memory.md 失败: %s", e)

    def get_summary(
        self,
        query: Optional[str] = None,
        max_tokens: int = 500,
    ) -> str:
        """获取相关摘要。

        参数:
            query: 检索关键词。为 None 时返回开头部分的摘要；
                   不为 None 时按关键词匹配返回相关段落（不调用 LLM）。
            max_tokens: 摘要最大 token 数（字符数/3 粗估）。

        返回:
            摘要文本，文件为空时返回空字符串。
        """
        content = self.read()
        if not content:
            return ""

        if query is None:
            # 返回开头部分摘要
            return self._truncate_to_tokens(content, max_tokens)

        # 按关键词匹配返回相关段落
        relevant = self._match_paragraphs(content, query)
        return self._truncate_to_tokens(relevant, max_tokens)

    def get_token_count(self) -> int:
        """获取当前 memory.md 的 token 数（字符数/3 粗估）。

        返回:
            估算的 token 数。
        """
        content = self.read()
        return len(content) // 3

    # ------------------------------------------------------------------
    # 内部方法（均在已持有 _lock 的上下文中调用）
    # ------------------------------------------------------------------
    def _read_unchecked(self) -> str:
        """读取文件内容（不加锁，调用方需自行持锁）。

        文件不存在或读取失败时返回空字符串。
        """
        if not self.file_path.exists():
            return ""
        try:
            return self.file_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error("读取 memory.md 失败: %s", e)
            return ""

    def _extract_items(self, facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """从 facts 列表中提取并标准化 user_profile 类条目。

        参数:
            facts: 原始事实列表。

        返回:
            标准化后的条目列表，每项含 content / category / importance / keywords。
        """
        items: List[Dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            # 仅处理 user_profile 类事实
            if str(fact.get("type", "")).lower() != "user_profile":
                continue
            content = str(fact.get("content", "")).strip()
            if not content:
                continue
            # 兼容字符串形式的重要性评分
            try:
                importance = float(fact.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            items.append(
                {
                    "content": content,
                    "category": _categorize(content),
                    "importance": importance,
                    "keywords": _extract_keywords(content),
                }
            )
        return items

    def _parse_existing(self) -> List[Dict[str, Any]]:
        """解析现有 memory.md 文件为条目列表。

        按二级标题（## 类别）分组，每个列表项（- 内容）作为一条。
        现有条目的重要性默认为 0.5（中等），便于新事实覆盖。

        返回:
            条目列表，每项含 content / category / importance / keywords。
        """
        text = self._read_unchecked()
        if not text:
            return []

        items: List[Dict[str, Any]] = []
        current_category = "其他"

        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                continue
            # 二级标题：## 类别
            if line.startswith("## "):
                current_category = line[3:].strip() or "其他"
                continue
            # 一级标题：# 用户画像
            if line.startswith("# "):
                continue
            # 列表项：- 内容
            if line.startswith("- "):
                content = line[2:].strip()
                if not content:
                    continue
                items.append(
                    {
                        "content": content,
                        "category": current_category,
                        "importance": 0.5,  # 现有内容默认中等重要性
                        "keywords": _extract_keywords(content),
                    }
                )

        return items

    def _merge_items(
        self,
        existing: List[Dict[str, Any]],
        new: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """合并新旧条目，关键词重叠的合并为一项。

        合并策略：新条目与现有条目关键词有交集时视为重复，
                  取重要性更高的内容作为最终内容，并合并关键词集合；
                  无交集则新增。

        参数:
            existing: 现有条目列表。
            new: 新条目列表。

        返回:
            合并后的条目列表。
        """
        # 深拷贝现有条目，避免修改原始数据
        merged: List[Dict[str, Any]] = [dict(item) for item in existing]

        for new_item in new:
            match_idx = -1
            for i, ex in enumerate(merged):
                # 关键词有交集即视为重复
                if ex["keywords"] & new_item["keywords"]:
                    match_idx = i
                    break

            if match_idx >= 0:
                ex = merged[match_idx]
                # 合并关键词集合
                ex["keywords"] = ex["keywords"] | new_item["keywords"]
                # 取重要性更高的内容作为最终内容
                if new_item["importance"] >= ex["importance"]:
                    ex["content"] = new_item["content"]
                    ex["category"] = new_item["category"]
                ex["importance"] = max(ex["importance"], new_item["importance"])
            else:
                merged.append(dict(new_item))

        return merged

    def _trim_by_importance(
        self,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """按重要性裁剪条目，使渲染后总 token 数不超过 max_tokens。

        从重要性最低的条目开始删除，直到满足上限或只剩一条。

        参数:
            items: 待裁剪的条目列表。

        返回:
            裁剪后的条目列表（按重要性从高到低排序）。
        """
        # 按重要性从高到低排序
        items_sorted = sorted(items, key=lambda x: x["importance"], reverse=True)

        while items_sorted and self._estimate_tokens(items_sorted) > self.max_tokens:
            if len(items_sorted) <= 1:
                # 至少保留一条，避免空文件
                break
            # 删除重要性最低的（末尾）
            removed = items_sorted.pop()
            logger.debug(
                "memory.md 超出 max_tokens，删除低重要性条目: %s",
                removed["content"][:50],
            )

        return items_sorted

    def _estimate_tokens(self, items: List[Dict[str, Any]]) -> int:
        """估算条目列表渲染为 Markdown 后的 token 数。

        参数:
            items: 条目列表。

        返回:
            估算的 token 数（字符数/3）。
        """
        text = self._render_markdown(items)
        return len(text) // 3

    def _render_markdown(self, items: List[Dict[str, Any]]) -> str:
        """将条目列表渲染为 Markdown 文本。

        按类别分组，每组一个二级标题，每条一个列表项。

        参数:
            items: 条目列表。

        返回:
            Markdown 格式文本。
        """
        if not items:
            return "# 用户画像\n"

        # 按类别分组
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            groups.setdefault(item["category"], []).append(item)

        lines: List[str] = ["# 用户画像", ""]
        for category in sorted(groups.keys()):
            lines.append(f"## {category}")
            for item in groups[category]:
                lines.append(f"- {item['content']}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _write_file(self, items: List[Dict[str, Any]]) -> None:
        """将条目写入 memory.md 文件。

        确保父目录存在，覆盖式写入。

        参数:
            items: 待写入的条目列表。
        """
        text = self._render_markdown(items)
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(text, encoding="utf-8")
            logger.debug("memory.md 已写入，共 %d 个条目", len(items))
        except OSError as e:
            logger.error("写入 memory.md 失败: %s", e)

    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """按 token 上限截断文本。

        在不超过 max_tokens * 3 字符的前提下，尽量在行边界截断。

        参数:
            text: 原始文本。
            max_tokens: 最大 token 数。

        返回:
            截断后的文本。
        """
        max_chars = max(max_tokens * 3, 0)
        if len(text) <= max_chars:
            return text
        truncated = text[:max_chars]
        # 尽量在行边界截断，避免半行
        last_newline = truncated.rfind("\n")
        if last_newline > 0:
            truncated = truncated[:last_newline]
        return truncated + "\n"

    def _match_paragraphs(self, content: str, query: str) -> str:
        """按关键词匹配返回相关段落。

        将 memory.md 按二级标题切分为段落，返回与查询关键词有交集的段落。
        若无匹配则返回全文（再由调用方截断）。

        参数:
            content: memory.md 全文。
            query: 查询关键词。

        返回:
            匹配的段落文本，多个段落以空行连接。
        """
        query_keywords = _extract_keywords(query)
        if not query_keywords:
            return content

        # 按二级标题切分段落（一级标题作为首段）
        paragraphs: List[str] = []
        current_lines: List[str] = []

        for line in content.splitlines():
            # 遇到新的标题行时，结束当前段落
            if line.startswith("# ") or line.startswith("## "):
                if current_lines:
                    paragraphs.append("\n".join(current_lines))
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            paragraphs.append("\n".join(current_lines))

        # 收集与查询关键词匹配的段落
        matched: List[str] = []
        for para in paragraphs:
            para_keywords = _extract_keywords(para)
            if para_keywords & query_keywords:
                matched.append(para)

        if not matched:
            # 无匹配时返回全文，由调用方截断
            return content

        return "\n\n".join(matched)
