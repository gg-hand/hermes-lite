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


def _extract_message_text(content_blocks: List[Dict[str, Any]]) -> str:
    """从 Anthropic content block 列表提取可读文本，用于归档到向量库。

    将 text / tool_use / tool_result 块拼接为纯文本，避免 ``str(list)``
    产生的 Python repr 污染 ChromaMemoryStore 的语义检索。
    """
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
        self.metrics: Optional["MetricsCollector"] = metrics
        self.audit_logger: Optional["AuditLogger"] = audit_logger
        self.config: Dict[str, Any] = load_config(config_path)

        memory_config: Dict[str, Any] = self.config.get("memory", {})
        storage_config: Dict[str, Any] = self.config.get("storage", {})
        tools_config: Dict[str, Any] = self.config.get("tools", {})
        security_cfg: Dict[str, Any] = self.config.get("security", {})
        tasks_cfg: Dict[str, Any] = self.config.get("tasks", {})
        llm_config: Dict[str, Any] = self.config.get("llm", {})
        # history 段：JSONL 持久化目录（相对 cwd），配置缺失时默认 data/history
        history_config: Dict[str, Any] = self.config.get("history", {})
        # cron 段：ops-reliability-uplift Task 7。inject_history 控制是否注入上次 run
        # 的 summary 摘要到 cron 上下文（默认 true，可热更新即时生效）
        cron_cfg: Dict[str, Any] = self.config.get("cron", {})
        self.cron_inject_history_enabled: bool = bool(
            cron_cfg.get("inject_history", True)
        )

        # 1. LLM 客户端
        self.llm_client = LLMClient(config=self.config, metrics_collector=self.metrics)

        # 2. 工具注册中心（可选，缺失时降级为纯对话模式）
        self.tool_registry: Optional[ToolRegistry] = None
        # Phase 5 v2: FileOperationRegistry 会话级文件操作记录（纯内存对象，
        # 不持久化，不依赖任何配置）。注入到 PolicyEngine 与 builtin_tools，
        # 使 write_file / delete_file 走 v2 参数感知决策表。
        self.file_registry: Optional["FileOperationRegistry"] = None
        if FileOperationRegistry is not None:
            try:
                self.file_registry = FileOperationRegistry()
            except Exception as e:
                logger.warning("FileOperationRegistry 初始化失败: %s", e)
                self.file_registry = None

        if ToolRegistry is not None:
            try:
                self.tool_registry = ToolRegistry(
                    defer_loading_threshold=int(
                        tools_config.get("defer_loading_threshold", 20)
                    ),
                )
                # 注：register_builtin_tools 调用推迟到 consolidation_engine
                # 创建之后（第 10 节），以便注入 consolidation_engine 注册
                # update_profile 工具（延迟合并写入策略）。
            except Exception as e:
                logger.warning(
                    "ToolRegistry 初始化失败，降级为纯对话模式: %s", e
                )
                self.tool_registry = None

        # Phase 6: TaskManager 装配（只读归档，缺失时降级）
        # 保留 task_manager 用于注入历史归档进度到 LLM 上下文（list_tasks /
        # get_progress_summary 等只读接口）。写接口已移除，plan 模式由
        # 下面的 TodoListRegistry 承载。
        self.task_manager: Optional["TaskManager"] = None
        if task_manager is not None:
            self.task_manager = task_manager
        elif TaskManager is not None:
            try:
                self.task_manager = TaskManager(
                    file_path=tasks_cfg.get("file_path", "data/tasks.md")
                )
            except Exception as e:
                logger.warning("TaskManager 初始化失败: %s", e)
                self.task_manager = None

        # Phase 6: TodoListRegistry 装配（会话级 plan 模式数据模型）
        # 启用磁盘持久化：传入 persistence_dir，重启后可懒加载恢复 plan。
        # 失败时降级为 None，跳过 plan 工具注册。
        # persistence_dir 提前读取（复用给下方 HistoryBuffer，避免重复读）。
        persistence_dir = history_config.get(
            "persistence_dir", "data/history"
        )
        self.todo_registry: Optional[TodoListRegistry] = None
        if TodoListRegistry is not None:
            try:
                self.todo_registry = TodoListRegistry(
                    persistence_dir=persistence_dir
                )
            except Exception as e:
                logger.warning("TodoListRegistry 初始化失败: %s", e)
                self.todo_registry = None

        # 当前请求的 session_id，供 plan 工具通过 get_session_id 回调获取。
        # 在 chat / chat_stream 入口设置，工具执行时由 lambda 读取最新值。
        self._current_session_id: Optional[str] = None

        # Task 5 P1 修复：当前请求的 intent 分类结果，供下游 Task 6 Planning
        # Phase / Task 7 ToolRegistry / Task 8 style_policy 读取。在 chat /
        # chat_stream 入口由 classify_intent 设置，ReactLoop 内通过
        # orchestrator_ref 弱引用访问。
        self._current_intent_result: Optional["IntentClassificationResult"] = None

        # P1-3: Skill 激活状态管理已迁移到 SkillManager（skill_mgr）。

        # 注册 plan 工具到 ToolRegistry（plan_task / update_todo，Core Tier）
        # get_session_id 回调通过闭包捕获 self，执行时读取 self._current_session_id
        if (
            self.todo_registry is not None
            and self.tool_registry is not None
            and register_plan_tools is not None
        ):
            try:
                register_plan_tools(
                    self.tool_registry,
                    self.todo_registry,
                    lambda: self._current_session_id,  # get_session_id 回调
                )
            except Exception as e:
                logger.warning("注册 plan 工具失败: %s", e)

        # 3. React 循环引擎（注入 tool_registry，为 None 时纯对话模式）
        # Phase 5: 在创建 react_loop 之前先创建 PolicyEngine 与 ApprovalManager
        # Phase 5 v2: 透传 file_registry 给 PolicyEngine，启用参数感知决策
        self.policy_engine: Optional["PolicyEngine"] = None
        if PolicyEngine is not None:
            try:
                self.policy_engine = PolicyEngine.from_config(
                    security_cfg, file_registry=self.file_registry
                )
            except Exception as e:
                logger.warning("PolicyEngine 初始化失败，策略拦截禁用: %s", e)
                self.policy_engine = None

        if approval_manager is not None:
            self.approval_manager: Optional["ApprovalManager"] = approval_manager
        elif ApprovalManager is not None:
            try:
                timeout = float(security_cfg.get("approval_timeout_seconds", 300))
                # Phase 2 反馈监控：fallback 路径也注入 metrics
                self.approval_manager = ApprovalManager(
                    timeout=timeout,
                    metrics=self.metrics,
                )
            except Exception as e:
                logger.warning("ApprovalManager 初始化失败: %s", e)
                self.approval_manager = None
        else:
            self.approval_manager = None

        # Phase 9 Task 6: 装配 GuardrailEngine（fail-open 软护栏）
        # 从 config["guardrails"] 构造，失败时降级为 noop（所有方法空操作），
        # 保证 react_loop 不抛空指针且主流程不被护栏故障阻塞。
        self.guardrail_engine: Optional["GuardrailEngine"] = None
        if GuardrailEngine is not None:
            try:
                self.guardrail_engine = GuardrailEngine.from_config(self.config)
                logger.info(
                    "GuardrailEngine 装配成功（input_scan=%s, sanitizer=%s, "
                    "output_filter=%s）",
                    self.guardrail_engine.input_scan_enabled,
                    self.guardrail_engine.sanitizer_enabled,
                    self.guardrail_engine.output_filter_enabled,
                )
            except Exception as e:
                logger.warning(
                    "GuardrailEngine.from_config 失败，降级为 noop: %s", e
                )
                self.guardrail_engine = GuardrailEngine.create_noop()
        else:
            # 模块不可用时也降级为 noop（避免 None 检查）
            logger.warning(
                "GuardrailEngine 模块不可用，护栏功能完全禁用"
            )
            self.guardrail_engine = None

        max_loops = int(tools_config.get("max_react_loops", 50))
        self.react_loop = ReactLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_loops=max_loops,
            audit_logger=self.audit_logger,
            metrics=self.metrics,
            policy_engine=self.policy_engine,
            approval_manager=self.approval_manager,
            guardrail_engine=self.guardrail_engine,
            orchestrator_ref=self,
        )

        # Phase 8 Task 5.7: cron 调度路径所需的依赖（lifespan 装配后注入，
        # 默认 None 表示用户会话路径，cron 会话路径在 server.py lifespan
        # 中通过 set_cron_dependencies 注入）。
        # - cron_scheduler: 用于读取调度项的 active_tools_snapshot 字段
        #   做请求级工具过滤
        # - cron_tool_registry: 独立 registry，提供 cron_tool schema 与
        #   子进程执行；注入到 react_loop 用于工具调用派发
        self.cron_scheduler: Optional[Any] = None
        self.cron_tool_registry: Optional[Any] = None

        # 4. 历史缓冲（可选模块，缺失时降级）
        # archive_turns_on_evict=true 时，FIFO 淘汰的消息通过回调归档到
        # ChromaMemoryStore（type=conversation_turn），可被后续向量检索找回。
        # 配置可热更新（每次 Orchestrator 初始化读取最新值，运行时即时生效）。
        self.history_buffer: Optional[HistoryBuffer] = None
        if HistoryBuffer is not None:
            try:
                archive_turns_on_evict = bool(
                    memory_config.get("archive_turns_on_evict", True)
                )
                archive_callback = (
                    self._build_archive_callback()
                    if archive_turns_on_evict
                    else None
                )
                # JSONL 持久化目录：已在 Phase 6 提前读取（persistence_dir），
                # 复用同一变量传给 HistoryBuffer。
                # HistoryBuffer 内部 os.makedirs(exist_ok=True) 处理目录创建。
                # 变更此项需重启服务（见 server.py _RESTART_REQUIRED_KEYS）。
                self.history_buffer = HistoryBuffer(
                    max_turns=int(memory_config.get("history_max_turns", 50)),
                    archive_callback=archive_callback,
                    persistence_dir=persistence_dir,
                )
            except Exception as e:
                logger.warning("HistoryBuffer 初始化失败，降级为 None: %s", e)
                self.history_buffer = None

        # 中断通知暂存：session_id -> notice_text
        # 不再直接写入 history_buffer，避免连续 user 消息违反 API 约束
        self._pending_interrupt_notices: Dict[str, str] = {}

        # 规范 2: 跨 run 空回复计数器（进程内持久，跨 run 累积）
        # 仅 termination_reason=="empty_response" 时累加；
        # tool_permanent_fail / user_cancel 不计数（避免级联误判）。
        # count >= 2 时直接返回友好提示，不再调 react_loop；
        # count == 1 时追加纠偏提示到 system_text（不持久化）。
        self._consecutive_empty_runs: Dict[str, int] = {}

        # 会话标题缓存和异步任务管理已迁移到 SessionManager（session_mgr）。
        # 以下字段保留为向后兼容属性（property 代理到 session_mgr），
        # 避免外部访问 orch._titled_sessions 时 AttributeError。

        # 4.5 Condenser（可选模块，缺失时降级为 None，history 原样传入）
        # 在 LLM 调用前对 history 做压缩（masking 旧 tool_result / LLM 摘要），
        # 减少上下文 token 消耗同时保留工具调用元信息。
        # strategy 变更需重启；enabled/keep_recent_n/keep_first/
        # llm_summary_threshold 可热更新（见 apply_condenser_config）。
        self.condenser: Optional[Condenser] = None
        if create_condenser_from_config is not None:
            try:
                token_counter = None
                if self.llm_client is not None:
                    token_counter = getattr(
                        self.llm_client, "count_messages_tokens", None
                    )
                self.condenser = create_condenser_from_config(
                    memory_config.get("condenser"),
                    llm_client=self.llm_client,
                    token_counter=token_counter,
                )
            except Exception as e:
                logger.warning("Condenser 初始化失败，降级为 None: %s", e)
                self.condenser = None

        # 5. 长期记忆向量库（可选）
        self.chroma_store: Optional[ChromaMemoryStore] = None
        try:
            self.chroma_store = ChromaMemoryStore(
                persist_path=memory_config.get("chroma_path", "data/chroma")
            )
        except Exception as e:
            logger.warning("ChromaMemoryStore 初始化失败: %s", e)
            self.chroma_store = None

        # 6. 会话日志（可选）
        self.session_logger: Optional["SessionStoreProtocol"] = None
        try:
            self.session_logger = SessionLogger(
                db_path=storage_config.get("sqlite_path", "data/sessions.db")
            )
        except Exception as e:
            logger.warning("SessionLogger 初始化失败: %s", e)
            self.session_logger = None

        # 7. 用户画像 memory.md 管理器（可选）
        self.memory_md_manager: Optional[MemoryMdManager] = None
        if MemoryMdManager is not None:
            try:
                self.memory_md_manager = MemoryMdManager(
                    file_path=memory_config.get("memory_md_path", "data/memory.md"),
                )
            except Exception as e:
                logger.warning("MemoryMdManager 初始化失败: %s", e)
                self.memory_md_manager = None

        # 7.5 MemoryDecay（可选，三因子衰减：recency × frequency × importance）
        # Phase 7 Task 1: 为 MemoryRetriever 提供 decayed_importance 计算，
        # 让常用知识不被老记忆挤掉。配置可热更新（_RUNTIME_HOTUPDATE_MAP
        # 直接修改 decay_rate / frequency_weight 属性，无需重启）。
        self.decay: Optional[MemoryDecay] = None
        if MemoryDecay is not None:
            try:
                self.decay = MemoryDecay(
                    decay_rate=float(memory_config.get("decay_rate", 0.01)),
                    frequency_weight=float(
                        memory_config.get("frequency_weight", 0.5)
                    ),
                )
            except Exception as e:
                logger.warning("MemoryDecay 初始化失败，降级为 None: %s", e)
                self.decay = None

        # 8. 记忆检索器（可选，依赖 chroma_store 与 memory_md_manager）
        # Phase 7 Task 1: 注入 decay 实例，启用三因子衰减排序；
        # decay 为 None 时 MemoryRetriever 回退到静态 importance（向后兼容）。
        self.memory_retriever: Optional[MemoryRetriever] = None
        if (
            MemoryRetriever is not None
            and self.chroma_store is not None
            and self.memory_md_manager is not None
        ):
            try:
                self.memory_retriever = MemoryRetriever(
                    chroma_store=self.chroma_store,
                    memory_md_manager=self.memory_md_manager,
                    llm_client=self.llm_client,
                    top_k=int(memory_config.get("retrieval_top_k", 5)),
                    decay=self.decay,
                    relevance_threshold=float(
                        memory_config.get("retrieval_relevance_threshold", 0.6)
                    ),
                )
            except Exception as e:
                logger.warning("MemoryRetriever 初始化失败: %s", e)
                self.memory_retriever = None

        # 9. Prompt 上下文管理器（可选，整合工具 / 画像 / 检索 / 历史）
        # Phase 7 Task 1: 透传 decay 实例（保留属性便于热更新与一致性装配）
        self.context_manager: Optional[ContextManager] = None
        if ContextManager is not None:
            try:
                self.context_manager = ContextManager(
                    tool_registry=self.tool_registry,
                    memory_md_manager=self.memory_md_manager,
                    memory_retriever=self.memory_retriever,
                    history_buffer=self.history_buffer,
                    condenser=self.condenser,
                    decay=self.decay,
                )
            except Exception as e:
                logger.warning("ContextManager 初始化失败: %s", e)
                self.context_manager = None

        # 10. Consolidation 引擎（可选模块，缺失时跳过 consolidation）
        # 需要 chroma_store；memory_md_writer 由 memory_md_manager.async_write 提供；
        # 注入 memory_md_manager 用于在 consolidate 时合并 pending_profile_updates
        # 队列（update_profile 工具的延迟合并写入）。
        self.consolidation_engine: Optional[ConsolidationEngine] = None
        if ConsolidationEngine is not None and self.chroma_store is not None:
            try:
                memory_md_writer = None
                if self.memory_md_manager is not None:
                    memory_md_writer = self.memory_md_manager.async_write
                self.consolidation_engine = ConsolidationEngine(
                    llm_client=self.llm_client,
                    chroma_store=self.chroma_store,
                    memory_md_writer=memory_md_writer,
                    threshold=int(
                        memory_config.get("consolidation_threshold", 15)
                    ),
                    dedup_threshold=float(
                        memory_config.get("dedup_similarity_threshold", 0.85)
                    ),
                    memory_md_manager=self.memory_md_manager,
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
                self.consolidation_engine = None

        # 10.2 信号池（可选，依赖 consolidation_engine 与 memory_md_manager）
        # L1/L2/L3 三层摘取路径的统一入口：信号入池前查重画像 → 情感增强 →
        # 相似去重合并 → 计数累加 → 达阈值（7）入 pending 队列写入画像。
        # signal_pool_path 默认 data/profile_signal_pool.json。
        self.signal_pool: Optional[SignalPool] = None
        if (
            SignalPool is not None
            and self.consolidation_engine is not None
            and self.memory_md_manager is not None
        ):
            try:
                from pathlib import Path as _Path
                pool_path = _Path(
                    memory_config.get(
                        "signal_pool_path", "data/profile_signal_pool.json"
                    )
                )
                profile_path = self.memory_md_manager.file_path
                self.signal_pool = SignalPool(
                    pool_path=pool_path,
                    consolidation_engine=self.consolidation_engine,
                    profile_path=profile_path,
                )
                # 反向注入到 consolidation_engine，使 L3 提取的 user_profile
                # 事实走信号池累积（weight=2）
                self.consolidation_engine.signal_pool = self.signal_pool
                # 启动后处理存量 triggered 信号：让它们重新入队 + 异步触发
                # consolidate 立即写入画像，避免永远卡在池中（count 已达阈值
                # 但未消费的情况）。
                self.signal_pool.flush_triggered_signals()
            except Exception as e:
                logger.warning(
                    "SignalPool 初始化失败，降级为 None: %s", e
                )
                self.signal_pool = None

        # 10.5 注册内置工具（推迟到此处，确保 consolidation_engine 已就绪）
        # 注入 file_registry / get_session_id / consolidation_engine / signal_pool：
        # - file_registry: write_file / delete_file 走 v2 closure 版本
        # - consolidation_engine: 注册 update_profile 工具（延迟合并写入）
        # - signal_pool: update_profile 的 add 操作走信号池累积
        # consolidation_engine 为 None 时不注册 update_profile（向后兼容）。
        if self.tool_registry is not None and register_builtin_tools is not None:
            try:
                register_builtin_tools(
                    self.tool_registry,
                    file_registry=self.file_registry,
                    get_session_id=lambda: self._current_session_id,
                    consolidation_engine=self.consolidation_engine,
                    signal_pool=self.signal_pool,
                )
            except Exception as e:
                logger.warning(
                    "注册内置工具失败: %s", e
                )

        # 10.6 注册记忆管理工具（Phase 7 Task 3）
        # 注入 chroma_store / consolidation_engine / get_session_id：
        # - chroma_store: search_memory 检索向量库
        # - consolidation_engine: delete_memory / update_memory 入队 pending_memory_ops
        # chroma_store 或 consolidation_engine 为 None 时不注册（向后兼容）。
        # 使用 getattr 防御测试中通过 __new__ 绕过 __init__ 的场景。
        _chroma_store = getattr(self, "chroma_store", None)
        _consolidation_engine = getattr(self, "consolidation_engine", None)
        _tool_registry = getattr(self, "tool_registry", None)
        if (
            _tool_registry is not None
            and _chroma_store is not None
            and _consolidation_engine is not None
            and register_memory_tools is not None
        ):
            try:
                register_memory_tools(
                    _tool_registry,
                    _chroma_store,
                    _consolidation_engine,
                    lambda: getattr(self, "_current_session_id", None),
                    memory_retriever=self.memory_retriever,
                )
            except Exception as e:
                logger.warning(
                    "注册记忆管理工具失败: %s", e
                )

        logger.info("Orchestrator 初始化完成")

        # 会话切换检测：记录上一次对话的 session_id，
        # 若本次 session_id 不同且 pending_messages 非空，先 flush 旧会话的沉淀
        self._last_session_id: Optional[str] = None

        # SessionManager: 会话管理（创建/标题生成）委托
        self.session_mgr = SessionManager(
            llm_client=self.llm_client,
            session_logger=self.session_logger,
        )

        # SkillManager: 技能激活/停用/上下文构建委托
        self.skill_mgr = SkillManager(
            skill_loader=getattr(self, "skill_loader", None),
        )

        # CronIsolator: cron 上下文隔离委托（方法对象模式，持有 self 引用）
        self.cron_isolator = CronIsolator(orchestrator=self)

        # ContextBuilder: 环境信息 + TODO 格式化 + 续接消息委托
        self.context_builder = ContextBuilder()

        # MessagePersistence: 消息持久化 + 中断通知 + 历史清理 + 沉淀触发委托
        # （方法对象模式，持有 self 引用）
        self.msg_persistence = MessagePersistence(orchestrator=self)

        # EnhancedContextBuilder: 增强上下文构建（画像/记忆/环境/任务/TodoList/Skill）
        self.enhanced_context_builder = EnhancedContextBuilder(self)

        # ChatHandler: 非流式对话处理
        self.chat_handler = ChatHandler(self)

    def _build_archive_callback(
        self,
    ) -> Callable[[str, Dict[str, Any]], None]:
        """构造 FIFO 淘汰消息的归档回调。

        返回的回调签名：(session_id, evicted_message) -> None。
        回调将被淘汰的消息写入 chroma_store（type=conversation_turn），
        以便后续通过向量检索找回相关历史。

        回调在调用时（而非构造时）读取 self.chroma_store，
        因此即使 history_buffer 装配早于 chroma_store 也能正常工作。
        chroma_store 为 None（初始化失败）或 content 为空时静默跳过；
        add_memory 抛出的异常由 HistoryBuffer.add_message 内部的
        try/except 兜底，不会影响主流程。
        """

        def _archive_evicted_message(sid: str, msg: Dict[str, Any]) -> None:
            """将被淘汰的会话消息归档到向量库。

            ops-reliability-uplift Task 6: cron session 分支已迁移到
            ``CronScheduler._build_cron_archive_callback``，本闭包仅处理
            user session（``type=conversation_turn``，向后兼容）。cron session
            归档改由 scheduler 层调用 ``runs_store.read_last(schedule_id)``
            读取上次 run 的 ``llm_summary`` 字段（精炼摘要而非完整对话），
            避免 history_buffer FIFO 淘汰导致摘要丢失或原文污染。
            """
            # chroma_store 可能在装配 history_buffer 之后才初始化，
            # 因此在回调被调用时再读取属性，初始化失败时静默跳过。
            chroma_store = getattr(self, "chroma_store", None)
            if chroma_store is None:
                return
            # cron session 由 scheduler 层归档（type=summary，非 conversation_turn）
            if sid.startswith("cron:"):
                return
            raw_content = msg.get("content", "")
            # content 可为 str（纯文本）或 list（Anthropic content blocks，
            # 含 tool_use / tool_result）；list 时提取可读文本，避免
            # str(list) 的 Python repr 污染向量库。
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

    async def chat_stream(
        self,
        session_id: str,
        user_input: str,
        cancel_event: Optional[threading.Event] = None,
        reasoning_cfg: Optional["ReasoningConfig"] = None,
        is_cron: bool = False,
        stream_manager: Optional[Any] = None,
    ):
        """流式主对话入口，异步生成器逐个 yield 事件 dict。

        与 :meth:`chat` 等价的会话管理逻辑（历史获取、消息记录、
        history_buffer 更新、consolidation 累加），但调用
        :meth:`ReactLoop.run_stream`，将 LLM 文本增量与工具调用事件
        实时透传给调用方。

        与 :meth:`chat` 不同的是，本方法在流式过程中**实时收集**所有事件
        （text / tool / round_start / done），并在 finally 块中按事件顺序
        **批量写入 session_logger**：
        - 每个 ``tool`` 事件：记录 2 条消息（tool_use + tool_result，附带
          ``tool_name`` / ``tool_call_id`` 字段）
        - 每轮 assistant 文本：在下一轮 ``round_start`` 或 ``done`` 时作为
          一条 assistant 消息提交
        - 兜底：若整个流未发出 ``round_start``（单轮场景）或收集列表末尾
          不是纯文本 assistant 消息，则用 ``response_text`` 补记一条

        这样保证流被中断时（客户端断连 / 异常）finally 块仍会保存已收集
        的消息，避免工具卡片刷新后丢失。

        事件格式（与 ``ReactLoop.run_stream`` 一致，并扩展 todo 事件）：
            - ``{"type": "round_start", "loop_idx": int}``：新一轮循环开始。
            - ``{"type": "text", "text": str}``：LLM 输出的文本增量。
            - ``{"type": "reasoning", "text": str, "signature": str|None}``：
              推理增量（reasoning 模式开启时）。
            - ``{"type": "tool", "name": str, "input": dict, "result": str,
              "is_error": bool, "session_id": str|None}``：工具调用事件。
            - ``{"type": "todo_init", "session_id": str, "todo": dict}``：
              plan_task 工具执行后发射的 todo 初始化事件，``todo`` 为
              TodoList.to_dict() 结果（含 goal / steps / completed）。
            - ``{"type": "todo_update", "session_id": str, "todo": dict}``：
              update_todo 工具执行后发射的 todo 更新事件，``todo`` 为变更后
              的完整 TodoList。
            - ``{"type": "todo_complete", "session_id": str, "todo": dict}``：
              update_todo 后所有 step 均为 completed 时发射的完成事件。
            - ``{"type": "approval_request", "approval_id": str, "tool_name": str,
              "tool_input": dict, "reason": str, "risk_level": str}``：HIL 审批请求事件。
            - ``{"type": "approval_resolved", "approval_id": str, "decision": str,
              "reason": str}``：审批决定事件。
            - ``{"type": "done", "response": str, "messages": list}``：
              整个对话结束事件。

        参数:
            session_id: 会话 ID。
            user_input: 用户输入文本。

        Yields:
            事件 dict。
        """
        # 0. 会话切换检测：若 session_id 变化且 pending 非空，先 flush 旧会话沉淀
        yield {"type": "status", "status": "loading_context", "message": "正在加载上下文..."}
        await self.msg_persistence.maybe_flush_on_switch(session_id)

        # 记录当前 session_id，供 plan 工具通过 get_session_id 回调获取
        self._current_session_id = session_id
        # 重置 intent_result，防止上一轮残留（Task 5 P1 修复）
        self._current_intent_result = None

        # 1. 获取 session 历史
        history: List[Dict[str, Any]] = []
        if self.history_buffer is not None:
            try:
                history = self.history_buffer.get_history(session_id) or []
            except Exception as e:
                logger.warning("从 HistoryBuffer 获取历史失败: %s", e)
                history = []

        # 1.5 消费暂存的中断通知（规范 3 Task 8.3-8.6）
        # 不再字符串拼接到 user_input，改为在 _build_enhanced_context 之后
        # 追加独立 {"role":"system"} 消息到 enhanced_history
        # TTL 清理：超过 5 分钟未消费的通知自动丢弃（Task 8.6）
        pending_notice = self._pending_interrupt_notices.pop(session_id, None)
        pending_notice_content: Optional[str] = None
        if pending_notice is not None:
            notice_age = time.time() - pending_notice.get("timestamp", 0)
            if notice_age > 300:  # 5 分钟 TTL
                logger.info(
                    "中断通知已过期（%.0f秒 > 300秒），丢弃: %s",
                    notice_age, session_id,
                )
            else:
                pending_notice_content = pending_notice.get("content")
                logger.info("准备注入 InterruptNotice 到 history（流式）: %s", session_id)

        # 1.6 安全网：清理历史中的连续 user 消息（兼容旧 JSONL 文件）
        history = MessagePersistence.sanitize_history(history)

        # 2. 流式执行 React 循环，透传事件并收集待持久化的消息
        # 构建含用户画像 + 检索记忆的增强上下文
        # Phase 8 Task 5.7: _build_enhanced_context 返回三元组，第三项为
        # tools_override（用户会话固定 None；cron 会话为请求级过滤后的列表）
        system_text, enhanced_history, tools_override = await self.enhanced_context_builder.build(
            session_id, user_input, history
        )
        # 规范 3 Task 8.4: 追加独立 system 消息到 enhanced_history（流式路径）
        if pending_notice_content is not None:
            enhanced_history = list(enhanced_history) + [
                {"role": "system", "content": pending_notice_content}
            ]
        enhanced_history_len = len(enhanced_history)

        # Phase 9 Task 6 接入点 D: 输入扫描（流式路径）
        # deny 时 yield 一个 error 事件并 return（不进入 run_stream）；
        # suspicious 时放行并记录审计；allow 时正常处理。
        if self.guardrail_engine is not None:
            guardrail_result = self.guardrail_engine.scan_input(user_input)
            if guardrail_result.action == "deny":
                # 审计记录（deny 为高风险）
                if self.audit_logger is not None:
                    self.audit_logger.log_guardrail_decision(
                        layer="input_scan",
                        action="deny",
                        reason=guardrail_result.reason,
                        session_id=session_id,
                        matched_patterns=guardrail_result.matched_patterns,
                        risk_level="high",
                    )
                logger.warning(
                    "输入扫描 deny（流式），拦截会话 %s: %s",
                    session_id,
                    guardrail_result.reason,
                )
                yield {
                    "type": "error",
                    "message": "检测到潜在的安全风险，请重新表述您的请求。",
                    "reason": guardrail_result.reason,
                }
                # 直接 yield done 事件，保证前端流正常结束
                yield self.react_loop._build_done_event(
                    response="检测到潜在的安全风险，请重新表述您的请求。",
                    messages=[],
                    is_complete=True,
                    termination_reason="error",
                )
                return
            # suspicious 或 allow 时放行；suspicious 记录审计（中等风险）
            if (
                guardrail_result.action == "suspicious"
                and self.audit_logger is not None
            ):
                self.audit_logger.log_guardrail_decision(
                    layer="input_scan",
                    action="allow",
                    reason=guardrail_result.reason,
                    session_id=session_id,
                    matched_patterns=guardrail_result.matched_patterns,
                    risk_level="medium",
                )

        # spec agent-metacognition-uplift Task 5: Intent Classifier 前置路由（流式路径）
        # 在 _build_enhanced_context 之后、run_stream 之前调用，对用户输入做
        # 轻量意图分类。classify_intent 不依赖 stream_manager，全链路异步，
        # 失败降级为 SIMPLE_QA，低置信度回退到 MULTI_STEP_TASK。
        # intent_result 保存到 self._current_intent_result，供下游 Task 6/7/8
        # 读取（与 chat() 路径保持一致）。
        # cron 会话为固定调度，跳过意图识别节省 LLM 调用成本。
        intent_result: Optional[IntentClassificationResult] = None
        if is_cron:
            # cron 会话跳过意图识别，记录监控指标
            if self.metrics is not None:
                self.metrics.observe_intent(is_cron_skip=True)
        elif classify_intent is not None and self.llm_client is not None:
            _intent_start = time.monotonic()
            intent_result = await classify_intent(
                llm_client=self.llm_client,
                user_input=user_input,
                history=history,
                cancel_event=cancel_event,
            )
            _intent_latency_ms = (time.monotonic() - _intent_start) * 1000
            logger.info(
                "会话 %s intent_classifier 结果（流式）: %s (confidence=%.2f)",
                session_id,
                intent_result.intent.value,
                intent_result.confidence,
            )
            # 上报 intent 分类监控指标
            if self.metrics is not None:
                self.metrics.observe_intent(
                    intent_type=intent_result.intent.value,
                    confidence=intent_result.confidence,
                    fallback_reason=intent_result.fallback_reason,
                    latency_ms=_intent_latency_ms,
                )
        # P1 修复：透传 intent_result 到实例属性（与 chat() 路径一致）。
        self._current_intent_result = intent_result

        response_text: str = ""
        # done 事件携带的完整 messages（含 history + 本轮新增），
        # 在 finally 块中切出本轮新增部分持久化到 history_buffer。
        # 流中断未收到 done 时保持 None，降级为 user_input + response_text。
        done_messages: Optional[List[Dict[str, Any]]] = None
        # 收集所有要持久化的事件/消息（按时间顺序），每项形如：
        # {"role": "user"/"assistant", "content": str,
        #  "tool_name": Optional[str], "tool_call_id": Optional[str]}
        collected_messages: List[Dict[str, Any]] = []
        # 当前轮的 assistant 文本累加器（在 round_start 时把上一轮累加的文本
        # 作为一条 assistant 消息提交，避免跨轮累加串台）
        current_round_text: str = ""
        # 当前轮的 reasoning（思考）文本累加器，随 assistant 消息一起持久化
        current_round_reasoning: str = ""
        # 工具调用 ID 自增计数器（ReactLoop 当前未在 tool 事件中透传 tool_use_id，
        # 这里用自增 ID 保证 tool_use 与 tool_result 能配对）
        last_tool_use_id_counter: int = 0

        # 规范 2: 跨 run 空回复检测（流式路径）
        # count >= 2 → yield error + done 后 return，不进入 run_stream
        # count == 1 → 追加纠偏提示到 system_text（不持久化，per-call）
        empty_count = self._consecutive_empty_runs.get(session_id, 0)
        if empty_count >= 2:
            logger.warning(
                "会话 %s 连续 %d 次空回复（流式），直接返回友好提示",
                session_id, empty_count,
            )
            yield {
                "type": "error",
                "message": "抱歉，连续两次未能生成回复，可能是模型异常或上下文冲突。请重试或换种问法。",
            }
            yield self.react_loop._build_done_event(
                response="抱歉，连续两次未能生成回复，可能是模型异常或上下文冲突。请重试或换种问法。",
                messages=[],
                is_complete=True,
                termination_reason="error",
            )
            return
        if empty_count == 1:
            system_text = (system_text or "") + (
                "\n\n[系统提示] 上一轮 LLM 返回了空回复。请确保本次明确回应用户问题，"
                "不要返回空内容。"
            )
            logger.info("会话 %s 注入空回复纠偏提示到 system_text（流式）", session_id)

        # 规范 2: 捕获 done 事件的 termination_reason，用于 finally 块计数
        stream_termination_reason: str = "normal"
        try:
            async for event in self.react_loop.run_stream(
                user_input=user_input,
                history=enhanced_history,
                system=system_text,
                session_id=session_id,
                tools_override=tools_override,
                cancel_event=cancel_event,
                reasoning_cfg=reasoning_cfg,
                is_cron=is_cron,
                stream_manager=stream_manager,
            ):
                etype = event.get("type")

                # T13: 对 plan_task / update_todo 工具，获取变更后的 todo_dict，
                # 用于：(1) 持久化 tool_result content 替换为 JSON（供 T14 前端
                # loadMessages 解析重建卡片）；(2) 发射 todo_init / todo_update /
                # todo_complete 事件。初始为 None，仅在 tool 分支内赋值。
                todo_dict: Optional[dict] = None

                # 收集逻辑（同时透传给 server.py）
                if etype == "round_start":
                    # 新一轮开始：把上一轮累加的 assistant 文本作为一条消息提交
                    if current_round_text:
                        msg_dict: Dict[str, Any] = {
                            "role": "assistant", "content": current_round_text
                        }
                        if current_round_reasoning:
                            msg_dict["reasoning"] = current_round_reasoning
                        collected_messages.append(msg_dict)
                        current_round_text = ""
                        current_round_reasoning = ""
                elif etype == "reasoning":
                    # 累加到当前轮的 reasoning 文本
                    current_round_reasoning += event.get("text", "")
                elif etype == "text":
                    # 累加到当前轮的 assistant 文本
                    current_round_text += event.get("text", "")
                elif etype == "tool":
                    # 先提交当前轮累加的 assistant 文本（LLM 先输出文本，
                    # 再调用工具），保证持久化顺序与事件实际顺序一致：
                    # text → tool_use → tool_result
                    if current_round_text:
                        msg_dict = {
                            "role": "assistant", "content": current_round_text
                        }
                        if current_round_reasoning:
                            msg_dict["reasoning"] = current_round_reasoning
                        collected_messages.append(msg_dict)
                        current_round_text = ""
                        current_round_reasoning = ""
                    # 工具调用：记录 2 条消息（tool_use + tool_result）
                    tool_name = event.get("name", "")
                    tool_input = event.get("input", {}) or {}
                    tool_result = event.get("result", "")
                    tool_is_error = bool(event.get("is_error", False))
                    # 优先用事件中的 tool_use_id（ReactLoop 当前未透传），
                    # 否则用自增 ID
                    tool_call_id = (
                        event.get("tool_use_id")
                        or f"tool_{last_tool_use_id_counter}"
                    )
                    last_tool_use_id_counter += 1

                    # T13: plan_task / update_todo 工具调用后，获取 todo_dict
                    # 用于持久化 content 替换与事件发射。todo_registry 为 None
                    # 或 session 无 plan 时降级跳过（todo_dict 保持 None）。
                    if (
                        tool_name in ("plan_create", "plan_update_step")
                        and self.todo_registry is not None
                    ):
                        try:
                            todo_dict = self.todo_registry.get_todo_dict(
                                session_id
                            )
                        except Exception as e:
                            logger.warning("获取 todo dict 失败: %s", e)
                            todo_dict = None

                    # tool_use 消息（assistant 角色，记录调用）
                    collected_messages.append(
                        {
                            "role": "assistant",
                            "content": f"调用工具 {tool_name}: "
                            f"{json.dumps(tool_input, ensure_ascii=False)}",
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                        }
                    )
                    # tool_result 消息（user 角色，记录返回结果）
                    # T13: 对 plan_task / update_todo，持久化的 content 替换为
                    # todo_dict 的 JSON 字符串（前端 loadMessages 可解析重建卡片），
                    # 而非工具返回给 LLM 的简短字符串。
                    if (
                        todo_dict is not None
                        and tool_name in ("plan_create", "plan_update_step")
                    ):
                        persisted_content = json.dumps(
                            todo_dict, ensure_ascii=False
                        )
                    else:
                        persisted_content = tool_result
                    collected_messages.append(
                        {
                            "role": "user",
                            "content": persisted_content,
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "is_error": tool_is_error,
                        }
                    )
                elif etype == "done":
                    # done 事件触发时，把最后一轮累加的文本也提交
                    if current_round_text:
                        msg_dict = {
                            "role": "assistant", "content": current_round_text
                        }
                        if current_round_reasoning:
                            msg_dict["reasoning"] = current_round_reasoning
                        collected_messages.append(msg_dict)
                        current_round_text = ""
                        current_round_reasoning = ""
                    response_text = event.get("response", "") or ""
                    # 捕获完整 messages（含 history + 本轮新增），用于
                    # 在 finally 块中切出本轮新增部分持久化到 history_buffer
                    done_messages = event.get("messages")
                    # Phase 9 Task 7.5: 读取 is_complete 字段（流式路径）
                    # TODO Phase 9+: 流式路径暂未实装自动续接（续接需在
                    # async for 外层包装 while 循环，并管理 collected_messages
                    # 的跨轮累加与 done_messages 切片边界，复杂度较高）。
                    # 当前仅记录 is_complete 供日志观察，达到 max_loops 或卡死
                    # 时流式路径直接结束（与旧行为一致），用户可手动发"继续"
                    # 触发下一轮 chat_stream。非流式路径（chat()）已完整实装
                    # 自动续接 + 总熔断 200 轮。
                    done_is_complete = event.get("is_complete", True)
                    if not done_is_complete:
                        logger.info(
                            "流式 React 循环未自然完成（is_complete=False），"
                            "流式路径暂不支持自动续接，需用户手动继续"
                        )
                    # 规范 2: 捕获 termination_reason，用于 finally 块空回复计数
                    stream_termination_reason = event.get(
                        "termination_reason", "normal"
                    )

                    # 在 done 事件捕获后、yield 前触发标题生成（避免 finally
                    # 块在 GeneratorExit 期间创建 task 失败被静默吞掉的问题）
                    self.session_mgr.generate_title_async(session_id, user_input)

                yield event  # 透传给 server.py

                # Phase 9 Task 6 接入点 E: done 事件后过滤 PII
                # 在 done 事件透传后，对最终 response 做 PII 脱敏。
                # 若发生替换，额外 yield 一个 output_filtered 事件，
                # 供前端将已显示的响应替换为脱敏版本。
                # 注意：仅在 try 块内（done 事件）yield output_filtered，
                # finally 块中不能可靠 yield（async generator 限制）。
                if etype == "done" and self.guardrail_engine is not None:
                    try:
                        _done_response = event.get("response", "") or ""
                        (
                            _filtered_done,
                            _done_pii_count,
                        ) = self.guardrail_engine.filter_output(_done_response)
                        if _filtered_done != _done_response:
                            yield {
                                "type": "output_filtered",
                                "filtered_response": _filtered_done,
                                "replacements_count": _done_pii_count,
                            }
                    except Exception as e:
                        logger.warning(
                            "done 事件 output_filtered 异常，fail-open 跳过: %s",
                            e,
                        )

                # T13: plan_task / update_todo 工具事件透传后，发射 todo 事件。
                # 事件顺序：tool → todo_init / (todo_update → todo_complete)，
                # 在下一轮 text 之前发射，确保前端实时渲染 todo 卡片。
                if etype == "tool" and todo_dict is not None:
                    tool_name = event.get("name", "")
                    if tool_name == "plan_create":
                        yield {
                            "type": "todo_init",
                            "session_id": session_id,
                            "todo": todo_dict,
                        }
                    elif tool_name == "plan_update_step":
                        yield {
                            "type": "todo_update",
                            "session_id": session_id,
                            "todo": todo_dict,
                        }
                        # 所有 step 均为 completed 时额外发射 todo_complete
                        if todo_dict.get("completed"):
                            yield {
                                "type": "todo_complete",
                                "session_id": session_id,
                                "todo": todo_dict,
                            }
        finally:
            # 反馈监控：上报流式终止原因（覆盖正常/异常/取消所有退出路径）
            if self.metrics is not None:
                self.metrics.observe_termination(stream_termination_reason)
            # 空回复计数：直接检查 response_text 是否为空
            # 取消/工具失败不计数（避免级联误判）
            is_empty_response_stream = (
                stream_termination_reason not in ("user_cancel", "tool_permanent_fail")
                and (not response_text or not response_text.strip())
            )
            if is_empty_response_stream:
                self._consecutive_empty_runs[session_id] = empty_count + 1
                logger.info(
                    "会话 %s 空回复计数 %d -> %d（流式）",
                    session_id, empty_count, empty_count + 1,
                )
            elif empty_count > 0:
                self._consecutive_empty_runs[session_id] = 0
                logger.info("会话 %s 收到非空回复，重置空回复计数（流式）", session_id)

            # 3. 批量记录到 session_logger（即使流被中断也保证保存）
            if self.session_logger is not None:
                try:
                    self.session_mgr.ensure_session(session_id)
                    # 先记录 user 输入
                    self.session_logger.log_message(
                        session_id=session_id,
                        role="user",
                        content=user_input,
                    )
                    # 按顺序记录所有收集的消息
                    for msg in collected_messages:
                        self.session_logger.log_message(
                            session_id=session_id,
                            role=msg["role"],
                            content=msg["content"],
                            tool_name=msg.get("tool_name"),
                            tool_call_id=msg.get("tool_call_id"),
                            is_error=msg.get("is_error", False),
                            reasoning=msg.get("reasoning"),
                        )
                    # 兜底：如果 collected_messages 为空，或最后一条不是纯文本
                    # assistant 消息（无 tool_name），且 response_text 非空，补记一条
                    # （单轮无 round_start 场景，response_text 是唯一 assistant 文本）
                    need_final_assistant = bool(response_text) and (
                        not collected_messages
                        or collected_messages[-1].get("tool_name")
                        or collected_messages[-1]["role"] != "assistant"
                    )
                    if need_final_assistant:
                        self.session_logger.log_message(
                            session_id=session_id,
                            role="assistant",
                            content=response_text,
                            reasoning=current_round_reasoning or None,
                        )
                except Exception as e:
                    logger.warning("记录会话日志失败: %s", e)

            # 4. 更新历史缓冲（持久化循环内完整 messages，含 tool_use + tool_result）
            # done_messages = enhanced_history + [user_input, ...loop messages...]，
            # 切掉 enhanced_history 部分即为本次新增的消息。
            # 流中断未收到 done（done_messages 为 None）时降级为
            # user_input + response_text，保证至少保留本轮纯文本对话。
            if self.history_buffer is not None:
                try:
                    if done_messages is not None:
                        new_messages = done_messages[enhanced_history_len:]
                        self.msg_persistence.persist_new_messages(
                            session_id, new_messages, user_input, response_text
                        )
                    else:
                        # 规范 3 Task 8.2: 中断降级路径不写半截 assistant
                        # 仅持久化 user_input，不持久化 partial response_text
                        # （半截 assistant 会污染下一轮上下文，中断通知已由
                        # _save_interrupt_notice 暂存，下次调用时注入）
                        self.history_buffer.add_message(
                            session_id, "user", user_input
                        )
                except Exception as e:
                    logger.warning("更新 HistoryBuffer 失败: %s", e)

            # 5. 累加信息到 ConsolidationEngine，达到阈值时触发沉淀
            # Phase 9 Task 6 接入点 F: ConsolidationEngine 累加 filtered_response
            # （防 PII 泄漏到长期记忆向量库）。collected_messages 与
            # session_logger / history_buffer 持久化原始文本（保留上下文完整）。
            # Note: 中断时 response_text 可能为空，兜底用 current_round_text
            _consolidation_response = (
                response_text or current_round_text or "[用户中断了回复]"
            )
            # 对 _consolidation_response 做 PII 脱敏后再累加到沉淀引擎
            if self.guardrail_engine is not None:
                try:
                    _consolidation_response, _ = (
                        self.guardrail_engine.filter_output(_consolidation_response)
                    )
                except Exception as e:
                    logger.warning(
                        "finally 块 filter_output 异常，fail-open 使用原值: %s", e
                    )
            if self.consolidation_engine is not None:
                try:
                    self.consolidation_engine.add_info(
                        {"role": "user", "content": user_input}
                    )
                    self.consolidation_engine.add_info(
                        {"role": "assistant", "content": _consolidation_response}
                    )
                    if self.consolidation_engine.should_consolidate():
                        await self.msg_persistence.trigger_consolidation(session_id)
                except Exception as e:
                    logger.warning("consolidation 信息累加或触发失败: %s", e)

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
