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
