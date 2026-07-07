"""记忆检索与注入模块。

从 ChromaDB 向量库检索 Top-K 长期记忆，可选 LLM 重排，
格式化为可注入 prompt 中部的文本。

注入位置为 prompt 中部（缓存失效区起点之后），由后续 context_manager 负责组装。

注意：用户画像（user_profile）由 memory.md 全文直接注入 system prompt
（缓存命中区），不再通过本模块检索/注入。本模块会过滤掉向量库中
残留的 type=user_profile 条目，避免与 system prompt 重复注入。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

# LLMClient、ChromaMemoryStore、MemoryMdManager 仅用于类型提示，
# 运行时通过鸭子类型调用实例方法，用 TYPE_CHECKING 守卫避免在 import 期
# 强制加载 anthropic / numpy / chromadb 等重依赖。
if TYPE_CHECKING:
    from ..llm.client import LLMClient
    from ..memory.decay import MemoryDecay
    from ..memory.memory_md import MemoryMdManager
    from ..storage.chroma_store import ChromaMemoryStore

logger = logging.getLogger(__name__)

# 分桶精度：similarity 在同一桶内（差值小于该精度）视为同等相关，
# 桶内按 importance 降序排列，使高重要性记忆在相似度接近时优先保留。
BUCKET_PRECISION = 0.05


def _sort_key(mem: Dict[str, Any]) -> tuple:
    """构造记忆排序键：similarity 分桶 + 桶内 importance 降序。

    similarity 按 BUCKET_PRECISION 分桶（例如 0.82 与 0.81 同属 0.80 桶，
    桶值=round(sim/0.05)=16）；同桶内由 importance 决定顺序。
    配合 sorted(..., reverse=True) 实现"高桶优先 + 桶内高 importance 优先"。

    参数:
        mem: 记忆条目，含 similarity 字段与 metadata.importance 字段。

    返回:
        (bucket, importance) 二元组，用作 sorted 的 key。
    """
    sim = mem.get("similarity", 0.0)
    imp = mem.get("metadata", {}).get("importance", 0.5)
    try:
        sim_val = float(sim)
    except (TypeError, ValueError):
        sim_val = 0.0
    try:
        imp_val = float(imp)
    except (TypeError, ValueError):
        imp_val = 0.5
    # 桶值高的排前面（降序），桶内 importance 高的排前面（降序）
    bucket = round(sim_val / BUCKET_PRECISION)
    return (bucket, imp_val)


class MemoryRetriever:
    """记忆检索与注入器。

    工作流：向量检索 Top-K 长期记忆 → 过滤 user_profile 残留 → 可选 LLM 重排 →
    格式化为可注入 prompt 的文本（受 max_memory_tokens 限制）。

    user_profile 类记忆由 memory.md 直接注入 system prompt，不参与向量检索注入。
    """

    def __init__(
        self,
        chroma_store: "ChromaMemoryStore",
        memory_md_manager: "MemoryMdManager",
        llm_client: Optional["LLMClient"] = None,
        top_k: int = 5,
        enable_rerank: bool = False,
        max_memory_tokens: int = 1000,
        decay: Optional["MemoryDecay"] = None,
        relevance_threshold: float = 0.6,
    ) -> None:
        """初始化记忆检索器。

        参数:
            chroma_store: ChromaDB 长期记忆库实例。
            memory_md_manager: memory.md 用户画像管理器实例（保留参数兼容，
                               当前实现不再使用，用户画像由 system prompt 直接注入）。
            llm_client: LLM 客户端实例，可选，用于 LLM 重排。
            top_k: 向量检索返回数量，默认 5。
            enable_rerank: 是否启用 LLM 重排，默认 False。
            max_memory_tokens: 注入 prompt 的最大 token 数，默认 1000。
            decay: 可选的 MemoryDecay 实例，用于三因子衰减排序
                （recency × frequency × importance）。为 None 时回退到
                原有静态 importance 排序（向后兼容）。
        """
        self.chroma_store = chroma_store
        # 保留参数兼容 orchestrator 装配，但当前实现不再调用其方法
        self.memory_md_manager = memory_md_manager
        self.llm_client = llm_client
        self.top_k = top_k
        self.enable_rerank = enable_rerank
        self.max_memory_tokens = max_memory_tokens
        # Phase 7 Task 1: 三因子衰减排序（可选，None 时回退到静态 importance）
        self.decay = decay
        # 记忆相关性过滤阈值（低于此值的记忆不会注入上下文）
        self.relevance_threshold = relevance_threshold

    # ------------------------------------------------------------------
    # 检索主流程
    # ------------------------------------------------------------------
    @staticmethod
    def _get_type_priority(mem: Dict[str, Any]) -> int:
        """获取记忆类型的优先级分值。

        fact 类（结构化沉淀的事实）优先级最高，conversation_turn（原始对话）最低。
        同 similarity 桶内，高优先级类型排在前面。

        参数:
            mem: 记忆条目。

        返回:
            优先级分值（越高越优先）。
        """
        mem_type = str(mem.get("metadata", {}).get("type", "")).lower()
        priority_map = {
            "fact": 4,
            "decision": 3,
            "preference": 2,
            "error_lesson": 2,
            "user_profile": 1,
            "conversation_turn": 0,
        }
        return priority_map.get(mem_type, 0)

    def _sort_key(self, mem: Dict[str, Any]) -> tuple:
        """构造记忆排序键：similarity 分桶 + 类型优先级 + 桶内 decayed_importance 降序。

        Phase 7 Task 1: 当 ``self.decay`` 非 None 时，桶内排序使用
        ``MemoryDecay.decayed_importance``（recency × frequency × importance）
        替代静态 importance；``self.decay`` 为 None 时回退到静态 importance
        逻辑（向后兼容，与模块级 :func:`_sort_key` 一致）。

        Phase 10: 增加 ``type_priority`` 维度，fact 类记忆在同等相似度下优先于
        conversation_turn 类记忆，确保结构化知识优先被注入上下文。

        参数:
            mem: 记忆条目，含 similarity 字段与 metadata 字段。

        返回:
            (bucket, type_priority, importance_score) 三元组，用作 sorted 的 key。
            importance_score 为 decayed_importance 或静态 importance。
        """
        sim = mem.get("similarity", 0.0)
        try:
            sim_val = float(sim)
        except (TypeError, ValueError):
            sim_val = 0.0
        bucket = round(sim_val / BUCKET_PRECISION)

        type_priority = self._get_type_priority(mem)

        metadata = mem.get("metadata", {}) or {}
        imp = metadata.get("importance", 0.5)
        try:
            imp_val = float(imp)
        except (TypeError, ValueError):
            imp_val = 0.5

        # 使用 getattr 兼容测试中通过 __new__ 绕过 __init__ 的场景
        # （未设置 self.decay 时不报错，按静态 importance 处理）
        decay = getattr(self, "decay", None)
        if decay is None:
            # 回退到静态 importance
            return (bucket, type_priority, imp_val)
        # 三因子衰减：recency × frequency × importance
        last_accessed = metadata.get("last_accessed")
        access_count = metadata.get("access_count")
        try:
            decayed = decay.decayed_importance(imp_val, last_accessed, access_count)
        except Exception as e:
            logger.debug(
                "decayed_importance 计算异常，回退到静态 importance: %s",
                e,
            )
            decayed = imp_val
        return (bucket, type_priority, decayed)

    def _filter_by_relevance(
        self,
        memories: List[Dict[str, Any]],
        user_input: str,
    ) -> List[Dict[str, Any]]:
        """按语义相关性过滤记忆，阻断弱相关记忆污染上下文。

        在 ChromaDB 已按向量相似度排序的基础上，再用 content ↔ user_input
        的余弦相似度做二次校验。低于 ``relevance_threshold`` 的记忆被丢弃。
        嵌入失败时保守放行；不会过滤到空列表（至少保留最高相关那条）。

        参数:
            memories: 待过滤的记忆列表。
            user_input: 用户输入文本。

        返回:
            过滤后的记忆列表。
        """
        if not memories or not user_input or self.relevance_threshold <= 0.0:
            return memories

        try:
            user_vec = self.chroma_store._embed(user_input)
        except Exception as e:
            logger.debug("相关性过滤：用户输入嵌入失败，跳过过滤: %s", e)
            return memories

        kept: List[Dict[str, Any]] = []
        for mem in memories:
            content = str(mem.get("content", ""))
            if not content:
                continue
            try:
                mem_vec = self.chroma_store._embed(content)
            except Exception as e:
                logger.debug(
                    "相关性过滤：记忆嵌入失败，保守放行: %s, content=%s",
                    e, content[:40],
                )
                kept.append(mem)
                continue

            if user_vec is None or mem_vec is None:
                kept.append(mem)
                continue

            sim = self._cosine_similarity(user_vec, mem_vec)
            if sim >= self.relevance_threshold:
                kept.append(mem)
            else:
                logger.debug(
                    "过滤低相关记忆: sim=%.3f < threshold=%.2f, content=%s",
                    sim, self.relevance_threshold, content[:60],
                )

        if kept:
            return kept
        # 全部低于阈值时返回空，让工具层给出"未找到相关记忆"提示
        # （旧逻辑兜底返回 memories[:1] 会污染上下文，详见 audit log 分析）
        logger.debug(
            "全部 %d 条记忆低于阈值 %.2f，返回空列表避免污染",
            len(memories), self.relevance_threshold,
        )
        return []

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """计算余弦相似度。

        参数:
            a: 向量 A。
            b: 向量 B。

        返回:
            余弦相似度（0.0 ~ 1.0）。任一向量为零向量时返回 0.0。
        """
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (na * nb)

    def retrieve(
        self,
        user_input: str,
        namespace: str = "user",
        cron_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """检索相关记忆。

        流程:
            1. 从 ChromaDB 向量检索 Top-K 长期记忆
            2. 过滤 type=user_profile 的条目（避免与 system prompt 中
               memory.md 全文重复注入）
            3. 如果 enable_rerank 且 llm_client 存在，调用 rerank 重排
            4. 返回结构化结果

        Phase 8 Task 1.3: 支持 namespace + cron_id 过滤召回。
        - 默认 ``namespace="user"``（保持用户会话行为不变，向后兼容）。
        - cron 调用方显式传 ``namespace="cron"`` + ``cron_id``，只召回该调度项
          自己命名空间下的记忆，绝不返回用户会话或其他调度项的记忆。
        - ``namespace=None`` 表示不按命名空间过滤（管理员跨命名空间检索视图）。

        参数:
            user_input: 用户输入文本。
            namespace: Phase 8 隔离层命名空间。``"user"``（默认）只检索用户
                命名空间条目；``"cron"`` 只检索 cron 命名空间且要求 ``cron_id``
                匹配；``None`` 不按命名空间过滤。
            cron_id: 当 ``namespace="cron"`` 时必填，用于匹配 ``metadata.cron_id``。

        返回:
            结果字典:
            - long_term_memories: 长期记忆列表，每项 {content, metadata, similarity}
            - user_profile_summary: 固定为空字符串（向后兼容字段；
              用户画像已由 system prompt 注入，不再此处注入）
            - total_tokens: 估算的总 token 数
        """
        # 1. 向量检索 Top-K 长期记忆
        # Phase 8 Task 1.3: 透传 namespace + cron_id 到 chroma_store 做命名空间过滤
        raw_memories = self.chroma_store.query_memory(
            user_input,
            top_k=self.top_k,
            namespace=namespace,
            cron_id=cron_id,
        )

        # 2. 过滤 type=user_profile 的条目（防御性，清理向量库残留脏数据）
        memories = [
            m for m in raw_memories
            if str(m.get("metadata", {}).get("type", "")).lower() != "user_profile"
        ]
        if len(memories) != len(raw_memories):
            logger.debug(
                "过滤掉 %d 条 user_profile 残留向量（由 memory.md 注入）",
                len(raw_memories) - len(memories),
            )

        # 2.5 相关性过滤：阻断弱相关记忆污染上下文
        memories = self._filter_by_relevance(memories, user_input)

        # 3. 可选 LLM 重排；enable_rerank=False 时直接用向量检索的原始排序
        if self.enable_rerank and self.llm_client is not None and memories:
            memories = self.rerank(memories, user_input)

        # 4. 估算总 token 数
        total_tokens = self._estimate_total_tokens(memories)

        return {
            "long_term_memories": memories,
            "user_profile_summary": "",
            "total_tokens": total_tokens,
        }

    # ------------------------------------------------------------------
    # LLM 重排
    # ------------------------------------------------------------------
    def rerank(
        self, memories: List[Dict[str, Any]], user_input: str
    ) -> List[Dict[str, Any]]:
        """LLM 重排检索结果。

        用 llm_client.chat_main 让 LLM 对检索结果按相关性重新排序。
        LLM 调用失败或返回格式异常时，回退到原始向量检索排序。

        参数:
            memories: 待重排的记忆列表，每项含 content / similarity 等字段。
            user_input: 用户输入文本，作为重排的相关性参照。

        返回:
            重排后的记忆列表（按相关性从高到低）。
        """
        if not memories:
            return []

        if self.llm_client is None:
            logger.warning("rerank 被调用但 llm_client 为 None，返回原始排序")
            return memories

        # 构建 LLM 重排 prompt 并调用
        prompt = self._build_rerank_prompt(memories, user_input)
        messages = [{"role": "user", "content": prompt}]

        try:
            response = self.llm_client.chat_main_sync(messages)
        except RuntimeError as e:
            logger.error("调用 LLM 重排失败，回退到原始排序: %s", e)
            return memories

        # 提取并解析返回的索引数组
        raw_text = self._extract_response_text(response)
        order = self._parse_rerank_indices(raw_text, len(memories))

        if not order:
            # 解析失败，回退到原始排序
            logger.warning("解析 LLM 重排结果失败，回退到原始排序")
            return memories

        # 按解析出的索引顺序重排
        reranked = [memories[i] for i in order]
        logger.debug("LLM 重排完成，新顺序: %s", order)
        return reranked

    # ------------------------------------------------------------------
    # 格式化与注入
    # ------------------------------------------------------------------
    def format_for_prompt(self, retrieval_result: Dict[str, Any]) -> str:
        """将检索结果格式化为可注入 prompt 的文本。

        格式::

            ## 相关记忆
            1. [记忆内容1] (相关度: 0.85)
            2. [记忆内容2] (相关度: 0.72)

        无长期记忆时返回空字符串。
        格式化后若超过 max_memory_tokens，按相关度从低到高截断长期记忆。

        注意：用户画像（user_profile）由 memory.md 全文注入 system prompt，
        不在此处注入，避免与缓存命中区重复。retrieval_result 中的
        user_profile_summary 字段（向后兼容保留）将被忽略。

        参数:
            retrieval_result: retrieve() 返回的结果字典。

        返回:
            可注入 prompt 的文本，无记忆时返回空字符串。
        """
        memories: List[Dict[str, Any]] = retrieval_result.get(
            "long_term_memories", []
        )

        # 无长期记忆时返回空字符串
        if not memories:
            return ""

        # 排序策略：similarity 分桶（精度 BUCKET_PRECISION）+ 桶内 importance 降序。
        # 同桶内 similarity 视为同等相关，由 importance 决定顺序；
        # 高桶整体优先于低桶。截断时从末尾（低桶 + 低 importance）开始剔除。
        # Phase 7 Task 1: 桶内排序使用 decayed_importance（self.decay 非 None 时），
        # 否则回退到静态 importance（与模块级 _sort_key 一致）。
        memories_sorted = sorted(memories, key=self._sort_key, reverse=True)

        # 逐步剔除最低相关度的记忆，直到不超过 max_memory_tokens
        while memories_sorted:
            text = self._build_injection_text(memories_sorted)
            if self._count_tokens(text) <= self.max_memory_tokens:
                return text
            # 剔除末尾（相关度最低）的一条
            memories_sorted.pop()

        return ""

    def get_injection_text(
        self,
        user_input: str,
        namespace: str = "user",
        cron_id: Optional[str] = None,
        exclude_types: Optional[Set[str]] = None,
    ) -> str:
        """一站式检索并格式化注入文本。

        调用 retrieve → format_for_prompt，返回可直接注入 prompt 中部的文本。
        无相关记忆时返回空字符串。

        Phase 8 Task 1.3: 支持 namespace + cron_id 过滤召回。
        - 默认 ``namespace="user"``（保持用户会话行为不变，向后兼容）。
        - cron 调用方显式传 ``namespace="cron"`` + ``cron_id``，只召回该调度项
          自己命名空间下的记忆。

        ops-reliability-uplift Task 5: 支持 exclude_types 过滤。
        - 在 retrieve() 召回后、format_for_prompt 格式化前，过滤 metadata.type
          命中 exclude_types 的记录。
        - 主要用途：cron 会话过滤 ``type=conversation_turn`` 避免注入上次完整
          assistant_response（get_injection_text 返回已格式化字符串，metadata
          已丢失，必须在 retriever 层过滤而非 orchestrator 层事后过滤）。
        - ``exclude_types`` 为 None 或空集时不过滤（向后兼容）。

        参数:
            user_input: 用户输入文本。
            namespace: Phase 8 隔离层命名空间。``"user"``（默认）只检索用户
                命名空间条目；``"cron"`` 只检索 cron 命名空间且要求 ``cron_id``
                匹配；``None`` 不按命名空间过滤。
            cron_id: 当 ``namespace="cron"`` 时必填，用于匹配 ``metadata.cron_id``。
            exclude_types: 需过滤掉的 metadata.type 集合。为 None 或空集时不过滤。

        返回:
            可注入 prompt 的文本，无相关记忆时返回空字符串。
        """
        retrieval_result = self.retrieve(
            user_input, namespace=namespace, cron_id=cron_id
        )
        # ops-reliability-uplift Task 5.2: 在 format_for_prompt 前过滤 exclude_types
        if exclude_types:
            memories = retrieval_result.get("long_term_memories", [])
            filtered = [
                m for m in memories
                if str(m.get("metadata", {}).get("type", "")).lower()
                not in {t.lower() for t in exclude_types}
            ]
            retrieval_result["long_term_memories"] = filtered
        return self.format_for_prompt(retrieval_result)

    def get_injection_text_lightweight(
        self,
        user_input: str,
        namespace: str = "user",
        cron_id: Optional[str] = None,
    ) -> str:
        """轻量级记忆检索——跳过 _filter_by_relevance 与 reinforce 写入。

        用于**后续对话轮次**（已有历史上下文），首轮仍需完整
        :meth:`get_injection_text` 确保记忆过滤的准确性。

        差异：
        - 调用 ``chroma_store.query_memory(reinforce=False)``，不更新访问时间
        - 跳过 ``_filter_by_relevance``，省掉 6 次 ONNX 嵌入
        - 仍然执行 ChromaDB 向量检索，确保新话题也能召回相关记忆

        参数:
            同 :meth:`get_injection_text`。

        返回:
            可注入 prompt 的文本，无相关记忆时返回空字符串。
        """
        if self.chroma_store is None:
            return ""
        try:
            raw = self.chroma_store.query_memory(
                user_input,
                top_k=self.top_k,
                reinforce=False,
                namespace=namespace,
                cron_id=cron_id,
            )
            # 过滤 user_profile 类型（与 retrieve 保持一致）
            memories = [
                m for m in raw
                if str(m.get("metadata", {}).get("type", "")).lower() != "user_profile"
            ]
            retrieval_result = {
                "long_term_memories": memories,
                "user_profile_summary": "",
                "total_tokens": self._estimate_total_tokens(memories),
            }
            return self.format_for_prompt(retrieval_result)
        except Exception as e:
            logger.warning("轻量级记忆检索失败，降级为空注入: %s", e)
            return ""

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _build_rerank_prompt(
        self, memories: List[Dict[str, Any]], user_input: str
    ) -> str:
        """构建 LLM 重排 prompt。

        将记忆列表带索引呈现给 LLM，要求其按相关性返回索引数组。

        参数:
            memories: 记忆列表。
            user_input: 用户输入文本。

        返回:
            完整的重排 prompt 文本。
        """
        lines = [
            "你是一个记忆重排助手。请根据用户输入，对以下检索到的记忆按相关性从高到低重新排序。",
            "",
            f"用户输入：{user_input}",
            "",
            "记忆列表：",
        ]
        for idx, mem in enumerate(memories):
            content = str(mem.get("content", ""))
            lines.append(f"[{idx}] {content}")
        lines.extend(
            [
                "",
                "请仅返回一个 JSON 数组，包含按相关性从高到低排序的原始索引，例如：[2, 0, 1, 3]",
                "不要包含任何其他文字或解释。",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """从 anthropic Message 响应中提取纯文本。

        anthropic.types.Message.content 是 content block 列表，
        本方法拼接所有 text block 的 text 字段。

        参数:
            response: chat_main 返回的响应对象。

        返回:
            拼接后的文本，若无文本则返回空串。
        """
        try:
            content = response.content
        except AttributeError:
            return ""

        parts: List[str] = []
        for block in content or []:
            # text block 含 .text 属性，且 type == "text"
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _parse_rerank_indices(raw_text: str, count: int) -> List[int]:
        """解析 LLM 返回的重排索引数组。

        容错处理:
            - 去除 ```json 和 ``` 代码块标记
            - 解析 JSON 数组
            - 过滤越界索引
            - 补全缺失索引（未出现在结果中的索引按原顺序追加到末尾）

        参数:
            raw_text: LLM 返回的原始文本。
            count: 原始记忆数量，用于校验索引范围。

        返回:
            重排后的索引列表，解析失败时返回空列表。
        """
        if not raw_text:
            return []

        text = raw_text.strip()

        # 去除 markdown 代码块标记：形如 ```json\n...\n``` 或 ```\n...\n```
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(
                "解析 LLM 重排返回的 JSON 失败: %s，原始文本: %s",
                e,
                raw_text[:200],
            )
            return []

        if not isinstance(data, list):
            logger.error(
                "LLM 重排返回的不是 JSON 数组: %s", type(data).__name__
            )
            return []

        # 过滤有效索引（在范围内且为整数），去重保序
        order: List[int] = []
        seen = set()
        for item in data:
            try:
                idx = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < count and idx not in seen:
                order.append(idx)
                seen.add(idx)

        # 补全未出现的索引（按原顺序追加），保证结果覆盖全部记忆
        for i in range(count):
            if i not in seen:
                order.append(i)

        return order

    def _build_injection_text(
        self, memories: List[Dict[str, Any]]
    ) -> str:
        """构建注入文本。

        参数:
            memories: 长期记忆列表（已按相关度排序）。

        返回:
            格式化后的注入文本。
        """
        if not memories:
            return ""

        lines: List[str] = ["## 相关记忆"]
        for i, mem in enumerate(memories, start=1):
            content = str(mem.get("content", ""))
            similarity = mem.get("similarity", 0.0)
            try:
                sim_val = float(similarity)
            except (TypeError, ValueError):
                sim_val = 0.0
            lines.append(f"{i}. {content} (相关度: {sim_val:.2f})")

        return "\n".join(lines).rstrip()

    def _estimate_total_tokens(
        self, memories: List[Dict[str, Any]]
    ) -> int:
        """估算检索结果格式化后的总 token 数。

        参数:
            memories: 长期记忆列表。

        返回:
            估算的 token 数。
        """
        text = self._build_injection_text(memories)
        return self._count_tokens(text)

    def _count_tokens(self, text: str) -> int:
        """估算文本的 token 数。

        优先使用 llm_client.count_tokens（tiktoken cl100k_base），
        llm_client 不可用或调用失败时回退到字符数 / 3 的粗略估算
        （与 memory_md.py 的估算方式保持一致）。

        参数:
            text: 待估算的文本。

        返回:
            估算的 token 数。
        """
        if not text:
            return 0
        if self.llm_client is not None:
            try:
                return self.llm_client.count_tokens(text)
            except Exception as e:
                logger.debug(
                    "llm_client.count_tokens 失败，回退到字符数估算: %s", e
                )
        # 回退估算：字符数 / 3
        return len(text) // 3

    @staticmethod
    def _truncate_text(text: str, max_tokens: int) -> str:
        """按 token 上限截断文本。

        在不超过 max_tokens * 3 字符的前提下，尽量在行边界截断，
        避免截断出半行内容。

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
        # 尽量在行边界截断
        last_newline = truncated.rfind("\n")
        if last_newline > 0:
            truncated = truncated[:last_newline]
        return truncated + "\n"
