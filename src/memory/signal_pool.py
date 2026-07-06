"""用户画像信号池。

L1/L2/L3 三层摘取路径的统一入口：信号入池前查重画像 → 情感强度增强 →
相似信号去重合并 + 计数累加 + 活动即续期 → 达阈值入 pending 队列触发写入。

设计哲学：画像是"系统观察到的稳定模式"，而非"用户声明的一次性信息"。
即使用户明确说"记住我喜欢 X"，也需多次出现才写入画像——只有多次出现
才证明是稳定偏好，而非临时兴起。

持久化：data/profile_signal_pool.json，debounce 1 秒异步写入 + 原子替换。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from .consolidation import ConsolidationEngine

logger = logging.getLogger(__name__)


# 中文停用词表（信号关键词提取时过滤）
_ZH_STOPWORDS: Set[str] = {
    "用户", "这个", "一个", "是", "的", "了", "在", "我", "你", "他",
    "她", "们", "和", "与", "及", "或", "但", "而", "也", "都", "就",
    "还", "又", "才", "只", "能", "会", "要", "想", "觉得", "认为",
    "什么", "怎么", "为什么", "哪里", "谁", "多少", "可以", "应该",
    "现在", "今天", "昨天", "明天", "已经", "正在", "将要", "这次",
    "那次", "上面", "下面", "里面", "外面", "前面", "后面", "左边",
    "右边", "比如", "例如", "如果", "因为", "所以", "虽然", "但是",
    "不过", "然后", "接着", "之后", "之前", "起来", "下来", "出去",
    "回来", "过来", "过去", "一下", "一些", "一点", "这种", "那种",
}

# 英文停用词表
_EN_STOPWORDS: Set[str] = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "shall", "to", "of", "in",
    "on", "at", "by", "for", "with", "about", "as", "into", "like",
    "through", "after", "over", "between", "out", "against", "during",
    "without", "before", "under", "around", "among", "and", "but", "or",
    "so", "if", "because", "as", "until", "while", "of", "at", "by",
    "for", "with", "about", "against", "between", "into", "through",
    "during", "before", "after", "above", "below", "from", "up", "down",
    "in", "out", "on", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how",
    "all", "each", "few", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "than", "too", "very",
    "s", "t", "just", "don", "now", "i", "me", "my", "myself", "we",
    "our", "ours", "ourselves", "you", "your", "yours", "yourself",
    "yourselves", "he", "him", "his", "himself", "she", "her", "hers",
    "herself", "it", "its", "itself", "they", "them", "their", "theirs",
    "themselves", "what", "which", "who", "whom", "this", "that", "these",
    "those", "am", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having", "do", "does", "did", "doing", "a",
    "an", "the", "and", "but", "if", "or", "because", "as", "until",
    "while", "of", "at", "by", "for", "with", "about", "against",
    "between", "into", "through", "during", "before", "after", "above",
    "below", "from", "up", "down", "in", "out", "on", "off", "over",
    "under", "again", "further", "then", "once",
}


def _now_iso() -> str:
    """当前 UTC 时间 ISO 格式字符串。"""
    return datetime.now().isoformat()


def _parse_iso(s: str) -> datetime:
    """解析 ISO 格式时间字符串，容错处理。"""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.now()


# 情感动词词表（独立抽取为 token，提升"喜欢 X vs 讨厌 X"区分度）
# 用于 _extract_keywords 第 1 步 + _find_similar 对象区分保护 + 方向相反保护
#
# 按情感强度 + 方向分四类：
# - 正向强（+3）：爱/最爱/酷爱/痴迷/沉迷/狂热/崇尚/钟爱
# - 正向弱（+2）：喜欢/偏好/倾向/看好/认可/偏爱
# - 负向强（+3）：讨厌/最烦/恨/受不了/厌恶/反感/嫌弃/排斥/憎恶/烦死/鄙视/痛恨
# - 负向弱（+2）：不爱/不喜欢/不想/不要/拒绝/回避/无感
#
# 修复 v2 bug：原词表把"极其/特别"（程度副词）、"希望/接受/要求"（非情感词）混入，
# 且单一集合导致 _extract_keywords 子串截胡（"不喜欢"含"喜欢"被双重计数）。
# 拆分后 _find_similar 可通过 _POSITIVE_EMOTIONS/_NEGATIVE_EMOTIONS 判断方向，
# 防止"我喜欢 X" 与 "我不喜欢 X" 误合并。
_POSITIVE_STRONG: Set[str] = {
    "爱", "最爱", "酷爱", "痴迷", "沉迷", "狂热", "崇尚", "钟爱",
}
_POSITIVE_WEAK: Set[str] = {
    "喜欢", "偏好", "倾向", "看好", "认可", "偏爱",
}
_NEGATIVE_STRONG: Set[str] = {
    "讨厌", "最烦", "恨", "受不了", "厌恶", "反感",
    "嫌弃", "排斥", "憎恶", "烦死", "鄙视", "痛恨",
}
_NEGATIVE_WEAK: Set[str] = {
    "不爱", "不喜欢", "不想", "不要", "拒绝", "回避", "无感",
}
_EMOTION_VERBS: Set[str] = (
    _POSITIVE_WEAK | _POSITIVE_STRONG
    | _NEGATIVE_WEAK | _NEGATIVE_STRONG
)
# 方向集合：用于 _find_similar 方向相反保护
_POSITIVE_EMOTIONS: Set[str] = _POSITIVE_WEAK | _POSITIVE_STRONG
_NEGATIVE_EMOTIONS: Set[str] = _NEGATIVE_WEAK | _NEGATIVE_STRONG

# 情感权重表（强+3 / 弱+2）
# 用于 _detect_emotion_boost 计算额外权重
_EMOTION_WEIGHTS: Dict[str, int] = {
    # 强情感（+3）
    "讨厌": 3, "最烦": 3, "恨": 3, "受不了": 3, "厌恶": 3, "反感": 3,
    "嫌弃": 3, "排斥": 3, "憎恶": 3, "烦死": 3, "鄙视": 3, "痛恨": 3,
    "爱": 3, "最爱": 3, "酷爱": 3, "痴迷": 3, "沉迷": 3, "狂热": 3,
    "崇尚": 3, "钟爱": 3,
    # 弱情感（+2）
    "不爱": 2, "不喜欢": 2, "不想": 2, "不要": 2, "拒绝": 2, "回避": 2, "无感": 2,
    "喜欢": 2, "偏好": 2, "倾向": 2, "看好": 2, "认可": 2, "偏爱": 2,
}
# 按长度降序排列，模块级常量避免每次调用重算
_EMOTION_VERBS_SORTED: List[str] = sorted(
    _EMOTION_WEIGHTS.keys(), key=len, reverse=True
)


def _scan_emotion_verbs(content: str) -> List[Tuple[str, int]]:
    """扫描 content 中的情感动词，长词优先匹配，返回 [(verb, position), ...]。

    匹配后占用字符区间，短词不在已占用区间重复匹配，避免"不喜欢"被"喜欢"截胡。
    用于 _extract_keywords（抽取 token）和 _detect_emotion_boost（计算权重）。
    """
    matches: List[Tuple[str, int]] = []
    occupied: List[Tuple[int, int]] = []
    for verb in _EMOTION_VERBS_SORTED:
        start = 0
        while True:
            idx = content.find(verb, start)
            if idx == -1:
                break
            end = idx + len(verb)
            # 检查是否与已匹配区间重叠：双否定等价"无重叠才占用"
            if not any(not (end <= s or idx >= e) for s, e in occupied):
                matches.append((verb, idx))
                occupied.append((idx, end))
            start = idx + 1
    return matches

# 复合句拆分正则：在中文/英文标点处切分（逗号/顿号/分号/句号/感叹/问号/换行）
_ATOMIC_SPLIT_RE = re.compile(r"[，,、；;。！!？?\n]+")


def _split_atomic(content: str) -> List[str]:
    """将复合内容拆分为原子事实列表。

    策略：在中文/英文标点处切分，过滤长度 <4 的碎片。
    若切分后仅 1 条，返回原内容（避免无意义拆分）。

    设计依据：用户要求"一条数据一个观点"。LLM 抽取的复合句如
    "用户讨厌emoji，偏好简洁正经的交流方式" 应拆为两条独立信号，
    避免次要事实噪音稀释主事实的关键词集。

    示例:
        "用户讨厌emoji，偏好简洁正经的交流方式"
        → ["用户讨厌emoji", "偏好简洁正经的交流方式"]
        "用户是后端工程师" → ["用户是后端工程师"]  # 不拆分

    参数:
        content: 原始文本。

    返回:
        原子事实列表。空内容返回空列表。
    """
    if not content:
        return []
    parts = [p.strip() for p in _ATOMIC_SPLIT_RE.split(content) if p.strip()]
    # 过滤过短碎片（<4 字符通常是无意义残留）
    parts = [p for p in parts if len(p) >= 4]
    # 若过滤后只剩 1 条或 0 条，返回原内容（避免拆分破坏单原子事实）
    if len(parts) <= 1:
        return [content.strip()] if content.strip() else []
    return parts


def _extract_keywords(content: str) -> Set[str]:
    """从文本提取关键词集合（用于 Jaccard 去重匹配）。

    策略（v3，长词优先匹配）：
    - 情感动词：通过 _scan_emotion_verbs 长词优先匹配，避免子串截胡
      （"不喜欢"含"喜欢"不再被双重计数）
    - 中文整段：连续汉字段（长度≥2）作为整段关键词，过滤停用词
    - 4 字滑窗：长段（≥6 字）补 4 字子串，捕捉"用户讨厌"这类核心短语
    - 英文：连续字母段（长度≥2），lowercase，过滤停用词

    v2 → v3 变更：情感动词抽取改用 _scan_emotion_verbs（长词优先 + 区间占用），
    防止"不喜欢"被"喜欢"截胡导致正负方向未区分。

    参数:
        content: 原始文本。

    返回:
        关键词集合（小写）。
    """
    if not content:
        return set()

    keywords: Set[str] = set()

    # 1. 情感动词：长词优先匹配，避免子串截胡
    for verb, _idx in _scan_emotion_verbs(content):
        keywords.add(verb)

    # 2. 中文整段（连续汉字，len≥2，过滤停用词）+ 4 字滑窗
    for match in re.finditer(r"[\u4e00-\u9fa5]+", content):
        segment = match.group()
        if len(segment) < 2 or segment in _ZH_STOPWORDS:
            continue
        keywords.add(segment)
        # 2b. 长段（≥6 字）补 4 字滑窗（捕捉"用户讨厌"核心短语）
        if len(segment) >= 6:
            for i in range(len(segment) - 3):
                sub = segment[i:i + 4]
                if sub not in _ZH_STOPWORDS:
                    keywords.add(sub)

    # 3. 英文单词（len≥2，lowercase，过滤停用词）
    for match in re.finditer(r"[A-Za-z]+", content):
        word = match.group().lower()
        if len(word) >= 2 and word not in _EN_STOPWORDS:
            keywords.add(word)

    return keywords


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """计算两个集合的 Jaccard 相似度。"""
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass
class Signal:
    """信号池中的单条信号。

    一条信号代表系统观察到的某个用户特征/偏好/习惯的累积证据。
    多次出现的相似信号会合并为一条，count 累加。
    达阈值（THRESHOLD）后状态变 triggered，入 pending 队列等待写入画像。
    consolidate 写入成功后状态变 written。
    """

    id: str
    content: str
    keywords: List[str] = field(default_factory=list)
    category: str = ""
    count: int = 0
    sources: List[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    status: str = "pending"  # pending / triggered / written
    section: str = "沉淀笔记"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 持久化的字典。"""
        return {
            "id": self.id,
            "content": self.content,
            "keywords": self.keywords,
            "category": self.category,
            "count": self.count,
            "sources": self.sources,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "status": self.status,
            "section": self.section,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Signal":
        """从字典反序列化。"""
        return cls(
            id=str(data.get("id", "")),
            content=str(data.get("content", "")),
            keywords=list(data.get("keywords", [])),
            category=str(data.get("category", "")),
            count=int(data.get("count", 0)),
            sources=list(data.get("sources", [])),
            first_seen=str(data.get("first_seen", "")),
            last_seen=str(data.get("last_seen", "")),
            status=str(data.get("status", "pending")),
            section=str(data.get("section", "沉淀笔记")),
        )


class SignalPool:
    """用户画像信号池。

    L1/L2/L3 三层摘取路径的统一入口。信号入池前查重画像 → 情感增强 →
    相似去重合并 → 计数累加 → 达阈值入 pending 队列。

    线程安全：所有公共方法通过 ``_lock`` 互斥。
    持久化：debounce 1 秒异步写入 + .tmp 原子替换。
    """

    THRESHOLD = 7  # 统一阈值，所有信号平等

    # 情感强度增强器：通过 _scan_emotion_verbs 长词优先匹配 + "我/用户"前缀检查
    # 弱情感（喜欢/偏好/不喜欢/不爱 等）+2，强情感（爱/讨厌/恨/厌恶 等）+3
    # 匹配"我喜欢X"与 LLM 抽取的"用户讨厌X"两种前缀
    # v3: 删除 _EMOTION_BOOST_PATTERNS dict，改用 _scan_emotion_verbs 避免子串截胡

    # Jaccard 相似度阈值，≥此值视为重复信号
    # v2: 4 字窗 + 情感动词独立 token 召回提升，配合 _find_similar 对象区分保护
    # 降阈值到 0.25 以合并同义改写（如"用户讨厌使用emoji" vs "用户讨厌emoji"）
    DEDUP_THRESHOLD = 0.25

    # 入池前查重画像的覆盖率阈值
    # 非对称：新信号关键词被画像覆盖的比例 ≥ 此值视为已存在
    # （画像可能含多个无关 section 稀释 Jaccard，覆盖率更准确反映"是否已被记录"）
    PROFILE_DEDUP_THRESHOLD = 0.5

    # 过期清理参数
    PENDING_EXPIRE_DAYS = 30       # pending 信号 30 天未活动过期
    TRIGGERED_EXPIRE_DAYS = 7      # triggered/written 信号 7 天后移除

    def __init__(
        self,
        pool_path: Path,
        consolidation_engine: Optional["ConsolidationEngine"],
        profile_path: Path,
    ) -> None:
        """初始化信号池。

        参数:
            pool_path: 信号池 JSON 文件路径（如 data/profile_signal_pool.json）。
            consolidation_engine: 记忆沉淀引擎实例，达阈值时调
                ``enqueue_profile_update`` 入 pending 队列。为 None 时
                达阈值的信号仅标记 triggered，不入队（测试/降级场景）。
            profile_path: memory.md 路径，用于入池前查重。
        """
        self._pool_path = pool_path
        self._consolidation_engine = consolidation_engine
        self._profile_path = profile_path
        self._signals: List[Signal] = []
        self._lock = threading.Lock()
        self._save_timer: Optional[threading.Timer] = None
        # 画像关键词缓存：hash 变化时刷新，避免每次 add 都重新提取
        self._profile_keywords_cache: Set[str] = set()
        self._profile_text_hash: Optional[str] = None
        # 信号 ID 单调递增计数器
        self._id_counter: int = 0
        self._load()

    # ------------------------------------------------------------------
    # 核心入口
    # ------------------------------------------------------------------
    def add(
        self,
        content: str,
        source: str,
        category: str = "",
        weight: int = 1,
        section: str = "沉淀笔记",
    ) -> None:
        """添加信号到池（入口：先拆分复合句为原子事实，再逐条入池）。

        用户要求"一条数据一个观点"。LLM 抽取的复合句如
        "用户讨厌emoji，偏好简洁正经的交流方式" 会被拆为两条独立信号，
        避免次要事实噪音稀释主事实的关键词集。

        多片段权重按 ``weight // n`` 均分（最低 1，避免总权重翻倍）。
        """
        if not content or not content.strip():
            return
        atomic_parts = _split_atomic(content)
        if len(atomic_parts) <= 1:
            self._add_single(content, source, category, weight, section)
            return
        part_weight = max(1, weight // len(atomic_parts))
        for part in atomic_parts:
            self._add_single(part, source, category, part_weight, section)

    def _add_single(
        self,
        content: str,
        source: str,
        category: str = "",
        weight: int = 1,
        section: str = "沉淀笔记",
    ) -> None:
        """单条原子信号入池（含锁与状态判断）。

        语义:
            - 入池前查重画像：若 memory.md 已包含该信息（覆盖率 ≥0.5），跳过
            - 情感强度增强：检测"我/用户喜欢/爱/讨厌"等，额外加权（+1/+2）
            - 相似信号合并：count 累加 + keywords 合并 + sources append
            - 活动即续期：任何更新都刷新 last_seen
            - 达阈值：status=triggered，调 enqueue_profile_update 入队
        """
        if not content or not content.strip():
            return

        with self._lock:
            # 1. 入池前查重：画像已包含该信息则跳过（避免无意义累积）
            if self._already_in_profile(content):
                logger.debug("信号 '%s' 已在画像中，跳过入池", content[:50])
                return

            # 2. 情感强度增强
            emotion_boost = self._detect_emotion_boost(content)
            effective_weight = weight + emotion_boost

            # 3. 相似信号去重合并
            keywords = _extract_keywords(content)
            similar = self._find_similar(keywords)

            if similar is not None:
                # 相似信号合并：count 累加 + keywords 合并 + sources append
                # 活动即续期：刷新 last_seen
                similar.count += effective_weight
                merged_keywords = set(similar.keywords) | keywords
                similar.keywords = list(merged_keywords)
                similar.sources.append(source)
                similar.last_seen = _now_iso()
                target = similar
            else:
                # 新信号入池
                target = Signal(
                    id=self._new_id(),
                    content=content,
                    keywords=sorted(keywords),
                    category=category,
                    count=effective_weight,
                    sources=[source],
                    first_seen=_now_iso(),
                    last_seen=_now_iso(),
                    status="pending",
                    section=section,
                )
                self._signals.append(target)

            # 4. 达阈值 → 入 pending 队列
            if (
                target.status == "pending"
                and target.count >= self.THRESHOLD
            ):
                target.status = "triggered"
                self._enqueue_profile_update(target)

            self._save_debounced()

    # ------------------------------------------------------------------
    # 入池前查重画像
    # ------------------------------------------------------------------
    def _already_in_profile(self, content: str) -> bool:
        """检查画像是否已包含该信号信息。覆盖率 ≥ PROFILE_DEDUP_THRESHOLD 视为已有。

        采用非对称覆盖率（新信号关键词被画像覆盖的比例），而非对称 Jaccard。
        画像可能包含多个无关 section（如"用户画像"/"我和你"等标题），其关键词
        会稀释 Jaccard 分母；覆盖率只关心"新信号的核心信息是否已被记录"，
        更符合查重语义。

        参数:
            content: 待入池的信号内容。

        返回:
            True 表示画像已包含，应跳过入池；False 表示可入池。
        """
        profile_keywords = self._get_profile_keywords()
        if not profile_keywords:
            return False
        signal_keywords = _extract_keywords(content)
        if not signal_keywords:
            return False
        coverage = len(signal_keywords & profile_keywords) / len(signal_keywords)
        return coverage >= self.PROFILE_DEDUP_THRESHOLD

    def _get_profile_keywords(self) -> Set[str]:
        """获取画像关键词（带缓存，hash 变化时刷新）。"""
        profile_text = self._load_profile_text()
        if not profile_text:
            return set()
        text_hash = hashlib.md5(profile_text.encode("utf-8")).hexdigest()
        if text_hash != self._profile_text_hash:
            self._profile_keywords_cache = _extract_keywords(profile_text)
            self._profile_text_hash = text_hash
        return self._profile_keywords_cache

    def _load_profile_text(self) -> str:
        """加载 memory.md 全文。失败时返回空字符串。"""
        try:
            if not self._profile_path.exists():
                return ""
            return self._profile_path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return ""

    # ------------------------------------------------------------------
    # 情感强度增强
    # ------------------------------------------------------------------
    def _detect_emotion_boost(self, content: str) -> int:
        """检测情感强度词，返回额外权重。取最高值不叠加。

        v3: 改用 _scan_emotion_verbs 长词优先匹配，避免子串截胡。
        匹配模式：要求"我"或"用户"前缀且间距 ≤5 字。
        - "我喜欢 Rust" → +2（弱情感）
        - "用户讨厌 emoji" → +3（强情感）
        - "我不喜欢 emoji" → +2（弱情感，"不喜欢"长词优先匹配，不再被"喜欢"截胡）
        - "你喜欢什么" → 0（"喜欢"前不是"我/用户"）
        - "这个我喜欢" → +2（"我喜欢"匹配）

        prefix 长度 7：覆盖"用户"+5字 或 "我"+6字。
        """
        boost = 0
        for verb, idx in _scan_emotion_verbs(content):
            # prefix 长度 7：覆盖"用户"+5字 或 "我"+6字
            prefix_start = max(0, idx - 7)
            prefix = content[prefix_start:idx]
            if re.search(r"(?:我|用户).{0,5}$", prefix):
                boost = max(boost, _EMOTION_WEIGHTS[verb])
        return boost

    # ------------------------------------------------------------------
    # 相似信号去重
    # ------------------------------------------------------------------
    def _find_similar(self, new_keywords: Set[str]) -> Optional[Signal]:
        """关键词 Jaccard 相似度 ≥阈值视为重复。pending/triggered 均可吸收新证据（written 跳过）。

        对象区分保护：两信号含相同情感动词但英文对象完全不交集时不合并
        （防止"喜欢rust" vs "喜欢go" 误合并）。

        方向相反保护：一边纯正、一边纯负，且同对象有交集 → 不合并
        （防止"我喜欢emoji" vs "我不喜欢emoji" 误合并）。
        混合情感（同时含正负）不触发保护，避免误判。

        参数:
            new_keywords: 新信号的关键词集合。

        返回:
            匹配到的 Signal 实例，无匹配返回 None。
        """
        if not new_keywords:
            return None
        new_verbs = new_keywords & _EMOTION_VERBS
        new_objs = {k for k in new_keywords if k.isascii() and k not in _EMOTION_VERBS}
        # 预计算新信号的方向纯正/纯负（用于方向相反保护）
        new_pos = new_verbs & _POSITIVE_EMOTIONS
        new_neg = new_verbs & _NEGATIVE_EMOTIONS
        new_pure_pos = bool(new_pos and not new_neg)
        new_pure_neg = bool(new_neg and not new_pos)
        for signal in self._signals:
            if signal.status == "written":
                continue
            sig_kw = set(signal.keywords)
            if not sig_kw:
                continue
            # 对象区分保护：共同情感动词但英文对象完全不交集 → 不合并
            sig_verbs = sig_kw & _EMOTION_VERBS
            sig_objs = {k for k in sig_kw if k.isascii() and k not in _EMOTION_VERBS}
            if new_verbs and sig_verbs and (new_verbs & sig_verbs):
                if new_objs and sig_objs and not (new_objs & sig_objs):
                    continue
            # 方向相反保护：一边纯正、一边纯负，且同对象有交集 → 不合并
            sig_pos = sig_verbs & _POSITIVE_EMOTIONS
            sig_neg = sig_verbs & _NEGATIVE_EMOTIONS
            sig_pure_pos = bool(sig_pos and not sig_neg)
            sig_pure_neg = bool(sig_neg and not sig_pos)
            if (new_pure_pos and sig_pure_neg) or (new_pure_neg and sig_pure_pos):
                if new_objs and sig_objs and (new_objs & sig_objs):
                    continue
            jaccard = _jaccard(new_keywords, sig_kw)
            if jaccard >= self.DEDUP_THRESHOLD:
                return signal
        return None

    def _has_distinct_objects(self, a: Set[str], b: Set[str]) -> bool:
        """两信号应视为不同对象不合并。返回 True 表示不应合并。

        用于 _backfill_consolidate 合并前的预检查，防止以下情况误合并：
        - 对象区分：两信号含相同情感动词但英文对象完全不交集（"喜欢rust" vs "喜欢go"）
        - 方向相反：一边纯正、一边纯负，且同对象有交集（"我喜欢emoji" vs "我不喜欢emoji"）

        混合情感（同时含正负）不视为方向相反，避免误判。
        """
        a_verbs = a & _EMOTION_VERBS
        b_verbs = b & _EMOTION_VERBS
        if not (a_verbs and b_verbs):
            return False
        # 对象区分：共同情感动词但英文对象完全不交集 → 不合并
        if a_verbs & b_verbs:
            a_objs = {k for k in a if k.isascii() and k not in _EMOTION_VERBS and len(k) >= 2}
            b_objs = {k for k in b if k.isascii() and k not in _EMOTION_VERBS and len(k) >= 2}
            if a_objs and b_objs and not (a_objs & b_objs):
                return True
        # 方向相反：一边纯正、一边纯负，且同对象有交集 → 不合并
        a_pos = a_verbs & _POSITIVE_EMOTIONS
        a_neg = a_verbs & _NEGATIVE_EMOTIONS
        b_pos = b_verbs & _POSITIVE_EMOTIONS
        b_neg = b_verbs & _NEGATIVE_EMOTIONS
        a_pure_pos = bool(a_pos and not a_neg)
        a_pure_neg = bool(a_neg and not a_pos)
        b_pure_pos = bool(b_pos and not b_neg)
        b_pure_neg = bool(b_neg and not b_pos)
        if (a_pure_pos and b_pure_neg) or (a_pure_neg and b_pure_pos):
            a_objs = {k for k in a if k.isascii() and k not in _EMOTION_VERBS and len(k) >= 2}
            b_objs = {k for k in b if k.isascii() and k not in _EMOTION_VERBS and len(k) >= 2}
            if a_objs and b_objs and (a_objs & b_objs):
                return True
        return False

    # ------------------------------------------------------------------
    # 阈值触发
    # ------------------------------------------------------------------
    def _enqueue_profile_update(self, signal: Signal) -> None:
        """达阈值的信号入 pending 队列，等 consolidate 时写入画像。"""
        if self._consolidation_engine is None:
            logger.debug(
                "consolidation_engine 未注入，信号 %s 仅标记 triggered 不入队",
                signal.id,
            )
            return
        try:
            self._consolidation_engine.enqueue_profile_update(
                "add", signal.section, signal.content
            )
            logger.info(
                "信号达阈值入队: id=%s section=%s count=%d content=%s",
                signal.id, signal.section, signal.count, signal.content[:50],
            )
        except Exception as e:
            logger.error("信号入队失败: %s", e)

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------
    def mark_written(self, signal_ids: List[str]) -> None:
        """consolidate apply 成功后，标记信号为 written。

        参数:
            signal_ids: 已写入画像的信号 ID 列表。
        """
        if not signal_ids:
            return
        with self._lock:
            id_set = set(signal_ids)
            for signal in self._signals:
                if signal.id in id_set:
                    signal.status = "written"
            self._save_debounced()

    def mark_written_by_contents(self, contents: List[str]) -> None:
        """consolidate apply 成功后，通过 content 匹配标记信号为 written。

        consolidate 的 _apply_pending_ops 应用 profile_updates 后，无法直接
        获知哪些 signal 触发了这次写入（pending 队列中混合了 L1 add 信号、
        replace/delete 显式修改）。本方法通过 content 文本匹配找出已写入的
        triggered 信号，标记为 written。

        参数:
            contents: 本次 apply 的 profile_updates 中所有 add 操作的 content 列表。
        """
        if not contents:
            return
        content_set = set(c.strip() for c in contents if c)
        if not content_set:
            return
        with self._lock:
            for signal in self._signals:
                if signal.status == "triggered" and signal.content.strip() in content_set:
                    signal.status = "written"
            self._save_debounced()

    # ------------------------------------------------------------------
    # 过期清理
    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        """清理过期信号。在 consolidate 时调用。

        过期判定基于 last_seen（最后活动时间）：
        - pending 信号 30 天未活动 → 移除
        - triggered/written 信号 7 天后 → 移除（已处理完毕，保留 7 天供审计）
        """
        now = datetime.now()
        with self._lock:
            removed = 0
            for signal in list(self._signals):
                age = (now - _parse_iso(signal.last_seen)).days
                if signal.status == "pending" and age > self.PENDING_EXPIRE_DAYS:
                    self._signals.remove(signal)
                    removed += 1
                    logger.info(
                        "清理过期 pending 信号: %s（%d 天未活动）",
                        signal.content[:50], age,
                    )
                elif (
                    signal.status in ("triggered", "written")
                    and age > self.TRIGGERED_EXPIRE_DAYS
                ):
                    self._signals.remove(signal)
                    removed += 1
                    logger.info(
                        "清理已处理信号: %s（%s，%d 天后移除）",
                        signal.content[:50], signal.status, age,
                    )
            if removed > 0:
                self._save_debounced()

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def get_status(self) -> List[Dict[str, Any]]:
        """返回信号池状态（供 profile_signals 工具查看）。"""
        with self._lock:
            return [
                {
                    "id": s.id,
                    "content": s.content,
                    "category": s.category,
                    "count": s.count,
                    "sources": list(s.sources),
                    "status": s.status,
                    "last_seen": s.last_seen,
                    "first_seen": s.first_seen,
                    "section": s.section,
                }
                for s in self._signals
            ]

    def get_dashboard_data(self) -> Dict[str, Any]:
        """返回监控面板数据（攻略进度条）。

        返回结构:
            {
                "signals": [...],              # 所有信号列表
                "sections": {section: [...]},  # 按 section 分组
                "summary": {total/pending/triggered/written/avg_progress},
                "threshold": 7,
            }
        """
        with self._lock:
            signals_data: List[Dict[str, Any]] = []
            for s in self._signals:
                progress = min(1.0, s.count / self.THRESHOLD) if self.THRESHOLD > 0 else 0.0
                signals_data.append({
                    "id": s.id,
                    "content": s.content,
                    "category": s.category,
                    "count": s.count,
                    "threshold": self.THRESHOLD,
                    "progress": progress,
                    "percent": round(progress * 100),
                    "status": s.status,
                    "last_seen": s.last_seen,
                    "section": s.section or "未分类",
                    "sources": list(s.sources),
                    "first_seen": s.first_seen,
                })

            # 按 section 分组
            sections: Dict[str, List[Dict[str, Any]]] = {}
            for sig in signals_data:
                sections.setdefault(sig["section"], []).append(sig)

            # 每个 section 内按 progress 降序（接近完成的在前）
            for sec_signals in sections.values():
                sec_signals.sort(key=lambda x: x["progress"], reverse=True)

            # 状态汇总
            total = len(signals_data)
            summary = {
                "total": total,
                "pending": sum(1 for s in signals_data if s["status"] == "pending"),
                "triggered": sum(1 for s in signals_data if s["status"] == "triggered"),
                "written": sum(1 for s in signals_data if s["status"] == "written"),
                "avg_progress": (
                    sum(s["progress"] for s in signals_data) / total
                    if total > 0 else 0.0
                ),
            }
            return {
                "signals": signals_data,
                "sections": sections,
                "summary": summary,
                "threshold": self.THRESHOLD,
            }

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _new_id(self) -> str:
        """生成新的信号 ID（单调递增）。"""
        self._id_counter += 1
        return f"sig_{self._id_counter:04d}"

    def _load(self) -> None:
        """从 JSON 文件加载信号池。文件不存在或解析失败时初始化空池。

        v1/v2 → v3 回填：version < 3 时用新抽取器（长词优先匹配）重算 keywords
        并合并重复信号（含方向相反保护）。
        幂等：v3 数据加载时 version >= 3 不会触发回填。
        """
        try:
            if not self._pool_path.exists():
                self._signals = []
                self._id_counter = 0
                return
            data = json.loads(self._pool_path.read_text(encoding="utf-8"))
            signals_data = data.get("signals", []) if isinstance(data, dict) else []
            self._signals = [Signal.from_dict(s) for s in signals_data if isinstance(s, dict)]
            version = data.get("version", 1) if isinstance(data, dict) else 1
            # 恢复 ID 计数器：取现有最大编号 +1
            max_num = 0
            for s in self._signals:
                if s.id.startswith("sig_"):
                    try:
                        num = int(s.id[4:])
                        if num > max_num:
                            max_num = num
                    except ValueError:
                        pass
            self._id_counter = max_num
            # v1/v2 → v3 回填：重新抽取 keywords（去除旧子串重复如"不喜欢"+"喜欢"并存）
            # + 合并重复信号（含新方向相反保护）
            if version < 3:
                self._backfill_consolidate()
            logger.info("信号池已加载: %d 条信号 (v%d)", len(self._signals), version)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("信号池加载失败，初始化空池: %s", e)
            self._signals = []
            self._id_counter = 0

    def _backfill_consolidate(self) -> None:
        """v1/v2 → v3 回填：重新抽取 keywords + 合并重复信号。

        在 _load（__init__）中调用，此时单线程，_save_debounced 内部加锁安全。
        幂等：v3 数据加载时 version >= 3 不会触发本方法。

        v3 变更：_extract_keywords 改用 _scan_emotion_verbs 长词优先匹配，
        _has_distinct_objects 新增方向相反保护。
        """
        for s in self._signals:
            s.keywords = sorted(_extract_keywords(s.content))
        merged_ids: Set[str] = set()
        for i, target in enumerate(self._signals):
            if target.id in merged_ids:
                continue
            for j in range(i + 1, len(self._signals)):
                other = self._signals[j]
                if other.id in merged_ids:
                    continue
                if target.status == "written" or other.status == "written":
                    continue
                if self._has_distinct_objects(set(target.keywords), set(other.keywords)):
                    continue
                jaccard = _jaccard(set(target.keywords), set(other.keywords))
                if jaccard < self.DEDUP_THRESHOLD:
                    continue
                # 合并 other → target
                if other.first_seen < target.first_seen:
                    target.first_seen = other.first_seen
                target.count += other.count
                target.keywords = sorted(set(target.keywords) | set(other.keywords))
                target.sources = list(dict.fromkeys(target.sources + other.sources))
                if other.last_seen > target.last_seen:
                    target.last_seen = other.last_seen
                if other.status == "triggered":
                    target.status = "triggered"
                merged_ids.add(other.id)
        if merged_ids:
            self._signals = [s for s in self._signals if s.id not in merged_ids]
            logger.info("信号池回填合并 %d 条重复信号", len(merged_ids))
            self._save_debounced()

    def _save_debounced(self) -> None:
        """debounce 1 秒异步写入，避免频繁 IO。

        在 _lock 内 schedule，实际写入时加锁读取最新数据。
        """
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(1.0, self._do_save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _do_save(self) -> None:
        """实际写入 JSON 文件。锁外写文件，避免持锁 IO。"""
        with self._lock:
            data = {
                "signals": [s.to_dict() for s in self._signals],
                "version": 3,
            }
        try:
            self._pool_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._pool_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self._pool_path)  # 原子替换
        except OSError as e:
            logger.error("信号池持久化失败: %s", e)

    def flush(self) -> None:
        """立即同步写入（用于关闭前持久化）。"""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        self._do_save()
