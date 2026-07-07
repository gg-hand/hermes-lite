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
    from .storage.sqlite_log import SessionLogger
except ImportError:  # pragma: no cover - 直接运行模块时回退
    import sys
    from pathlib import Path

    _SRC_DIR = str(Path(__file__).resolve().parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from config import load_config  # type: ignore
    from llm.client import LLMClient  # type: ignore
    from llm.prompts import SYSTEM_PROMPT, TITLE_GENERATION_PROMPT  # type: ignore
    from llm.reasoning_profiles import ReasoningConfig  # type: ignore
    from agent.react_loop import ReactLoop  # type: ignore
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
    from .agent.builtin_tools import (
        register_builtin_tools,
        register_plan_tools,
        register_memory_tools,
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
        from agent.builtin_tools import (  # type: ignore
            register_builtin_tools,
            register_plan_tools,
            register_memory_tools,
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

        # P1-3: 已激活 Skill 表（session_id → 已激活 skill 名称有序列表）
        # LLM 调用 skill__{name}() 后，activate_skill 将 name 追加到此表，
        # 下一轮 _build_enhanced_context 末位注入 body。
        self._active_skills: Dict[str, List[str]] = {}

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

        # 会话标题缓存：记录已知已有标题的 session_id，避免每次 chat() 都查
        # SQLite 的 sessions.title 列。进程内 dict，服务重启后重新从 DB 回填。
        # 仅缓存"已有标题"状态，不缓存标题内容本身（避免与 DB 不一致）。
        self._titled_sessions: set = set()
        # 异步生成标题任务强引用容器：asyncio.create_task 返回的 Task 仅被事件
        # 循环持弱引用，未保存会被 GC 回收导致任务从未执行。任务完成后由
        # add_done_callback 自动从 set 中移除，避免内存泄漏。
        self._pending_title_tasks: set = set()

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
            """将被淘汰的会话消息归档到向量库。"""
            # chroma_store 可能在装配 history_buffer 之后才初始化，
            # 因此在回调被调用时再读取属性，初始化失败时静默跳过。
            chroma_store = getattr(self, "chroma_store", None)
            if chroma_store is None:
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
            # Phase 8 Task 2.11: cron session 归档到 cron namespace + cron_id，
            # 与 user session 的 conversation_turn 隔离（与 SubTask 1.4 一致）。
            # add_memory 在 namespace="cron" 时要求传 cron_id，写入
            # metadata.namespace / metadata.cron_id 字段，便于检索时按命名空间过滤。
            if sid.startswith("cron:"):
                cron_id = sid[5:]
                chroma_store.add_memory(
                    content,
                    metadata=metadata,
                    namespace="cron",
                    cron_id=cron_id,
                )
            else:
                chroma_store.add_memory(content, metadata=metadata)

        return _archive_evicted_message

    async def chat(self, session_id: str, user_input: str,
             cancel_event: Optional[threading.Event] = None,
             reasoning_cfg: Optional["ReasoningConfig"] = None,
             is_cron: bool = False) -> str:
        """主对话入口。

        流程:
            1. 获取 session 的历史（优先 history_buffer，降级 session_logger）；
            2. 调用 react_loop.run 执行 React 循环；
            3. 记录用户输入与 assistant 回复到 session_logger；
            4. 更新 history_buffer；
            5. 将本轮 user 输入与 assistant 回复累加到 consolidation_engine，
               达到阈值时触发 consolidation（consolidate 内部会重置计数器）。

        参数:
            session_id: 会话 ID。
            user_input: 用户输入文本。

        返回:
            assistant 回复文本。
        """
        # 记录当前 session_id，供 plan 工具通过 get_session_id 回调获取
        self._current_session_id = session_id

        # 0. 会话切换检测：若 session_id 变化且 pending 非空，先 flush 旧会话沉淀
        await self._maybe_flush_on_session_switch(session_id)

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
                logger.info("准备注入 InterruptNotice 到 history: %s", session_id)

        # 1.6 安全网：清理历史中的连续 user 消息（兼容旧 JSONL 文件）
        history = self._sanitize_history_alternation(history)

        # 2. 执行 React 循环
        # 构建含用户画像 + 检索记忆的增强上下文
        # Phase 8 Task 5.7: _build_enhanced_context 返回三元组，第三项为
        # tools_override（用户会话固定 None；cron 会话为请求级过滤后的列表）
        system_text, enhanced_history, tools_override = await self._build_enhanced_context(
            session_id, user_input, history
        )
        # 规范 3 Task 8.4: 追加独立 system 消息到 enhanced_history
        # （react_loop.run 会自动追加 user_input，形成 [..., system(通知), user(新)]）
        if pending_notice_content is not None:
            enhanced_history = list(enhanced_history) + [
                {"role": "system", "content": pending_notice_content}
            ]
        enhanced_history_len = len(enhanced_history)

        # Phase 9 Task 6 接入点 A: 输入扫描（fail-open 软护栏）
        # deny 时直接返回拦截消息（不进入 react_loop）；
        # suspicious 时放行并记录审计；allow 时正常处理。
        # GuardrailEngine 内部已 try/except fail-open，此处不重复捕获。
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
                    "输入扫描 deny，拦截会话 %s: %s",
                    session_id,
                    guardrail_result.reason,
                )
                return "检测到潜在的安全风险，请重新表述您的请求。"
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

        # Phase 9 Task 7.6-7.7: 自动续接包装层 + 总熔断 200 轮
        # react_loop.run 返回 is_complete=False 时（达到 max_loops 或卡死），
        # 检查 TodoList 是否有未完成步骤，有则自动以续接消息重启循环
        # （不重置 messages，保留全部上下文）。累计 total_rounds 达到
        # MAX_TOTAL_ROUNDS 时强制终止，防彻底失控。
        MAX_TOTAL_ROUNDS = 200
        total_rounds = 0
        current_user_input = user_input
        current_history = enhanced_history
        response_text: str = ""
        messages_used: List[Dict[str, Any]] = []

        # 规范 2: 跨 run 空回复检测（仅 termination_reason=="empty_response" 计数）
        # count >= 2 → 直接返回友好提示，不再调 react_loop
        # count == 1 → 追加纠偏提示到 system_text（不持久化，per-call）
        empty_count = self._consecutive_empty_runs.get(session_id, 0)
        if empty_count >= 2:
            logger.warning(
                "会话 %s 连续 %d 次空回复，直接返回友好提示",
                session_id, empty_count,
            )
            return "抱歉，连续两次未能生成回复，可能是模型异常或上下文冲突。请重试或换种问法。"
        if empty_count == 1:
            system_text = (system_text or "") + (
                "\n\n[系统提示] 上一轮 LLM 返回了空回复。请确保本次明确回应用户问题，"
                "不要返回空内容。"
            )
            logger.info("会话 %s 注入空回复纠偏提示到 system_text", session_id)

        while total_rounds < MAX_TOTAL_ROUNDS:
            response_text, messages_used, is_complete, termination_reason = await self.react_loop.run(
                user_input=current_user_input,
                history=current_history,
                system=system_text,
                session_id=session_id,
                tools_override=tools_override,
                cancel_event=cancel_event,
                reasoning_cfg=reasoning_cfg,
                is_cron=is_cron,
            )
            # 反馈监控：上报终止原因（每次 run() 调用都计数，反映循环级分布）
            if self.metrics is not None:
                self.metrics.observe_termination(termination_reason)
            # 累计本轮消耗的轮次（用 max_loops 作为上界估计）
            total_rounds += self.react_loop.max_loops

            if is_complete:
                break

            # is_complete=False：检查 TodoList 是否有未完成步骤
            todo_dict = None
            if self.todo_registry is not None:
                try:
                    todo_dict = self.todo_registry.get_todo_dict(session_id)
                except Exception as e:
                    logger.warning("获取 TodoList 失败，跳过自动续接: %s", e)
                    todo_dict = None

            if not self._has_unfinished_steps(todo_dict):
                # TodoList 全部完成或不存在 → 不续接
                break

            # 构造续接消息，继续循环（不重置 messages）
            continuation_msg = self._build_continuation_message(todo_dict)
            current_user_input = continuation_msg
            current_history = messages_used  # 保留全部上下文
            logger.info(
                "React 循环未完成（total_rounds=%d/%d），TodoList 有未完成步骤，"
                "自动续接",
                total_rounds,
                MAX_TOTAL_ROUNDS,
            )

        if total_rounds >= MAX_TOTAL_ROUNDS:
            response_text = "已达总轮次上限 200，任务终止"
            logger.warning(
                "达到总轮次上限 %d，强制终止", MAX_TOTAL_ROUNDS
            )

        # 空回复计数：直接检查 response_text 是否为空
        # 取消/工具失败不计数（避免级联误判），max_loops 总结为空也不计数
        is_empty_response = (
            termination_reason not in ("user_cancel", "tool_permanent_fail")
            and (not response_text or not response_text.strip())
        )
        if is_empty_response:
            self._consecutive_empty_runs[session_id] = empty_count + 1
            logger.info(
                "会话 %s 空回复计数 %d -> %d",
                session_id, empty_count, empty_count + 1,
            )
        elif empty_count > 0:
            self._consecutive_empty_runs[session_id] = 0
            logger.info("会话 %s 收到非空回复，重置空回复计数", session_id)

        # Phase 9 Task 6 接入点 B: 输出过滤（PII 脱敏）
        # 在 react_loop 循环结束后、session_logger / history_buffer 之前
        # 过滤 LLM 响应中的 PII。filtered_response 用于 ConsolidationEngine
        # 与最终返回值；session_logger / history_buffer 仍持久化原始
        # response_text（保留 LLM 上下文完整，便于追溯与调试）。
        # GuardrailEngine 内部已 try/except fail-open，异常时返回原值。
        filtered_response: str = response_text
        if self.guardrail_engine is not None:
            try:
                filtered_response, pii_count = (
                    self.guardrail_engine.filter_output(response_text)
                )
                if pii_count > 0:
                    logger.info(
                        "输出过滤脱敏 %d 处 PII（会话 %s）",
                        pii_count,
                        session_id,
                    )
            except Exception as e:
                logger.warning("filter_output 异常，fail-open 使用原值: %s", e)
                filtered_response = response_text

        # 3. 记录消息到 session_logger（持久化原始 response_text，保留完整上下文）
        if self.session_logger is not None:
            try:
                self._ensure_session(session_id)
                self.session_logger.log_message(
                    session_id=session_id,
                    role="user",
                    content=user_input,
                )
                self.session_logger.log_message(
                    session_id=session_id,
                    role="assistant",
                    content=response_text,
                )
            except Exception as e:
                logger.warning("记录会话日志失败: %s", e)

        # 4. 更新历史缓冲（持久化循环内完整 messages，含 tool_use + tool_result）
        # messages_used = enhanced_history + [user_input, ...loop messages...]，
        # 切掉 enhanced_history 部分即为本次新增的消息。
        # Phase 9 Task 6 接入点 C: history_buffer 持久化原始 new_messages
        # （LLM 上下文完整，不受 PII 脱敏影响）。
        if self.history_buffer is not None:
            try:
                new_messages = messages_used[enhanced_history_len:]
                self._persist_new_messages(session_id, new_messages, user_input, response_text)
            except Exception as e:
                logger.warning("更新 HistoryBuffer 失败: %s", e)

        # 5. 累加信息到 ConsolidationEngine，达到阈值时触发沉淀
        # Phase 9 Task 6 接入点 C: ConsolidationEngine 累加 filtered_response
        # （防 PII 泄漏到长期记忆向量库）。user_input 不脱敏（保留语义）。
        if self.consolidation_engine is not None:
            try:
                # 将本轮 user 输入与 assistant 回复累加到沉淀引擎的缓冲区
                self.consolidation_engine.add_info(
                    {"role": "user", "content": user_input}
                )
                self.consolidation_engine.add_info(
                    {"role": "assistant", "content": filtered_response}
                )
                # 达到阈值则触发沉淀（consolidate 内部会重置计数器与消息缓冲）
                if self.consolidation_engine.should_consolidate():
                    await self._trigger_consolidation(session_id)
            except Exception as e:
                logger.warning("consolidation 信息累加或触发失败: %s", e)

        # 6. 首次对话后异步生成会话标题（fire-and-forget，不阻塞响应返回）
        # cron 会话跳过（由 CronScheduler 直接设置 schedule.name）
        self._maybe_generate_title_async(session_id, user_input)

        # Phase 9 Task 6 接入点 C: 返回 filtered_response（用户可见脱敏文本）
        return filtered_response

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
        await self._maybe_flush_on_session_switch(session_id)

        # 记录当前 session_id，供 plan 工具通过 get_session_id 回调获取
        self._current_session_id = session_id

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
        history = self._sanitize_history_alternation(history)

        # 2. 流式执行 React 循环，透传事件并收集待持久化的消息
        # 构建含用户画像 + 检索记忆的增强上下文
        # Phase 8 Task 5.7: _build_enhanced_context 返回三元组，第三项为
        # tools_override（用户会话固定 None；cron 会话为请求级过滤后的列表）
        system_text, enhanced_history, tools_override = await self._build_enhanced_context(
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
                        collected_messages.append(
                            {"role": "assistant", "content": current_round_text}
                        )
                        current_round_text = ""
                elif etype == "text":
                    # 累加到当前轮的 assistant 文本
                    current_round_text += event.get("text", "")
                elif etype == "tool":
                    # 先提交当前轮累加的 assistant 文本（LLM 先输出文本，
                    # 再调用工具），保证持久化顺序与事件实际顺序一致：
                    # text → tool_use → tool_result
                    if current_round_text:
                        collected_messages.append(
                            {"role": "assistant", "content": current_round_text}
                        )
                        current_round_text = ""
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
                        collected_messages.append(
                            {"role": "assistant", "content": current_round_text}
                        )
                        current_round_text = ""
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
                    self._maybe_generate_title_async(session_id, user_input)

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
                    self._ensure_session(session_id)
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
                        self._persist_new_messages(
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
                        await self._trigger_consolidation(session_id)
                except Exception as e:
                    logger.warning("consolidation 信息累加或触发失败: %s", e)

    def _maybe_generate_title_async(
        self, session_id: str, user_input: str
    ) -> None:
        """异步生成会话标题（fire-and-forget）。

        首次对话后调用 LLM 生成 5-10 字标题。cron 会话跳过（由
        CronScheduler 直接设置 schedule.name）。已生成标题的会话跳过。

        参数:
            session_id: 会话 ID。
            user_input: 用户首条输入（用于生成标题）。
        """
        if not session_id or session_id.startswith("cron:"):
            return
        if self.session_logger is None or self.llm_client is None:
            return
        # 进程内缓存命中：已知有标题，直接返回，零 IO
        if session_id in self._titled_sessions:
            return
        try:
            existing = self.session_logger.get_session_title(session_id)
            if existing:
                # 缓存回填：服务重启后首次查到已有标题，加入 set 避免后续重复查 DB
                self._titled_sessions.add(session_id)
                return
        except Exception as e:
            logger.warning("查询会话标题失败: %s", e)
            return
        try:
            task = asyncio.create_task(self._generate_title_task(session_id, user_input))
            self._pending_title_tasks.add(task)
            task.add_done_callback(self._pending_title_tasks.discard)
        except RuntimeError as e:
            logger.warning("创建标题生成任务失败: %s", e)

    async def _generate_title_task(
        self, session_id: str, user_input: str
    ) -> None:
        """生成标题并写入 session_logger（内部 task 实现）。

        截取 user_input 前 500 字符避免 prompt 过长；max_tokens=50 限制
        输出长度。失败时仅记录 warning，不影响主流程。
        """
        try:
            prompt = TITLE_GENERATION_PROMPT.replace(
                "{user_message}", user_input[:500]
            )
            messages = [{"role": "user", "content": prompt}]
            try:
                response = await asyncio.wait_for(
                    self.llm_client.chat_consolidation(
                        messages=messages, system=None, max_tokens=50,
                        reasoning_cfg=ReasoningConfig(enabled=False),
                    ),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.warning("会话 %s 标题生成超时（15s），跳过", session_id)
                return
            text_parts = []
            for block in response.content or []:
                if block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        text_parts.append(t)
            title = "".join(text_parts).strip()
            # 清理可能的引号、换行、首尾空白
            title = title.split("\n")[0].strip('「」""\' \t')
            if title and self.session_logger is not None:
                self.session_logger.update_session_title(session_id, title)
                # 写入成功后缓存，后续该会话的 chat() 直接跳过，零 IO
                self._titled_sessions.add(session_id)
                logger.info("已为会话 %s 生成标题: %s", session_id, title)
            else:
                logger.info("会话 %s 标题生成返回空响应，未写入", session_id)
        except Exception as e:
            logger.warning("生成会话标题失败: %s", e)

    def _ensure_session(self, session_id: str) -> None:
        """确保 session 存在，不存在则创建。

        Phase 9 优化：使用 SessionLogger.ensure_session() 的 INSERT OR IGNORE
        原子操作，替代先 list_sessions() 全表扫描再 create_session() 的 O(n) 方式。
        """
        if self.session_logger is None:
            return
        try:
            self.session_logger.ensure_session(session_id)
        except Exception as e:
            logger.warning("创建 session 失败: %s", e)

    def _build_environment_section(self) -> str:
        """构造运行环境信息段（跨平台自适应）。

        采集当前进程的运行时环境信息（OS、工作目录、Shell、Python 启动
        命令与路径），用于注入到 messages[0] 顶部（缓存失效区，不污染
        system_text），帮助 LLM 感知运行环境以生成更贴合环境的指令
        （例如 Windows 下用 PowerShell 命令、Linux 下用 bash 命令）。

        跨平台自适应策略：
        - 操作系统：``platform.system() + platform.release()``（如
          ``Windows 10`` / ``Linux 5.15.0`` / ``Darwin 23.4.0``）
        - Shell：Windows 优先检测 ``powershell``，缺失时降级到 ``cmd``；
          Linux/Mac 优先检测 ``bash``，缺失时降级到 ``sh``
        - Python 启动命令：优先 ``python3``，缺失时降级到 ``python``
        - Python 路径：``sys.executable``（当前解释器绝对路径）
        - 工作目录：``os.getcwd()``（当前进程工作目录）

        所有字段均为运行时真实值（非快照锁定），同一进程多次调用结果
        一致；不同进程（如 cron 调度 fork 出的子进程）可能不同。

        返回:
            markdown 格式的运行环境信息段，固定以 ``## 运行环境`` 开头。
        """
        import platform
        import shutil
        import sys as _sys
        import os as _os

        # 操作系统（如 "Windows 10" / "Linux 5.15.0-91-generic"）
        os_name = f"{platform.system()} {platform.release()}"

        # Shell 检测：subprocess.Popen(shell=True) 在 Windows 默认走 cmd.exe，
        # 仅当 pwsh (PowerShell 7) 实测存在时才标注
        if platform.system() == "Windows":
            shell = "PowerShell 7 (pwsh)" if shutil.which("pwsh") else "cmd.exe"
        else:
            shell = "bash" if shutil.which("bash") else "sh"

        # Python 启动命令：实测 python --version（避免 MS Store stub 误命中）
        try:
            import subprocess as _sp
            _sp.check_output(
                ["python", "--version"], stderr=_sp.STDOUT, timeout=3
            ).decode().strip()
            python_cmd = "python"
        except (FileNotFoundError, _sp.SubprocessError, OSError):
            python_cmd = "python3"

        # Python 解释器绝对路径
        python_path = _sys.executable

        # 当前工作目录
        cwd = _os.getcwd()

        env_lines = [
            "## 运行环境",
            f"- 操作系统: {os_name}",
            f"- 工作目录: {cwd}",
            f"- Shell: {shell}",
            f"- Python 启动命令: {python_cmd}",
            f"- Python 路径: {python_path}",
            f"- 当前日期: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)",
        ]
        # Windows 下追加跨盘 cd 提示
        if platform.system() == "Windows":
            env_lines.append("- 提示: 跨盘切换目录请用 `cd /d <路径>`（如 cd /d E:\\proj）")
        return "\n".join(env_lines)

    def _format_todo_for_injection(self, todo_dict: Optional[dict]) -> str:
        """将 TodoList dict 格式化为可注入 messages[0] 的"## 当前计划进度"段。

        Phase 9 Task 5: 让 LLM 每轮看到 TodoList 状态自然更新，避免多轮
        工具调用后忘记调用 update_todo 标记进度。注入位置在 TaskManager
        进度段之后（缓存失效区，不污染 system_text）。

        step 状态映射规则（与 plan 工具状态机一致）：
        - ``completed`` → ``[x]``
        - ``pending`` / ``in_progress`` / ``failed`` → ``[ ]``
          （``failed`` 也用 ``[ ]`` 表示未完成，避免 LLM 误判为已完成而
          跳过重试；具体失败原因可通过 step.result 查询。）

        参数:
            todo_dict: ``TodoListRegistry.get_todo_dict`` 返回的 dict，
                形如 ``{"goal": str, "steps": [...], "completed": bool}``，
                每个 step 含 ``id`` / ``content`` / ``status`` /
                ``depends_on`` / ``result``。``None`` 或缺字段时返回空串。

        返回:
            markdown 格式的"## 当前计划进度"段；``todo_dict`` 为 ``None``
            或无 steps 时返回空串（跳过注入）。
        """
        if not todo_dict:
            return ""
        steps = todo_dict.get("steps") or []
        if not steps:
            return ""

        goal = todo_dict.get("goal", "") or ""
        completed_count = sum(1 for s in steps if s.get("status") == "completed")
        total = len(steps)

        # 步骤渲染：completed → [x]，其他状态（pending/in_progress/failed）→ [ ]
        step_lines = []
        for s in steps:
            mark = "[x]" if s.get("status") == "completed" else "[ ]"
            content = s.get("content", "") or ""
            step_lines.append(f"{mark} {content}")
        steps_block = "\n".join(step_lines)

        return (
            "## 当前计划进度\n\n"
            f"**目标**: {goal}\n\n"
            f"**总进度**: {completed_count}/{total}\n\n"
            "**步骤**:\n"
            f"{steps_block}\n\n"
            "提醒：每完成一个步骤，必须调用 update_todo 标记为 completed"
        )

    # ------------------------------------------------------------------
    # Phase 9 Task 7.6-7.8: 自动续接辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _has_unfinished_steps(todo_dict: Optional[dict]) -> bool:
        """检查 TodoList 是否有未完成步骤。

        Phase 9 Task 7.6: 自动续接决策依据。``todo_dict`` 为 ``None`` 或
        无 steps 时返回 ``False``（不续接）；有任意 step 状态非
        ``completed`` 时返回 ``True``（需续接）。``failed`` 步骤也算
        未完成（允许 LLM 重试或换路径）。

        参数:
            todo_dict: ``TodoListRegistry.get_todo_dict`` 返回的 dict。

        返回:
            有未完成步骤返回 ``True``，否则 ``False``。
        """
        if not todo_dict:
            return False
        steps = todo_dict.get("steps") or []
        if not steps:
            return False
        return any(s.get("status") != "completed" for s in steps)

    def _build_continuation_message(self, todo_dict: Optional[dict]) -> str:
        """构造自动续接消息（Phase 9 Task 7.8）。

        当 ``react_loop.run`` 返回 ``is_complete=False`` 且 TodoList 有
        未完成步骤时，用此消息作为下一轮 ``user_input`` 继续 React 循环。
        不重置 messages（保留全部上下文）。

        参数:
            todo_dict: ``TodoListRegistry.get_todo_dict`` 返回的 dict，
                为 ``None`` 时降级为通用续接消息。

        返回:
            续接消息字符串，格式：
            ``"上一轮已达循环上限。当前进度：{todo_summary}。请继续完成剩余步骤，无需重复已完成的工作。"``
        """
        if not todo_dict or not todo_dict.get("steps"):
            return (
                "上一轮已达循环上限。请继续完成剩余步骤，无需重复已完成的工作。"
            )
        goal = todo_dict.get("goal", "") or ""
        steps = todo_dict.get("steps") or []
        completed_count = sum(
            1 for s in steps if s.get("status") == "completed"
        )
        total = len(steps)
        unfinished = [
            s.get("content", "")
            for s in steps
            if s.get("status") != "completed"
        ]
        unfinished_block = "\n".join(
            f"- {c}" for c in unfinished if c
        )
        return (
            "上一轮已达循环上限。"
            f"当前进度：目标「{goal}」，已完成 {completed_count}/{total}。"
            f"未完成步骤：\n{unfinished_block}\n"
            "请继续完成剩余步骤，无需重复已完成的工作。"
        )

    # ------------------------------------------------------------------
    # P1-3: Skill 激活状态管理（L2 body 注入）
    # ------------------------------------------------------------------

    def activate_skill(self, skill_name: str, session_id: str = "default") -> None:
        """标记 Skill 为已激活（下一轮注入 body 到 messages[0] 末位）。

        重复激活同一 Skill 不重复追加（去重），但保留首次激活顺序。

        参数:
            skill_name: Skill 名称。
            session_id: 会话 ID（隔离不同会话的激活状态）。
        """
        active = self._active_skills.setdefault(session_id, [])
        if skill_name not in active:
            active.append(skill_name)
            logger.info("Skill 已激活: %s (session=%s)", skill_name, session_id)

    def deactivate_skill(self, skill_name: str, session_id: str = "default") -> None:
        """取消激活指定 Skill。

        参数:
            skill_name: Skill 名称。
            session_id: 会话 ID。
        """
        active = self._active_skills.get(session_id, [])
        if skill_name in active:
            active.remove(skill_name)
            logger.info("Skill 已取消激活: %s (session=%s)", skill_name, session_id)

    def _build_active_skills_section(self, session_id: str) -> str:
        """构建已激活 Skill body 段（注入 injection_text 末位）。

        参数:
            session_id: 会话 ID。

        返回:
            拼接好的 skill body 段字符串。无激活 Skill 返回空串。
        """
        active = self._active_skills.get(session_id, [])
        if not active:
            return ""
        # skill_loader 可能未注入（纯内置工具模式），降级返回空
        skill_loader = getattr(self, "skill_loader", None)
        if skill_loader is None:
            return ""
        sections = []
        for name in active:
            try:
                body = skill_loader.load_body(name)
                if body:
                    sections.append(f"## 已激活 Skill: {name}\n{body}")
                else:
                    sections.append(f"## 已激活 Skill: {name}\n(body 为空)")
            except Exception as e:
                logger.warning("加载 Skill %s body 失败: %s", name, e)
        return "\n\n".join(sections)

    async def _build_enhanced_context(
        self,
        session_id: str,
        user_input: str,
        history: List[Dict[str, Any]],
    ) -> tuple:
        """构建含用户画像与检索记忆的上下文。

        利用 ContextManager / MemoryRetriever / MemoryMdManager 将：
        1. 用户画像（memory.md 全文）注入到 system prompt（缓存命中区）；
        2. 检索到的长期记忆（chroma 向量检索）作为 history 前置的
           user 消息注入（缓存失效区起点）。

        Phase 8 Task 1.4: 检测 ``session_id`` 以 ``cron:`` 开头时走 cron 隔离
        路径（:meth:`_build_cron_enhanced_context`），不注入用户画像与
        TaskManager 进度，检索记忆按 cron namespace 过滤。其他 session_id
        走原有用户会话路径（向后兼容）。

        Phase 8 Task 5.7: 返回值由二元组扩展为三元组，新增 ``tools_override``
        字段。用户会话路径固定返回 ``None``（react_loop 走默认 registry
        路径，tools schema 字节级稳定）；cron 路径由
        :meth:`_build_cron_enhanced_context` 返回请求级过滤后的列表。

        若相关组件未启用或检索为空，降级为原始 SYSTEM_PROMPT + 原始 history。

        参数:
            session_id: 会话 ID。``cron:`` 前缀触发 cron 隔离路径。
            user_input: 当前用户输入，用于检索相关记忆。
            history: 原始对话历史。

        返回:
            (system_text, enhanced_history, tools_override) 三元组：
            - system_text: 含画像的 system prompt（若画像为空则等于 SYSTEM_PROMPT）；
              cron 路径下不含画像（仅 SYSTEM_PROMPT）。
            - enhanced_history: 含检索注入的 history（若无注入则等于原始 history）。
            - tools_override: 工具 schema 覆盖列表。用户会话固定 ``None``；
              cron 会话为请求级过滤后的列表（或 ``None`` 表示未启用过滤）。
        """
        # Phase 8 Task 1.4: cron 会话走隔离路径
        cron_isolation = self._build_cron_isolation(session_id)
        if cron_isolation is not None:
            return await self._build_cron_enhanced_context(
                session_id, user_input, history, cron_isolation
            )

        system_text = SYSTEM_PROMPT
        enhanced_history = history
        injection_text = ""

        # 1. 注入用户画像到 system（通过 ContextManager 的缓存命中区构建）
        if self.context_manager is not None:
            try:
                system_text = self.context_manager.get_cache_stable_prefix()
            except Exception as e:
                logger.warning("构建含画像的 system 失败，降级为 SYSTEM_PROMPT: %s", e)
                system_text = SYSTEM_PROMPT

        # 2. 检索长期记忆并作为 history 前置 user 消息注入
        # Phase X 优化：后续轮次（已有对话历史）走轻量检索路径，
        # 跳过 _filter_by_relevance 与 reinforce 写入，省掉 ~2.6s。
        if self.memory_retriever is not None:
            try:
                # 判断是否为首轮：history 尚无完整 user↔assistant 交换
                is_first_round = len(history) < 2
                if is_first_round:
                    memory_text = await asyncio.to_thread(
                        self.memory_retriever.get_injection_text, user_input
                    )
                else:
                    memory_text = await asyncio.to_thread(
                        self.memory_retriever.get_injection_text_lightweight,
                        user_input,
                    )
                # 上报记忆检索命中/未命中指标
                if self.metrics is not None:
                    self.metrics.observe_memory_retrieval(hit=bool(memory_text))
                if memory_text:
                    injection_text = memory_text
            except Exception as e:
                logger.warning("长期记忆检索注入失败，跳过: %s", e)

        # 2.5 注入运行环境信息到 messages[0]（缓存失效区，不污染 system_text）
        # 环境信息置于 TaskManager 进度注入**之前**（即 messages[0] 顶部），
        # 便于 LLM 优先感知运行环境（OS / Shell / Python 路径），生成贴合
        # 环境的指令。环境信息为运行时真实值，同一进程内多次调用稳定。
        try:
            env_section = self._build_environment_section()
            if env_section:
                if injection_text:
                    injection_text = f"{env_section}\n\n{injection_text}"
                else:
                    injection_text = env_section
        except Exception as e:
            logger.warning("运行环境信息注入失败，跳过: %s", e)

        # 3. 注入任务进度摘要到 messages[0]（缓存失效区，不污染 system_text）
        if self.task_manager is not None:
            try:
                task_summary = self.task_manager.get_progress_summary()
                if task_summary:  # 非空字符串才注入
                    task_section = f"## 当前任务状态\n{task_summary}"
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{task_section}"
                    else:
                        injection_text = task_section
            except Exception as e:
                logger.warning("任务进度注入失败，跳过: %s", e)

        # 3.5 注入 TodoList 状态到 messages[0]（缓存失效区，不污染 system_text）
        # Phase 9 Task 5: 让 LLM 每轮看到 TodoList 状态自然更新，避免多轮
        # 工具调用后忘记更新 TodoList。注入位置在 TaskManager 进度段之后
        # （环境信息段已在最前面）。仅在 plan 模式下注入（todo_registry 有
        # TodoList 时）；todo_registry 为 None 或 session 无 plan 时降级跳过。
        if self.todo_registry is not None:
            try:
                todo_dict = self.todo_registry.get_todo_dict(session_id)
                todo_section = self._format_todo_for_injection(todo_dict)
                if todo_section:
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{todo_section}"
                    else:
                        injection_text = todo_section
            except Exception as e:
                logger.warning("TodoList 状态注入失败，跳过: %s", e)

        # 4. 注入已上传文件摘要到 messages[0]（缓存失效区）
        # 让 LLM 每轮感知会话内已上传文件，无需主动调用 file_list_uploads
        if self.context_manager is not None:
            try:
                file_section = self.context_manager.get_file_injection(session_id)
                if file_section:
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{file_section}"
                    else:
                        injection_text = file_section
            except Exception as e:
                logger.warning("文件摘要注入失败，跳过: %s", e)

        # 5. P1-3: 注入已激活 Skill body 到 messages[0]（末位，L2 激活后注入）
        # LLM 调用 skill__{name}() 后，下一轮在此注入 body 到上下文末位。
        # 末位注入保证不破坏前面 section 的相对顺序，且不影响缓存前缀。
        try:
            skill_section = self._build_active_skills_section(session_id)
            if skill_section:
                if injection_text:
                    injection_text = f"{injection_text}\n\n{skill_section}"
                else:
                    injection_text = skill_section
        except Exception as e:
            logger.warning("已激活 skill body 注入失败，跳过: %s", e)

        # 统一前置 injection_text 到 history（若存在）
        # history 先经 condenser 压缩（masking 旧 tool_result / LLM 摘要），
        # 压缩在送入 ReactLoop 前完成，不影响 history_buffer 存储。
        condensed_history = await self._apply_condenser(history)
        if injection_text:
            enhanced_history = [
                {"role": "user", "content": injection_text}
            ] + condensed_history
        else:
            enhanced_history = condensed_history

        # Phase 8 Task 5.7: 用户会话路径 tools_override 固定 None，
        # react_loop 走默认 tool_registry.get_tools_schema() 路径，
        # 保证 tools schema 字节级稳定（缓存约束 1）。
        return system_text, enhanced_history, None

    def _build_cron_isolation(
        self, session_id: Optional[str]
    ) -> Optional["CronIsolation"]:
        """从 session_id 解析 CronIsolation context。

        Phase 8 Task 1.4: ``session_id`` 以 ``cron:`` 开头时返回
        CronIsolation 实例（cron_id 取前缀之后的部分）；其他值或 None
        返回 None（表示用户会话，不走隔离路径）。

        CronIsolation 模块不可用时（可选依赖缺失）返回 None，降级到
        用户会话路径（向后兼容）。

        参数:
            session_id: 会话 ID。

        返回:
            CronIsolation 实例（cron 会话）或 None（用户会话）。
        """
        if CronIsolation is None:
            return None
        return CronIsolation.from_session_id(session_id)

    async def _build_cron_enhanced_context(
        self,
        session_id: str,
        user_input: str,
        history: List[Dict[str, Any]],
        cron_isolation: "CronIsolation",
    ) -> tuple:
        """构建 cron 调度会话的隔离上下文。

        Phase 8 Task 1.4: cron 路径专用上下文构建，与用户会话路径隔离：
        - system_text 只含 SYSTEM_PROMPT，**不注入 memory.md 用户画像**
          （``inject_profile=False``，缓存硬约束：system_text 禁含动态变量）
        - 检索记忆按 ``namespace="cron"`` + ``cron_id`` 过滤，与用户会话
          记忆互不可见
        - **不注入 TaskManager 进度**（``inject_todo=False``），避免动态
          变量破坏缓存稳定性
        - 工作流数据注入接口预留（``extra_injection``，Task 2 填充）

        Phase 8 Task 5.7: 额外返回 ``tools_override``（请求级过滤后的工具
        schema 列表），由 :meth:`_build_cron_tools` 从调度项的
        ``active_tools_snapshot`` 字段锁定 + 合并 cron_tool_registry schema
        得到。``tools_override`` 非 None 时透传给
        :meth:`ReactLoop.run` / :meth:`ReactLoop.run_stream`，覆盖默认的
        ``tool_registry.get_tools_schema()``（缓存约束 1+2：用户会话 tools
        schema 字节级稳定，不修改全局 ToolRegistry）。

        参数:
            session_id: 会话 ID（形如 ``cron:<cron_id>``）。
            user_input: 当前用户输入文本。
            history: 原始对话历史。
            cron_isolation: cron 隔离上下文。

        返回:
            (system_text, enhanced_history, tools_override) 三元组：
            - system_text: 仅 SYSTEM_PROMPT（不含画像）
            - enhanced_history: 含 cron namespace 检索注入的 history
            - tools_override: 请求级过滤后的工具 schema 列表（None 表示
              未启用过滤，由 react_loop 走默认 registry 路径）
        """
        # cron system_text：仅 SYSTEM_PROMPT，不注入 memory.md 用户画像
        # （inject_profile=False，缓存硬约束：system_text 禁含动态变量）
        system_text = SYSTEM_PROMPT

        injection_text = ""

        # 0. 注入运行环境信息到 messages[0] 顶部（缓存失效区，不污染 system_text）
        # 与用户会话路径保持一致，便于 LLM 感知 cron 调度执行环境的 OS /
        # Shell / Python 路径，生成贴合环境的指令。环境信息为运行时真实值，
        # 同一进程内多次调用稳定。
        try:
            env_section = self._build_environment_section()
            if env_section:
                injection_text = env_section
        except Exception as e:
            logger.warning("cron 运行环境信息注入失败，跳过: %s", e)

        # 1. 检索 cron namespace 长期记忆（按 cron_id 过滤）
        if self.memory_retriever is not None:
            try:
                memory_text = await asyncio.to_thread(
                    self.memory_retriever.get_injection_text,
                    user_input,
                    namespace=cron_isolation.namespace,
                    cron_id=cron_isolation.cron_id,
                )
                # 上报记忆检索命中/未命中指标
                if self.metrics is not None:
                    self.metrics.observe_memory_retrieval(hit=bool(memory_text))
                if memory_text:
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{memory_text}"
                    else:
                        injection_text = memory_text
            except Exception as e:
                logger.warning("cron 长期记忆检索注入失败，跳过: %s", e)

        # 2. cron 路径不注入 TaskManager 进度（inject_todo=False）
        #    避免动态变量破坏缓存稳定性

        # 2.5 文件注入不适用于 cron 会话（无用户上传绑定）
        #     cron session_id 形如 cron:<id>，upload_manager.get_session_files
        #     对该 session_id 返回空列表，注入无意义且浪费 IO，故显式跳过。

        # 3. 统一前置 injection_text 到 history（若存在）
        condensed_history = await self._apply_condenser(history)
        if injection_text:
            enhanced_history = [
                {"role": "user", "content": injection_text}
            ] + condensed_history
        else:
            enhanced_history = condensed_history

        # 4. Phase 8 Task 5.7: 构建请求级过滤后的 tools_override
        tools_override = self._build_cron_tools(session_id)

        return system_text, enhanced_history, tools_override

    def _build_cron_tools(
        self, session_id: Optional[str]
    ) -> Optional[List[Dict[str, Any]]]:
        """构建 cron 调度会话的请求级工具过滤列表（Phase 8 Task 5.7）。

        从调度项的 ``active_tools_snapshot`` 字段读取锁定的工具名列表，
        在全局 ``tool_registry.get_tools_schema()`` 上做请求级过滤（不修改
        全局 ToolRegistry），并合并 ``cron_tool_registry`` 的 schema（cron_tool
        始终对 cron 会话可见，不受 snapshot 限制——snapshot 仅约束内置工具集）。

        缓存约束：
        - 用户会话路径（``session_id`` 不以 ``cron:`` 开头）：本方法返回
          ``None``，react_loop 走默认 ``tool_registry.get_tools_schema()``
          路径，tools schema 字节级稳定。
        - cron 会话路径：返回过滤后的列表，仅本次请求生效，不污染全局
          registry。同一调度项多次触发时 snapshot 不变，过滤结果稳定。

        降级策略：
        - ``cron_scheduler`` 未注入（lifespan 未装配）→ 返回 None（向后兼容）
        - ``session_id`` 非 cron 会话 → 返回 None
        - 调度项不存在 → 返回 None（用完整工具集）
        - ``active_tools_snapshot`` 为 None 或空 → 返回完整工具集 + cron_tool
          schema（不限制内置工具）
        - ``tool_registry`` 为 None → 仅返回 cron_tool schema

        参数:
            session_id: 会话 ID（形如 ``cron:<cron_id>``）。

        返回:
            过滤后的工具 schema 列表，或 ``None``（表示未启用过滤，由
            react_loop 走默认 registry 路径）。
        """
        # 1. 仅 cron 会话路径启用过滤
        if not session_id or not session_id.startswith("cron:"):
            return None

        # 2. cron_scheduler 未注入时降级（向后兼容：未装配 lifespan 时
        #    无法读取调度项配置，返回 None 让 react_loop 走默认路径）
        cron_scheduler = getattr(self, "cron_scheduler", None)
        if cron_scheduler is None:
            return None

        # 3. 解析 cron_id（session_id 去掉 "cron:" 前缀）
        cron_id = session_id[5:]
        try:
            sched_dict = cron_scheduler.get_schedule(cron_id)
        except Exception as e:
            logger.warning(
                "读取调度项 %s 失败，cron 工具过滤降级为 None: %s",
                cron_id,
                e,
            )
            return None
        if sched_dict is None:
            # 调度项不存在（如手动构造的 cron:xxx 测试会话），用完整工具集
            logger.debug(
                "调度项 %s 不存在，cron 工具过滤返回完整工具集", cron_id
            )
            return None

        snapshot = sched_dict.get("active_tools_snapshot")
        # 4. 收集过滤后的工具 schema
        filtered: List[Dict[str, Any]] = []

        # 4.1 全局 tool_registry 按 snapshot 过滤
        tool_registry = getattr(self, "tool_registry", None)
        if tool_registry is not None:
            try:
                full_schema = tool_registry.get_tools_schema()
            except Exception as e:
                logger.warning(
                    "获取全局工具 schema 失败，cron 工具过滤跳过内置工具: %s",
                    e,
                )
                full_schema = []
            if snapshot:
                # 请求级过滤：仅保留 snapshot 中列出的工具名
                snapshot_set = set(snapshot)
                filtered = [
                    t for t in full_schema if t.get("name") in snapshot_set
                ]
            else:
                # snapshot 为 None 或空：不限制内置工具集，全量纳入
                filtered = list(full_schema)

        # 4.2 合并 cron_tool_registry schema（始终对 cron 会话可见）
        cron_tool_registry = getattr(self, "cron_tool_registry", None)
        if cron_tool_registry is not None:
            try:
                cron_tool_schema = cron_tool_registry.get_tools_schema()
                if cron_tool_schema:
                    filtered = filtered + list(cron_tool_schema)
            except Exception as e:
                logger.warning(
                    "获取 cron_tool schema 失败，跳过合并: %s", e
                )

        # 5. 注入 cron_tool_registry 到 react_loop（若尚未注入）
        #    使工具调用派发能路由到 cron_tool 子进程执行路径
        react_loop = getattr(self, "react_loop", None)
        if (
            react_loop is not None
            and cron_tool_registry is not None
            and getattr(react_loop, "cron_tool_registry", None) is None
        ):
            react_loop.cron_tool_registry = cron_tool_registry

        return filtered

    def set_cron_dependencies(
        self,
        cron_scheduler: Optional[Any] = None,
        cron_tool_registry: Optional[Any] = None,
    ) -> None:
        """注入 cron 调度路径所需的依赖（Phase 8 Task 5.7）。

        由 server.py lifespan 在装配 CronScheduler / CronToolRegistry 后
        调用。注入后 cron 会话路径（``session_id`` 以 ``cron:`` 开头）会
        启用请求级工具过滤；用户会话路径不受影响（``cron_scheduler`` /
        ``cron_tool_registry`` 仅在 cron 会话路径读取）。

        参数:
            cron_scheduler: ``CronScheduler`` 实例，用于读取调度项的
                ``active_tools_snapshot`` 字段。为 ``None`` 时禁用过滤。
            cron_tool_registry: ``CronToolRegistry`` 实例，提供 cron_tool
                schema 与子进程执行。为 ``None`` 时 cron 会话不可用
                cron_tool（但仍可过滤内置工具集）。同时注入到
                ``react_loop.cron_tool_registry`` 用于工具调用派发。
        """
        if cron_scheduler is not None:
            self.cron_scheduler = cron_scheduler
        if cron_tool_registry is not None:
            self.cron_tool_registry = cron_tool_registry
            # 同步注入到 react_loop，使工具调用派发能路由到 cron_tool
            if getattr(self, "react_loop", None) is not None:
                self.react_loop.cron_tool_registry = cron_tool_registry

    async def _apply_condenser(
        self, history: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """对 history 应用 condenser 压缩，返回压缩后的新列表。

        压缩在送入 ReactLoop 前完成，不影响 history_buffer 存储。
        先将 history 规整为 ``{role, content}``（剔除 timestamp 等附加字段，
        保证消息结构符合 Anthropic API 规范），再调用 condenser。
        condenser 为 None 或压缩失败时原样返回规整后的 history。
        """
        clean = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in history
            if m.get("role") is not None and m.get("content") is not None
        ]
        # 使用 getattr 兼容测试中通过 __new__ 绕过 __init__ 的场景
        # （未设置 self.condenser 时不报错，按"未启用压缩"处理）
        condenser = getattr(self, "condenser", None)
        if condenser is None:
            return clean
        try:
            return await asyncio.to_thread(condenser.condense, clean)
        except Exception as e:
            logger.warning("condenser 压缩历史失败，使用原始历史: %s", e)
            return clean

    def _persist_new_messages(
        self,
        session_id: str,
        new_messages: List[Dict[str, Any]],
        user_input: str,
        response_text: str,
    ) -> None:
        """将本轮 React 循环新增的完整 messages 持久化到 history_buffer。

        ``new_messages`` 为 ``messages_used[enhanced_history_len:]``，即本次
        循环产生的消息（user_input + assistant tool_use + user tool_result +
        assistant final），content 可为 str 或 Anthropic content block 列表。

        ``new_messages`` 为空（流中断或异常）时降级为仅存 user_input +
        response_text，保证至少保留本轮纯文本对话。
        """
        if not new_messages:
            self.history_buffer.add_message(session_id, "user", user_input)
            self.history_buffer.add_message(
                session_id, "assistant", response_text
            )
            return
        for msg in new_messages:
            role = msg.get("role")
            content = msg.get("content")
            if role is None or content is None:
                continue
            self.history_buffer.add_message(session_id, role, content)

    def _save_interrupt_notice(
        self, session_id: str, new_message: Optional[str] = None
    ) -> None:
        """中断发生时，暂存 InterruptNotice 到内存。

        规范 3 Task 8.1: 改用结构化 dict 存储（含 timestamp，供 TTL 清理）。
        通知将在下次 chat()/chat_stream() 开始时作为独立 system 消息注入到
        enhanced_history，不再字符串拼接到 user_input（Task 8.3-8.5）。
        """
        if new_message:
            content = (
                "【系统通知：用户中断了回复】\n"
                f"用户的新消息如下：\n{new_message}\n\n"
                "请直接响应用户的新消息，不要续写被中断的内容。"
            )
        else:
            content = "【系统通知：用户中断了回复】请等待用户的下一条指令。不要续写被中断的内容。"

        self._pending_interrupt_notices[session_id] = {
            "content": content,
            "timestamp": time.time(),
        }
        logger.info("InterruptNotice 已暂存: %s", session_id)

    @staticmethod
    def _sanitize_history_alternation(
        history: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """清理历史消息，确保 user/assistant 交替约束。

        规范 3 Task 9 增强：
        1. ``[..., user, user]`` → 合并（现有逻辑，兼容旧 JSONL）
        2. ``[..., user, system_notice, user]`` → 保留（Task 9.1）
           system_notice 为 role=system 的中断通知，不与相邻 user 合并
        3. ``[..., assistant(空/半截), user]`` → 弹出空 assistant（Task 9.2）
           避免空 assistant 污染上下文（Task 5 守卫已拦截新增，此处清理旧数据）
        """
        if not history or len(history) < 2:
            return history
        cleaned: List[Dict[str, Any]] = [history[0]]
        for msg in history[1:]:
            last = cleaned[-1]
            # Task 9.2: 弹出末尾空 assistant（content 为 None/""/[]/纯空 text 块）
            # 当下一条是 user 消息且上一条是空 assistant 时，弹出空 assistant
            if (
                msg.get("role") == "user"
                and last.get("role") == "assistant"
                and Orchestrator._is_empty_assistant_content(last.get("content"))
            ):
                cleaned.pop()
                last = cleaned[-1] if cleaned else None
                if last is None:
                    cleaned.append(msg)
                    continue
            # 现有逻辑：合并连续 user 消息（兼容旧 JSONL 中遗留的连续 user）
            # Task 9.1: user → system → user 模式不合并（system_notice 隔开）
            if (
                msg.get("role") == "user"
                and last.get("role") == "user"
                and isinstance(last.get("content"), str)
                and isinstance(msg.get("content"), str)
            ):
                cleaned[-1] = {
                    **last,
                    "content": f"{last['content']}\n{msg['content']}",
                }
            else:
                cleaned.append(msg)
        return cleaned

    @staticmethod
    def _is_empty_assistant_content(content: Any) -> bool:
        """判断 assistant content 是否为空（None / "" / [] / 纯空 text 块）。

        用于 _sanitize_history_alternation 清理残留的空 assistant。
        含 tool_use 块的不算空（应由 _drop_trailing_orphan_tool_calls 处理）。
        """
        if content is None or content == "":
            return True
        if isinstance(content, list):
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )
            if has_tool_use:
                return False
            has_substance = any(
                isinstance(b, dict)
                and (
                    b.get("type") != "text"
                    or (isinstance(b.get("text"), str) and b.get("text").strip())
                )
                for b in content
            )
            return not has_substance
        if isinstance(content, str):
            return not content.strip()
        return False

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

    async def _maybe_flush_on_session_switch(self, session_id: str) -> None:
        """会话切换时自动 flush 旧会话的沉淀。

        若 ``_last_session_id`` 与当前 ``session_id`` 不同，且
        ``consolidation_engine.pending_messages`` 非空，则调用
        :meth:`flush_consolidation` 强制沉淀上一个会话的对话，
        避免短会话的消息因达不到阈值而丢失。

        无论如何都会更新 ``_last_session_id`` 为当前 session_id。

        参数:
            session_id: 当前会话 ID。
        """
        if self.consolidation_engine is None:
            self._last_session_id = session_id
            return

        # 仅当 session_id 真正切换且缓冲区非空时才 flush
        if (
            self._last_session_id is not None
            and self._last_session_id != session_id
            and self.consolidation_engine.pending_messages
        ):
            logger.info(
                "检测到会话切换 %s -> %s，flush 旧会话的沉淀缓冲（%d 条消息）",
                self._last_session_id,
                session_id,
                self.consolidation_engine.info_counter,
            )
            # flush 旧会话：按旧 session_id 路由 namespace
            # flush_consolidation 内部调用 consolidation_engine.force_consolidate
            # （同步 LLM 调用），通过 to_thread 在线程中执行避免阻塞事件循环
            await asyncio.to_thread(
                self.flush_consolidation, session_id=self._last_session_id
            )

        self._last_session_id = session_id

    def flush_consolidation(self, session_id: Optional[str] = None) -> Dict[str, int]:
        """强制触发记忆沉淀（不判断阈值）。

        用于会话切换 / 会话结束 / 前端手动触发等场景，将
        ``pending_messages`` 缓冲的对话立即交给 LLM 提取事实并写入
        长期记忆。若缓冲为空则跳过。

        参数:
            session_id: 当前会话 ID。``cron:`` 前缀触发 cron 命名空间路由
                （Phase 8 Task 1.2）；其他值或 None 走 user 命名空间
                （向后兼容）。

        返回:
            与 :meth:`ConsolidationEngine.consolidate` 相同的统计字典。
            若 ConsolidationEngine 未启用，返回空字典。
        """
        if self.consolidation_engine is None:
            logger.debug("ConsolidationEngine 未启用，跳过 flush")
            return {}
        try:
            return self.consolidation_engine.force_consolidate(session_id=session_id)
        except Exception as e:
            logger.warning("flush consolidation 失败: %s", e)
            return {}

    async def _trigger_consolidation(self, session_id: Optional[str] = None) -> None:
        """触发记忆沉淀流程。

        ConsolidationEngine.consolidate() 使用内部 pending_messages 缓冲；
        consolidate 内部会重置 info_counter 与 pending_messages。
        若 ConsolidationEngine 未启用，则跳过。

        参数:
            session_id: 当前会话 ID。``cron:`` 前缀触发 cron 命名空间路由
                （Phase 8 Task 1.2）；其他值或 None 走 user 命名空间
                （向后兼容）。
        """
        if self.consolidation_engine is None:
            logger.debug("ConsolidationEngine 未启用，跳过 consolidation")
            return
        try:
            await asyncio.to_thread(
                self.consolidation_engine.consolidate, session_id=session_id
            )
        except Exception as e:
            logger.warning("consolidation 执行失败: %s", e)

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
