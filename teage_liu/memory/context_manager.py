"""Prompt 上下文管理器，前缀缓存优化模块。

通过结构化分层构建 Prompt，将稳定内容前置（缓存命中区）、易变内容后置
（缓存失效区），最大化 Anthropic API 的前缀缓存命中率。

分层结构（从稳定到易变）：
1. 系统提示词（最稳定）：SYSTEM_PROMPT
2. 用户画像（较稳定，异步更新）：memory.md 全文
3. 工具 schema（字节级稳定）：tool_registry.get_tools_schema()
4. 检索记忆（每轮可能变）：memory_retriever.get_injection_text(user_input)
5. 对话历史（每轮增长）：history_buffer.get_history(session_id)
6. 当前用户输入（最易变）：user_input

Prompt 结构示意::

    [缓存命中区 - 稳定内容]
    ┌─────────────────────────┐
    │ system:                 │
    │   SYSTEM_PROMPT         │
    │   ---                   │
    │   用户画像 (memory.md)   │
    │ tools:                  │
    │   [完整工具 schema]      │
    └─────────────────────────┘

    [缓存失效区 - 易变内容]
    ┌─────────────────────────┐
    │ messages[0]:            │
    │   role: user            │
    │   content: 检索记忆注入  │ ← 缓存失效点
    │ messages[1..N]:         │
    │   history 对话历史      │
    │ messages[N+1]:          │
    │   role: user            │
    │   content: 当前用户输入  │
    └─────────────────────────┘

Plan 模式采用软约束：通过 tool result 注入 system-reminder，
不修改 system prompt 也不修改 tools 列表，保证字节级稳定。
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

# ToolRegistry / MemoryMdManager / MemoryRetriever / HistoryBuffer 仅用于类型提示，
# 运行时通过鸭子类型调用实例方法，用 TYPE_CHECKING 守卫避免在 import 期强制
# 加载 chromadb / numpy / anthropic 等重依赖。
if TYPE_CHECKING:
    from ..agent.tool_registry import ToolRegistry
    from ..files.context_injector import FileContextInjector
    from ..memory.cron_isolation import CronIsolation
    from ..memory.decay import MemoryDecay
    from ..memory.memory_md import MemoryMdManager
    from ..memory.retrieval import MemoryRetriever
    from ..storage.history_buffer import HistoryBuffer
    from ..memory.condenser import Condenser

# SYSTEM_PROMPT 运行时需要使用，必须真实导入；兼容相对导入与直接运行两种方式
from teage_liu.llm.prompts import SYSTEM_PROMPT
logger = logging.getLogger(__name__)


# system 提示词与用户画像之间的分隔符
# 使用稳定的分隔符保证字节级稳定，便于前缀缓存命中
_PROFILE_SEPARATOR = "\n\n---\n\n"

# 历史教训注入的默认 JSONL 路径（Task 9 的 failure_library.py 写入）
_DEFAULT_FAILURE_CASES_PATH = "data/failure_cases.jsonl"

# 历史教训段 token 硬上限（spec 约定 ≤300 token）
_MAX_LESSONS_TOKENS = 300

# Agent 自画像段 token 硬上限（spec.md line 81 约定 ≤200 token）
_MAX_AGENT_PROFILE_TOKENS = 200

# 沟通偏好段字符硬上限（spec.md line 264 约定 ≤1000 字符）
_MAX_COMMUNICATION_CHARS = 1000

# messages[0] 全部注入段的字符软上限（避免上下文膨胀）
# 按 1 token ≈ 3.5 字符（中英混合）估算，8000 字符 ≈ 2300 token
_MAX_INJECTION_CHARS = 8000

# 惊讶门控阈值：失败案例与已有记忆相似度 > 此值时跳过注入（避免冗余）
_SURPRISE_GATE_THRESHOLD = 0.92

# 失败案例置信度下限：低于此值的案例不注入
_MIN_CONFIDENCE = 0.6


def _estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数（1 token ≈ 3.5 字符，中英混合近似值）。

    用于 token 预算检查，不需要精确值。精确 tokenization 由 LLM API 侧完成。
    """
    return max(1, len(text) // 3 + 1) if text else 0


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """计算两个向量的余弦相似度（假设已 L2 归一化则等价于点积）。

    避免在 context_manager 顶层导入 numpy，使用纯 Python 实现。
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _compute_embedding(text: str) -> List[float]:
    """计算文本的 embedding 向量（懒加载 chromadb ONNX 模型）。

    复用 ChromaMemoryStore 的 ONNXMiniLM_L6_V2 模型，保证 embedding 空间一致。
    模型加载失败时返回空列表（调用方负责降级处理）。
    """
    try:
        from ..storage.chroma_store import _get_onnx_embedder, _normalize
        model = _get_onnx_embedder()
        raw = model([text])[0]
        return _normalize(raw).tolist()
    except Exception as e:
        logger.warning("embedding 计算失败，跳过相似度检索: %s", e)
        return []


# Plan 模式软约束 system-reminder 文本
_PLAN_MODE_REMINDER = """<system-reminder>
你现在处于 Plan 模式。在此模式下，你必须 ONLY 规划和分析任务，MUST NOT 执行任何修改操作（如 edit、write、execute_command）。只能读取和规划。
</system-reminder>"""

# 退出 Plan 模式的解除提示
_PLAN_MODE_EXIT_REMINDER = """<system-reminder>
已退出 Plan 模式。你现在可以正常执行所有操作。
</system-reminder>"""


class ContextManager:
    """Prompt 上下文管理器，构建最大化前缀缓存命中率的请求结构。

    核心原则：稳定内容前置（缓存命中区），易变内容后置（缓存失效区）。

    缓存命中区（system + tools）：
        - system: SYSTEM_PROMPT + 用户画像（memory.md 全文）
        - tools: 完整工具 schema 列表（始终不变，不因 Plan 模式改变）

    缓存失效区（messages）：
        - messages[0]: 检索记忆注入（缓存失效区起点）
        - messages[1..N]: 对话历史
        - messages[N+1]: 当前用户输入

    Plan 模式采用软约束：通过 tool result 注入 system-reminder，
    不修改 system prompt 也不修改 tools 列表，保证字节级稳定。

    Attributes:
        system_prompt: 系统提示词，默认为 SYSTEM_PROMPT。
        tool_registry: 工具注册中心实例，用于获取工具 schema。
        memory_md_manager: 用户画像 memory.md 管理器，用于读取用户画像全文。
        memory_retriever: 记忆检索器，用于检索并格式化长期记忆注入文本。
        history_buffer: 短期对话历史缓冲区，用于获取会话历史。
    """

    def __init__(
        self,
        system_prompt: str = SYSTEM_PROMPT,
        tool_registry: Optional["ToolRegistry"] = None,
        memory_md_manager: Optional["MemoryMdManager"] = None,
        memory_retriever: Optional["MemoryRetriever"] = None,
        history_buffer: Optional["HistoryBuffer"] = None,
        condenser: Optional["Condenser"] = None,
        decay: Optional["MemoryDecay"] = None,
        file_context_injector: Optional["FileContextInjector"] = None,
        failure_cases_path: str = _DEFAULT_FAILURE_CASES_PATH,
    ) -> None:
        """初始化上下文管理器。

        参数:
            system_prompt: 系统提示词，默认为 SYSTEM_PROMPT。
            tool_registry: 工具注册中心实例，用于获取工具 schema。
            memory_md_manager: 用户画像 memory.md 管理器，用于读取用户画像全文。
            memory_retriever: 记忆检索器，用于检索并格式化长期记忆注入文本。
            history_buffer: 短期对话历史缓冲区，用于获取会话历史。
            condenser: 可选的历史压缩器，在构建 messages 前对 history 做压缩
                （masking 旧 tool_result / LLM 摘要）。为 None 时 history 原样
                拼入（向后兼容）。压缩仅影响送入 LLM 的 messages，不影响
                history_buffer 存储。
            decay: 可选的 MemoryDecay 实例，用于三因子衰减排序。当前实现
                仅做属性存储（MemoryRetriever 在构造时已注入 decay），
                保留参数便于后续扩展与 orchestrator 装配一致性。
            failure_cases_path: 失败案例库 JSONL 路径，默认 data/failure_cases.jsonl。
                文件不存在时教训反哺注入返回空字符串（空文件兜底）。
        """
        self.system_prompt = system_prompt
        self.tool_registry = tool_registry
        self.memory_md_manager = memory_md_manager
        self.memory_retriever = memory_retriever
        self.history_buffer = history_buffer
        self.condenser = condenser
        # Phase 7 Task 1: 三因子衰减（保留属性，便于 orchestrator 装配与热更新）
        self.decay = decay
        # 文件摘要注入器（可选，用于文件 ETL 管道）
        self.file_context_injector = file_context_injector
        # 失败案例库路径（Task 4 教训反哺，Task 9 的 failure_library.py 写入）
        self.failure_cases_path = failure_cases_path
        # 透传给 memory_retriever：若已注入 retriever 且未显式设置 decay，则透传
        # （orchestrator 通常会同时传 decay 给 retriever 构造函数，此处为兜底）
        if self.memory_retriever is not None and decay is not None:
            existing_decay = getattr(self.memory_retriever, "decay", None)
            if existing_decay is None:
                self.memory_retriever.decay = decay

    def build_prompt(
        self,
        session_id: str,
        user_input: str,
        tools_override: Optional[list] = None,
    ) -> dict:
        """构建 Anthropic API 格式的请求参数，最大化前缀缓存命中率。

        分层构建逻辑（从稳定到易变）：
        1. system: SYSTEM_PROMPT + 用户画像（缓存命中区）
        2. tools: 完整工具 schema 列表（缓存命中区，字节级稳定）
        3. messages[0]: 检索记忆注入（缓存失效区起点）
        4. messages[1..N]: 对话历史
        5. messages[N+1]: 当前用户输入

        参数:
            session_id: 会话 ID，用于获取对话历史。
            user_input: 当前用户输入文本。
            tools_override: 工具 schema 覆盖列表。为 None 时使用 tool_registry
                返回的完整列表。注意：覆盖会破坏前缀缓存的字节级稳定性，
                仅在特殊场景（如测试）使用。

        返回:
            Anthropic API 请求参数字典::

                {
                    "system": "系统提示词 + 用户画像",
                    "messages": [...],   # 检索记忆 + history + 用户输入
                    "tools": [...]       # 工具 schema
                }
        """
        # 1. 构建 system（最稳定）：SYSTEM_PROMPT + 用户画像
        system_text = self._build_system_text()

        # 2. 构建 tools（字节级稳定）：完整工具 schema 列表
        tools = self._build_tools(tools_override)

        # 3. 构建 messages（缓存失效区）
        messages = self._build_messages(session_id, user_input)

        return {
            "system": system_text,
            "messages": messages,
            "tools": tools,
        }

    def get_cache_stable_prefix(self) -> str:
        """返回缓存命中区的内容（system + 画像），用于调试和验证缓存命中率。

        返回的内容即 build_prompt 返回的 system 字段，包含 SYSTEM_PROMPT
        与用户画像全文（用分隔符连接）。

        返回:
            缓存命中区文本（即 system 字段的完整内容）。
        """
        return self._build_system_text()

    def get_cache_break_point(self) -> int:
        """返回缓存失效区起点的消息索引（即检索记忆注入的位置）。

        检索记忆始终作为 messages[0] 注入，故缓存失效点固定为 0。
        若无检索记忆注入，缓存失效点仍是 messages[0]（即 history 第一条
        或当前用户输入），因为 messages 部分整体属于缓存失效区。

        返回:
            缓存失效区起点的消息索引，固定为 0。
        """
        return 0

    # ------------------------------------------------------------------
    # Phase 8 Task 1.5: cron 隔离分支
    # ------------------------------------------------------------------
    def build_cron_context(
        self,
        session_id: str,
        user_input: str,
        cron_isolation: "CronIsolation",
        tools_override: Optional[list] = None,
        extra_injection: Optional[str] = None,
        time_context: Optional[str] = None,
        history_summaries: Optional[str] = None,
        env_section: Optional[str] = None,
    ) -> dict:
        """构建 cron 调度会话的隔离 prompt 上下文。

        与 :meth:`build_prompt` 的区别（5 条缓存硬约束之一：system_text
        禁含动态变量）：
        - system_text 只含 SYSTEM_PROMPT + 工作流模板固定 prompt，**不注入
          memory.md 用户画像**（``inject_profile=False``），避免 memory.md
          异步更新让缓存命中区失效。
        - messages[0] 注入运行环境 + 时间上下文 + 历史执行摘要 + 检索记忆
          （cron namespace）+ 工作流数据（``extra_injection``），不注入
          TaskManager 进度（``inject_todo=False``），避免动态变量破坏缓存
          稳定性。
        - 检索记忆按 ``namespace="cron"`` + ``cron_id`` 过滤，与用户会话
          记忆互不可见。

        工作流数据（``extra_injection``）由调用方（CronScheduler /
        WorkflowTemplate）提供，本方法只负责拼接到 messages[0]，不感知
        其内容（Task 2 实装时填充）。

        Phase 8 Task 2.10 新增 ``time_context`` / ``history_summaries``
        两个注入字段，由调用方从 :class:`RunsJsonlStore` 读取并格式化为
        markdown 段后传入。两者均属缓存失效区，不影响 system_text 稳定性。

        新增 ``env_section`` 注入字段（运行环境信息段），由调用方通过
        :meth:`ContextBuilder.build_environment` 构造后传入。注入
        位置在 messages[0] **最前面**（在 ``time_context`` 之前），便于
        LLM 优先感知运行环境。该字段属缓存失效区，不影响 system_text
        稳定性。

        参数:
            session_id: 会话 ID（形如 ``cron:<cron_id>``）。
            user_input: 当前用户输入文本（cron 调度场景为 schedule.task
                渲染后的任务文本，已由上层完成时间变量替换）。
            cron_isolation: cron 隔离上下文，含 cron_id / namespace 等。
            tools_override: 工具 schema 覆盖列表（cron 路径可用
                active_tools_snapshot 过滤后的列表实现请求级隔离）。
                为 None 时使用 tool_registry 返回的完整列表。
            extra_injection: 工作流数据注入文本（Task 2 实装时由
                WorkflowResult.metrics_for_injection 提供）。为 None 时
                不注入。该字段属缓存失效区，不影响 system_text 稳定性。
            time_context: 时间上下文 markdown 段（SubTask 2.10）。含
                ``current_time`` / ``last_run`` / ``days_since_last`` 等
                动态信息。为 None 时跳过注入。
            history_summaries: 历史执行摘要 markdown 段（SubTask 2.10）。
                含最近 3 条 RunSummary 的 ``llm_summary``。为 None 时跳过注入。
            env_section: 运行环境信息 markdown 段。含 OS / 工作目录 / Shell
                / Python 启动命令 / Python 路径。为 None 时跳过注入。
                注入位置在 messages[0] 最前面（在 time_context 之前）。

        返回:
            Anthropic API 请求参数字典::

                {
                    "system": "SYSTEM_PROMPT（不含画像）",
                    "messages": [...],   # 环境+时间+历史摘要+检索记忆+工作流数据 + history + 用户输入
                    "tools": [...]       # 工具 schema（可由 tools_override 过滤）
                }
        """
        # 1. cron system_text：仅 SYSTEM_PROMPT，不注入 memory.md 用户画像
        #    （inject_profile=False，缓存硬约束：system_text 禁含动态变量）
        system_text = self.system_prompt

        # 2. 构建 tools（字节级稳定，可由 tools_override 过滤实现请求级隔离）
        tools = self._build_tools(tools_override)

        # 3. 构建 messages（缓存失效区）
        messages = self._build_cron_messages(
            session_id,
            user_input,
            cron_isolation,
            extra_injection,
            time_context,
            history_summaries,
            env_section,
        )

        return {
            "system": system_text,
            "messages": messages,
            "tools": tools,
        }

    def _build_cron_messages(
        self,
        session_id: str,
        user_input: str,
        cron_isolation: "CronIsolation",
        extra_injection: Optional[str],
        time_context: Optional[str] = None,
        history_summaries: Optional[str] = None,
        env_section: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """构建 cron 会话的 messages 列表（缓存失效区）。

        拼接顺序（SubTask 2.10 补充时间上下文与历史执行摘要，新增
        运行环境信息段注入到最前面）：
        1. 运行环境 + 时间上下文 + 历史执行摘要 + 检索记忆（cron namespace）
           + 工作流数据（合并为 messages[0]，缓存失效区起点）
        2. 对话历史（history_buffer 返回的完整列表，经 condenser 压缩）
        3. 当前用户输入（作为最后一条 user 消息）

        与 :meth:`_build_messages` 的区别：
        - 检索记忆按 ``namespace="cron"`` + ``cron_id`` 过滤
        - 不注入 TaskManager 进度（inject_todo=False）
        - 支持 ``extra_injection`` 工作流数据拼接（Task 2 填充）
        - 支持 ``time_context`` 时间上下文注入（SubTask 2.10）
        - 支持 ``history_summaries`` 历史执行摘要注入（SubTask 2.10）
        - 支持 ``env_section`` 运行环境信息注入（置于 messages[0] 最前面）

        参数:
            session_id: 会话 ID。
            user_input: 当前用户输入文本。
            cron_isolation: cron 隔离上下文。
            extra_injection: 工作流数据注入文本。
            time_context: 时间上下文 markdown 段。为 None 时跳过。
            history_summaries: 历史执行摘要 markdown 段。为 None 时跳过。
            env_section: 运行环境信息 markdown 段。为 None 时跳过。注入
                位置在 messages[0] 最前面（在 time_context 之前）。

        返回:
            消息列表。
        """
        messages: List[Dict[str, Any]] = []

        # 1. 运行环境 + 时间上下文 + 历史执行摘要 + 检索记忆 + 工作流数据
        #    （合并为 messages[0]）
        # 拼接顺序：运行环境 → 时间上下文 → 历史执行摘要 → 检索记忆 → 工作流数据
        # （运行环境前置便于 LLM 优先感知 OS/Shell；时间相关的动态信息次之；
        #   检索记忆与工作流数据紧贴当前任务，便于 LLM 关联理解）
        injection_text = self._get_cron_memory_injection(
            user_input, cron_isolation
        )
        # 拼接时间上下文（SubTask 2.10）
        if time_context:
            if injection_text:
                injection_text = f"{time_context}\n\n{injection_text}"
            else:
                injection_text = time_context
        # 拼接历史执行摘要（SubTask 2.10）
        if history_summaries:
            if injection_text:
                injection_text = f"{injection_text}\n\n{history_summaries}"
            else:
                injection_text = history_summaries
        # 拼接工作流数据（Task 2 填充，当前为 None 时跳过）
        if extra_injection:
            if injection_text:
                injection_text = f"{injection_text}\n\n{extra_injection}"
            else:
                injection_text = extra_injection
        # 拼接运行环境信息到最前面（在 time_context 之前）
        if env_section:
            if injection_text:
                injection_text = f"{env_section}\n\n{injection_text}"
            else:
                injection_text = env_section
        if injection_text:
            messages.append({"role": "user", "content": injection_text})

        # 2. 对话历史（每轮增长）
        if self.history_buffer is not None:
            try:
                history = self.history_buffer.get_history(session_id) or []
            except Exception as e:
                logger.error("获取对话历史失败，跳过历史拼接: %s", e)
                history = []
            clean_history = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") is not None and m.get("content") is not None
            ]
            if self.condenser is not None:
                try:
                    clean_history = self.condenser.condense(clean_history)
                except Exception as e:
                    logger.warning("condenser 压缩历史失败，使用原始历史: %s", e)
            messages.extend(clean_history)

        # 3. 当前用户输入（最易变）：作为最后一条 user 消息
        messages.append({"role": "user", "content": user_input})

        return messages

    def _get_cron_memory_injection(
        self, user_input: str, cron_isolation: "CronIsolation"
    ) -> str:
        """获取 cron 命名空间下的检索记忆注入文本。

        按 ``namespace="cron"`` + ``cron_id`` 过滤召回，只注入该调度项
        自己命名空间下的记忆，绝不返回用户会话或其他调度项的记忆。

        参数:
            user_input: 当前用户输入文本，用于检索相关记忆。
            cron_isolation: cron 隔离上下文（含 cron_id）。

        返回:
            注入文本。无 memory_retriever 或无相关记忆时返回空字符串。
        """
        if self.memory_retriever is None:
            return ""
        try:
            text = self.memory_retriever.get_injection_text(
                user_input,
                namespace=cron_isolation.namespace,
                cron_id=cron_isolation.cron_id,
            )
            return text or ""
        except Exception as e:
            logger.error("cron 检索记忆注入失败，跳过注入: %s", e)
            return ""

    def verify_cache_stability(
        self,
        session_id: str,
        user_input: str,
    ) -> bool:
        """验证缓存命中区（system + tools）在相同输入下的字节级稳定性。

        对相同输入连续调用两次 build_prompt，比对 system 字段字符串与
        tools 字段列表的相等性。前缀缓存策略依赖缓存区的字节级稳定，
        任何变化（如 tool_registry 动态返回不同 schema）都会导致缓存
        反复 miss，token 成本与延迟显著上升。

        用途：
        - 单元测试：验证 ContextManager 实现符合前缀缓存设计约束
        - 运维巡检：动态加载工具/MCP 后验证缓存策略未被破坏

        参数:
            session_id: 会话 ID。
            user_input: 当前用户输入文本。

        返回:
            True 表示 system + tools 字段在两次调用间字节级稳定；
            False 表示不稳定（日志记录 WARNING）。
        """
        prompt1 = self.build_prompt(session_id, user_input)
        prompt2 = self.build_prompt(session_id, user_input)

        system_stable = prompt1["system"] == prompt2["system"]
        tools_stable = prompt1["tools"] == prompt2["tools"]

        if system_stable and tools_stable:
            return True

        logger.warning(
            "缓存区不稳定：system 或 tools 字段在两次调用间发生变化 "
            "(system_stable=%s, tools_stable=%s)",
            system_stable,
            tools_stable,
        )
        return False

    def enter_plan_mode(self) -> str:
        """进入 Plan 模式，返回 system-reminder 软约束文本。

        本方法不修改工具列表，也不修改 system prompt。
        实际的 Plan 模式提示通过 tool result 注入到 messages 中
        （作为 assistant 消息后的 user 消息），由调用方负责组装。

        工具列表完全不变，保证字节级稳定，不影响前缀缓存命中。

        返回:
            Plan 模式软约束 system-reminder 文本。
        """
        return _PLAN_MODE_REMINDER

    def exit_plan_mode(self) -> str:
        """退出 Plan 模式，返回解除提示。

        本方法不修改工具列表与 system prompt。解除提示同样通过
        tool result 注入到 messages 中，由调用方负责组装。

        返回:
            Plan 模式解除提示文本。
        """
        return _PLAN_MODE_EXIT_REMINDER

    # ------------------------------------------------------------------
    # 内部构建方法
    # ------------------------------------------------------------------
    def _build_system_text(self) -> str:
        """构建 system 字段文本（缓存命中区的核心部分）。

        拼接顺序：SYSTEM_PROMPT + 分隔符 + 用户画像主体（排除 Agent 自画像
        和沟通偏好段）。Agent 自画像和沟通偏好段从 system_text 移出注入
        messages[0] 动态区，避免异步更新破坏 system_text 缓存稳定性。
        用户画像为空时仅返回 SYSTEM_PROMPT，保证字节级稳定。

        返回:
            system 字段的完整文本。
        """
        # 用户画像主体（较稳定，异步更新）：memory.md 排除 Agent 自画像和沟通偏好段
        profile_text = ""
        if self.memory_md_manager is not None:
            try:
                profile_text = self.memory_md_manager.read_system_profile() or ""
            except Exception as e:
                logger.error("读取用户画像失败，仅使用 SYSTEM_PROMPT: %s", e)
                profile_text = ""

        if profile_text:
            return f"{self.system_prompt}{_PROFILE_SEPARATOR}{profile_text}"
        return self.system_prompt

    def _build_tools(self, tools_override: Optional[list]) -> list:
        """构建 tools 字段（缓存命中区，字节级稳定）。

        优先使用 tools_override（特殊场景），否则使用 tool_registry
        返回的完整工具 schema 列表。始终保持完整列表，不因 Plan 模式改变。

        参数:
            tools_override: 工具 schema 覆盖列表。

        返回:
            工具 schema 列表。无 tool_registry 且无 override 时返回空列表。
        """
        if tools_override is not None:
            return list(tools_override)

        if self.tool_registry is None:
            return []

        try:
            schema = self.tool_registry.get_tools_schema()
        except Exception as e:
            logger.error("获取工具 schema 失败，返回空列表: %s", e)
            return []

        # 浅拷贝避免外部修改污染 tool_registry 内部状态
        return list(schema)

    def _build_messages(
        self,
        session_id: str,
        user_input: str,
    ) -> List[Dict[str, Any]]:
        """构建 messages 列表（缓存失效区）。

        拼接顺序：
        1. 文件注入 → Agent 自画像 → 沟通偏好 → 历史教训(预留) → 检索记忆
           （合并为第一条 user 消息，缓存失效区起点）
        2. 对话历史（history_buffer 返回的完整列表，经 condenser 压缩）
        3. 当前用户输入（作为最后一条 user 消息）

        Agent 自画像和沟通偏好从 system_text 移出注入此处，避免异步更新
        破坏 system_text 缓存稳定性。注入顺序遵循 spec 约定：
        文件注入 → Agent 自画像 → 沟通偏好 → 历史教训 → 检索记忆。
        所有注入段均为空时跳过第一条 user 消息，避免产生空消息。
        历史消息仅保留 role 与 content 字段，剔除 timestamp 等附加字段，
        保证消息结构符合 Anthropic API 规范。
        若 :attr:`condenser` 非 None，在拼接前对 history 应用压缩
        （masking 旧 tool_result / LLM 摘要），减少上下文 token 消耗。
        压缩仅影响送入 LLM 的 messages，不影响 history_buffer 存储。

        参数:
            session_id: 会话 ID。
            user_input: 当前用户输入文本。

        返回:
            消息列表，每项 {"role": "user"/"assistant", "content": "..."}。
        """
        messages: List[Dict[str, Any]] = []

        # 1. 文件注入 → Agent 自画像 → 沟通偏好 → 历史教训 → 检索记忆
        #    合并为第一条 user 消息（缓存失效区起点）；全为空时跳过
        file_injection = self.get_file_injection(session_id)
        agent_profile = self._get_agent_profile_injection()
        communication_prefs = self._get_communication_injection()
        lessons_injection = self._get_lessons_injection(user_input)
        memory_injection = self._get_memory_injection(user_input)

        # SubTask 4.5: token 预算不足时按优先级裁剪
        # 裁剪顺序：Agent 自画像 → 沟通偏好 → 历史教训（后者优先保留）
        # 文件注入和检索记忆始终保留（高优先级）
        parts = [
            p for p in [
                file_injection,
                agent_profile,
                communication_prefs,
                lessons_injection,
                memory_injection,
            ] if p
        ]
        total_chars = sum(len(p) for p in parts)
        if total_chars > _MAX_INJECTION_CHARS:
            # 按优先级从低到高依次裁剪
            if agent_profile and total_chars > _MAX_INJECTION_CHARS:
                parts = [p for p in parts if p != agent_profile]
                total_chars = sum(len(p) for p in parts)
            if communication_prefs and total_chars > _MAX_INJECTION_CHARS:
                parts = [p for p in parts if p != communication_prefs]
                total_chars = sum(len(p) for p in parts)
            if lessons_injection and total_chars > _MAX_INJECTION_CHARS:
                parts = [p for p in parts if p != lessons_injection]
                total_chars = sum(len(p) for p in parts)

        if parts:
            injection_text = "\n\n".join(parts)
            messages.append({"role": "user", "content": injection_text})

        # 2. 对话历史（每轮增长）
        if self.history_buffer is not None:
            try:
                history = self.history_buffer.get_history(session_id) or []
            except Exception as e:
                logger.error("获取对话历史失败，跳过历史拼接: %s", e)
                history = []
            # 仅保留 role 与 content 字段，剔除 timestamp 等附加字段
            # 保证消息结构符合 Anthropic API 规范
            clean_history = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") is not None and m.get("content") is not None
            ]
            # 应用 condenser 压缩（masking 旧 tool_result / LLM 摘要）
            # condenser 为 None 时原样返回，不影响 history_buffer 存储
            if self.condenser is not None:
                try:
                    clean_history = self.condenser.condense(clean_history)
                except Exception as e:
                    logger.warning("condenser 压缩历史失败，使用原始历史: %s", e)
            messages.extend(clean_history)

        # 3. 当前用户输入（最易变）：作为最后一条 user 消息
        messages.append({"role": "user", "content": user_input})

        return messages

    def _get_agent_profile_injection(self) -> str:
        """获取 Agent 自画像注入文本（messages[0] 动态区）。

        从 memory.md 的 `## Agent 自画像` section 读取 body，按 spec 要求
        选取 top-3 模式（前 3 条非空行）并截断到 ≤200 token，包裹为带
        标题的 markdown 段注入。body 为空时返回空字符串（跳过注入）。

        spec.md line 81:
            THEN 系统 SHALL 通过 messages[0] 注入 Agent 自画像 top-3 模式
            （≤200 token）

        返回:
            Agent 自画像注入文本，形如 ``## Agent 自画像\\n{body}``。
            无 memory_md_manager、body 为空、或 top-3 截断后无内容时
            返回空字符串。
        """
        if self.memory_md_manager is None:
            return ""
        try:
            body = self.memory_md_manager.read_section_body("Agent 自画像")
            if not body:
                return ""
            # spec: top-3 模式（取前 3 条非空行）+ ≤200 token 截断
            lines = body.strip().split("\n")
            top_lines: List[str] = []
            accumulated_tokens = 0
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                line_tokens = _estimate_tokens(stripped)
                if accumulated_tokens + line_tokens > _MAX_AGENT_PROFILE_TOKENS:
                    break
                top_lines.append(line)
                accumulated_tokens += line_tokens
                if len(top_lines) >= 3:
                    break
            if not top_lines:
                return ""
            truncated_body = "\n".join(top_lines)
            return f"## Agent 自画像\n{truncated_body}"
        except Exception as e:
            logger.error("Agent 自画像注入失败，跳过: %s", e)
            return ""

    def _get_communication_injection(self) -> str:
        """获取沟通偏好注入文本（messages[0] 动态区）。

        从 memory.md 的 `## 沟通偏好` section 读取 body，按 spec 要求
        截断到 ≤1000 字符，包裹为带标题的 markdown 段注入。body 为空时
        返回空字符串（跳过注入）。

        spec.md line 264:
            沟通偏好（≤1000）

        返回:
            沟通偏好注入文本，形如 ``## 沟通偏好\\n{body}``。
            无 memory_md_manager 或 body 为空时返回空字符串。
        """
        if self.memory_md_manager is None:
            return ""
        try:
            body = self.memory_md_manager.read_section_body("沟通偏好")
            if not body:
                return ""
            # spec: ≤1000 字符截断
            if len(body) > _MAX_COMMUNICATION_CHARS:
                body = body[:_MAX_COMMUNICATION_CHARS].rstrip()
            return f"## 沟通偏好\n{body}"
        except Exception as e:
            logger.error("沟通偏好注入失败，跳过: %s", e)
            return ""

    def _get_lessons_injection(self, user_input: str) -> str:
        """获取历史教训注入文本（messages[0] 动态区）。

        读取 failure_cases.jsonl（若存在），按当前任务 embedding 检索 top-3
        失败案例，注入 ``## 历史教训`` section（≤300 token）。启用惊讶门控
        （与已有记忆相似度 >0.92 时不重复注入）。

        空文件兜底：failure_cases.jsonl 不存在或为空时返回空字符串，不报错。

        参数:
            user_input: 当前用户输入文本，用于 embedding 检索相关失败案例。

        返回:
            历史教训注入文本，形如 ``## 历史教训\\n- ...``。
            文件不存在、为空、或检索无结果时返回空字符串。
        """
        # SubTask 4.7: 空文件兜底
        if not os.path.exists(self.failure_cases_path):
            return ""

        # 读取 JSONL
        try:
            cases = self._read_failure_cases()
        except Exception as e:
            logger.warning("读取失败案例库 %s 失败，跳过教训注入: %s",
                           self.failure_cases_path, e)
            return ""
        if not cases:
            return ""

        # SubTask 4.2: 按当前任务 embedding 检索 top-3
        query_embedding = _compute_embedding(user_input)
        if not query_embedding:
            # embedding 计算失败，降级为按置信度取 top-3
            ranked = sorted(
                cases,
                key=lambda c: c.get("confidence", 0.0),
                reverse=True,
            )[:3]
        else:
            scored = []
            for case in cases:
                sim = _cosine_similarity(
                    query_embedding, case.get("embedding", [])
                )
                scored.append((sim, case))
            ranked = [c for _, c in sorted(
                scored, key=lambda x: x[0], reverse=True
            )[:3]]

        # SubTask 4.3: 惊讶门控——与已有记忆相似度 >0.92 时不重复注入
        memory_text = ""
        if self.memory_retriever is not None:
            try:
                memory_text = self.memory_retriever.get_injection_text(user_input) or ""
            except Exception:
                memory_text = ""
        memory_embedding = _compute_embedding(memory_text) if memory_text else []
        if memory_embedding:
            ranked = [
                c for c in ranked
                if _cosine_similarity(
                    memory_embedding, c.get("embedding", [])
                ) < _SURPRISE_GATE_THRESHOLD
            ]
            if not ranked:
                return ""

        # 过滤低置信度案例
        ranked = [
            c for c in ranked
            if c.get("confidence", 0.0) >= _MIN_CONFIDENCE
        ]
        if not ranked:
            return ""

        # SubTask 4.4: token 上限动态计算（硬上限 300 token）
        lessons_text = self._format_lessons(ranked, _MAX_LESSONS_TOKENS)
        return lessons_text

    def _read_failure_cases(self) -> List[Dict[str, Any]]:
        """读取 failure_cases.jsonl 文件，返回案例列表。

        每行是一个 JSON 对象，包含 pattern/root_cause/prevention_rule/
        confidence/embedding 字段。空行和解析失败的行被跳过。

        返回:
            案例字典列表。文件不存在或为空时返回空列表。
        """
        cases: List[Dict[str, Any]] = []
        try:
            with open(self.failure_cases_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        case = json.loads(line)
                        if isinstance(case, dict):
                            cases.append(case)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        return cases

    @staticmethod
    def _format_lessons(cases: List[Dict[str, Any]], max_tokens: int) -> str:
        """将失败案例格式化为 ``## 历史教训`` 注入文本，受 token 上限约束。

        参数:
            cases: 已排序的失败案例列表（最相关在前）。
            max_tokens: token 硬上限。

        返回:
            格式化的教训注入文本。超出 token 上限时截断后面的案例。
        """
        lines: List[str] = ["## 历史教训"]
        current_tokens = _estimate_tokens("\n".join(lines))
        for case in cases:
            pattern = case.get("pattern", "")
            root_cause = case.get("root_cause", "")
            prevention = case.get("prevention_rule", "")
            entry = f"- **模式**：{pattern}；**根因**：{root_cause}；**规避**：{prevention}"
            entry_tokens = _estimate_tokens(entry)
            if current_tokens + entry_tokens > max_tokens:
                break
            lines.append(entry)
            current_tokens += entry_tokens
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def get_file_injection(self, session_id: str) -> str:
        """获取文件摘要注入文本（public 入口，供 orchestrator 调用）。

        参数:
            session_id: 会话 ID。

        返回:
            注入文本。无 file_context_injector 或无已完成文件时返回空字符串。
        """
        if self.file_context_injector is None:
            return ""
        try:
            text = self.file_context_injector.get_injection_text(session_id)
            return text or ""
        except Exception as e:
            logger.error("文件摘要注入失败，跳过注入: %s", e)
            return ""

    def _get_memory_injection(self, user_input: str) -> str:
        """获取检索记忆注入文本。

        参数:
            user_input: 当前用户输入文本，用于检索相关记忆。

        返回:
            注入文本。无 memory_retriever 或无相关记忆时返回空字符串。
        """
        if self.memory_retriever is None:
            return ""
        try:
            text = self.memory_retriever.get_injection_text(user_input)
            return text or ""
        except Exception as e:
            logger.error("检索记忆注入失败，跳过注入: %s", e)
            return ""


if __name__ == "__main__":
    # —— 简单验证逻辑 ——
    print("=== ContextManager 验证 ===\n")

    # 1. 基本构建（无依赖）
    cm = ContextManager()
    prompt = cm.build_prompt("test-session", "你好")
    print(f"[Build] system 长度: {len(prompt['system'])}")
    print(f"[Build] messages 数量: {len(prompt['messages'])}")
    print(f"[Build] tools 数量: {len(prompt['tools'])}")
    assert SYSTEM_PROMPT in prompt["system"], "system 应包含 SYSTEM_PROMPT"
    assert prompt["messages"][-1] == {"role": "user", "content": "你好"}, "最后一条应为当前用户输入"
    assert prompt["tools"] == [], "无 tool_registry 时 tools 应为空列表"
    print("[Build] 基本构建验证通过\n")

    # 2. 缓存命中区与失效点
    prefix = cm.get_cache_stable_prefix()
    break_point = cm.get_cache_break_point()
    print(f"[Cache] 缓存命中区长度: {len(prefix)}")
    print(f"[Cache] 缓存失效点索引: {break_point}")
    assert prefix == prompt["system"], "缓存命中区应等于 system 字段"
    assert break_point == 0, "缓存失效点应为 0"
    print("[Cache] 缓存命中区与失效点验证通过\n")

    # 3. Plan 模式软约束（不改变工具列表）
    plan_text = cm.enter_plan_mode()
    exit_text = cm.exit_plan_mode()
    print(f"[Plan] enter_plan_mode 返回长度: {len(plan_text)}")
    print(f"[Plan] exit_plan_mode 返回长度: {len(exit_text)}")
    assert "Plan 模式" in plan_text, "enter_plan_mode 应包含 'Plan 模式'"
    assert "system-reminder" in plan_text, "enter_plan_mode 应包含 system-reminder"
    assert "ONLY" in plan_text, "enter_plan_mode 应包含 ONLY 约束"
    assert "MUST NOT" in plan_text, "enter_plan_mode 应包含 MUST NOT 约束"
    # Plan 模式不改变工具列表
    prompt_after_plan = cm.build_prompt("test-session", "你好")
    assert prompt_after_plan["tools"] == prompt["tools"], "Plan 模式不应改变工具列表"
    print("[Plan] Plan 模式软约束验证通过\n")

    # 4. 带 mock 依赖的完整构建
    class MockToolRegistry:
        def get_tools_schema(self):
            return [
                {
                    "name": "test_tool",
                    "description": "测试工具",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]

    class MockMemoryMdManager:
        def read(self):
            return "# 用户画像\n\n## 基本信息\n- 用户是测试用户"

    class MockMemoryRetriever:
        def get_injection_text(self, user_input):
            return "## 相关记忆\n1. 测试记忆 (相关度: 0.90)"

    class MockHistoryBuffer:
        def get_history(self, session_id):
            return [
                {"role": "user", "content": "历史用户", "timestamp": "2024-01-01"},
                {"role": "assistant", "content": "历史助手", "timestamp": "2024-01-01"},
            ]

    cm2 = ContextManager(
        tool_registry=MockToolRegistry(),
        memory_md_manager=MockMemoryMdManager(),
        memory_retriever=MockMemoryRetriever(),
        history_buffer=MockHistoryBuffer(),
    )
    prompt2 = cm2.build_prompt("test-session", "当前问题")

    print(f"[Full] system 长度: {len(prompt2['system'])}")
    print(f"[Full] messages 数量: {len(prompt2['messages'])}")
    print(f"[Full] tools 数量: {len(prompt2['tools'])}")

    # 4.1 system 包含 SYSTEM_PROMPT + 用户画像
    assert SYSTEM_PROMPT in prompt2["system"], "system 应包含 SYSTEM_PROMPT"
    assert "用户是测试用户" in prompt2["system"], "system 应包含用户画像全文"
    assert "---" in prompt2["system"], "system 应包含分隔符"

    # 4.2 tools 是完整列表
    assert len(prompt2["tools"]) == 1, "tools 应为完整列表"
    assert prompt2["tools"][0]["name"] == "test_tool"

    # 4.3 messages 第0条是检索记忆注入
    assert prompt2["messages"][0]["role"] == "user"
    assert "相关记忆" in prompt2["messages"][0]["content"], "messages[0] 应为检索记忆注入"

    # 4.4 历史在中间
    assert prompt2["messages"][1]["content"] == "历史用户"
    assert prompt2["messages"][2]["content"] == "历史助手"

    # 4.5 最后一条是当前用户输入
    assert prompt2["messages"][-1] == {"role": "user", "content": "当前问题"}

    # 4.6 历史消息不含 timestamp 等附加字段（符合 Anthropic API 规范）
    assert "timestamp" not in prompt2["messages"][1], "历史消息不应包含 timestamp"
    assert set(prompt2["messages"][1].keys()) == {"role", "content"}, "历史消息应仅含 role 和 content"
    print("[Full] 带 mock 依赖完整构建验证通过\n")

    # 5. 缓存命中区在带依赖场景下也正确
    prefix2 = cm2.get_cache_stable_prefix()
    assert prefix2 == prompt2["system"], "缓存命中区应等于 system 字段"
    assert "用户是测试用户" in prefix2, "缓存命中区应包含用户画像"
    print("[Cache] 带依赖缓存命中区验证通过\n")

    # 6. 检索记忆为空时跳过注入
    class MockEmptyRetriever:
        def get_injection_text(self, user_input):
            return ""

    cm3 = ContextManager(
        tool_registry=MockToolRegistry(),
        memory_retriever=MockEmptyRetriever(),
        history_buffer=MockHistoryBuffer(),
    )
    prompt3 = cm3.build_prompt("test-session", "当前问题")
    # 无检索记忆注入时，messages[0] 应为历史第一条
    assert prompt3["messages"][0]["content"] == "历史用户", "无检索记忆时 messages[0] 应为历史第一条"
    assert prompt3["messages"][-1] == {"role": "user", "content": "当前问题"}
    print("[Empty] 检索记忆为空时跳过注入验证通过\n")

    print("=== 所有验证通过 ===")
