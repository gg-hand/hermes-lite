"""主编排逻辑。

协调 LLM 客户端、React 循环、工具注册中心、历史缓冲、长期记忆向量库、
记忆检索与沉淀引擎、会话日志等模块，对外提供统一的对话入口
`Orchestrator.chat(session_id, user_input)`。

对于记忆 / 工具子系统中的可选模块（HistoryBuffer、ConsolidationEngine、
MemoryMdManager、MemoryRetriever、ContextManager、ToolRegistry），使用
try/except import 占位，允许其在缺失或依赖未安装时降级运行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

# 兼容相对导入与直接运行两种方式（与 llm/client.py 保持一致）
# 仅导入核心必需模块（不依赖 chromadb 等重型可选依赖）
try:
    from .config import load_config
    from .llm.client import LLMClient
    from .llm.prompts import SYSTEM_PROMPT, TITLE_GENERATION_PROMPT
    from .llm.reasoning_profiles import ReasoningConfig
    from .agent.react_loop import ReactLoop
    from .agent.session_manager import SessionManager
    from .agent.skill_manager import SkillManager
    from .agent.cron_isolator import CronIsolator
    from .agent.context_builder import ContextBuilder
    from .agent.msg_persistence import MessagePersistence
    from .storage.sqlite_log import SessionLogger
except ImportError:  # pragma: no cover - 直接运行模块时回退
    import sys
    from pathlib import Path

    _SRC_DIR = str(Path(__file__).resolve().parent.parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from config import load_config  # type: ignore
    from llm.client import LLMClient  # type: ignore
    from llm.prompts import SYSTEM_PROMPT, TITLE_GENERATION_PROMPT  # type: ignore
    from llm.reasoning_profiles import ReasoningConfig  # type: ignore
    from agent.react_loop import ReactLoop  # type: ignore
    from agent.session_manager import SessionManager  # type: ignore
    from agent.skill_manager import SkillManager  # type: ignore
    from agent.cron_isolator import CronIsolator  # type: ignore
    from agent.context_builder import ContextBuilder  # type: ignore
    from agent.msg_persistence import MessagePersistence  # type: ignore
    from storage.sqlite_log import SessionLogger  # type: ignore

# 可选模块：记忆 / 工具子系统。
# 分组导入：chromadb 依赖单独一组，其余模块不依赖 chromadb，
# 避免 chromadb 缺失时连带禁用 HistoryBuffer / ToolRegistry 等无关模块。

# 组 1：ChromaMemoryStore（依赖 chromadb + sentence-transformers，较重）
try:
    from .storage.chroma_store import ChromaMemoryStore
except ImportError:  # pragma: no cover
    try:
        from storage.chroma_store import ChromaMemoryStore  # type: ignore
    except ImportError:
        ChromaMemoryStore = None  # type: ignore

# 组 2：不依赖 chromadb 的记忆 / 工具模块
try:
    from .storage.history_buffer import HistoryBuffer
    from .memory.consolidation import ConsolidationEngine
    from .memory.cron_isolation import CronIsolation
    from .memory.memory_md import MemoryMdManager
    from .memory.retrieval import MemoryRetriever
    from .memory.context_manager import ContextManager
    from .memory.condenser import (
        Condenser,
        MaskingCondenser,
        LLMSummarizingCondenser,
        create_condenser_from_config,
    )
    from .memory.decay import MemoryDecay
    from .memory.signal_pool import SignalPool
    from .agent.tool_registry import ToolRegistry
    from .agent.tools import register_builtin_tools
    from .agent.tools.plan_tools import register_plan_tools
    from .agent.tools.memory_tools import register_memory_tools
    from .agent.intent_classifier import (
        IntentClassificationResult,
        IntentType,
        classify_intent,
    )
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from storage.history_buffer import HistoryBuffer  # type: ignore
        from memory.consolidation import ConsolidationEngine  # type: ignore
        from memory.cron_isolation import CronIsolation  # type: ignore
        from memory.memory_md import MemoryMdManager  # type: ignore
        from memory.retrieval import MemoryRetriever  # type: ignore
        from memory.context_manager import ContextManager  # type: ignore
        from memory.condenser import (  # type: ignore
            Condenser,
            MaskingCondenser,
            LLMSummarizingCondenser,
            create_condenser_from_config,
        )
        from memory.decay import MemoryDecay  # type: ignore
        from memory.signal_pool import SignalPool  # type: ignore
        from agent.tool_registry import ToolRegistry  # type: ignore
        from agent.tools import register_builtin_tools  # type: ignore
        from agent.tools.plan_tools import register_plan_tools  # type: ignore
        from agent.tools.memory_tools import register_memory_tools  # type: ignore
        from agent.intent_classifier import (  # type: ignore
            IntentClassificationResult,
            IntentType,
            classify_intent,
        )
    except ImportError:  # pragma: no cover
        HistoryBuffer = None  # type: ignore
        ConsolidationEngine = None  # type: ignore
        CronIsolation = None  # type: ignore
        MemoryMdManager = None  # type: ignore
        MemoryRetriever = None  # type: ignore
        ContextManager = None  # type: ignore
        Condenser = None  # type: ignore
        MaskingCondenser = None  # type: ignore
        LLMSummarizingCondenser = None  # type: ignore
        create_condenser_from_config = None  # type: ignore
        MemoryDecay = None  # type: ignore
        SignalPool = None  # type: ignore
        ToolRegistry = None  # type: ignore
        register_builtin_tools = None  # type: ignore
        register_plan_tools = None  # type: ignore
        register_memory_tools = None  # type: ignore
        IntentClassificationResult = None  # type: ignore
        IntentType = None  # type: ignore
        classify_intent = None  # type: ignore

if TYPE_CHECKING:
    try:
        from .monitoring.metrics import MetricsCollector
        from .agent.audit import AuditLogger
        from .agent.policy import PolicyEngine
        from .agent.approval import ApprovalManager
        from .guardrails import GuardrailEngine
        from .llm.reasoning_profiles import ReasoningConfig
    except ImportError:
        from monitoring.metrics import MetricsCollector  # type: ignore
        from agent.audit import AuditLogger  # type: ignore
        from agent.policy import PolicyEngine  # type: ignore
        from agent.approval import ApprovalManager  # type: ignore
        from guardrails import GuardrailEngine  # type: ignore
        from llm.reasoning_profiles import ReasoningConfig  # type: ignore

# Phase 5: 安全策略与审批模块（运行时导入，与 monitoring 模块同样降级为 None）
try:
    from .agent.policy import PolicyEngine
    from .agent.approval import ApprovalManager
    from .agent.file_registry import FileOperationRegistry
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from agent.policy import PolicyEngine  # type: ignore
        from agent.approval import ApprovalManager  # type: ignore
        from agent.file_registry import FileOperationRegistry  # type: ignore
    except ImportError:  # pragma: no cover
        PolicyEngine = None  # type: ignore
        ApprovalManager = None  # type: ignore
        FileOperationRegistry = None  # type: ignore

# Phase 6: 任务编排模块
# - TaskManager: 只读归档（保留 list_tasks / get_progress_summary 等只读接口）
# - TodoListRegistry: 会话级 plan 模式数据模型（纯内存对象，不依赖任何配置）
try:
    from .tasks.task_manager import TaskManager
    from .tasks.todo_list import TodoListRegistry
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from tasks.task_manager import TaskManager  # type: ignore
        from tasks.todo_list import TodoListRegistry  # type: ignore
    except ImportError:  # pragma: no cover
        TaskManager = None  # type: ignore
        TodoListRegistry = None  # type: ignore

# Phase 9 Task 6: AI 护栏统一编排器（fail-open 软护栏，与 PolicyEngine 并存）
try:
    from .guardrails import GuardrailEngine
except ImportError:  # pragma: no cover - 直接运行模块时回退
    try:
        from guardrails import GuardrailEngine  # type: ignore
    except ImportError:  # pragma: no cover
        GuardrailEngine = None  # type: ignore

# Phase 3 Task 9: EnhancedContextBuilder（增强上下文构建器）
from .enhanced_context import EnhancedContextBuilder  # noqa: E402

# Phase 3 Task 10: ChatHandler（非流式对话处理器）
from .chat_handler import ChatHandler  # noqa: E402

logger = logging.getLogger(__name__)


class Orchestrator:
    """主编排器，协调 Agent 各模块完成一次对话。

    初始化时根据 config.yaml 创建并装配以下组件：
    - llm_client: LLM 客户端（主对话 + consolidation）
    - tool_registry: 工具注册中心，注册基础工具（可选，缺失时纯对话模式）
    - react_loop: React 循环引擎（注入 tool_registry）
    - history_buffer: 短期对话历史缓冲（可选，缺失时降级到 session_logger）
    - chroma_store: 长期记忆向量库（可选）
    - session_logger: 会话日志持久化（可选）
    - memory_md_manager: 用户画像 memory.md 管理器（可选）
    - memory_retriever: 长期记忆检索器（可选，依赖 chroma_store 与 memory_md_manager）
    - context_manager: Prompt 上下文管理器（可选，整合上述组件构建分层 Prompt）
    - consolidation_engine: 记忆沉淀引擎（可选，缺失时跳过 consolidation）
    - policy_engine: 策略评估器（可选，缺失时跳过策略拦截）
    - approval_manager: HIL 审批管理器（可选，缺失时流式 confirm 降级为 deny）
    - task_manager: 只读任务归档管理器（可选，缺失时跳过归档进度注入）
    - todo_registry: 会话级 TodoList 注册表（plan 模式数据模型，纯内存对象，
      无条件创建，缺失时跳过 plan 工具注册）
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        metrics: Optional["MetricsCollector"] = None,
        audit_logger: Optional["AuditLogger"] = None,
        approval_manager: Optional["ApprovalManager"] = None,
        task_manager: Optional["TaskManager"] = None,
    ) -> None:
        """初始化编排器并装配各组件。

        参数:
            config_path: 配置文件路径。
            metrics: 可选的指标采集器，非 None 时透传给 ReactLoop / LLMClient
                     并在记忆检索命中/未命中时上报指标。
            audit_logger: 可选的审计日志记录器，非 None 时透传给 ReactLoop
                          以记录每次工具调用。
            approval_manager: 可选的 HIL 审批管理器，非 None 时透传给 ReactLoop；
                     为 None 时若 ApprovalManager 可用则按 config.security
                     内部创建独立实例，否则流式 confirm 降级为 deny。
            task_manager: 可选的只读任务归档管理器，非 None 时直接使用；
                     为 None 时若 TaskManager 可用则按 config.tasks
                     内部创建独立实例，用于注入历史归档进度到 LLM 上下文。
                     写接口已移除（plan 模式改由 TodoListRegistry 承载）。
        """
        from .factories import (
            create_llm_client,
            create_security_subsystem,
            create_task_manager,
            create_todo_registry,
            create_memory_subsystem,
            create_tool_registry,
            create_react_loop,
        )

        self.metrics = metrics
        self.audit_logger = audit_logger
        self.config = load_config(config_path)

        # cron 段：inject_history 控制是否注入上次 run 的 summary 到 cron 上下文
        cron_cfg = self.config.get("cron", {})
        self.cron_inject_history_enabled = bool(cron_cfg.get("inject_history", True))

        # 1. LLM 客户端
        self.llm_client = create_llm_client(self.config, metrics=metrics)

        # 2. 安全子系统
        (
            self.policy_engine,
            self.approval_manager,
            self.guardrail_engine,
            self.file_registry,
        ) = create_security_subsystem(
            self.config, metrics=metrics, approval_manager=approval_manager
        )

        # 3. TaskManager + TodoListRegistry
        self.task_manager = create_task_manager(self.config, task_manager=task_manager)
        self.todo_registry = create_todo_registry(self.config)
        self._current_session_id: Optional[str] = None
        self._current_intent_result: Optional["IntentClassificationResult"] = None

        # 4. 记忆子系统
        mem = create_memory_subsystem(
            self.config,
            llm_client=self.llm_client,
            metrics=metrics,
            orchestrator_ref=self,
        )
        self.history_buffer = mem["history_buffer"]
        self.condenser = mem["condenser"]
        self.chroma_store = mem["chroma_store"]
        self.session_logger = mem["session_logger"]
        self.memory_md_manager = mem["memory_md_manager"]
        self.decay = mem["decay"]
        self.memory_retriever = mem["memory_retriever"]
        self.context_manager = mem["context_manager"]
        self.consolidation_engine = mem["consolidation_engine"]
        self.signal_pool = mem["signal_pool"]

        # 5. 工具注册中心（需 consolidation_engine / signal_pool 已就绪）
        self.tool_registry = create_tool_registry(
            self.config,
            file_registry=self.file_registry,
            consolidation_engine=self.consolidation_engine,
            signal_pool=self.signal_pool,
            todo_registry=self.todo_registry,
            get_session_id=lambda: self._current_session_id,
            memory_retriever=self.memory_retriever,
        )

        # 6. React 循环引擎
        self.react_loop = create_react_loop(
            self.config,
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            audit_logger=self.audit_logger,
            metrics=self.metrics,
            policy_engine=self.policy_engine,
            approval_manager=self.approval_manager,
            guardrail_engine=self.guardrail_engine,
            orchestrator_ref=self,
        )

        # 7. cron 调度依赖（lifespan 装配后注入，默认 None 表示用户会话路径）
        self.cron_scheduler: Optional[Any] = None
        self.cron_tool_registry: Optional[Any] = None

        # 8. 会话状态
        self._pending_interrupt_notices: Dict[str, str] = {}
        self._consecutive_empty_runs: Dict[str, int] = {}
        self._last_session_id: Optional[str] = None

        # 9. 委托组件
        self.session_mgr = SessionManager(
            llm_client=self.llm_client,
            session_logger=self.session_logger,
        )
        self.skill_mgr = SkillManager(
            skill_loader=getattr(self, "skill_loader", None),
        )
        self.cron_isolator = CronIsolator(orchestrator=self)
        self.context_builder = ContextBuilder()
        self.msg_persistence = MessagePersistence(orchestrator=self)
        self.enhanced_context_builder = EnhancedContextBuilder(self)
        self.chat_handler = ChatHandler(self)

        logger.info("Orchestrator 初始化完成")

    async def chat(self, session_id: str, user_input: str,
             cancel_event: Optional[threading.Event] = None,
             reasoning_cfg: Optional["ReasoningConfig"] = None,
             is_cron: bool = False) -> str:
        """主对话入口（委托到 ChatHandler）。"""
        return await self.chat_handler.chat(
            session_id, user_input,
            cancel_event=cancel_event,
            reasoning_cfg=reasoning_cfg,
            is_cron=is_cron,
        )

    async def chat_stream(self, session_id: str, user_input: str,
                          cancel_event: Optional[threading.Event] = None,
                          reasoning_cfg: Optional["ReasoningConfig"] = None,
                          is_cron: bool = False,
                          stream_manager: Optional[Any] = None):
        """流式对话入口（委托到 ChatHandler）。"""
        async for event in self.chat_handler.chat_stream(
            session_id, user_input,
            cancel_event=cancel_event,
            reasoning_cfg=reasoning_cfg,
            is_cron=is_cron,
            stream_manager=stream_manager,
        ):
            yield event

    def apply_condenser_config(
        self, condenser_cfg: Dict[str, Any]
    ) -> Optional[Condenser]:
        """热更新 condenser 配置（enabled / keep_recent_n / keep_first /
        llm_summary_threshold 即时生效；strategy 变更需重启，由 server 层
        :data:`_RESTART_REQUIRED_KEYS` 拦截，调用本方法时 strategy 已保证不变）。

        根据当前 condenser 配置段重建 Condenser 实例并注入到
        :attr:`context_manager` 与 :attr:`condenser`。``enabled: false``
        时置空（向后兼容）。

        参数:
            condenser_cfg: ``config.yaml`` 中 ``memory.condenser`` 段字典。

        返回:
            应用后的 Condenser 实例（禁用时为 None）。
        """
        token_counter = None
        if self.llm_client is not None:
            token_counter = getattr(
                self.llm_client, "count_messages_tokens", None
            )
        new_condenser = (
            create_condenser_from_config(
                condenser_cfg,
                llm_client=self.llm_client,
                token_counter=token_counter,
            )
            if create_condenser_from_config is not None
            else None
        )
        self.condenser = new_condenser
        if self.context_manager is not None:
            self.context_manager.condenser = new_condenser
        return new_condenser

    def close(self) -> None:
        """关闭所有资源。

        依次关闭 chroma_store、session_logger、history_buffer、
        consolidation_engine（若对应对象提供 close 方法）。
        """
        for attr in (
            "chroma_store",
            "session_logger",
            "history_buffer",
            "consolidation_engine",
        ):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            closer = getattr(obj, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as e:
                    logger.warning("关闭 %s 失败: %s", attr, e)

    def shutdown(self) -> None:
        """优雅关闭：冲刷积压记忆，保留 ChromaDB/SQLite 连接供新实例复用。

        供软重启流程调用。与 :meth:`close` 的区别在于：
        - 先调用 ``consolidation_engine.consolidate()`` 将待处理记忆落盘，
          避免重启导致最近对话的记忆丢失。
        - **不关闭 chroma_store / session_logger**（软重启时新 Orchestrator
          会建新实例，但 ChromaDB/SQLite 文件级别并发连接由服务端处理，
          旧实例释放即可，无需主动 close）。
        """
        # 1. 冲刷 consolidation 待处理队列（避免沉淀丢失）
        if self.consolidation_engine is not None:
            try:
                logger.info("shutdown: 正在冲刷 pending consolidation...")
                stats = self.consolidation_engine.consolidate()
                logger.info("shutdown: consolidation 完成: %s", stats)
            except Exception as e:
                logger.warning("shutdown: consolidation 冲刷失败: %s", e)
        # 2. 关闭非数据库组件（history_buffer 无持久化连接，可安全关闭）
        for attr in ("history_buffer",):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            closer = getattr(obj, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as e:
                    logger.warning("shutdown: 关闭 %s 失败: %s", attr, e)
