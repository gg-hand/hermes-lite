"""Orchestrator 组件工厂函数。

将 Orchestrator.__init__ 中按职责分组的组件构造逻辑提取为独立工厂函数，
使 __init__ 简化为纯粹的「调用工厂 + 装配」流程。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 延迟导入：与 orchestrator/__init__.py 保持一致的 try/except 降级策略
try:
    from ..llm.client import LLMClient
    from ..agent.react_loop import ReactLoop
    from ..agent.tool_registry import ToolRegistry
    from ..agent.tools import register_builtin_tools
    from ..agent.tools.plan_tools import register_plan_tools
    from ..agent.tools.memory_tools import register_memory_tools
    from ..storage.history_buffer import HistoryBuffer
    from ..storage.chroma_store import ChromaMemoryStore
    from ..storage.sqlite_log import SessionLogger
    from ..memory.consolidation import ConsolidationEngine
    from ..memory.memory_md import MemoryMdManager
    from ..memory.retrieval import MemoryRetriever
    from ..memory.context_manager import ContextManager
    from ..memory.condenser import create_condenser_from_config, Condenser
    from ..memory.decay import MemoryDecay
    from ..memory.signal_pool import SignalPool
    from ..agent.policy import PolicyEngine
    from ..agent.approval import ApprovalManager
    from ..agent.file_registry import FileOperationRegistry
    from ..guardrails import GuardrailEngine
    from ..tasks.task_manager import TaskManager
    from ..tasks.todo_list import TodoListRegistry
except ImportError:  # pragma: no cover
    try:
        from llm.client import LLMClient  # type: ignore
        from agent.react_loop import ReactLoop  # type: ignore
        from agent.tool_registry import ToolRegistry  # type: ignore
        from agent.tools import register_builtin_tools  # type: ignore
        from agent.tools.plan_tools import register_plan_tools  # type: ignore
        from agent.tools.memory_tools import register_memory_tools  # type: ignore
        from storage.history_buffer import HistoryBuffer  # type: ignore
        from storage.chroma_store import ChromaMemoryStore  # type: ignore
        from storage.sqlite_log import SessionLogger  # type: ignore
        from memory.consolidation import ConsolidationEngine  # type: ignore
        from memory.memory_md import MemoryMdManager  # type: ignore
        from memory.retrieval import MemoryRetriever  # type: ignore
        from memory.context_manager import ContextManager  # type: ignore
        from memory.condenser import create_condenser_from_config, Condenser  # type: ignore
        from memory.decay import MemoryDecay  # type: ignore
        from memory.signal_pool import SignalPool  # type: ignore
        from agent.policy import PolicyEngine  # type: ignore
        from agent.approval import ApprovalManager  # type: ignore
        from agent.file_registry import FileOperationRegistry  # type: ignore
        from guardrails import GuardrailEngine  # type: ignore
        from tasks.task_manager import TaskManager  # type: ignore
        from tasks.todo_list import TodoListRegistry  # type: ignore
    except ImportError:  # pragma: no cover
        LLMClient = None  # type: ignore
        ReactLoop = None  # type: ignore
        ToolRegistry = None  # type: ignore
        register_builtin_tools = None  # type: ignore
        register_plan_tools = None  # type: ignore
        register_memory_tools = None  # type: ignore
        HistoryBuffer = None  # type: ignore
        ChromaMemoryStore = None  # type: ignore
        SessionLogger = None  # type: ignore
        ConsolidationEngine = None  # type: ignore
        MemoryMdManager = None  # type: ignore
        MemoryRetriever = None  # type: ignore
        ContextManager = None  # type: ignore
        create_condenser_from_config = None  # type: ignore
        Condenser = None  # type: ignore
        MemoryDecay = None  # type: ignore
        SignalPool = None  # type: ignore
        PolicyEngine = None  # type: ignore
        ApprovalManager = None  # type: ignore
        FileOperationRegistry = None  # type: ignore
        GuardrailEngine = None  # type: ignore
        TaskManager = None  # type: ignore
        TodoListRegistry = None  # type: ignore


def _extract_message_text(content_blocks: List[Dict[str, Any]]) -> str:
    """从 Anthropic content block 列表提取可读文本，用于归档到向量库。"""

    parts: List[str] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        elif btype == "tool_use":
            parts.append(str(block.get("name", "")))
        elif btype == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                parts.append(_extract_message_text(inner))
    return "".join(parts)


def _build_archive_callback(orchestrator_ref: Any) -> Callable[[str, Dict[str, Any]], None]:
    """构造 FIFO 淘汰消息的归档回调。

    回调在调用时（而非构造时）读取 orchestrator_ref.chroma_store，
    因此即使 history_buffer 装配早于 chroma_store 也能正常工作。
    """

    def _archive_evicted_message(sid: str, msg: Dict[str, Any]) -> None:
        chroma_store = getattr(orchestrator_ref, "chroma_store", None)
        if chroma_store is None:
            return
        if sid.startswith("cron:"):
            return
        raw_content = msg.get("content", "")
        if isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            content = _extract_message_text(raw_content)
        else:
            content = str(raw_content) if raw_content is not None else ""
        if not content:
            return
        metadata = {
            "type": "conversation_turn",
            "session_id": sid,
            "role": msg.get("role", "unknown"),
            "timestamp": msg.get("timestamp", ""),
        }
        chroma_store.add_memory(content, metadata=metadata)

    return _archive_evicted_message


def create_llm_client(config: Dict[str, Any], metrics=None):
    """创建 LLM 客户端。"""
    if LLMClient is None:
        return None
    try:
        return LLMClient(config=config, metrics_collector=metrics)
    except Exception as e:
        logger.warning("LLMClient 初始化失败: %s", e)
        return None


def create_security_subsystem(
    config: Dict[str, Any],
    metrics=None,
    approval_manager=None,
):
    """创建安全子系统组件。

    返回 (policy_engine, approval_manager, guardrail_engine, file_registry)。
    """
    security_cfg = config.get("security", {})

    # FileOperationRegistry
    file_registry = None
    if FileOperationRegistry is not None:
        try:
            file_registry = FileOperationRegistry()
        except Exception as e:
            logger.warning("FileOperationRegistry 初始化失败: %s", e)
            file_registry = None

    # PolicyEngine
    policy_engine = None
    if PolicyEngine is not None:
        try:
            policy_engine = PolicyEngine.from_config(
                security_cfg, file_registry=file_registry
            )
        except Exception as e:
            logger.warning("PolicyEngine 初始化失败，策略拦截禁用: %s", e)
            policy_engine = None

    # ApprovalManager
    if approval_manager is not None:
        am = approval_manager
    elif ApprovalManager is not None:
        try:
            timeout = float(security_cfg.get("approval_timeout_seconds", 300))
            am = ApprovalManager(timeout=timeout, metrics=metrics)
        except Exception as e:
            logger.warning("ApprovalManager 初始化失败: %s", e)
            am = None
    else:
        am = None

    # GuardrailEngine
    guardrail_engine = None
    if GuardrailEngine is not None:
        try:
            guardrail_engine = GuardrailEngine.from_config(config)
            logger.info(
                "GuardrailEngine 装配成功（input_scan=%s, sanitizer=%s, "
                "output_filter=%s）",
                guardrail_engine.input_scan_enabled,
                guardrail_engine.sanitizer_enabled,
                guardrail_engine.output_filter_enabled,
            )
        except Exception as e:
            logger.warning(
                "GuardrailEngine.from_config 失败，降级为 noop: %s", e
            )
            guardrail_engine = GuardrailEngine.create_noop()
    else:
        logger.warning(
            "GuardrailEngine 模块不可用，护栏功能完全禁用"
        )
        guardrail_engine = None

    return policy_engine, am, guardrail_engine, file_registry


def create_task_manager(
    config: Dict[str, Any], task_manager=None
):
    """创建 TaskManager（只读归档）。"""
    if task_manager is not None:
        return task_manager
    if TaskManager is None:
        return None
    tasks_cfg = config.get("tasks", {})
    try:
        return TaskManager(file_path=tasks_cfg.get("file_path", "data/tasks.md"))
    except Exception as e:
        logger.warning("TaskManager 初始化失败: %s", e)
        return None


def create_todo_registry(config: Dict[str, Any]):
    """创建 TodoListRegistry（会话级 plan 模式数据模型）。"""
    if TodoListRegistry is None:
        return None
    history_config = config.get("history", {})
    persistence_dir = history_config.get("persistence_dir", "data/history")
    try:
        return TodoListRegistry(persistence_dir=persistence_dir)
    except Exception as e:
        logger.warning("TodoListRegistry 初始化失败: %s", e)
        return None


def create_memory_subsystem(
    config: Dict[str, Any],
    llm_client=None,
    metrics=None,
    orchestrator_ref=None,
):
    """创建记忆子系统组件。

    返回 dict，包含 history_buffer / chroma_store / session_logger /
    memory_md_manager / memory_retriever / context_manager /
    consolidation_engine / signal_pool / condenser / decay。
    """
    memory_config = config.get("memory", {})
    storage_config = config.get("storage", {})
    history_config = config.get("history", {})
    persistence_dir = history_config.get("persistence_dir", "data/history")

    # HistoryBuffer
    history_buffer = None
    if HistoryBuffer is not None:
        try:
            archive_turns_on_evict = bool(
                memory_config.get("archive_turns_on_evict", True)
            )
            archive_callback = None
            if archive_turns_on_evict and orchestrator_ref is not None:
                archive_callback = _build_archive_callback(orchestrator_ref)
            history_buffer = HistoryBuffer(
                max_turns=int(memory_config.get("history_max_turns", 50)),
                archive_callback=archive_callback,
                persistence_dir=persistence_dir,
            )
        except Exception as e:
            logger.warning("HistoryBuffer 初始化失败，降级为 None: %s", e)
            history_buffer = None

    # Condenser
    condenser = None
    if create_condenser_from_config is not None:
        try:
            token_counter = None
            if llm_client is not None:
                token_counter = getattr(
                    llm_client, "count_messages_tokens", None
                )
            condenser = create_condenser_from_config(
                memory_config.get("condenser"),
                llm_client=llm_client,
                token_counter=token_counter,
            )
        except Exception as e:
            logger.warning("Condenser 初始化失败，降级为 None: %s", e)
            condenser = None

    # ChromaMemoryStore
    chroma_store = None
    try:
        chroma_store = ChromaMemoryStore(
            persist_path=memory_config.get("chroma_path", "data/chroma")
        )
    except Exception as e:
        logger.warning("ChromaMemoryStore 初始化失败: %s", e)
        chroma_store = None

    # SessionLogger
    session_logger = None
    try:
        session_logger = SessionLogger(
            db_path=storage_config.get("sqlite_path", "data/sessions.db")
        )
    except Exception as e:
        logger.warning("SessionLogger 初始化失败: %s", e)
        session_logger = None

    # MemoryMdManager
    memory_md_manager = None
    if MemoryMdManager is not None:
        try:
            memory_md_manager = MemoryMdManager(
                file_path=memory_config.get("memory_md_path", "data/memory.md"),
            )
        except Exception as e:
            logger.warning("MemoryMdManager 初始化失败: %s", e)
            memory_md_manager = None

    # MemoryDecay
    decay = None
    if MemoryDecay is not None:
        try:
            decay = MemoryDecay(
                decay_rate=float(memory_config.get("decay_rate", 0.01)),
                frequency_weight=float(
                    memory_config.get("frequency_weight", 0.5)
                ),
            )
        except Exception as e:
            logger.warning("MemoryDecay 初始化失败，降级为 None: %s", e)
            decay = None

    # MemoryRetriever
    memory_retriever = None
    if (
        MemoryRetriever is not None
        and chroma_store is not None
        and memory_md_manager is not None
    ):
        try:
            memory_retriever = MemoryRetriever(
                chroma_store=chroma_store,
                memory_md_manager=memory_md_manager,
                llm_client=llm_client,
                top_k=int(memory_config.get("retrieval_top_k", 5)),
                decay=decay,
                relevance_threshold=float(
                    memory_config.get("retrieval_relevance_threshold", 0.6)
                ),
            )
        except Exception as e:
            logger.warning("MemoryRetriever 初始化失败: %s", e)
            memory_retriever = None

    # ContextManager
    context_manager = None
    if ContextManager is not None:
        try:
            context_manager = ContextManager(
                tool_registry=None,
                memory_md_manager=memory_md_manager,
                memory_retriever=memory_retriever,
                history_buffer=history_buffer,
                condenser=condenser,
                decay=decay,
            )
        except Exception as e:
            logger.warning("ContextManager 初始化失败: %s", e)
            context_manager = None

    # ConsolidationEngine
    consolidation_engine = None
    if ConsolidationEngine is not None and chroma_store is not None:
        try:
            memory_md_writer = None
            if memory_md_manager is not None:
                memory_md_writer = memory_md_manager.async_write
            consolidation_engine = ConsolidationEngine(
                llm_client=llm_client,
                chroma_store=chroma_store,
                memory_md_writer=memory_md_writer,
                threshold=int(
                    memory_config.get("consolidation_threshold", 15)
                ),
                dedup_threshold=float(
                    memory_config.get("dedup_similarity_threshold", 0.85)
                ),
                memory_md_manager=memory_md_manager,
                surprise_gate_enabled=bool(
                    memory_config.get("surprise_gate_enabled", True)
                ),
                surprise_similarity_threshold=float(
                    memory_config.get("surprise_similarity_threshold", 0.85)
                ),
                surprise_skip_threshold=float(
                    memory_config.get("surprise_skip_threshold", 0.92)
                ),
            )
        except Exception as e:
            logger.warning(
                "ConsolidationEngine 初始化失败，使用占位: %s", e
            )
            consolidation_engine = None

    # SignalPool
    signal_pool = None
    if (
        SignalPool is not None
        and consolidation_engine is not None
        and memory_md_manager is not None
    ):
        try:
            pool_path = Path(
                memory_config.get(
                    "signal_pool_path", "data/profile_signal_pool.json"
                )
            )
            profile_path = memory_md_manager.file_path
            signal_pool = SignalPool(
                pool_path=pool_path,
                consolidation_engine=consolidation_engine,
                profile_path=profile_path,
            )
            consolidation_engine.signal_pool = signal_pool
            signal_pool.flush_triggered_signals()
        except Exception as e:
            logger.warning(
                "SignalPool 初始化失败，降级为 None: %s", e
            )
            signal_pool = None

    return {
        "history_buffer": history_buffer,
        "condenser": condenser,
        "chroma_store": chroma_store,
        "session_logger": session_logger,
        "memory_md_manager": memory_md_manager,
        "decay": decay,
        "memory_retriever": memory_retriever,
        "context_manager": context_manager,
        "consolidation_engine": consolidation_engine,
        "signal_pool": signal_pool,
    }


def create_tool_registry(
    config: Dict[str, Any],
    file_registry=None,
    consolidation_engine=None,
    signal_pool=None,
    todo_registry=None,
    get_session_id=None,
    memory_retriever=None,
):
    """创建工具注册中心并注册内置工具。"""
    tools_config = config.get("tools", {})
    tool_registry = None
    if ToolRegistry is None:
        return None
    try:
        tool_registry = ToolRegistry(
            defer_loading_threshold=int(
                tools_config.get("defer_loading_threshold", 20)
            ),
        )
    except Exception as e:
        logger.warning(
            "ToolRegistry 初始化失败，降级为纯对话模式: %s", e
        )
        return None

    # 注册 plan 工具
    if todo_registry is not None and register_plan_tools is not None:
        try:
            register_plan_tools(
                tool_registry,
                todo_registry,
                get_session_id,
            )
        except Exception as e:
            logger.warning("注册 plan 工具失败: %s", e)

    # 注册内置工具
    if register_builtin_tools is not None:
        try:
            register_builtin_tools(
                tool_registry,
                file_registry=file_registry,
                get_session_id=get_session_id,
                consolidation_engine=consolidation_engine,
                signal_pool=signal_pool,
            )
        except Exception as e:
            logger.warning("注册内置工具失败: %s", e)

    # 注册记忆管理工具
    chroma_store = getattr(consolidation_engine, "chroma_store", None) if consolidation_engine else None
    if (
        chroma_store is not None
        and consolidation_engine is not None
        and register_memory_tools is not None
    ):
        try:
            register_memory_tools(
                tool_registry,
                chroma_store,
                consolidation_engine,
                get_session_id,
                memory_retriever=memory_retriever,
            )
        except Exception as e:
            logger.warning("注册记忆管理工具失败: %s", e)

    return tool_registry


def create_react_loop(
    config: Dict[str, Any],
    llm_client=None,
    tool_registry=None,
    audit_logger=None,
    metrics=None,
    policy_engine=None,
    approval_manager=None,
    guardrail_engine=None,
    orchestrator_ref=None,
):
    """创建 React 循环引擎。"""
    if ReactLoop is None:
        return None
    tools_config = config.get("tools", {})
    max_loops = int(tools_config.get("max_react_loops", 50))
    return ReactLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        max_loops=max_loops,
        audit_logger=audit_logger,
        metrics=metrics,
        policy_engine=policy_engine,
        approval_manager=approval_manager,
        guardrail_engine=guardrail_engine,
        orchestrator_ref=orchestrator_ref,
    )
