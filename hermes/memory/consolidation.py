"""记忆沉淀引擎。

按信息计数阈值触发，将缓冲的对话消息提交给 consolidation LLM 提取结构化事实，
写入 ChromaDB 长期记忆库（含去重检测），并对 user_profile 类事实异步写入 memory.md。

consolidation 是工作流的固定流程，不是独立 Agent。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

# LLMClient 与 ChromaMemoryStore 仅用于类型提示，运行时通过鸭子类型调用实例方法，
# 用 TYPE_CHECKING 守卫避免在 import 期强制加载 anthropic / numpy / chromadb 等重依赖。
if TYPE_CHECKING:
    from ..llm.client import LLMClient
    from ..memory.memory_md import MemoryMdManager
    from ..storage.chroma_store import ChromaMemoryStore

# CONSOLIDATION_PROMPT 运行时需要使用，必须真实导入；兼容相对导入与直接运行两种方式
from hermes.llm.prompts import CONSOLIDATION_PROMPT
from hermes.llm.reasoning_profiles import ReasoningConfig
logger = logging.getLogger(__name__)


class ConsolidationEngine:
    """记忆沉淀引擎，按信息计数阈值触发。

    工作流：累加对话消息 → 达到阈值 → 调用 LLM 提取事实 →
    去重写入向量库 → user_profile 事实异步写入 memory.md → 重置计数器。

    consolidation 是工作流的固定流程，不是独立 Agent。
    """

    def __init__(
        self,
        llm_client: "LLMClient",
        chroma_store: "ChromaMemoryStore",
        memory_md_writer: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        threshold: int = 15,
        dedup_threshold: float = 0.85,
        memory_md_manager: Optional["MemoryMdManager"] = None,
        surprise_gate_enabled: bool = True,
        surprise_similarity_threshold: float = 0.85,
        surprise_skip_threshold: float = 0.92,
        signal_pool: Optional[Any] = None,
    ) -> None:
        """初始化记忆沉淀引擎。

        参数:
            llm_client: LLM 客户端实例，用于调用 chat_consolidation。
            chroma_store: ChromaDB 记忆库实例，用于事实的增改查与去重。
            memory_md_writer: 异步写入 memory.md 的回调，接收 facts 列表。
                              仅 user_profile 类事实会传入。为 None 时不写入。
            threshold: 信息计数阈值，达到后触发沉淀，默认 15。
            dedup_threshold: 去重相似度阈值，默认 0.85。
            memory_md_manager: 可选的 ``MemoryMdManager`` 实例，用于在 consolidate
                时统一合并 ``pending_profile_updates`` 队列中 LLM 通过
                ``update_profile`` 工具请求的显式画像修改。为 ``None`` 时
                pending 队列仍可入队但不会在 consolidate 时应用（向后兼容）。
            surprise_gate_enabled: 惊讶门控开关，默认 True。开启后在 fact 写入
                循环中走双阈值方案（surprise_similarity_threshold /
                surprise_skip_threshold）过滤低价值事实；关闭时走原有去重逻辑
                （find_duplicates 命中则更新，否则新增）。可热更新即时生效。
            surprise_similarity_threshold: 惊讶门控命中阈值，默认 0.85。
                ``sim < surprise_similarity_threshold`` 视为「惊讶，新知识」→ 新增。
            surprise_skip_threshold: 惊讶门控跳过阈值，默认 0.92。
                ``sim ≥ surprise_skip_threshold`` 视为「不惊讶，已有等价记忆」→ 跳过；
                介于两阈值之间视为「惊讶，纠正/补充旧记忆」→ 更新。
            signal_pool: 可选的 ``SignalPool`` 实例。注入后 L3 提取的
                user_profile 事实走信号池累积（source="L3", weight=2），
                达阈值才写入画像；为 None 时回退到 ``memory_md_writer``
                直接异步写入（向后兼容）。
        """
        self.llm_client = llm_client
        self.chroma_store = chroma_store
        self.memory_md_writer = memory_md_writer
        self.threshold = threshold
        self.dedup_threshold = dedup_threshold
        self.memory_md_manager = memory_md_manager
        # 惊讶门控配置（Phase 7 Task 2）：写入侧过滤低价值事实
        self.surprise_gate_enabled = surprise_gate_enabled
        self.surprise_similarity_threshold = surprise_similarity_threshold
        self.surprise_skip_threshold = surprise_skip_threshold
        # 信号池（L3 改走信号池累积，达阈值才写入画像）
        self.signal_pool = signal_pool

        # 内部状态
        self.info_counter: int = 0
        self.pending_messages: List[Dict[str, Any]] = []
        # LLM 通过 update_profile 工具请求的画像修改队列；
        # 下次 consolidate() 时统一合并到 memory.md，避免每轮缓存失效。
        self.pending_profile_updates: List[Dict[str, Any]] = []
        # Phase 7 Task 3: LLM 通过 delete_memory / update_memory 工具请求的
        # 向量库操作队列。下次 consolidate() 时统一应用（delete 优先于 update，
        # 二者优先于 fact 写入），避免每轮对话都触发向量库写操作（写放大控制）。
        self.pending_memory_ops: List[Dict[str, Any]] = []

        # Phase 9: 并发安全锁
        # _consolidation_data_lock: 保护 info_counter + pending_messages 的原子操作
        # _consolidation_lock: 保护异步 consolidate 的并发，防止同时运行多个
        self._consolidation_data_lock = threading.Lock()
        self._consolidation_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------
    def add_info(self, message: Dict[str, Any]) -> None:
        """累加信息计数器并缓存消息。

        每条 user/assistant/tool 消息各算 1 条信息。
        使用 _consolidation_data_lock 保护，防止与后台 consolidate 的原子 swap 竞态。

        参数:
            message: 消息字典，格式 {role, content, ...}。
        """
        with self._consolidation_data_lock:
            self.info_counter += 1
            self.pending_messages.append(message)

    def should_consolidate(self) -> bool:
        """判断是否达到沉淀阈值。

        返回:
            True 表示 info_counter 已达到或超过 threshold。
        """
        return self.info_counter >= self.threshold

    def enqueue_profile_update(
        self, action: str, section: str, content: str
    ) -> None:
        """将画像更新操作加入 pending 队列，下次 consolidate 时合并写入。

        由 ``update_profile`` 工具 handler 调用。本方法仅入队，不立即写
        memory.md，避免每轮对话都让 system prompt 的缓存命中区失效。
        实际合并写入发生在下次 :meth:`consolidate` 时（统一应用全部
        pending 操作后清空队列）。

        参数:
            action: ``"add"`` | ``"replace"`` | ``"delete"`` 之一。
                - add: 在 section 末尾追加 content；section 不存在则新建。
                - replace: 替换 section 的全部 body；section 不存在则新建。
                - delete: 删除整个 section（含标题与 body）。
            section: memory.md 中的 section 标题（不含 ``## `` 前缀，
                如 ``"背景"`` / ``"偏好"``）。
            content: 新内容（add/replace 时必填，delete 时忽略，可传空串）。
        """
        with self._consolidation_data_lock:
            self.pending_profile_updates.append(
                {
                    "action": action,
                    "section": section,
                    "content": content,
                }
            )
        logger.info(
            "画像更新入队: action=%s section=%s content_len=%d",
            action,
            section,
            len(content) if content else 0,
        )

    def enqueue_memory_op(
        self, action: str, memory_id: str, content: str = ""
    ) -> None:
        """将向量库记忆操作加入 pending 队列，下次 consolidate 时统一应用。

        由 ``delete_memory`` / ``update_memory`` 工具 handler 调用。本方法
        仅入队，不立即执行向量库写操作，避免每轮对话都触发向量库写放大。
        实际应用发生在下次 :meth:`consolidate` 时（在 fact 处理循环之前，
        delete 优先于 update，二者优先于 fact 写入），统一应用全部 pending
        操作后清空队列。

        参数:
            action: ``"delete"`` | ``"update"`` 之一。
                - delete: 删除指定 memory_id 的记忆。
                - update: 更新指定 memory_id 的记忆内容为 content。
            memory_id: 待操作的记忆 ID。
            content: update 操作的新内容（delete 时忽略，可传空串）。
        """
        with self._consolidation_data_lock:
            self.pending_memory_ops.append(
                {
                    "action": action,
                    "memory_id": memory_id,
                    "content": content,
                }
            )
        logger.info(
            "记忆操作入队: action=%s memory_id=%s content_len=%d",
            action,
            memory_id,
            len(content) if content else 0,
        )

    # ------------------------------------------------------------------
    # 沉淀主流程
    # ------------------------------------------------------------------
    def consolidate(self, session_id: Optional[str] = None) -> Dict[str, int]:
        """执行记忆沉淀。

        流程:
            1. 将 pending_messages 格式化为对话文本
            2. 填充 CONSOLIDATION_PROMPT 并调用 LLM 提取事实
            3. 解析返回的 JSON（容错处理 markdown 代码块）
            4. 合并 pending_profile_updates 队列到 memory.md（来自
               update_profile 工具的延迟合并写入；独立于 LLM 提取的 facts，
               即使本次未提取到任何事实也会应用）
            5. 应用 pending_memory_ops 队列到向量库（来自 delete_memory /
               update_memory 工具的延迟合并写入；独立于 LLM 提取的 facts，
               即使本次未提取到任何事实也会应用）。**delete 优先于 update**，
               二者均优先于 fact 写入——避免刚 delete 的记忆又被 fact 重新
               写入。操作失败记录 ERROR 日志但不中断主流程。
            6. user_profile 类事实仅收集供 memory.md 写入（不入向量库，
               避免与 memory.md 双写导致脏数据；memory.md 全文已注入
               system prompt 缓存命中区，无需向量检索）
            7. 其他类事实走惊讶门控（surprise_gate_enabled=True 时）或原有
               去重逻辑（surprise_gate_enabled=False 时 / 降级场景）:
               - 惊讶门控双阈值方案:
                 sim < surprise_similarity_threshold → 新增（新知识）
                 surprise_similarity_threshold ≤ sim < surprise_skip_threshold
                   → 更新（纠正/补充旧记忆）
                 sim ≥ surprise_skip_threshold → 跳过（不惊讶，已有等价记忆）
               - 原有去重逻辑: find_duplicates 命中则更新，否则新增
            8. user_profile 类事实异步写入 memory.md
            9. 重置 info_counter 与 pending_messages

        Phase 8 Task 1.2: 按 ``session_id`` 前缀路由 namespace：
        - ``session_id`` 以 ``cron:`` 开头 → ``namespace="cron"``，
          ``cron_id=session_id[5:]``，惊讶门控范围限定在同 cron_id 内
          （不同调度项之间互不影响）。
        - 其他 / None → ``namespace="user"``（保持原有行为，向后兼容）。

        参数:
            session_id: 当前会话 ID。``cron:`` 前缀触发 cron 命名空间路由；
                其他值或 None 走 user 命名空间（向后兼容）。

        返回:
            统计字典:
            - facts_extracted: LLM 提取的事实总数
            - facts_added: 新增到向量库的事实数（不含 user_profile）
            - facts_updated: 更新的重复事实数（不含 user_profile）
            - duplicates: 命中重复的事实数（不含 user_profile）
            - profile_only: 仅写入 memory.md 的 user_profile 事实数
            - skipped_not_surprising: 惊讶门控判定为「不惊讶」而跳过写入的事实数
              （仅 surprise_gate_enabled=True 时可能 > 0）
            - memory_ops_applied: 本次 consolidate 应用的 pending_memory_ops
              操作数（delete + update 总和，Phase 7 Task 3）
            - namespace: 本次沉淀使用的命名空间（``"user"`` / ``"cron"``）
            - cron_id: cron 命名空间下的调度项 ID（user 命名空间为 None）
        """
        # Phase 9: 原子性地获取 pending_messages/profile_updates/memory_ops，
        # 确保后台异步 consolidate 不会与主线程 add_info/enqueue 竞态。
        with self._consolidation_data_lock:
            pending = self.pending_messages
            self.pending_messages = []
            self.info_counter = 0
            profile_updates = self.pending_profile_updates
            self.pending_profile_updates = []
            memory_ops = self.pending_memory_ops
            self.pending_memory_ops = []

        if not pending:
            logger.debug("无可沉淀的对话消息，跳过沉淀")
            # 使用第一次原子 swap 已取出的 profile_updates / memory_ops
            # （行 263-266 已从实例变量取出并清空了 self.pending_*，
            #  此处直接使用局部变量，不做第二次冗余 swap）
            namespace, cron_id = self._resolve_namespace(session_id)
            stats = self._make_stats(namespace=namespace, cron_id=cron_id)
            self._apply_pending_ops(profile_updates, memory_ops, namespace, cron_id, stats)
            # 信号池过期清理：无 pending 时也需触发，避免长期无对话
            # 累积的信号池无法被清理（触发后立即 apply 后应清理过期项）
            if self.signal_pool is not None:
                try:
                    self.signal_pool.cleanup()
                except Exception as e:
                    logger.warning("信号池过期清理失败: %s", e)
            return stats

        # Phase 8 Task 1.2: 按 session_id 前缀路由 namespace
        namespace, cron_id = self._resolve_namespace(session_id)

        stats = self._make_stats(namespace=namespace, cron_id=cron_id)

        # 1. 格式化对话文本
        conversation_text = self._format_conversation(pending)

        # 2. 填充 prompt 并调用 LLM
        # 注意：不能用 str.format()，因为 CONSOLIDATION_PROMPT 中包含 JSON 示例
        # （如 {"facts": [...], "content": ...}），str.format() 会把 JSON 中的
        # {...} 当作占位符解析，抛出 KeyError。改用 replace() 安全替换。
        system_prompt = CONSOLIDATION_PROMPT.replace(
            "{conversation}", conversation_text
        )
        messages = [
            {"role": "user", "content": "请根据上述对话历史提取值得长期记住的事实。"}
        ]

        try:
            response = self.llm_client.chat_consolidation_sync(
                messages, system=system_prompt,
                reasoning_cfg=ReasoningConfig(enabled=False),
            )
        except Exception as e:
            # LLM 调用失败（含 RuntimeError / ConnectionError / TimeoutError 等）：
            # 消息/profile_updates/memory_ops 已在原子 swap 中取出，
            # 需全部重新入队，便于后续重试。
            logger.error("调用 consolidation LLM 失败: %s", e)
            with self._consolidation_data_lock:
                self.pending_messages = pending + self.pending_messages
                self.info_counter += len(pending)
                self.pending_profile_updates = profile_updates + self.pending_profile_updates
                self.pending_memory_ops = memory_ops + self.pending_memory_ops
            return stats

        # 3. 提取并解析返回的 JSON
        raw_text = self._extract_response_text(response)
        facts = self._parse_facts_json(raw_text)
        stats["facts_extracted"] = len(facts)

        # 4. 应用 swapped-out 的 pending_profile_updates 和 pending_memory_ops
        #    （已在开始时通过原子 swap 取出，当前 self.pending_* 是空列表，
        #     主线程的 enqueue 操作安全地进入新列表）。
        #    即使本次 LLM 未提取到任何事实，profile_updates / memory_ops
        #     也应被应用，它们独立于对话消息。
        self._apply_pending_ops(profile_updates, memory_ops, namespace, cron_id, stats)

        # 4.5 信号池过期清理：在 if not facts 早返回前执行，确保 cleanup
        #     在所有 consolidate 调用路径上都被触发（无 LLM 输出也需清理）。
        #     cleanup 内部用 _lock 保护，O(n) 单次扫描开销低。
        #     阈值：pending 30 天未活动 / triggered 7 天后。
        if self.signal_pool is not None:
            try:
                self.signal_pool.cleanup()
            except Exception as e:
                logger.warning("信号池过期清理失败: %s", e)

        if not facts:
            logger.info("本次沉淀未提取到任何事实")
            # 计数器和消息列表已在开始时通过原子 swap 重置
            return stats

        # 5. 逐条处理：user_profile 仅收集给 memory.md，其他类型去重写入向量库
        profile_facts: List[Dict[str, Any]] = []
        for fact in facts:
            content = str(fact.get("content", "")).strip()
            if not content:
                continue

            fact_type = str(fact.get("type", "fact"))
            importance = fact.get("importance", 0.5)
            # 兼容 LLM 返回字符串形式的重要性评分
            try:
                importance = float(importance)
            except (TypeError, ValueError):
                importance = 0.5

            # user_profile 类事实：仅收集给信号池，跳过向量库写入
            # 原因：memory.md 全文已注入 system prompt 缓存命中区，
            # 向量库再存一份会导致双写不一致（手改 md 后向量变脏数据）
            if fact_type == "user_profile":
                profile_facts.append(fact)
                stats["profile_only"] += 1
                logger.debug("user_profile 事实仅写入信号池: %s", content[:50])
                continue
            elif fact_type == "preference":
                # preference 双写：信号池累积 + 向量库检索
                # 信号池用于达阈值后写入画像，向量库用于 memory_search 实时检索
                profile_facts.append(fact)
                logger.debug("preference 事实入信号池+向量库: %s", content[:50])
                # 不 continue，继续走下方 ChromaDB 写入分支

            metadata = {
                "type": fact_type,
                "importance": importance,
                "timestamp": datetime.now().isoformat(),
            }

            # 惊讶门控（Phase 7 Task 2）：写入侧过滤低价值事实
            # surprise_gate_enabled=True 时走双阈值方案；False 或降级时走原有去重逻辑
            # Phase 8 Task 1.2: find_duplicates 限定在 namespace + cron_id 范围内，
            # 使 cron 调度项的惊讶门控只与同 cron_id 的已有记忆对比，不污染用户记忆。
            use_surprise_gate = self.surprise_gate_enabled
            duplicates: List[Dict[str, Any]] = []

            if use_surprise_gate:
                # 双阈值方案：先用 surprise_similarity_threshold 召回相似记忆
                try:
                    duplicates = self.chroma_store.find_duplicates(
                        content,
                        threshold=self.surprise_similarity_threshold,
                        namespace=namespace,
                        cron_id=cron_id,
                    )
                except Exception as e:
                    # find_duplicates 异常时降级到原有去重逻辑，不中断主流程
                    logger.warning(
                        "惊讶门控查找重复失败，降级到原有去重逻辑: %s", e
                    )
                    use_surprise_gate = False

            if not use_surprise_gate:
                # 原有去重逻辑（含降级场景）：find_duplicates 命中则更新，否则新增
                try:
                    duplicates = self.chroma_store.find_duplicates(
                        content,
                        threshold=self.dedup_threshold,
                        namespace=namespace,
                        cron_id=cron_id,
                    )
                except Exception as e:
                    # 降级场景下 find_duplicates 仍异常时按"无重复"处理，
                    # 尝试新增，保证主流程不中断
                    logger.warning(
                        "去重检测 find_duplicates 异常，按无重复处理: %s", e
                    )
                    duplicates = []
                if duplicates:
                    # 重复则更新（取最新），用相似度最高的那条
                    target_id = duplicates[0]["id"]
                    try:
                        self.chroma_store.update_memory(
                            target_id, content, metadata
                        )
                        stats["facts_updated"] += 1
                        stats["duplicates"] += 1
                        logger.debug(
                            "更新重复记忆 %s: %s", target_id, content[:50]
                        )
                    except Exception as e:
                        logger.error("更新记忆 %s 失败: %s", target_id, e)
                else:
                    # 不重复则新增
                    try:
                        self.chroma_store.add_memory(
                            content,
                            metadata=metadata,
                            namespace=namespace,
                            cron_id=cron_id,
                        )
                        stats["facts_added"] += 1
                        logger.debug("新增记忆: %s", content[:50])
                    except Exception as e:
                        logger.error("新增记忆失败: %s", e)
            else:
                # 惊讶门控：双阈值方案
                # find_duplicates 已按相似度降序排列，duplicates[0] 即最高相似度
                if duplicates:
                    max_sim = float(duplicates[0]["similarity"])
                    target_id = duplicates[0]["id"]
                    if max_sim >= self.surprise_skip_threshold:
                        # 跳过：不惊讶，已有等价记忆
                        stats["skipped_not_surprising"] += 1
                        logger.debug(
                            "惊讶门控：跳过不惊讶的事实（sim=%.2f）", max_sim
                        )
                    else:
                        # 更新：惊讶，纠正/补充旧记忆
                        # （max_sim ∈ [surprise_similarity_threshold, surprise_skip_threshold)）
                        try:
                            self.chroma_store.update_memory(
                                target_id, content, metadata
                            )
                            stats["facts_updated"] += 1
                            logger.debug(
                                "惊讶门控：更新冲突记忆"
                                "（sim=%.2f, target_id=%s）",
                                max_sim,
                                target_id,
                            )
                        except Exception as e:
                            logger.error(
                                "更新记忆 %s 失败: %s", target_id, e
                            )
                else:
                    # 新增：惊讶，新知识（无相似记忆命中）
                    try:
                        self.chroma_store.add_memory(
                            content,
                            metadata=metadata,
                            namespace=namespace,
                            cron_id=cron_id,
                        )
                        stats["facts_added"] += 1
                        logger.debug(
                            "惊讶门控：新增新知识（max_sim=%.2f）", 0.0
                        )
                    except Exception as e:
                        logger.error("新增记忆失败: %s", e)

        # 6. user_profile 事实分流：信号池累积 或 异步写入 memory.md
        # cron 会话跳过画像更新：cron 与普通用户会话隔离，避免自动任务
        # 提取的"事实"污染用户画像。cron namespace 的事实仍正常写入向量库
        # （上面 step 5 已处理），仅跳过 user_profile 类事实的画像写入。
        is_cron_session = (
            session_id is not None
            and isinstance(session_id, str)
            and session_id.startswith("cron:")
        )
        if profile_facts and not is_cron_session:
            if self.signal_pool is not None:
                # L3 改走信号池：weight=2（一次隐含 15 条对话消息），
                # section="沉淀笔记"（L3 自动提取的累积区）
                for fact in profile_facts:
                    content = str(fact.get("content", "")).strip()
                    if not content:
                        continue
                    self.signal_pool.add(
                        content=content,
                        source="L3",
                        weight=2,
                        section="沉淀笔记",
                    )
                logger.debug("L3 提取 %d 条 user_profile/preference 事实已入信号池", len(profile_facts))
            elif self.memory_md_writer is not None:
                # 向后兼容：signal_pool 未注入时走原异步写入路径
                self._async_write_memory_md(profile_facts)
        elif profile_facts and is_cron_session:
            logger.info(
                "cron 会话 (session_id=%s) 跳过 user_profile 事实画像写入 (%d 条)",
                session_id, len(profile_facts)
            )

        # 7. 重置计数器与消息缓冲
        # Phase 9: 计数器和消息列表已在开始时通过原子 swap 重置，
        # 此处不再调用 self._reset()，避免清除在 consolidate 执行期间
        # 由 add_info() 添加的新消息。pending_profile_updates 和
        # pending_memory_ops 在各自的处理方法中已清空。

        logger.info(
            "记忆沉淀完成: 提取 %d 条，新增 %d 条，更新 %d 条，重复 %d 条，"
            "仅写入 memory.md %d 条，惊讶门控跳过 %d 条，记忆操作应用 %d 条"
            "（namespace=%s, cron_id=%s）",
            stats["facts_extracted"],
            stats["facts_added"],
            stats["facts_updated"],
            stats["duplicates"],
            stats["profile_only"],
            stats["skipped_not_surprising"],
            stats["memory_ops_applied"],
            namespace,
            cron_id,
        )
        return stats

    def force_consolidate(self, session_id: Optional[str] = None) -> Dict[str, int]:
        """强制触发记忆沉淀（后台异步执行，不阻塞调用方）。

        使用 _consolidation_lock.acquire(blocking=False) 确保同一时刻只有
        一个 consolidate 在执行。如果已有 consolidate 在运行，静默跳过。

        参数:
            session_id: 当前会话 ID。``cron:`` 前缀触发 cron 命名空间路由；
                其他值或 None 走 user 命名空间（向后兼容）。

        返回:
            空字典（调用方不应依赖返回值）。实际统计由后台线程的日志记录。
        """
        if not self._consolidation_lock.acquire(blocking=False):
            logger.debug("consolidation 已在执行中，跳过本次触发")
            return {}

        logger.info(
            "触发异步 consolidation（session_id=%s）", session_id
        )
        thread = threading.Thread(
            target=self._run_async_consolidation,
            args=(session_id,),
            daemon=True,
        )
        thread.start()
        return {}

    def _run_async_consolidation(self, session_id: Optional[str] = None) -> None:
        """后台线程执行 consolidate，确保异常被捕获且锁被释放。"""
        try:
            self.consolidate(session_id=session_id)
        except Exception as e:
            logger.error("异步 consolidate 失败: %s", e, exc_info=True)
        finally:
            self._consolidation_lock.release()

    def close(self) -> None:
        """关闭引擎，等待异步操作完成后执行最终同步 consolidate。

        确保进程退出前所有 pending 数据被持久化。
        加 30 秒超时防止死锁。
        """
        # 等待异步 consolidate 完成
        acquired = self._consolidation_lock.acquire(timeout=30)
        if acquired:
            try:
                # 执行最终同步 consolidate（处理在等待期间积累的消息）
                self.consolidate(session_id=None)
            except Exception as e:
                logger.error("最终 consolidate 失败: %s", e)
            finally:
                self._consolidation_lock.release()
        else:
            logger.warning("等待异步 consolidate 超时（30s），跳过最终 consolidate")

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _apply_pending_ops(
        self,
        profile_updates: List[Dict[str, Any]],
        memory_ops: List[Dict[str, Any]],
        namespace: str,
        cron_id: Optional[str],
        stats: Dict[str, int],
    ) -> None:
        """应用 pending_profile_updates 和 pending_memory_ops（原子 swap 后的副本）。

        由 consolidate() 调用，使用已在 consolidate 开始时通过 _consolidation_data_lock
        交换出的本地副本，避免与主线程的 enqueue 操作竞态。
        """
        # 应用 pending 画像更新
        if profile_updates:
            try:
                if self.memory_md_manager is not None:
                    self.memory_md_manager.apply_profile_updates(profile_updates)
                    logger.info(
                        "合并 %d 条 pending 画像更新到 memory.md",
                        len(profile_updates),
                    )
                    # 信号池状态回写：apply 成功后，将已写入的 triggered 信号
                    # 标记为 written。通过 content 匹配（pending 队列中混合了
                    # L1 add 信号、replace/delete 显式修改，仅 add 操作有对应信号）
                    if self.signal_pool is not None:
                        add_contents = [
                            str(u.get("content", ""))
                            for u in profile_updates
                            if isinstance(u, dict)
                            and u.get("action") == "add"
                            and u.get("content")
                        ]
                        if add_contents:
                            self.signal_pool.mark_written_by_contents(add_contents)
                else:
                    logger.warning(
                        "pending_profile_updates 非空但未注入 memory_md_manager，"
                        "丢弃 %d 条更新",
                        len(profile_updates),
                    )
            except Exception as e:
                logger.error("合并 pending 画像更新失败: %s", e)

        # 应用 pending memory 操作（delete 优先于 update）
        if memory_ops:
            applied = 0
            delete_ops = [op for op in memory_ops if op.get("action") == "delete"]
            for op in delete_ops:
                memory_id = op.get("memory_id", "")
                if not memory_id:
                    continue
                try:
                    self.chroma_store.delete_memory(memory_id)
                    applied += 1
                except Exception as e:
                    logger.error("pending delete 操作失败: %s", e)
            update_ops = [op for op in memory_ops if op.get("action") == "update"]
            for op in update_ops:
                memory_id = op.get("memory_id", "")
                content = op.get("content", "")
                if not memory_id:
                    continue
                try:
                    self.chroma_store.update_memory(memory_id, content)
                    applied += 1
                except Exception as e:
                    logger.error("pending update 操作失败: %s", e)
            stats["memory_ops_applied"] = applied

    @staticmethod
    def _resolve_namespace(
        session_id: Optional[str],
    ) -> tuple:
        """按 session_id 前缀解析 namespace + cron_id（Phase 8 Task 1.2）。

        - ``session_id`` 以 ``"cron:"`` 开头 → ``("cron", session_id[5:])``
        - 其他 / None → ``("user", None)``

        参数:
            session_id: 会话 ID。

        返回:
            ``(namespace, cron_id)`` 二元组。
        """
        if session_id and isinstance(session_id, str) and session_id.startswith("cron:"):
            return ("cron", session_id[5:])
        return ("user", None)

    @staticmethod
    def _make_stats(namespace: str = "user", cron_id: Optional[str] = None) -> Dict[str, int]:
        """创建空的 stats 字典。"""
        return {
            "facts_extracted": 0,
            "facts_added": 0,
            "facts_updated": 0,
            "duplicates": 0,
            "profile_only": 0,
            "skipped_not_surprising": 0,
            "memory_ops_applied": 0,
            "namespace": namespace,
            "cron_id": cron_id,
        }


    def _apply_pending_memory_ops(self) -> int:
        """应用 pending_memory_ops 队列到向量库（Phase 7 Task 3）。

        处理顺序：**delete 优先于 update**，避免刚 delete 的记忆又被
        update 操作恢复。所有 delete 操作先批量执行，再执行所有 update
        操作。操作失败记录 ERROR 日志但不中断主流程（与
        pending_profile_updates 的异常处理策略一致），最终清空队列。

        返回:
            成功应用的操作数（delete + update 总和；失败的操作不计入）。
        """
        ops = self.pending_memory_ops
        if not ops:
            return 0

        applied = 0
        # 第一遍：处理 delete 操作（优先）
        delete_ops = [op for op in ops if op.get("action") == "delete"]
        for op in delete_ops:
            memory_id = op.get("memory_id", "")
            if not memory_id:
                logger.warning("跳过非法 delete 操作：memory_id 为空")
                continue
            try:
                self.chroma_store.delete_memory(memory_id)
                applied += 1
                logger.info(
                    "应用 pending delete 操作: memory_id=%s", memory_id
                )
            except Exception as e:
                logger.error(
                    "应用 pending delete 操作失败 (memory_id=%s): %s",
                    memory_id,
                    e,
                )

        # 第二遍：处理 update 操作
        update_ops = [op for op in ops if op.get("action") == "update"]
        for op in update_ops:
            memory_id = op.get("memory_id", "")
            content = op.get("content", "")
            if not memory_id:
                logger.warning("跳过非法 update 操作：memory_id 为空")
                continue
            if not content:
                logger.warning(
                    "跳过非法 update 操作：content 为空 (memory_id=%s)",
                    memory_id,
                )
                continue
            try:
                self.chroma_store.update_memory(memory_id, content)
                applied += 1
                logger.info(
                    "应用 pending update 操作: memory_id=%s content_len=%d",
                    memory_id,
                    len(content),
                )
            except Exception as e:
                logger.error(
                    "应用 pending update 操作失败 (memory_id=%s): %s",
                    memory_id,
                    e,
                )

        # 清空队列（无论成功失败，避免重复应用）
        self.pending_memory_ops.clear()
        return applied

    def _format_conversation(self, messages: List[Dict[str, Any]]) -> str:
        """将消息列表格式化为可读的对话文本。

        格式::

            [user]: 用户输入内容
            [assistant]: 助手回复内容
            [tool: tool_name]: 工具输出内容

        参数:
            messages: 消息列表，每条包含 role 和 content 字段。

        返回:
            格式化后的对话文本。
        """
        lines: List[str] = []
        for message in messages:
            role = message.get("role", "unknown")
            content = self._stringify_content(message.get("content", ""))

            if role == "user":
                lines.append(f"[user]: {content}")
            elif role == "assistant":
                lines.append(f"[assistant]: {content}")
            elif role == "tool":
                tool_name = message.get("name", "unknown")
                lines.append(f"[tool: {tool_name}]: {content}")
            else:
                lines.append(f"[{role}]: {content}")

        return "\n".join(lines)

    @staticmethod
    def _stringify_content(content: Any) -> str:
        """将消息 content 字段统一转换为字符串。

        content 可能是字符串，也可能是 content block 列表
        （如 [{"type": "text", "text": "..."}, {"type": "tool_use", ...}]）。

        参数:
            content: 原始 content。

        返回:
            拼接后的纯文本。
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if not isinstance(block, dict):
                    parts.append(str(block))
                    continue
                block_type = block.get("type", "")
                if block_type == "text":
                    parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    name = block.get("name", "")
                    parts.append(f"[调用工具 {name}]")
                elif block_type == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, list):
                        sub_parts = [
                            b.get("text", "")
                            for b in result
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        parts.append("\n".join(sub_parts))
                    else:
                        parts.append(str(result))
                else:
                    parts.append(str(block))
            return "\n".join(p for p in parts if p)
        return str(content)

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """从 LLM 响应中提取纯文本。

        兼容两种 content block 表示：
        - anthropic SDK 的对象类型（TextBlock，含 .type / .text 属性）
        - dict 类型（如 {"type": "text", "text": "..."}，常见于 mock / OpenAI 兼容层）

        参数:
            response: chat_consolidation 返回的响应对象，需有 .content 属性。

        返回:
            拼接后的文本，若无文本则返回空串。
        """
        try:
            content = response.content
        except AttributeError:
            return ""

        parts: List[str] = []
        for block in content or []:
            # 同时支持 dict 与对象类型的 block
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(text)
            else:
                # 对象类型（anthropic SDK TextBlock）
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "")
                    if text:
                        parts.append(text)
        return "".join(parts)

    @staticmethod
    def _parse_facts_json(raw_text: str) -> List[Dict[str, Any]]:
        """解析 LLM 返回的事实 JSON。

        容错处理:
            - 去除 ```json 和 ``` 代码块标记
            - 去除首尾空白
            - 解析失败时记录错误并返回空列表，不抛异常

        参数:
            raw_text: LLM 返回的原始文本。

        返回:
            事实列表，每个元素形如 {"content", "type", "importance"}。
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
                "解析 consolidation 返回的 JSON 失败: %s，原始文本: %s",
                e,
                raw_text[:200],
            )
            return []

        # 兼容两种返回格式：
        #   1. {"facts": [...]}  — 旧格式（prompt 期望格式）
        #   2. [...]             — LLM 直接返回 JSON 数组（实际更常见）
        if isinstance(data, list):
            facts = data
        elif isinstance(data, dict):
            facts = data.get("facts", [])
            if not isinstance(facts, list):
                logger.error(
                    "consolidation 返回的 facts 字段不是列表: %s",
                    type(facts).__name__,
                )
                return []
        else:
            logger.error(
                "consolidation 返回的 JSON 根节点不是对象或数组: %s",
                type(data).__name__,
            )
            return []

        # 过滤掉非字典或缺少 content 的条目
        valid_facts = [f for f in facts if isinstance(f, dict) and f.get("content")]
        return valid_facts

    def _async_write_memory_md(self, facts: List[Dict[str, Any]]) -> None:
        """异步调用 memory_md_writer 写入 memory.md。

        使用守护线程执行，避免阻塞沉淀主流程。
        若回调抛出异常，仅记录日志不影响主流程。

        参数:
            facts: user_profile 类事实列表。
        """
        writer = self.memory_md_writer

        def _run() -> None:
            try:
                writer(facts)  # type: ignore[misc]
            except Exception as e:
                logger.error("异步写入 memory.md 失败: %s", e)

        thread = threading.Thread(target=_run, daemon=True, name="memory-md-writer")
        thread.start()
