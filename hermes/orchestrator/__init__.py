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

from hermes.config import load_config
from hermes.llm.client import LLMClient
from hermes.llm.prompts import SYSTEM_PROMPT, TITLE_GENERATION_PROMPT
from hermes.llm.reasoning_profiles import ReasoningConfig
from hermes.agent.react_loop import ReactLoop
from hermes.agent.session_manager import SessionManager
from hermes.agent.skill_manager import SkillManager
from hermes.agent.cron_isolator import CronIsolator
from hermes.agent.context_builder import ContextBuilder
from hermes.agent.msg_persistence import MessagePersistence
from hermes.storage.sqlite_log import SessionLogger
from hermes.storage.chroma_store import ChromaMemoryStore
from hermes.storage.history_buffer import HistoryBuffer
from hermes.memory.consolidation import ConsolidationEngine
from hermes.memory.cron_isolation import CronIsolation
from hermes.memory.memory_md import MemoryMdManager
from hermes.memory.retrieval import MemoryRetriever
from hermes.memory.context_manager import ContextManager
from hermes.memory.condenser import (
    Condenser,
    MaskingCondenser,
    LLMSummarizingCondenser,
    create_condenser_from_config,
)
from hermes.memory.decay import MemoryDecay
from hermes.memory.signal_pool import SignalPool
from hermes.agent.tool_registry import ToolRegistry
from hermes.agent.tools import register_builtin_tools
from hermes.agent.tools.plan_tools import register_plan_tools
from hermes.agent.tools.memory_tools import register_memory_tools
from hermes.agent.intent_classifier import (
    IntentClassificationResult,
    IntentType,
    classify_intent,
)
if TYPE_CHECKING:
    from ..monitoring.metrics import MetricsCollector
    from ..agent.audit import AuditLogger
    from ..agent.policy import PolicyEngine
    from ..agent.approval import ApprovalManager
    from ..guardrails import GuardrailEngine

from hermes.agent.policy import PolicyEngine
from hermes.agent.approval import ApprovalManager
from hermes.agent.file_registry import FileOperationRegistry
from hermes.tasks.task_manager import TaskManager
from hermes.tasks.todo_list import TodoListRegistry
from hermes.guardrails import GuardrailEngine
from .enhanced_context import EnhancedContextBuilder  # noqa: E402
from .chat_handler import ChatHandler  # noqa: E402

logger = logging.getLogger(__name__)


class Orchestrator:
    """主编排器，协调 Agent 各模块完成一次对话。

    根据 config.yaml 创建并装配 LLM 客户端、React 循环、工具注册中心、
    记忆子系统、安全子系统等组件。可选组件缺失时降级运行。
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        metrics: Optional["MetricsCollector"] = None,
        audit_logger: Optional["AuditLogger"] = None,
        approval_manager: Optional["ApprovalManager"] = None,
        task_manager: Optional["TaskManager"] = None,
    ) -> None:
        """初始化编排器并装配各组件。"""
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

    def apply_condenser_config(self, condenser_cfg: Dict[str, Any]) -> Optional[Condenser]:
        """热更新 condenser 配置（strategy 变更需重启，其余即时生效）。"""
        token_counter = getattr(self.llm_client, "count_messages_tokens", None) if self.llm_client else None
        new_condenser = create_condenser_from_config(condenser_cfg, llm_client=self.llm_client, token_counter=token_counter) if create_condenser_from_config else None
        self.condenser = new_condenser
        if self.context_manager is not None:
            self.context_manager.condenser = new_condenser
        return new_condenser

    def close(self) -> None:
        """关闭所有资源（chroma_store / session_logger / history_buffer / consolidation_engine）。"""
        for attr in ("chroma_store", "session_logger", "history_buffer", "consolidation_engine"):
            obj = getattr(self, attr, None)
            if obj is None: continue
            closer = getattr(obj, "close", None)
            if callable(closer):
                try: closer()
                except Exception as e: logger.warning("关闭 %s 失败: %s", attr, e)

    def shutdown(self) -> None:
        """优雅关闭：冲刷积压记忆后关闭非数据库组件（供软重启调用）。"""
        if self.consolidation_engine is not None:
            try:
                logger.info("shutdown: 正在冲刷 pending consolidation...")
                stats = self.consolidation_engine.consolidate()
                logger.info("shutdown: consolidation 完成: %s", stats)
            except Exception as e:
                logger.warning("shutdown: consolidation 冲刷失败: %s", e)
        for attr in ("history_buffer",):
            obj = getattr(self, attr, None)
            if obj is None: continue
            closer = getattr(obj, "close", None)
            if callable(closer):
                try: closer()
                except Exception as e: logger.warning("shutdown: 关闭 %s 失败: %s", attr, e)
