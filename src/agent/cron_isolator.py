"""Cron 上下文隔离:构建 cron 专用上下文和工具集。

从 Orchestrator 提取的 cron 隔离职责:
- build_isolation: 从 session_id 解析 CronIsolation context
- build_enhanced_context: 构建 cron 调度会话的隔离上下文（system_text + history + tools_override）
- build_cron_tools: 构建请求级工具过滤列表
- set_dependencies: 注入 cron 调度依赖

采用方法对象模式：CronIsolator 持有 Orchestrator 引用，
因为 cron 上下文构建依赖 memory_retriever / metrics / tool_registry 等多个组件。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

# 兼容相对导入与直接运行两种方式
try:
    from .llm.prompts import SYSTEM_PROMPT
    from .memory.cron_isolation import CronIsolation
except ImportError:  # pragma: no cover - 直接运行模块时回退
    import sys
    from pathlib import Path

    _SRC_DIR = str(Path(__file__).resolve().parent.parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from llm.prompts import SYSTEM_PROMPT  # type: ignore
    try:
        from memory.cron_isolation import CronIsolation  # type: ignore
    except ImportError:
        CronIsolation = None  # type: ignore

logger = logging.getLogger(__name__)


class CronIsolator:
    """cron 会话与用户会话隔离。

    持有 Orchestrator 引用（方法对象模式），通过它访问
    memory_retriever / metrics / tool_registry 等组件。
    """

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator

    def build_isolation(
        self, session_id: Optional[str]
    ) -> Optional["CronIsolation"]:
        """从 session_id 解析 CronIsolation context。

        session_id 以 cron: 开头时返回 CronIsolation 实例；
        其他值或 None 返回 None（用户会话，不走隔离路径）。

        CronIsolation 模块不可用时（可选依赖缺失）返回 None。
        """
        if CronIsolation is None:
            return None
        return CronIsolation.from_session_id(session_id)

    async def build_enhanced_context(
        self,
        session_id: str,
        user_input: str,
        history: List[Dict[str, Any]],
        cron_isolation: "CronIsolation",
    ) -> tuple:
        """构建 cron 调度会话的隔离上下文。

        cron 路径专用上下文构建，与用户会话路径隔离:
        - system_text 只含 SYSTEM_PROMPT，不注入 memory.md 用户画像
        - 检索记忆按 namespace="cron" + cron_id 过滤
        - 不注入 TaskManager 进度
        - 返回 tools_override（请求级过滤后的工具 schema 列表）
        """
        orch = self._orch
        system_text = SYSTEM_PROMPT
        injection_text = ""

        # 0. 注入运行环境信息
        try:
            env_section = orch.context_builder.build_environment()
            if env_section:
                injection_text = env_section
        except Exception as e:
            logger.warning("cron 运行环境信息注入失败，跳过: %s", e)

        # 1. 检索 cron namespace 长期记忆
        memory_retriever = getattr(orch, "memory_retriever", None)
        inject_history_enabled = getattr(orch, "cron_inject_history_enabled", True)
        metrics = getattr(orch, "metrics", None)

        if memory_retriever is not None and inject_history_enabled:
            try:
                memory_text = await asyncio.to_thread(
                    memory_retriever.get_injection_text,
                    user_input,
                    namespace=cron_isolation.namespace,
                    cron_id=cron_isolation.cron_id,
                    exclude_types={"conversation_turn"},
                )
                if metrics is not None:
                    metrics.observe_memory_retrieval(hit=bool(memory_text))
                if memory_text:
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{memory_text}"
                    else:
                        injection_text = memory_text
            except Exception as e:
                logger.warning("cron 长期记忆检索注入失败，跳过: %s", e)
        elif memory_retriever is not None and not inject_history_enabled:
            if metrics is not None:
                metrics.observe_memory_retrieval(hit=False)
            logger.debug(
                "cron inject_history=false，跳过记忆检索 cron_id=%s",
                cron_isolation.cron_id,
            )

        # 2. 统一前置 injection_text 到 history
        condensed_history = await orch.enhanced_context_builder._apply_condenser(history)
        if injection_text:
            enhanced_history = [
                {"role": "user", "content": injection_text}
            ] + condensed_history
        else:
            enhanced_history = condensed_history

        # 3. 构建请求级过滤后的 tools_override
        tools_override = self.build_cron_tools(session_id)

        return system_text, enhanced_history, tools_override

    def build_cron_tools(
        self, session_id: Optional[str]
    ) -> Optional[List[Dict[str, Any]]]:
        """构建 cron 调度会话的请求级工具过滤列表。

        从调度项的 active_tools_snapshot 字段读取锁定的工具名列表，
        在全局 tool_registry.get_tools_schema() 上做请求级过滤，
        并合并 cron_tool_registry 的 schema。

        非 cron 会话 / cron_scheduler 未注入 / 调度项不存在时返回 None。
        """
        if not session_id or not session_id.startswith("cron:"):
            return None

        orch = self._orch
        cron_scheduler = getattr(orch, "cron_scheduler", None)
        if cron_scheduler is None:
            return None

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
            logger.debug(
                "调度项 %s 不存在，cron 工具过滤返回完整工具集", cron_id
            )
            return None

        snapshot = sched_dict.get("active_tools_snapshot")
        filtered: List[Dict[str, Any]] = []

        tool_registry = getattr(orch, "tool_registry", None)
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
                snapshot_set = set(snapshot)
                filtered = [
                    t for t in full_schema if t.get("name") in snapshot_set
                ]
            else:
                filtered = list(full_schema)

        cron_tool_registry = getattr(orch, "cron_tool_registry", None)
        if cron_tool_registry is not None:
            try:
                cron_tool_schema = cron_tool_registry.get_tools_schema()
                if cron_tool_schema:
                    filtered = filtered + list(cron_tool_schema)
            except Exception as e:
                logger.warning(
                    "获取 cron_tool schema 失败，跳过合并: %s", e
                )

        react_loop = getattr(orch, "react_loop", None)
        if (
            react_loop is not None
            and cron_tool_registry is not None
            and getattr(react_loop, "cron_tool_registry", None) is None
        ):
            react_loop.cron_tool_registry = cron_tool_registry

        return filtered

    def set_dependencies(
        self,
        cron_scheduler: Optional[Any] = None,
        cron_tool_registry: Optional[Any] = None,
    ) -> None:
        """注入 cron 调度路径所需的依赖。

        由 server.py lifespan 在装配 CronScheduler / CronToolRegistry 后调用。
        """
        orch = self._orch
        if cron_scheduler is not None:
            orch.cron_scheduler = cron_scheduler
        if cron_tool_registry is not None:
            orch.cron_tool_registry = cron_tool_registry
            if getattr(orch, "react_loop", None) is not None:
                orch.react_loop.cron_tool_registry = cron_tool_registry
