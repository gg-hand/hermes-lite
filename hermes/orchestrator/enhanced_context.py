"""增强上下文构建器。

从 Orchestrator._build_enhanced_context 提取，负责构建含用户画像、检索记忆、
环境信息、任务进度、TodoList、文件摘要、已激活 Skill 的完整上下文。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from hermes.llm.prompts import SYSTEM_PROMPT
logger = logging.getLogger(__name__)


class EnhancedContextBuilder:
    """构建含画像与检索记忆的上下文。

    持有 Orchestrator 的引用（通过 orchestrator 属性访问组件），
    所有原 _build_enhanced_context 的逻辑迁移至此。
    """

    def __init__(self, orchestrator: Any) -> None:
        """初始化上下文构建器。

        参数:
            orchestrator: Orchestrator 实例，通过属性访问组件。
        """
        self.orch = orchestrator

    async def build(
        self,
        session_id: str,
        user_input: str,
        history: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        """构建含用户画像与检索记忆的上下文。

        返回 (system_text, enhanced_history, tools_override) 三元组。
        用户会话 tools_override 固定 None；cron 会话由 CronIsolator 返回。
        """
        # cron 会话走隔离路径
        cron_isolation = None
        if self.orch.cron_isolator is not None:
            cron_isolation = self.orch.cron_isolator.build_isolation(session_id)
        if cron_isolation is not None:
            return await self.orch.cron_isolator.build_enhanced_context(
                session_id, user_input, history, cron_isolation
            )

        system_text = SYSTEM_PROMPT
        enhanced_history = history
        injection_text = ""

        # 1. 注入用户画像到 system（通过 ContextManager 的缓存命中区构建）
        if self.orch.context_manager is not None:
            try:
                system_text = self.orch.context_manager.get_cache_stable_prefix()
            except Exception as e:
                logger.warning("构建含画像的 system 失败，降级为 SYSTEM_PROMPT: %s", e)
                system_text = SYSTEM_PROMPT

        # 2. 检索长期记忆并作为 history 前置 user 消息注入
        # Phase X 优化：后续轮次（已有对话历史）走轻量检索路径，
        # 跳过 _filter_by_relevance 与 reinforce 写入，省掉 ~2.6s。
        if self.orch.memory_retriever is not None:
            try:
                is_first_round = len(history) < 2
                if is_first_round:
                    memory_text = await asyncio.to_thread(
                        self.orch.memory_retriever.get_injection_text, user_input
                    )
                else:
                    memory_text = await asyncio.to_thread(
                        self.orch.memory_retriever.get_injection_text_lightweight,
                        user_input,
                    )
                if self.orch.metrics is not None:
                    self.orch.metrics.observe_memory_retrieval(hit=bool(memory_text))
                if memory_text:
                    injection_text = memory_text
            except Exception as e:
                logger.warning("长期记忆检索注入失败，跳过: %s", e)

        # 2.5 注入运行环境信息到 messages[0]（缓存失效区，不污染 system_text）
        try:
            env_section = self.orch.context_builder.build_environment()
            if env_section:
                if injection_text:
                    injection_text = f"{env_section}\n\n{injection_text}"
                else:
                    injection_text = env_section
        except Exception as e:
            logger.warning("运行环境信息注入失败，跳过: %s", e)

        # 3. 注入任务进度摘要到 messages[0]（缓存失效区，不污染 system_text）
        if self.orch.task_manager is not None:
            try:
                task_summary = self.orch.task_manager.get_progress_summary()
                if task_summary:
                    task_section = f"## 当前任务状态\n{task_summary}"
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{task_section}"
                    else:
                        injection_text = task_section
            except Exception as e:
                logger.warning("任务进度注入失败，跳过: %s", e)

        # 3.5 注入 TodoList 状态到 messages[0]（缓存失效区，不污染 system_text）
        if self.orch.todo_registry is not None:
            try:
                todo_dict = self.orch.todo_registry.get_todo_dict(session_id)
                todo_section = self.orch.context_builder.format_todo(todo_dict)
                if todo_section:
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{todo_section}"
                    else:
                        injection_text = todo_section
            except Exception as e:
                logger.warning("TodoList 状态注入失败，跳过: %s", e)

        # 4. 注入已上传文件摘要到 messages[0]（缓存失效区）
        if self.orch.context_manager is not None:
            try:
                file_section = self.orch.context_manager.get_file_injection(session_id)
                if file_section:
                    if injection_text:
                        injection_text = f"{injection_text}\n\n{file_section}"
                    else:
                        injection_text = file_section
            except Exception as e:
                logger.warning("文件摘要注入失败，跳过: %s", e)

        # 5. 注入已激活 Skill body 到 messages[0]（末位，L2 激活后注入）
        try:
            skill_section = self.orch.skill_mgr.build_active_section(session_id)
            if skill_section:
                if injection_text:
                    injection_text = f"{injection_text}\n\n{skill_section}"
                else:
                    injection_text = skill_section
        except Exception as e:
            logger.warning("已激活 skill body 注入失败，跳过: %s", e)

        # 统一前置 injection_text 到 history（若存在）
        condensed_history = await self._apply_condenser(history)
        if injection_text:
            enhanced_history = [
                {"role": "user", "content": injection_text}
            ] + condensed_history
        else:
            enhanced_history = condensed_history

        return system_text, enhanced_history, None

    async def _apply_condenser(
        self, history: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """对 history 应用 condenser 压缩，返回压缩后的新列表。

        压缩在送入 ReactLoop 前完成，不影响 history_buffer 存储。
        先将 history 规整为 ``{role, content}``（剔除 timestamp 等附加字段，
        保证消息结构符合 Anthropic API 规范），再调用 condenser。
        """
        clean = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in history
            if m.get("role") is not None and m.get("content") is not None
        ]
        condenser = getattr(self.orch, "condenser", None)
        if condenser is None:
            return clean
        try:
            return await asyncio.to_thread(condenser.condense, clean)
        except Exception as e:
            logger.warning("condenser 压缩历史失败，使用原始历史: %s", e)
            return clean
